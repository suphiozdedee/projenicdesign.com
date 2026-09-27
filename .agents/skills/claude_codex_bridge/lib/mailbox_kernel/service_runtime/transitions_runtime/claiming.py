from __future__ import annotations

from dataclasses import replace

from ..mailbox import rebuild_mailbox_summary
from ..lease_expiry import lease_expires_at_value
from ..queries import head_pending_event, peek_next
from ..summary import apply_transition_summary_update, summary_head_from_event
from .leasing import next_lease_version


def claim(service, agent_name: str, inbound_event_id: str, *, started_at: str | None = None):
    normalized = service._normalize_agent_name(agent_name)
    timestamp = started_at or service._clock()
    current = _load_claim_candidate(service, normalized, inbound_event_id)
    if current is None:
        return _refresh_none(service, normalized, timestamp)

    if _has_conflicting_lease(service, normalized, inbound_event_id):
        return _refresh_none(service, normalized, timestamp)
    if current.status is service._status_delivering:
        return _refresh_current(service, normalized, current, timestamp)
    if not _is_claimable_head(service, normalized, current):
        return _refresh_none(service, normalized, timestamp)
    return _claim_current(service, normalized, current, timestamp)


def _load_claim_candidate(service, agent_name: str, inbound_event_id: str):
    current = service._inbound_store.get_latest(agent_name, inbound_event_id)
    if current is None or current.status in service._terminal_event_states:
        return None
    return current


def _has_conflicting_lease(service, agent_name: str, inbound_event_id: str) -> bool:
    lease = service._lease_store.load(agent_name)
    return (
        lease is not None
        and lease.lease_state is service._lease_state_acquired
        and lease.inbound_event_id != inbound_event_id
    )


def _is_claimable_head(service, agent_name: str, current) -> bool:
    if current.status not in service._claimable_event_states:
        return False
    head = head_pending_event(service, agent_name)
    return head is not None and head.inbound_event_id == current.inbound_event_id


def _claim_current(service, agent_name: str, current, timestamp: str):
    updated = _delivery_record(service, current, timestamp)
    service._inbound_store.append(updated)
    service._lease_store.save(_delivery_lease(service, agent_name, current.inbound_event_id, timestamp))
    prior = service._mailbox_store.load(agent_name)
    if prior is None or prior.queue_depth <= 0 or prior.head_inbound_event_id != current.inbound_event_id:
        rebuild_mailbox_summary(service, agent_name, updated_at=timestamp)
        return updated
    apply_transition_summary_update(
        service,
        agent_name,
        queue_depth=prior.queue_depth,
        pending_reply_count=prior.pending_reply_count,
        active_inbound_event_id=current.inbound_event_id,
        last_started_at=_latest_timestamp(prior.last_inbound_started_at, updated.started_at),
        last_finished_at=prior.last_inbound_finished_at,
        updated_at=timestamp,
        summary_source='transition-claim',
        summary_head=summary_head_from_event(updated),
    )
    return updated


def _delivery_record(service, current, timestamp: str):
    return replace(
        current,
        status=service._status_delivering,
        started_at=current.started_at or timestamp,
        finished_at=None,
    )


def _delivery_lease(service, agent_name: str, inbound_event_id: str, timestamp: str):
    return service._delivery_lease_cls(
        agent_name=agent_name,
        inbound_event_id=inbound_event_id,
        lease_version=next_lease_version(service, agent_name),
        acquired_at=timestamp,
        last_progress_at=timestamp,
        expires_at=lease_expires_at_value(service, timestamp),
        lease_state=service._lease_state_acquired,
    )


def _refresh_current(service, agent_name: str, current, timestamp: str):
    rebuild_mailbox_summary(service, agent_name, updated_at=timestamp)
    return current


def claim_next(service, agent_name: str, *, event_type=None, started_at: str | None = None):
    next_event = peek_next(service, agent_name, event_type=event_type)
    if next_event is None:
        rebuild_mailbox_summary(service, agent_name, updated_at=started_at or service._clock())
        return None
    return claim(service, agent_name, next_event.inbound_event_id, started_at=started_at)


def _refresh_none(service, agent_name: str, timestamp: str):
    rebuild_mailbox_summary(service, agent_name, updated_at=timestamp)
    return None


def _latest_timestamp(current: str | None, candidate: str | None) -> str | None:
    if candidate and (current is None or candidate > current):
        return candidate
    return current


__all__ = ['claim', 'claim_next']
