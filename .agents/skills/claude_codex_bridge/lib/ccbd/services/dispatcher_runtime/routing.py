from __future__ import annotations

from typing import Iterable

from agents.models import QueuePolicy, normalize_agent_name
from ccbd.api_models import DeliveryScope
from ccbd.reload_drain import DrainQueueStore
from mailbox_runtime.targets import NON_AGENT_ACTORS

from .records import get_job, latest_for_agent
from .visible_reply import visible_reply_for_job


def validate_sender(dispatcher, sender: str) -> None:
    normalized = str(sender or '').strip().lower()
    if not normalized:
        raise dispatcher._dispatch_error('sender cannot be empty')
    if normalized in NON_AGENT_ACTORS:
        if normalized == 'cmd':
            raise dispatcher._dispatch_error(f'unknown sender agent: {normalized}')
        return
    agent_name = normalize_agent_name(normalized)
    if agent_name not in dispatcher._config.agents:
        raise dispatcher._dispatch_error(f'unknown sender agent: {agent_name}')


def resolve_targets(dispatcher, request) -> tuple[str, ...]:
    if request.delivery_scope is DeliveryScope.SINGLE:
        _ensure_dispatch_enabled(dispatcher, request.to_agent)
        return (request.to_agent,)

    alive = [
        runtime.agent_name
        for runtime in dispatcher._registry.list_alive()
        if not _dispatch_disabled(dispatcher, runtime.agent_name)
    ]
    if request.from_actor not in NON_AGENT_ACTORS:
        alive = [name for name in alive if name != request.from_actor]
    return tuple(sorted(alive))


def validate_targets_available(dispatcher, targets: Iterable[str]) -> None:
    for agent_name in targets:
        spec = dispatcher._registry.spec_for(agent_name)
        if bool(getattr(spec, 'dispatch_disabled', False)):
            raise dispatcher._dispatch_rejected_error(f'agent {agent_name} is dispatch-disabled')
        if _drain_blocks_dispatch(dispatcher, agent_name):
            raise dispatcher._dispatch_rejected_error(f'agent {agent_name} is draining and rejects new work')
        if spec.queue_policy is QueuePolicy.REJECT_WHEN_BUSY and dispatcher._has_outstanding_work(agent_name):
            raise dispatcher._dispatch_rejected_error(f'agent {agent_name} rejects new work while busy')


def _ensure_dispatch_enabled(dispatcher, agent_name: str):
    spec = dispatcher._registry.spec_for(agent_name)
    if bool(getattr(spec, 'dispatch_disabled', False)):
        raise dispatcher._dispatch_rejected_error(f'agent {agent_name} is dispatch-disabled')
    if _drain_blocks_dispatch(dispatcher, agent_name):
        raise dispatcher._dispatch_rejected_error(f'agent {agent_name} is draining and rejects new work')
    return spec


def _dispatch_disabled(dispatcher, agent_name: str) -> bool:
    try:
        spec = dispatcher._registry.spec_for(agent_name)
    except Exception:
        return True
    return bool(getattr(spec, 'dispatch_disabled', False)) or _drain_blocks_dispatch(dispatcher, agent_name)


def _drain_blocks_dispatch(dispatcher, agent_name: str) -> bool:
    try:
        return DrainQueueStore(dispatcher._layout).load().blocks_new_work_for(agent_name)
    except Exception:
        return False


def resolve_watch_target(dispatcher, target: str) -> tuple[str, str]:
    normalized = target.strip().lower()
    if not normalized:
        raise dispatcher._dispatch_error('watch target cannot be empty')
    if normalized.startswith('job_'):
        record = get_job(dispatcher, normalized)
        if record is None:
            raise dispatcher._dispatch_error(f'unknown job: {normalized}')
        return record.target_name, record.job_id

    dispatcher._registry.spec_for(normalized)
    active_job_id = dispatcher._state.active_job(normalized)
    if active_job_id is not None:
        return normalized, active_job_id
    latest = latest_for_agent(dispatcher, normalized)
    if latest is None:
        raise dispatcher._dispatch_error(f'agent {normalized} has no jobs to watch')
    return normalized, latest.job_id


def build_watch_payload(dispatcher, target: str, *, start_line: int = 0) -> dict:
    target_name, job_id = resolve_watch_target(dispatcher, target)
    latest = get_job(dispatcher, job_id)
    if latest is None:
        raise dispatcher._dispatch_error(f'unknown job: {job_id}')
    cursor, events = dispatcher._event_store.read_since_target(latest.target_kind, target_name, start_line)
    filtered = [event for event in events if event.job_id == job_id]
    snapshot = dispatcher._snapshot_writer.load(job_id)
    visible = visible_reply_for_job(dispatcher, latest, snapshot)
    terminal = False
    if latest.status in dispatcher._terminal_event_by_status:
        terminal = True
    return {
        'target': target,
        'job_id': job_id,
        'agent_name': latest.agent_name,
        'target_kind': latest.target_kind.value,
        'target_name': latest.target_name,
        'provider': latest.provider,
        'provider_instance': latest.provider_instance,
        'cursor': cursor,
        'terminal': terminal,
        'status': latest.status.value,
        'reply': visible.reply,
        'completion_reason': visible.reason,
        'visible_reply_source': visible.source,
        'visible_reply_id': visible.reply_id,
        'message_id': visible.message_id,
        'events': [event.to_record() for event in filtered],
    }
