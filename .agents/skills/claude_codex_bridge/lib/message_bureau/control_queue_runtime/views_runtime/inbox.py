from __future__ import annotations

from mailbox_kernel import InboundEventType
from message_bureau.reply_metadata import (
    reply_heartbeat_silence_seconds,
    reply_last_progress_at,
    reply_notice,
    reply_notice_kind,
)
from message_bureau.reply_payloads import reply_id_from_payload

from .agent import agent_queue_summary
from ..common import require_mailbox_target
from ..events import inbox_item_summary, pending_event_records
from mailbox_kernel.service_runtime.summary import mailbox_head_payload
from .common import read_mailbox_summary


def inbox(service, agent_name: str, *, detail: bool | None = None) -> dict[str, object]:
    normalized = require_mailbox_target(service, agent_name)
    mailbox_payload = agent_queue_summary(service, normalized)
    summary_read = read_mailbox_summary(service, normalized)
    mailbox = summary_read.mailbox
    if detail is not True:
        return {
            'target': normalized,
            'summary_status': summary_read.status,
            'summary_error': summary_read.error,
            'agent': mailbox_payload,
            'item_count': int(mailbox_payload.get('queue_depth') or 0),
            'head': _enrich_mailbox_head(service, normalized, mailbox_head_payload(mailbox)),
            'items': [],
        }
    records = pending_event_records(service, normalized)
    items = [inbox_item_summary(service, record, position=index) for index, record in enumerate(records, start=1)]
    head = _enrich_mailbox_head(service, normalized, mailbox_head_payload(mailbox)) or (items[0] if items else None)
    return {
        'target': normalized,
        'summary_status': summary_read.status,
        'summary_error': summary_read.error,
        'agent': mailbox_payload,
        'item_count': len(items),
        'head': head,
        'items': items,
    }


def mailbox_head(service, agent_name: str) -> dict[str, object]:
    normalized = require_mailbox_target(service, agent_name)
    summary_read = read_mailbox_summary(service, normalized)
    mailbox = summary_read.mailbox
    return {
        'target': normalized,
        'summary_status': summary_read.status,
        'summary_error': summary_read.error,
        'head': _enrich_mailbox_head(service, normalized, mailbox_head_payload(mailbox)),
    }


def _enrich_mailbox_head(service, agent_name: str, head: dict | None) -> dict | None:
    if not isinstance(head, dict):
        return None
    if str(head.get('event_type') or '').strip().lower() != InboundEventType.TASK_REPLY.value:
        return head
    attempt = service._attempt_store.get_latest(head.get('attempt_id')) if head.get('attempt_id') else None
    reply_id = reply_id_from_payload(head.get('payload_ref'))
    if not reply_id and head.get('inbound_event_id'):
        record = service._inbound_store.get_latest(agent_name, head['inbound_event_id'])  # type: ignore[index]
        reply_id = reply_id_from_payload(getattr(record, 'payload_ref', None)) if record is not None else None
    if not reply_id:
        return head
    reply = service._reply_store.get_latest(reply_id)
    if reply is None:
        return head
    enriched = dict(head)
    enriched.update(
        {
            'reply_id': reply.reply_id,
            'source_actor': reply.agent_name,
            'reply_terminal_status': reply.terminal_status.value,
            'reply_notice': reply_notice(reply),
            'reply_notice_kind': reply_notice_kind(reply),
            'job_id': attempt.job_id if attempt is not None else None,
            'reply_finished_at': reply.finished_at,
            'reply_last_progress_at': reply_last_progress_at(reply),
            'reply_heartbeat_silence_seconds': reply_heartbeat_silence_seconds(reply),
            'reply': reply.reply,
        }
    )
    return enriched


__all__ = ['inbox', 'mailbox_head']
