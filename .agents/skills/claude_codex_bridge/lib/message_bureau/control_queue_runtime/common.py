from __future__ import annotations

from mailbox_runtime.targets import normalize_mailbox_target
from mailbox_kernel import MailboxState


def derive_mailbox_state(has_active: bool, queue_depth: int) -> str:
    if has_active:
        return MailboxState.DELIVERING.value
    if queue_depth > 0:
        return MailboxState.BLOCKED.value
    return MailboxState.IDLE.value


def require_mailbox_target(service, agent_name: str) -> str:
    normalized = normalize_mailbox_target(agent_name, known_targets=service._known_mailboxes)
    if normalized is None:
        raise ValueError(f'unknown mailbox target: {str(agent_name or "").strip().lower()}')
    return normalized


def summary_targets(service) -> tuple[str, ...]:
    return tuple(sorted(set(getattr(service._config, 'agents', {}).keys())))


def mailbox_has_activity(service, agent_name: str, *, mailbox=None) -> bool:
    record = mailbox if mailbox is not None else service._mailbox_store.load(agent_name)
    if record is None:
        return False
    if getattr(record, 'active_inbound_event_id', None):
        return True
    if int(getattr(record, 'queue_depth', 0) or 0) > 0:
        return True
    return int(getattr(record, 'pending_reply_count', 0) or 0) > 0


def preview_text(value: str, *, limit: int = 120) -> str:
    text = str(value or '').replace('\r', '').replace('\n', '\\n').strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + '...'


__all__ = [
    'derive_mailbox_state',
    'mailbox_has_activity',
    'preview_text',
    'require_mailbox_target',
    'summary_targets',
]
