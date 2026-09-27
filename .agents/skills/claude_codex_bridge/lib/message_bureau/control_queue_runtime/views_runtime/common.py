from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MailboxSummaryRead:
    mailbox: object | None
    status: str
    error: str | None = None


def read_mailbox_summary(service, agent_name: str) -> MailboxSummaryRead:
    try:
        mailbox = service._mailbox_store.load(agent_name)
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        return MailboxSummaryRead(mailbox=None, status='error', error=detail)
    if mailbox is None:
        return MailboxSummaryRead(mailbox=None, status='missing')
    return MailboxSummaryRead(mailbox=mailbox, status='ok')


__all__ = ['MailboxSummaryRead', 'read_mailbox_summary']
