from __future__ import annotations

from mailbox_kernel import InboundEventType
from message_bureau.reply_metadata import (
    reply_heartbeat_silence_seconds,
    reply_last_progress_at,
    reply_notice,
    reply_notice_kind,
)
from message_bureau.reply_payloads import reply_id_from_payload

from ..common import derive_mailbox_state, require_mailbox_target
from ..events import pending_events
from .common import read_mailbox_summary


def agent_queue(service, agent_name: str) -> dict[str, object]:
    return agent_queue_detail(service, agent_name)


def agent_queue_detail(service, agent_name: str) -> dict[str, object]:
    normalized = require_mailbox_target(service, agent_name)
    summary_read = read_mailbox_summary(service, normalized)
    mailbox = summary_read.mailbox
    events = pending_events(service, normalized)
    summary = agent_queue_summary(service, normalized)
    active = _active_event(service, normalized, mailbox, events)
    if mailbox is None:
        queue_depth = len(events)
        pending_reply_count = sum(1 for event in events if event['event_type'] == 'task_reply')
        mailbox_state = derive_mailbox_state(active is not None, queue_depth)
        active_inbound_event_id = active['inbound_event_id'] if active is not None else None
        last_started, last_finished = _last_event_timestamps(events)
    else:
        queue_depth = summary['queue_depth']
        pending_reply_count = summary['pending_reply_count']
        mailbox_state = summary['mailbox_state']
        active_inbound_event_id = summary['active_inbound_event_id']
        last_started = summary['last_inbound_started_at']
        last_finished = summary['last_inbound_finished_at']
    return {
        'agent_name': normalized,
        'mailbox_id': summary['mailbox_id'],
        'mailbox_state': mailbox_state,
        'lease_version': summary['lease_version'],
        'queue_depth': queue_depth,
        'pending_reply_count': pending_reply_count,
        'active_inbound_event_id': active_inbound_event_id,
        'active': active,
        'last_inbound_started_at': last_started,
        'last_inbound_finished_at': last_finished,
        'summary_status': summary['summary_status'],
        'summary_error': summary.get('summary_error'),
        'queued_events': events,
    }


def agent_queue_summary(service, agent_name: str) -> dict[str, object]:
    normalized = require_mailbox_target(service, agent_name)
    summary_read = read_mailbox_summary(service, normalized)
    mailbox = summary_read.mailbox
    if mailbox is None:
        return {
            'agent_name': normalized,
            'mailbox_id': f'mbx_{normalized}',
            'mailbox_state': None,
            'lease_version': 0,
            'queue_depth': 0,
            'pending_reply_count': 0,
            'active_inbound_event_id': None,
            'last_inbound_started_at': None,
            'last_inbound_finished_at': None,
            'summary_status': summary_read.status,
            'summary_error': summary_read.error,
        }
    return {
        'agent_name': normalized,
        'mailbox_id': mailbox.mailbox_id,
        'mailbox_state': getattr(mailbox.mailbox_state, 'value', mailbox.mailbox_state),
        'lease_version': mailbox.lease_version,
        'queue_depth': mailbox.queue_depth,
        'pending_reply_count': mailbox.pending_reply_count,
        'active_inbound_event_id': mailbox.active_inbound_event_id,
        'last_inbound_started_at': mailbox.last_inbound_started_at,
        'last_inbound_finished_at': mailbox.last_inbound_finished_at,
        'summary_status': summary_read.status,
        'summary_error': summary_read.error,
    }


def _active_event(service, agent_name: str, mailbox, events: tuple[dict, ...] | list[dict]) -> dict | None:
    if mailbox is None or not mailbox.active_inbound_event_id:
        return _active_event_from_events(events)
    get_latest = getattr(service._inbound_store, 'get_latest', None)
    if not callable(get_latest):
        return _active_event_from_events(events)
    record = get_latest(agent_name, mailbox.active_inbound_event_id)
    if record is None:
        return _active_event_from_events(events)
    return _event_payload(service, record)


def _active_event_from_events(events: tuple[dict, ...] | list[dict]) -> dict | None:
    event_index = {event['inbound_event_id']: event for event in events}
    for event in event_index.values():
        if event['status'] == 'delivering':
            return event
    return next(iter(events), None)


def _event_payload(service, record) -> dict[str, object]:
    attempt = service._attempt_store.get_latest(record.attempt_id) if record.attempt_id else None
    message = service._message_store.get_latest(record.message_id)
    item = {
        'position': 1,
        'inbound_event_id': record.inbound_event_id,
        'event_type': record.event_type.value,
        'status': record.status.value,
        'priority': record.priority,
        'message_id': record.message_id,
        'message_state': message.message_state.value if message is not None else None,
        'attempt_id': record.attempt_id,
        'attempt_state': attempt.attempt_state.value if attempt is not None else None,
        'job_id': attempt.job_id if attempt is not None else None,
        'created_at': record.created_at,
        'started_at': record.started_at,
        'finished_at': record.finished_at,
    }
    if record.event_type is not InboundEventType.TASK_REPLY:
        return item
    reply_id = reply_id_from_payload(record.payload_ref)
    if not reply_id:
        return item
    reply = service._reply_store.get_latest(reply_id)
    if reply is None:
        return item
    item.update(
        {
            'reply_id': reply.reply_id,
            'source_actor': reply.agent_name,
            'reply_terminal_status': reply.terminal_status.value,
            'reply_notice': reply_notice(reply),
            'reply_notice_kind': reply_notice_kind(reply),
            'reply_finished_at': reply.finished_at,
            'reply_last_progress_at': reply_last_progress_at(reply),
            'reply_heartbeat_silence_seconds': reply_heartbeat_silence_seconds(reply),
        }
    )
    return item


def _last_event_timestamps(events: tuple[dict, ...] | list[dict]) -> tuple[str | None, str | None]:
    last_started = None
    last_finished = None
    for event in events:
        started_at = event.get('started_at')
        finished_at = event.get('finished_at')
        if started_at and (last_started is None or started_at > last_started):
            last_started = started_at
        if finished_at and (last_finished is None or finished_at > last_finished):
            last_finished = finished_at
    return last_started, last_finished


__all__ = ['agent_queue', 'agent_queue_detail', 'agent_queue_summary']
