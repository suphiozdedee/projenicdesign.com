from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from completion.models import CompletionItem, CompletionItemKind
from provider_execution.base import ProviderSubmission
from provider_execution.common import build_item


@dataclass
class ClaudePollState:
    request_anchor: str
    next_seq: int
    anchor_seen: bool
    reply_buffer: str
    raw_buffer: str
    session_path: str
    last_assistant_uuid: str
    active_assistant_message_id: str = ""
    active_assistant_text: str = ""
    active_assistant_stop_reason: str = ""
    active_assistant_has_tool_use: bool = False
    terminal_reply: str = ""
    prompt_enqueued: bool = False
    queue_dequeue_observed: bool = False
    prompt_activated: bool = False
    prompt_enqueue_uuid: str = ""
    prompt_activation_uuid: str = ""
    items: list[CompletionItem] = field(default_factory=list)
    reached_turn_boundary: bool = False


def build_poll_state(submission: ProviderSubmission) -> ClaudePollState:
    from provider_execution.common import request_anchor_from_runtime_state

    no_wrap = bool(submission.runtime_state.get("no_wrap", False))
    anchor_seen = bool(submission.runtime_state.get("anchor_seen", False))
    has_activation_state = "prompt_activated" in submission.runtime_state
    legacy_synthetic_anchor = bool(submission.runtime_state.get("prompt_anchor_emitted_at"))
    prompt_activated = no_wrap or bool(submission.runtime_state.get("prompt_activated", False))
    if not has_activation_state and anchor_seen and not legacy_synthetic_anchor:
        prompt_activated = True
    if not no_wrap and not prompt_activated:
        anchor_seen = False

    reply_buffer = str(submission.runtime_state.get("reply_buffer") or "")
    return ClaudePollState(
        request_anchor=request_anchor_from_runtime_state(submission.runtime_state, fallback=submission.job_id),
        next_seq=int(submission.runtime_state.get("next_seq", 1)),
        anchor_seen=anchor_seen,
        reply_buffer=reply_buffer,
        raw_buffer=str(submission.runtime_state.get("raw_buffer") or ""),
        session_path=str(submission.runtime_state.get("session_path") or ""),
        last_assistant_uuid=str(submission.runtime_state.get("last_assistant_uuid") or ""),
        active_assistant_message_id=str(
            submission.runtime_state.get("active_assistant_message_id") or ""
        ),
        active_assistant_text=str(
            submission.runtime_state.get("active_assistant_text") or ""
        ),
        active_assistant_stop_reason=str(
            submission.runtime_state.get("active_assistant_stop_reason") or ""
        ),
        active_assistant_has_tool_use=bool(
            submission.runtime_state.get("active_assistant_has_tool_use", False)
        ),
        terminal_reply=str(submission.runtime_state.get("terminal_reply") or ""),
        prompt_enqueued=bool(submission.runtime_state.get("prompt_enqueued", False)),
        queue_dequeue_observed=bool(submission.runtime_state.get("queue_dequeue_observed", False)),
        prompt_activated=prompt_activated,
        prompt_enqueue_uuid=str(submission.runtime_state.get("prompt_enqueue_uuid") or ""),
        prompt_activation_uuid=str(submission.runtime_state.get("prompt_activation_uuid") or ""),
    )


def apply_session_rotation(submission: ProviderSubmission, poll: ClaudePollState, *, new_session_path: str, now: str) -> None:
    if not new_session_path or new_session_path == poll.session_path:
        return
    poll.items.append(
        build_item(
            submission,
            kind=CompletionItemKind.SESSION_ROTATE,
            timestamp=now,
            seq=poll.next_seq,
            payload={
                "session_path": new_session_path,
                "provider_session_id": Path(new_session_path).stem,
            },
        )
    )
    poll.next_seq += 1
    poll.session_path = new_session_path
    poll.anchor_seen = bool(submission.runtime_state.get("no_wrap", False))
    poll.reply_buffer = ""
    poll.raw_buffer = ""
    poll.last_assistant_uuid = ""
    poll.active_assistant_message_id = ""
    poll.active_assistant_text = ""
    poll.active_assistant_stop_reason = ""
    poll.active_assistant_has_tool_use = False
    poll.terminal_reply = ""
    poll.prompt_enqueued = False
    poll.queue_dequeue_observed = False
    poll.prompt_activated = bool(submission.runtime_state.get("no_wrap", False))
    poll.prompt_enqueue_uuid = ""
    poll.prompt_activation_uuid = ""


__all__ = [
    "ClaudePollState",
    "apply_session_rotation",
    "build_poll_state",
]
