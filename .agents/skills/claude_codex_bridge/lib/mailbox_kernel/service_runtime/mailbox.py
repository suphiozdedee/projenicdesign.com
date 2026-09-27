from __future__ import annotations

from mailbox_runtime.targets import normalize_mailbox_owner_name

from .queries import latest_events, pending_events
from .summary import build_mailbox_summary_record, save_summary_record


def project_mailbox_summary(
    service,
    agent_name: str,
    *,
    updated_at: str | None = None,
    prior=Ellipsis,
    summary_source: str = 'history-refresh',
):
    normalized = normalize_mailbox_owner_name(agent_name)
    timestamp = updated_at or service._clock()
    if prior is Ellipsis:
        prior = service._mailbox_store.load(normalized)
    lease = service._lease_store.load(normalized)
    events = pending_events(service, normalized)
    queue_depth = len(events)
    pending_reply_count = sum(1 for event in events if event.event_type is service._reply_event_type)
    last_started, last_finished = _latest_activity(
        service,
        normalized,
        prior=prior,
    )
    return build_mailbox_summary_record(
        service,
        normalized,
        prior=prior,
        lease=lease,
        queue_depth=queue_depth,
        pending_reply_count=pending_reply_count,
        active_inbound_event_id=_active_inbound_event_id(service, lease=lease),
        last_started_at=last_started,
        last_finished_at=last_finished,
        updated_at=timestamp,
        summary_source=summary_source,
    )


def rebuild_mailbox_summary(service, agent_name: str, *, updated_at: str | None = None):
    normalized = normalize_mailbox_owner_name(agent_name)
    prior = service._mailbox_store.load(normalized)
    expected_summary_version = None if prior is None else int(prior.summary_version)
    record = project_mailbox_summary(
        service,
        normalized,
        updated_at=updated_at,
        prior=prior,
        summary_source='history-refresh',
    )
    return save_summary_record(
        service,
        record,
        expected_summary_version=expected_summary_version,
    )


def _active_inbound_event_id(service, *, lease):
    if lease is None or lease.lease_state is not service._lease_state_acquired:
        return None
    return lease.inbound_event_id or None


def _latest_activity(service, normalized: str, *, prior):
    last_started = prior.last_inbound_started_at if prior is not None else None
    last_finished = prior.last_inbound_finished_at if prior is not None else None
    for event in latest_events(service, normalized):
        last_started = _latest_timestamp(last_started, event.started_at)
        last_finished = _latest_timestamp(last_finished, event.finished_at)
    return last_started, last_finished


def _latest_timestamp(current: str | None, candidate: str | None) -> str | None:
    if candidate and (current is None or candidate > current):
        return candidate
    return current


__all__ = ['project_mailbox_summary', 'rebuild_mailbox_summary']
