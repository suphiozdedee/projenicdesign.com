from __future__ import annotations

from completion.models import CompletionItemKind
from provider_execution.base import ProviderSubmission
from provider_execution.common import build_item

from ...protocol import extract_reply_for_req, is_done_text, strip_done_text
from .models import ClaudePollState


def handle_assistant_event(
    submission: ProviderSubmission,
    poll: ClaudePollState,
    event: dict[str, object],
    *,
    now: str,
) -> None:
    text = assistant_text(event)
    subagent_id, subagent_name, is_subagent = assistant_identity(event)
    event_assistant_uuid = assistant_uuid(event)
    cleaned = cleaned_assistant_text(text, request_anchor=poll.request_anchor)
    if is_subagent:
        if has_visible_text(cleaned):
            append_chunk_item(
                submission,
                poll,
                event=event,
                cleaned=cleaned,
                subagent_id=subagent_id,
                subagent_name=subagent_name,
                event_assistant_uuid=event_assistant_uuid,
                now=now,
            )
        return

    update_top_level_assistant_message(
        poll,
        event=event,
        cleaned=cleaned,
        event_assistant_uuid=event_assistant_uuid,
    )
    poll.raw_buffer = append_buffer(poll.raw_buffer, text)
    if has_visible_text(cleaned):
        append_chunk_item(
            submission,
            poll,
            event=event,
            cleaned=cleaned,
            subagent_id=subagent_id,
            subagent_name=subagent_name,
            event_assistant_uuid=event_assistant_uuid,
            now=now,
        )

    if is_stalled_response_text(poll.active_assistant_text):
        append_stalled_response_error(submission, poll, now=now)
        return

    maybe_append_turn_boundary(
        submission,
        poll,
        event_text=text,
        now=now,
    )
    maybe_append_assistant_end_turn_boundary(
        submission,
        poll,
        event=event,
        now=now,
    )


def assistant_text(event: dict[str, object]) -> str:
    return str(event.get("text") or "")


def assistant_identity(event: dict[str, object]) -> tuple[str, str, bool]:
    subagent_id = str(event.get("subagent_id") or "").strip()
    subagent_name = str(event.get("subagent_name") or "").strip()
    return subagent_id, subagent_name, bool(
        subagent_id or subagent_name or event.get("is_sidechain")
    )


def assistant_uuid(event: dict[str, object]) -> str:
    return str(event.get("uuid") or "").strip()


def assistant_message_id(event: dict[str, object]) -> str:
    message_id = str(event.get("message_id") or "").strip()
    if message_id:
        return message_id
    entry = event.get("entry")
    if not isinstance(entry, dict):
        return ""
    message = entry.get("message")
    if isinstance(message, dict):
        message_id = str(
            message.get("id")
            or message.get("messageId")
            or message.get("message_id")
            or ""
        ).strip()
        if message_id:
            return message_id
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return ""
    return str(
        payload.get("id")
        or payload.get("messageId")
        or payload.get("message_id")
        or ""
    ).strip()


def assistant_stop_reason(event: dict[str, object]) -> str:
    return str(event.get("stop_reason") or "").strip().lower()


def append_buffer(buffer: str, text: str) -> str:
    return f"{buffer}\n{text}".strip() if buffer else text


def cleaned_assistant_text(text: str, *, request_anchor: str) -> str:
    if not request_anchor:
        return text
    return strip_done_text(text, request_anchor)


def has_visible_text(text: str) -> bool:
    return bool(text.strip())


def update_top_level_assistant_message(
    poll: ClaudePollState,
    *,
    event: dict[str, object],
    cleaned: str,
    event_assistant_uuid: str,
) -> None:
    message_id = assistant_message_id(event) or event_assistant_uuid
    previous_message_id = poll.active_assistant_message_id
    previous_message_text = poll.active_assistant_text
    if message_id and message_id != poll.active_assistant_message_id:
        poll.active_assistant_message_id = message_id
        poll.active_assistant_text = ""
        poll.active_assistant_stop_reason = ""
        poll.active_assistant_has_tool_use = False
        previous_message_text = ""

    content = assistant_message_content(event)
    if has_visible_text(cleaned):
        # Claude transcript entries are message snapshots, not reply fragments.
        # Replacing the current message text prevents earlier tool narration
        # from becoming the terminal reply while still allowing a later
        # snapshot with the same message.id to complete the message.
        poll.active_assistant_text = cleaned
        poll.reply_buffer = append_progress_snapshot(
            poll.reply_buffer,
            previous_snapshot=previous_message_text,
            snapshot=cleaned,
            same_message=bool(message_id and message_id == previous_message_id),
        )
    elif content is not None:
        poll.active_assistant_text = ""

    stop_reason = assistant_stop_reason(event)
    if stop_reason:
        poll.active_assistant_stop_reason = stop_reason
    if content is not None:
        poll.active_assistant_has_tool_use = content_has_type(content, "tool_use")
    elif stop_reason:
        poll.active_assistant_has_tool_use = stop_reason == "tool_use"

    if event_assistant_uuid:
        poll.last_assistant_uuid = event_assistant_uuid


def append_progress_snapshot(
    buffer: str,
    *,
    previous_snapshot: str,
    snapshot: str,
    same_message: bool,
) -> str:
    """Append visible progress without treating it as terminal reply authority."""
    if not has_visible_text(snapshot) or snapshot == previous_snapshot:
        return buffer
    if (
        same_message
        and previous_snapshot
        and snapshot.startswith(previous_snapshot)
        and buffer.endswith(previous_snapshot)
    ):
        return f"{buffer}{snapshot[len(previous_snapshot):]}"
    return append_buffer(buffer, snapshot)


def append_chunk_item(
    submission: ProviderSubmission,
    poll: ClaudePollState,
    *,
    event: dict[str, object],
    cleaned: str,
    subagent_id: str,
    subagent_name: str,
    event_assistant_uuid: str,
    now: str,
) -> None:
    current_assistant_uuid = event_assistant_uuid or poll.last_assistant_uuid or None
    poll.items.append(
        build_item(
            submission,
            kind=CompletionItemKind.ASSISTANT_CHUNK,
            timestamp=now,
            seq=poll.next_seq,
            payload=chunk_payload(
                poll=poll,
                event=event,
                cleaned=cleaned,
                assistant_uuid=current_assistant_uuid,
                subagent_id=subagent_id,
                subagent_name=subagent_name,
            ),
        )
    )
    poll.next_seq += 1


def chunk_payload(
    *,
    poll: ClaudePollState,
    event: dict[str, object],
    cleaned: str,
    assistant_uuid: str | None,
    subagent_id: str,
    subagent_name: str,
) -> dict[str, object]:
    return {
        "text": cleaned,
        "merged_text": poll.reply_buffer,
        "turn_id": poll.request_anchor,
        "session_path": poll.session_path or None,
        "assistant_uuid": assistant_uuid,
        "assistant_message_id": assistant_message_id(event) or None,
        "subagent_id": subagent_id or None,
        "subagent_name": subagent_name or None,
        "stop_reason": event.get("stop_reason"),
    }


def maybe_append_turn_boundary(
    submission: ProviderSubmission,
    poll: ClaudePollState,
    *,
    event_text: str,
    now: str,
) -> None:
    if poll.reached_turn_boundary:
        return
    if not poll.request_anchor or not is_done_text(event_text, poll.request_anchor):
        return
    reply = (
        extract_reply_for_req(event_text, poll.request_anchor)
        or poll.active_assistant_text
    )
    if not has_visible_text(reply):
        return
    poll.items.append(
        build_item(
            submission,
            kind=CompletionItemKind.TURN_BOUNDARY,
            timestamp=now,
            seq=poll.next_seq,
            payload=turn_boundary_payload(poll=poll, reply=reply),
        )
    )
    poll.next_seq += 1
    poll.terminal_reply = reply
    poll.reached_turn_boundary = True


def maybe_append_assistant_end_turn_boundary(
    submission: ProviderSubmission,
    poll: ClaudePollState,
    *,
    event: dict[str, object],
    now: str,
) -> None:
    if poll.reached_turn_boundary:
        return
    if not poll.anchor_seen or not poll.request_anchor:
        return
    event_message_id = assistant_message_id(event) or assistant_uuid(event)
    if (
        event_message_id
        and poll.active_assistant_message_id
        and event_message_id != poll.active_assistant_message_id
    ):
        return
    stop_reason = poll.active_assistant_stop_reason
    if stop_reason != "end_turn" or poll.active_assistant_has_tool_use:
        return
    reply = poll.active_assistant_text
    if not has_visible_text(reply):
        return
    event_assistant_uuid = assistant_uuid(event)
    if event_assistant_uuid:
        poll.last_assistant_uuid = event_assistant_uuid
    poll.items.append(
        build_item(
            submission,
            kind=CompletionItemKind.TURN_BOUNDARY,
            timestamp=now,
            seq=poll.next_seq,
            payload=turn_boundary_payload(
                poll=poll,
                reply=reply,
                reason="assistant_end_turn",
                stop_reason=stop_reason,
            ),
        )
    )
    poll.next_seq += 1
    poll.terminal_reply = reply
    poll.reached_turn_boundary = True


def assistant_message_content(event: dict[str, object]) -> object | None:
    entry = event.get("entry")
    if not isinstance(entry, dict):
        return None
    message = entry.get("message")
    if isinstance(message, dict):
        role = str(message.get("role") or entry.get("type") or "").strip().lower()
        if role == "assistant":
            return message.get("content")
    payload = entry.get("payload")
    if isinstance(payload, dict):
        payload_type = str(payload.get("type") or "").strip().lower()
        payload_role = str(payload.get("role") or "").strip().lower()
        if payload_type == "message" and payload_role == "assistant":
            return payload.get("content")
    entry_type = str(entry.get("type") or "").strip().lower()
    if entry_type == "assistant":
        return entry.get("content")
    return None


def content_has_type(content: object, expected: str) -> bool:
    expected_type = expected.strip().lower()
    if not expected_type:
        return False
    if isinstance(content, dict):
        return str(content.get("type") or "").strip().lower() == expected_type
    if not isinstance(content, list):
        return False
    for item in content:
        if isinstance(item, dict) and str(item.get("type") or "").strip().lower() == expected_type:
            return True
    return False


def is_stalled_response_text(text: str) -> bool:
    normalized = str(text or "").strip()
    first_line = normalized.splitlines()[0].strip().casefold() if normalized else ""
    return first_line.startswith("api error: response stalled mid-stream")


def append_stalled_response_error(
    submission: ProviderSubmission,
    poll: ClaudePollState,
    *,
    now: str,
) -> None:
    poll.items.append(
        build_item(
            submission,
            kind=CompletionItemKind.ERROR,
            timestamp=now,
            seq=poll.next_seq,
            payload={
                "reason": "claude_response_stalled_mid_stream",
                "message": poll.active_assistant_text,
                "error_type": "provider_api_error",
                "response_incomplete": True,
                "turn_id": poll.request_anchor,
                "session_path": poll.session_path or None,
                "assistant_uuid": poll.last_assistant_uuid or None,
                "assistant_message_id": poll.active_assistant_message_id or None,
            },
        )
    )
    poll.next_seq += 1
    poll.reached_turn_boundary = True


def turn_boundary_payload(
    *,
    poll: ClaudePollState,
    reply: str,
    reason: str = "task_complete",
    stop_reason: str | None = None,
) -> dict[str, object]:
    payload = {
        "reason": reason,
        "last_agent_message": reply,
        "turn_id": poll.request_anchor,
        "session_path": poll.session_path or None,
        "assistant_uuid": poll.last_assistant_uuid or None,
        "assistant_message_id": poll.active_assistant_message_id or None,
    }
    if stop_reason:
        payload["stop_reason"] = stop_reason
    return payload


__all__ = ["handle_assistant_event"]
