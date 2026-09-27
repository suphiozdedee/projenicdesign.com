from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VisibleReply:
    reply: str
    reason: str | None
    confidence: str | None
    updated_at: str | None
    source: str
    reply_id: str | None = None
    message_id: str | None = None


def visible_reply_for_job(dispatcher, job, snapshot=None) -> VisibleReply:
    terminal = job.terminal_decision if isinstance(job.terminal_decision, dict) else None
    if _is_delegated_terminal(terminal):
        reply = _latest_message_reply_for_job(dispatcher, job)
        if reply is not None:
            return VisibleReply(
                reply=reply.reply,
                reason=_reply_reason(reply),
                confidence=None,
                updated_at=reply.finished_at,
                source='message_bureau_reply',
                reply_id=reply.reply_id,
                message_id=reply.message_id,
            )
        return VisibleReply(
            reply='',
            reason='callback_pending',
            confidence=None,
            updated_at=job.updated_at,
            source='callback_delegated_pending',
            message_id=_message_id_for_job(dispatcher, job),
        )

    if terminal is not None:
        return VisibleReply(
            reply=str(terminal.get('reply') or ''),
            reason=str(terminal.get('reason') or '') or None,
            confidence=str(terminal.get('confidence') or '') or None,
            updated_at=job.updated_at,
            source='job_terminal_decision',
            message_id=_message_id_for_job(dispatcher, job),
        )

    latest_decision = snapshot.latest_decision if snapshot is not None else None
    if latest_decision is not None:
        confidence = latest_decision.confidence.value if latest_decision.confidence else None
        return VisibleReply(
            reply=latest_decision.reply,
            reason=latest_decision.reason,
            confidence=confidence,
            updated_at=snapshot.updated_at,
            source='snapshot',
            message_id=_message_id_for_job(dispatcher, job),
        )

    return VisibleReply(
        reply='',
        reason=None,
        confidence=None,
        updated_at=getattr(job, 'updated_at', None),
        source='none',
        message_id=_message_id_for_job(dispatcher, job),
    )


def _is_delegated_terminal(terminal: dict | None) -> bool:
    return bool(terminal and (terminal.get('delegated') or terminal.get('chain_edge_id')))


def _latest_message_reply_for_job(dispatcher, job):
    message_id = _message_id_for_job(dispatcher, job)
    if not message_id or dispatcher._message_bureau is None:
        return None
    replies = dispatcher._message_bureau._reply_store.list_message(message_id)
    if not replies:
        return None
    return sorted(replies, key=lambda item: (item.finished_at, item.reply_id))[-1]


def _message_id_for_job(dispatcher, job) -> str | None:
    if dispatcher._message_bureau is None:
        return None
    attempt = dispatcher._message_bureau._attempt_store.get_latest_by_job_id(job.job_id)
    if attempt is None:
        return None
    return attempt.message_id


def _reply_reason(reply) -> str | None:
    diagnostics = dict(getattr(reply, 'diagnostics', None) or {})
    reason = str(diagnostics.get('reason') or '').strip()
    return reason or None


__all__ = ['VisibleReply', 'visible_reply_for_job']
