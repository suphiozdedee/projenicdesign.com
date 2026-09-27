from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents.models import AgentState
from ccbd.api_models import JobRecord, JobStatus
from ccbd.services.runtime_recovery_policy import RECOVERABLE_RUNTIME_HEALTHS, normalized_runtime_health
from mailbox_kernel import InboundEventStatus, InboundEventType
from message_bureau import AttemptState
from message_bureau.reply_payloads import delivery_job_id_from_payload

from .lifecycle_start import tick_jobs
from .records import get_job
from .reply_delivery import is_reply_delivery_job, prepare_reply_deliveries
from .reply_delivery_runtime.common import reply_delivery_inbound_event_id, reply_delivery_reply_id
from .reply_delivery_runtime.head import rewrite_reply_head

_FAILED_TERMINAL_STATUSES = frozenset({JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.INCOMPLETE})
_TERMINAL_ATTEMPT_STATES = frozenset(
    {
        AttemptState.COMPLETED,
        AttemptState.INCOMPLETE,
        AttemptState.FAILED,
        AttemptState.CANCELLED,
        AttemptState.SUPERSEDED,
        AttemptState.DEAD_LETTER,
    }
)
_RECOVERABLE_RUNNING_STATES = frozenset({AgentState.DEGRADED, AgentState.FAILED, AgentState.STOPPED})
_STALE_PANE_STATES = frozenset({'dead', 'missing', 'lost', 'exited'})
_STALE_RUNTIME_HEALTHS = frozenset(RECOVERABLE_RUNTIME_HEALTHS | {'dead', 'failed', 'stopped', 'unhealthy'})
_RUNNING_RECOVERY_HINTS = frozenset(
    {'provider_prompt_idle', 'provider_prompt_idle_stale', 'provider_prompt_input_stuck', 'job_running_stale'}
)


@dataclass(frozen=True)
class CommsRecoverTarget:
    job_id: str
    reply_delivery_job_id: str | None = None
    block_reason: str | None = None


@dataclass(frozen=True)
class CommsRecoverability:
    recoverable: bool
    block_reason: str | None
    target: CommsRecoverTarget | None

    def to_record(self) -> dict[str, object]:
        return {
            'recoverable': self.recoverable,
            'recover_target': None if self.target is None else {
                'job_id': self.target.job_id,
                'reply_delivery_job_id': self.target.reply_delivery_job_id,
                'block_reason': self.target.block_reason,
            },
            'block_reason': self.block_reason,
        }


def comms_recoverability_for_job(
    dispatcher,
    job: JobRecord,
    *,
    reply_delivery: JobRecord | None = None,
    running_hint: str | None = None,
    lineage_for_job=None,
) -> CommsRecoverability:
    if (
        reply_delivery is not None
        and reply_delivery.status in _FAILED_TERMINAL_STATUSES
        and _can_retry_job(dispatcher, reply_delivery, lineage_for_job=lineage_for_job)
    ):
        return CommsRecoverability(
            True,
            f'reply_delivery_{reply_delivery.status.value}',
            CommsRecoverTarget(job.job_id, reply_delivery.job_id),
        )
    if job.status in _FAILED_TERMINAL_STATUSES and _can_retry_job(dispatcher, job, lineage_for_job=lineage_for_job):
        return CommsRecoverability(True, f'job_{job.status.value}', CommsRecoverTarget(job.job_id, None))
    if job.status is JobStatus.RUNNING:
        stale_reason = _running_stale_reason(dispatcher, job, running_hint=running_hint)
        if stale_reason is not None:
            return CommsRecoverability(True, stale_reason, CommsRecoverTarget(job.job_id, None, stale_reason))
    return CommsRecoverability(False, None, None)


def comms_recover(dispatcher, payload: dict[str, Any] | str) -> dict[str, object]:
    target = _recover_target_from_payload(payload)
    source = get_job(dispatcher, target.job_id)
    if source is None:
        raise dispatcher._dispatch_error(f'unknown comms job: {target.job_id}')
    if is_reply_delivery_job(source):
        raise dispatcher._dispatch_error(f'comms recovery requires source business job: {target.job_id}')

    reply_delivery = _job_or_none(dispatcher, target.reply_delivery_job_id)
    recoverability = comms_recoverability_for_job(
        dispatcher,
        source,
        reply_delivery=reply_delivery,
        running_hint=target.block_reason,
    )
    audit = _audit(source, recoverability)

    if not recoverability.recoverable or recoverability.target is None:
        already_retried = _already_retried_job_id(dispatcher, source)
        if already_retried is not None:
            audit['status'] = 'noop'
            audit['noop_reason'] = 'already_retried'
            audit['latest_job_id'] = already_retried
            return audit
        audit['status'] = 'noop'
        audit['noop_reason'] = recoverability.block_reason or 'not_recoverable'
        return audit

    if recoverability.target.reply_delivery_job_id:
        _recover_reply_delivery(dispatcher, reply_delivery, audit)
    elif source.status in _FAILED_TERMINAL_STATUSES:
        _recover_terminal_retry(dispatcher, source, audit, retry_target=source.job_id)
    elif source.status is JobStatus.RUNNING:
        _recover_stale_running(dispatcher, source, audit, running_hint=target.block_reason)
    else:
        audit['status'] = 'noop'
        audit['noop_reason'] = 'not_recoverable'
        return audit

    if audit.get('noop_reason') and not _audit_changed(audit):
        audit['status'] = 'noop'
        return audit

    _tick_after_recovery(dispatcher, audit)
    audit['status'] = 'recovered' if _audit_changed(audit) else 'noop'
    source_after = get_job(dispatcher, source.job_id) or source
    reply_delivery_after = _reply_delivery_after_recovery(dispatcher, audit, target.reply_delivery_job_id)
    audit['recoverability_after'] = comms_recoverability_for_job(
        dispatcher,
        source_after,
        reply_delivery=reply_delivery_after,
    ).to_record()
    return audit


def _recover_target_from_payload(payload: dict[str, Any] | str) -> CommsRecoverTarget:
    if isinstance(payload, str):
        job_id = payload.strip()
        reply_delivery_job_id = None
        block_reason = None
    else:
        job_id = str(payload.get('job_id') or payload.get('id') or payload.get('target') or '').strip()
        reply_delivery_job_id = str(payload.get('reply_delivery_job_id') or '').strip() or None
        block_reason = _clean_running_hint(payload.get('block_reason'))
    if not job_id:
        raise ValueError('comms_recover requires job_id')
    return CommsRecoverTarget(job_id, reply_delivery_job_id, block_reason)


def _job_or_none(dispatcher, job_id: str | None) -> JobRecord | None:
    if not job_id:
        return None
    return get_job(dispatcher, job_id)


def _reply_delivery_after_recovery(dispatcher, audit: dict[str, object], original_job_id: str | None) -> JobRecord | None:
    retried = audit.get('retried_job')
    if isinstance(retried, dict):
        retry_job_id = str(retried.get('job_id') or '').strip()
        retry_job = _job_or_none(dispatcher, retry_job_id)
        if retry_job is not None and is_reply_delivery_job(retry_job):
            return retry_job
    return _job_or_none(dispatcher, original_job_id)


def _audit(job: JobRecord, recoverability: CommsRecoverability) -> dict[str, object]:
    return {
        'job_id': job.job_id,
        'agent_name': job.agent_name,
        'status': 'pending',
        'block_reason': recoverability.block_reason,
        'recoverable': recoverability.recoverable,
        'cancelled_old': None,
        'released_event': None,
        'retried_job': None,
        'next_started': [],
        'noop_reason': None,
    }


def _recover_terminal_retry(dispatcher, job: JobRecord | None, audit: dict[str, object], *, retry_target: str | None) -> None:
    if job is None or not retry_target:
        audit['noop_reason'] = 'retry_target_missing'
        return
    lineage = _lineage_for_job(dispatcher, job)
    if lineage is not None and not _is_latest_attempt(lineage):
        audit['noop_reason'] = 'already_retried'
        audit['latest_job_id'] = lineage.latest_attempt.job_id if lineage.latest_attempt is not None else None
        return
    if job.status is JobStatus.COMPLETED:
        audit['noop_reason'] = 'already_completed'
        return
    audit['released_event'] = _release_lineage_head_if_blocking(dispatcher, job)
    try:
        audit['retried_job'] = dispatcher.retry(retry_target)
    except Exception as exc:
        audit['noop_reason'] = str(exc)


def _clean_running_hint(value: object) -> str | None:
    text = str(value or '').strip()
    if not text:
        return None
    return text if text in _RUNNING_RECOVERY_HINTS else None


def _recover_stale_running(dispatcher, job: JobRecord, audit: dict[str, object], *, running_hint: str | None = None) -> None:
    stale_reason = _running_stale_reason(dispatcher, job, running_hint=running_hint)
    if stale_reason is None:
        audit['noop_reason'] = 'not_stale_running'
        return
    lineage = _lineage_for_job(dispatcher, job)
    if lineage is not None and not _is_latest_attempt(lineage):
        audit['noop_reason'] = 'already_retried'
        audit['latest_job_id'] = lineage.latest_attempt.job_id if lineage.latest_attempt is not None else None
        return
    audit['block_reason'] = stale_reason
    try:
        receipt = dispatcher.cancel(job.job_id, record_reply=False)
    except Exception as exc:
        audit['noop_reason'] = str(exc)
        return
    audit['cancelled_old'] = receipt.to_record()
    try:
        audit['retried_job'] = dispatcher.retry(job.job_id)
    except Exception as exc:
        audit['noop_reason'] = str(exc)


def _recover_reply_delivery(dispatcher, job: JobRecord | None, audit: dict[str, object]) -> None:
    if job is None:
        audit['noop_reason'] = 'reply_delivery_missing'
        return
    if job.status is JobStatus.COMPLETED:
        audit['noop_reason'] = 'already_completed'
        return
    inbound_event_id = reply_delivery_inbound_event_id(job)
    reply_id = reply_delivery_reply_id(job)
    if not inbound_event_id or not reply_id:
        audit['noop_reason'] = 'reply_delivery_metadata_missing'
        return
    control = getattr(dispatcher, '_message_bureau_control', None)
    if control is None:
        audit['noop_reason'] = 'message_bureau_missing'
        return
    current = control._inbound_store.get_latest(job.agent_name, inbound_event_id)
    if current is None:
        audit['noop_reason'] = 'reply_event_missing'
        return
    if current.status is InboundEventStatus.CONSUMED:
        audit['noop_reason'] = 'already_delivered'
        return
    current_delivery_job_id = delivery_job_id_from_payload(current.payload_ref)
    if current_delivery_job_id and current_delivery_job_id != job.job_id:
        latest_delivery = get_job(dispatcher, current_delivery_job_id)
        if latest_delivery is not None and latest_delivery.status in {
            JobStatus.ACCEPTED,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.COMPLETED,
        }:
            audit['noop_reason'] = 'already_retried'
            audit['latest_job_id'] = current_delivery_job_id
            return
    rewrite_reply_head(
        dispatcher,
        current,
        reply_id=reply_id,
        delivery_job_id=None,
        status=InboundEventStatus.QUEUED,
        updated_at=dispatcher._clock(),
        clear_progress=True,
    )
    audit['released_event'] = {
        'agent_name': current.agent_name,
        'inbound_event_id': current.inbound_event_id,
        'attempt_id': current.attempt_id,
        'status': InboundEventStatus.QUEUED.value,
    }
    created = prepare_reply_deliveries(dispatcher)
    if created:
        audit['retried_job'] = created[0].to_record()


def _tick_after_recovery(dispatcher, audit: dict[str, object]) -> None:
    if audit.get('retried_job') is None:
        prepare_reply_deliveries(dispatcher)
    started = tick_jobs(dispatcher)
    audit['next_started'] = [job.to_record() for job in started]


def _audit_changed(audit: dict[str, object]) -> bool:
    return bool(audit.get('cancelled_old') or audit.get('released_event') or audit.get('retried_job') or audit.get('next_started'))


def _running_stale_reason(dispatcher, job: JobRecord, *, running_hint: str | None = None) -> str | None:
    hinted_reason = _clean_running_hint(running_hint)
    if hinted_reason is not None:
        return hinted_reason
    runtime = getattr(dispatcher, '_registry', None).get(job.agent_name) if getattr(dispatcher, '_registry', None) is not None else None
    if runtime is None:
        return 'runtime_missing'
    if getattr(runtime, 'state', None) in _RECOVERABLE_RUNNING_STATES:
        health = normalized_runtime_health(runtime)
        if health in _STALE_RUNTIME_HEALTHS:
            return health.replace('-', '_') or 'runtime_unhealthy'
        if getattr(runtime, 'state', None) is AgentState.STOPPED:
            return 'runtime_stopped'
        if getattr(runtime, 'state', None) is AgentState.FAILED:
            return 'runtime_failed'
    pane_state = str(getattr(runtime, 'pane_state', '') or '').strip().lower()
    if pane_state in _STALE_PANE_STATES:
        return f'pane_{pane_state}'
    health = normalized_runtime_health(runtime)
    if health in _STALE_RUNTIME_HEALTHS:
        return health.replace('-', '_') or 'runtime_unhealthy'
    return None


@dataclass(frozen=True)
class _Lineage:
    attempt: object
    latest_attempt: object | None
    inbound: object | None


def _lineage_for_job(dispatcher, job: JobRecord) -> _Lineage | None:
    control = getattr(dispatcher, '_message_bureau_control', None)
    if control is None:
        return None
    attempt = control._attempt_store.get_latest_by_job_id(job.job_id)
    if attempt is None:
        return None
    latest_attempt = _latest_attempt_for_agent(control, attempt.message_id, attempt.agent_name)
    inbound = control._inbound_store.get_latest_for_attempt(attempt.agent_name, attempt.attempt_id)
    return _Lineage(attempt=attempt, latest_attempt=latest_attempt, inbound=inbound)


def _lookup_lineage_for_job(dispatcher, job: JobRecord, lineage_for_job=None):
    if callable(lineage_for_job):
        try:
            return lineage_for_job(job)
        except Exception:
            return None
    return _lineage_for_job(dispatcher, job)


def _latest_attempt_for_agent(control, message_id: str, agent_name: str):
    latest = None
    for attempt in control._attempt_store.list_message(message_id):
        if attempt.agent_name == agent_name:
            latest = attempt
    return latest


def _is_latest_attempt(lineage: _Lineage) -> bool:
    latest = lineage.latest_attempt
    if latest is None:
        return False
    if latest.attempt_id == lineage.attempt.attempt_id:
        return True
    if latest.attempt_state in _TERMINAL_ATTEMPT_STATES:
        return latest.attempt_id == lineage.attempt.attempt_id
    return False


def _can_retry_job(dispatcher, job: JobRecord, *, lineage_for_job=None) -> bool:
    lineage = _lookup_lineage_for_job(dispatcher, job, lineage_for_job=lineage_for_job)
    if lineage is None or lineage.latest_attempt is None:
        return False
    if not _is_latest_attempt(lineage):
        return False
    return lineage.attempt.attempt_state in _TERMINAL_ATTEMPT_STATES and lineage.attempt.attempt_state is not AttemptState.COMPLETED


def _already_retried_job_id(dispatcher, job: JobRecord) -> str | None:
    lineage = _lineage_for_job(dispatcher, job)
    if lineage is None or lineage.latest_attempt is None:
        return None
    if lineage.latest_attempt.attempt_id == lineage.attempt.attempt_id:
        return None
    return lineage.latest_attempt.job_id


def _release_lineage_head_if_blocking(dispatcher, job: JobRecord) -> dict[str, object] | None:
    lineage = _lineage_for_job(dispatcher, job)
    if lineage is None or lineage.inbound is None:
        return None
    inbound = lineage.inbound
    if inbound.status not in {InboundEventStatus.CREATED, InboundEventStatus.QUEUED, InboundEventStatus.DELIVERING}:
        return None
    if inbound.event_type is not InboundEventType.TASK_REQUEST:
        return None
    control = dispatcher._message_bureau_control
    head = control._mailbox_kernel.head_pending_event(inbound.agent_name)
    if head is None or head.inbound_event_id != inbound.inbound_event_id:
        return None
    released = control._mailbox_kernel.abandon(inbound.agent_name, inbound.inbound_event_id, finished_at=dispatcher._clock())
    if released is None:
        return None
    return {
        'agent_name': released.agent_name,
        'inbound_event_id': released.inbound_event_id,
        'attempt_id': released.attempt_id,
        'status': released.status.value,
    }


__all__ = ['CommsRecoverability', 'CommsRecoverTarget', 'comms_recover', 'comms_recoverability_for_job']
