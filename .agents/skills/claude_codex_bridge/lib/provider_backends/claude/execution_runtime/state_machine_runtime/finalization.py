from __future__ import annotations

from dataclasses import replace

from provider_execution.base import ProviderPollResult, ProviderSubmission

from .models import ClaudePollState


def finalize_poll_result(
    submission: ProviderSubmission,
    poll: ClaudePollState,
    *,
    state: dict[str, object],
) -> ProviderPollResult:
    terminal_reply = str(getattr(poll, "terminal_reply", "") or "")
    resolved_reply = terminal_reply or poll.reply_buffer
    updated = replace(
        submission,
        reply=resolved_reply,
        runtime_state={
            **submission.runtime_state,
            "state": state,
            "next_seq": poll.next_seq,
            "anchor_seen": poll.anchor_seen,
            "reply_buffer": poll.reply_buffer,
            "raw_buffer": poll.raw_buffer,
            "session_path": poll.session_path,
            "last_assistant_uuid": poll.last_assistant_uuid,
            "active_assistant_message_id": str(
                getattr(poll, "active_assistant_message_id", "") or ""
            ),
            "active_assistant_text": str(
                getattr(poll, "active_assistant_text", "") or ""
            ),
            "active_assistant_stop_reason": str(
                getattr(poll, "active_assistant_stop_reason", "") or ""
            ),
            "active_assistant_has_tool_use": bool(
                getattr(poll, "active_assistant_has_tool_use", False)
            ),
            "terminal_reply": terminal_reply,
            "prompt_enqueued": bool(getattr(poll, "prompt_enqueued", False)),
            "queue_dequeue_observed": bool(getattr(poll, "queue_dequeue_observed", False)),
            "prompt_activated": bool(getattr(poll, "prompt_activated", False)),
            "prompt_enqueue_uuid": str(getattr(poll, "prompt_enqueue_uuid", "") or ""),
            "prompt_activation_uuid": str(getattr(poll, "prompt_activation_uuid", "") or ""),
        },
    )
    if not poll.items:
        return ProviderPollResult(submission=updated, items=())
    return ProviderPollResult(submission=updated, items=tuple(poll.items))


__all__ = ["finalize_poll_result"]
