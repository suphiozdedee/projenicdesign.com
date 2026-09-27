from __future__ import annotations

from dataclasses import replace

from ..mailbox import rebuild_mailbox_summary
from ..queries import head_pending_event
from ..summary import apply_transition_summary_update, summary_head_from_event
from .claiming import claim


def ack_reply(
    service,
    agent_name: str,
    inbound_event_id: str,
    *,
    started_at: str | None = None,
    finished_at: str | None = None,
):
    normalized = service._normalize_agent_name(agent_name)
    head = head_pending_event(service, normalized)
    timestamp = _ack_timestamp(service, started_at=started_at, finished_at=finished_at)
    if not _head_matches_reply(service, head, inbound_event_id=inbound_event_id):
        return _refresh_and_return(service, normalized, timestamp=timestamp)

    current = service._inbound_store.get_latest(normalized, inbound_event_id)
    if current is None or current.status in service._terminal_event_states:
        return _refresh_and_return(service, normalized, timestamp=timestamp, value=current)

    if current.status is service._status_delivering:
        return mark_terminal(
            service,
            normalized,
            inbound_event_id,
            status=service._status_consumed,
            finished_at=finished_at or timestamp,
        )

    claimed = claim(service, normalized, inbound_event_id, started_at=started_at or timestamp)
    if claimed is None:
        return _refresh_and_return(service, normalized, timestamp=timestamp)
    return mark_terminal(
        service,
        normalized,
        inbound_event_id,
        status=service._status_consumed,
        finished_at=finished_at or timestamp,
    )


def mark_terminal(
    service,
    agent_name: str,
    inbound_event_id: str,
    *,
    status,
    finished_at: str | None = None,
):
    normalized = service._normalize_agent_name(agent_name)
    timestamp = finished_at or service._clock()
    current = service._inbound_store.get_latest(normalized, inbound_event_id)
    if current is None:
        return _refresh_and_return(service, normalized, timestamp=timestamp)
    if current.status in service._terminal_event_states:
        return _refresh_and_return(service, normalized, timestamp=timestamp, value=current)

    updated = replace(current, status=status, finished_at=timestamp)
    service._inbound_store.append(updated)
    _release_matching_lease(service, normalized, inbound_event_id=inbound_event_id)
    prior = service._mailbox_store.load(normalized)
    if prior is None or prior.queue_depth <= 0:
        rebuild_mailbox_summary(service, normalized, updated_at=timestamp)
        return updated
    queue_depth = max(0, prior.queue_depth - 1)
    pending_reply_count = max(
        0,
        prior.pending_reply_count - (1 if current.event_type is service._reply_event_type else 0),
    )
    active_inbound_event_id = None if prior.active_inbound_event_id == current.inbound_event_id else prior.active_inbound_event_id
    if prior.head_inbound_event_id == current.inbound_event_id:
        if queue_depth == 0:
            summary_head = summary_head_from_event(None)
        elif queue_depth == 1:
            summary_head = summary_head_from_event(head_pending_event(service, normalized))
        else:
            next_head = head_pending_event(service, normalized)
            if next_head is None:
                rebuild_mailbox_summary(service, normalized, updated_at=timestamp)
                return updated
            summary_head = summary_head_from_event(next_head)
    else:
        summary_head = {
            'head_inbound_event_id': prior.head_inbound_event_id,
            'head_event_type': prior.head_event_type,
            'head_status': prior.head_status,
            'head_message_id': prior.head_message_id,
            'head_attempt_id': prior.head_attempt_id,
            'head_payload_ref': prior.head_payload_ref,
        }
    apply_transition_summary_update(
        service,
        normalized,
        queue_depth=queue_depth,
        pending_reply_count=pending_reply_count,
        active_inbound_event_id=active_inbound_event_id,
        last_started_at=prior.last_inbound_started_at,
        last_finished_at=_latest_timestamp(prior.last_inbound_finished_at, timestamp),
        updated_at=timestamp,
        summary_source='transition-terminal',
        summary_head=summary_head,
    )
    return updated


def rewrite_head(
    service,
    agent_name: str,
    inbound_event_id: str,
    *,
    payload_ref: str | None,
    status,
    updated_at: str | None = None,
    clear_progress: bool = False,
):
    normalized = service._normalize_agent_name(agent_name)
    timestamp = updated_at or service._clock()
    current = service._inbound_store.get_latest(normalized, inbound_event_id)
    if current is None or current.status in service._terminal_event_states:
        rebuild_mailbox_summary(service, normalized, updated_at=timestamp)
        return None

    updated = replace(
        current,
        payload_ref=payload_ref,
        status=status,
        started_at=None if clear_progress else current.started_at,
        finished_at=None if clear_progress else current.finished_at,
    )
    service._inbound_store.append(updated)
    _release_matching_lease(service, normalized, inbound_event_id=inbound_event_id)
    prior = service._mailbox_store.load(normalized)
    if prior is None or prior.head_inbound_event_id != inbound_event_id or prior.queue_depth <= 0:
        rebuild_mailbox_summary(service, normalized, updated_at=timestamp)
        return updated

    apply_transition_summary_update(
        service,
        normalized,
        queue_depth=prior.queue_depth,
        pending_reply_count=prior.pending_reply_count,
        active_inbound_event_id=None if prior.active_inbound_event_id == inbound_event_id else prior.active_inbound_event_id,
        last_started_at=prior.last_inbound_started_at if not clear_progress else prior.last_inbound_started_at,
        last_finished_at=prior.last_inbound_finished_at if not clear_progress else prior.last_inbound_finished_at,
        updated_at=timestamp,
        summary_source='transition-rewrite-head',
        summary_head=summary_head_from_event(updated),
    )
    return updated


def _ack_timestamp(service, *, started_at: str | None, finished_at: str | None) -> str:
    return finished_at or started_at or service._clock()


def _head_matches_reply(service, head, *, inbound_event_id: str) -> bool:
    if head is None:
        return False
    if head.inbound_event_id != inbound_event_id:
        return False
    return head.event_type is service._reply_event_type


def _refresh_and_return(service, agent_name: str, *, timestamp: str, value=None):
    rebuild_mailbox_summary(service, agent_name, updated_at=timestamp)
    return value


def _latest_timestamp(current: str | None, candidate: str | None) -> str | None:
    if candidate and (current is None or candidate > current):
        return candidate
    return current


def _release_matching_lease(service, agent_name: str, *, inbound_event_id: str) -> None:
    lease = service._lease_store.load(agent_name)
    if lease is None:
        return
    if lease.lease_state is not service._lease_state_acquired:
        return
    if lease.inbound_event_id != inbound_event_id:
        return
    service._lease_store.remove(agent_name)


__all__ = ['ack_reply', 'mark_terminal', 'rewrite_head']
