from __future__ import annotations

from mailbox_kernel import InboundEventStatus
from message_bureau.reply_payloads import compose_reply_payload


def rewrite_reply_head(
    dispatcher,
    current,
    *,
    reply_id: str,
    delivery_job_id: str | None,
    status: InboundEventStatus,
    updated_at: str,
    clear_progress: bool,
) -> None:
    control = dispatcher._message_bureau_control
    control._mailbox_kernel.rewrite_head(
        current.agent_name,
        current.inbound_event_id,
        payload_ref=compose_reply_payload(reply_id, delivery_job_id=delivery_job_id),
        status=status,
        updated_at=updated_at,
        clear_progress=clear_progress,
    )


__all__ = ['rewrite_reply_head']
