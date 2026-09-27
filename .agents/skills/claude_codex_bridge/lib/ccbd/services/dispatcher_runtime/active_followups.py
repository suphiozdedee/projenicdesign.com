from __future__ import annotations

from dataclasses import replace

from ccbd.active_followups import ActiveFollowupRecord
from ccbd.api_models import JobStatus
from provider_execution.followups import ActiveFollowupRequest

from .records import get_job


_TERMINAL_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.CANCELLED,
        JobStatus.FAILED,
        JobStatus.INCOMPLETE,
    }
)


def request_active_followup(dispatcher, job_id: str, message: str) -> dict[str, object]:
    normalized_job_id = str(job_id or '').strip()
    normalized_message = str(message or '').strip()
    if not normalized_job_id:
        raise dispatcher._dispatch_error('followup requires job_id')
    if not normalized_message:
        raise dispatcher._dispatch_error('followup requires a non-empty message')

    with dispatcher._chain_transition_lock:
        current = get_job(dispatcher, normalized_job_id)
        created_at = dispatcher._clock()
        record = ActiveFollowupRecord(
            followup_id=dispatcher._new_id('fup'),
            job_id=normalized_job_id,
            message=normalized_message,
            agent_name=str(getattr(current, 'agent_name', '') or ''),
            provider=str(getattr(current, 'provider', '') or ''),
            sequence=dispatcher._active_followup_store.next_sequence(normalized_job_id),
            status='rejected',
            reason='unknown_job',
            mechanism='none',
            expected_provider_turn_ref=None,
            provider_turn_ref=None,
            created_at=created_at,
            updated_at=created_at,
        )
        if current is None:
            return _persist(dispatcher, record).public_record()
        if current.status in _TERMINAL_STATUSES:
            return _persist(
                dispatcher,
                replace(
                    record,
                    status='too_late',
                    reason=f'job_already_{current.status.value}',
                    diagnostics={'terminal_status': current.status.value},
                ),
            ).public_record()
        if current.status is not JobStatus.RUNNING:
            return _persist(
                dispatcher,
                replace(record, reason=f'job_not_running:{current.status.value}'),
            ).public_record()
        active_job_id = dispatcher._state.active_job_for(current.target_kind, current.target_name)
        if active_job_id != current.job_id:
            return _persist(
                dispatcher,
                replace(
                    record,
                    reason='job_not_active_for_target',
                    diagnostics={'active_job_id': active_job_id or ''},
                ),
            ).public_record()
        if dispatcher._execution_service is None:
            return _persist(
                dispatcher,
                replace(record, reason='provider_execution_unavailable'),
            ).public_record()

        capability = dispatcher._execution_service.active_followup_capability(current.job_id)
        provider_turn_ref = str(capability.provider_turn_ref or '').strip()
        if not capability.supported or not provider_turn_ref:
            reason = capability.reason or (
                'active_provider_turn_unbound' if capability.supported else 'provider_active_followup_unsupported'
            )
            status = (
                'terminal'
                if reason in {'provider_terminal_pending', 'active_submission_missing'}
                else 'rejected'
            )
            return _persist(
                dispatcher,
                replace(
                    record,
                    status=status,
                    reason=reason,
                    mechanism=capability.mechanism,
                    provider_turn_ref=provider_turn_ref or None,
                    diagnostics=dict(capability.diagnostics),
                ),
            ).public_record()

        accepted = _persist(
            dispatcher,
            replace(
                record,
                status='accepted',
                reason='durable_outbox_accepted',
                mechanism=capability.mechanism,
                expected_provider_turn_ref=provider_turn_ref,
                provider_turn_ref=provider_turn_ref,
                diagnostics=dict(capability.diagnostics),
            ),
        )
        dispatcher._append_event(
            current,
            'active_followup_accepted',
            _event_payload(accepted),
            timestamp=accepted.updated_at,
        )
        if any(
            pending.followup_id != accepted.followup_id
            for pending in dispatcher._active_followup_store.accepted()
        ):
            return _persist(
                dispatcher,
                replace(
                    accepted,
                    reason='durable_outbox_waiting_for_prior_followup',
                    updated_at=dispatcher._clock(),
                ),
            ).public_record()
        return _inject_accepted(dispatcher, current, accepted).public_record()


def replay_accepted_followups(dispatcher) -> tuple[ActiveFollowupRecord, ...]:
    if dispatcher._execution_service is None:
        return ()
    outcomes: list[ActiveFollowupRecord] = []
    with dispatcher._chain_transition_lock:
        for record in dispatcher._active_followup_store.accepted():
            current = get_job(dispatcher, record.job_id)
            if current is None:
                outcomes.append(
                    _persist(
                        dispatcher,
                        replace(
                            record,
                            status='rejected',
                            reason='job_missing_after_restart',
                            updated_at=dispatcher._clock(),
                        ),
                    )
                )
                continue
            if current.status in _TERMINAL_STATUSES:
                outcomes.append(
                    _persist(
                        dispatcher,
                        replace(
                            record,
                            status='terminal',
                            reason=f'job_{current.status.value}_before_replay',
                            updated_at=dispatcher._clock(),
                            diagnostics={
                                **record.diagnostics,
                                'terminal_status': current.status.value,
                            },
                        ),
                    )
                )
                continue
            if current.status is not JobStatus.RUNNING:
                outcomes.append(
                    _persist(
                        dispatcher,
                        replace(
                            record,
                            status='rejected',
                            reason=f'job_not_running_after_restart:{current.status.value}',
                            updated_at=dispatcher._clock(),
                        ),
                    )
                )
                continue
            outcome = _inject_accepted(dispatcher, current, record)
            outcomes.append(outcome)
            if outcome.status == 'accepted':
                break
    return tuple(outcomes)


def trace_active_followups(dispatcher, target: str) -> dict[str, object] | None:
    direct = dispatcher._active_followup_store.get_latest(target)
    if direct is not None:
        current = get_job(dispatcher, direct.job_id)
        result = (
            dict(dispatcher._message_bureau_control.trace(direct.job_id))
            if current is not None
            else {}
        )
        result['resolved_job_kind'] = result.get('resolved_kind')
        result['resolved_kind'] = 'active_followup'
        result['followup_id'] = direct.followup_id
        result['job_id'] = direct.job_id
        result['active_followups'] = [
            record.public_record()
            for record in dispatcher._active_followup_store.latest_for_job(direct.job_id)
        ]
        return result
    return None


def followups_for_trace(dispatcher, trace: dict[str, object]) -> list[dict[str, object]]:
    job_ids = {str(trace.get('job_id') or '').strip()}
    for job in trace.get('jobs') or ():
        if isinstance(job, dict):
            job_ids.add(str(job.get('job_id') or '').strip())
    job_ids.discard('')
    records: list[ActiveFollowupRecord] = []
    for job_id in sorted(job_ids):
        records.extend(dispatcher._active_followup_store.latest_for_job(job_id))
    records.sort(key=lambda item: (item.created_at, item.job_id, item.sequence, item.followup_id))
    return [record.public_record() for record in records]


def _inject_accepted(dispatcher, current, record: ActiveFollowupRecord) -> ActiveFollowupRecord:
    latest = get_job(dispatcher, current.job_id)
    if latest is None or latest.status in _TERMINAL_STATUSES:
        terminal_status = latest.status.value if latest is not None else 'missing'
        return _persist(
            dispatcher,
            replace(
                record,
                status='terminal',
                reason=f'job_{terminal_status}_before_injection',
                updated_at=dispatcher._clock(),
                diagnostics={**record.diagnostics, 'terminal_status': terminal_status},
            ),
        )
    result = dispatcher._execution_service.inject_active_followup(
        ActiveFollowupRequest(
            followup_id=record.followup_id,
            job_id=record.job_id,
            message=record.message,
            expected_provider_turn_ref=str(record.expected_provider_turn_ref or ''),
        )
    )
    updated = _persist(
        dispatcher,
        replace(
            record,
            status=result.status,
            reason=result.reason,
            mechanism=result.mechanism or record.mechanism,
            provider_turn_ref=result.provider_turn_ref or record.provider_turn_ref,
            updated_at=dispatcher._clock(),
            diagnostics={**record.diagnostics, **dict(result.diagnostics)},
        ),
    )
    dispatcher._append_event(
        latest,
        f'active_followup_{updated.status}',
        _event_payload(updated),
        timestamp=updated.updated_at,
    )
    return updated


def _persist(dispatcher, record: ActiveFollowupRecord) -> ActiveFollowupRecord:
    persisted = record if record.status == 'accepted' else replace(record, message='')
    dispatcher._active_followup_store.append(persisted)
    marker = getattr(dispatcher, 'mark_project_view_dirty', None)
    if callable(marker):
        marker()
    return persisted


def _event_payload(record: ActiveFollowupRecord) -> dict[str, object]:
    return {
        'followup_id': record.followup_id,
        'sequence': record.sequence,
        'status': record.status,
        'reason': record.reason,
        'mechanism': record.mechanism,
        'expected_provider_turn_ref': record.expected_provider_turn_ref,
        'provider_turn_ref': record.provider_turn_ref,
    }


__all__ = [
    'followups_for_trace',
    'replay_accepted_followups',
    'request_active_followup',
    'trace_active_followups',
]
