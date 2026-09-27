from __future__ import annotations

from provider_core.protocol import REQ_ID_PREFIX

from .models import CodexPollState


def update_binding_refs(poll: CodexPollState, entry: dict[str, object]) -> None:
    entry_turn_id = str(entry.get("turn_id") or "").strip()
    pending_turn_candidate = not poll.anchor_seen and (_is_task_started(entry) or _is_current_anchor(entry, poll))
    if entry_turn_id and (not poll.bound_turn_id or pending_turn_candidate):
        poll.bound_turn_id = entry_turn_id
    entry_task_id = str(entry.get("task_id") or "").strip()
    if entry_task_id and (not poll.bound_task_id or pending_turn_candidate):
        poll.bound_task_id = entry_task_id


def _is_task_started(entry: dict[str, object]) -> bool:
    payload_type = str(entry.get("payload_type") or entry.get("entry_type") or "").strip().lower()
    return payload_type == "task_started"


def _is_current_anchor(entry: dict[str, object], poll: CodexPollState) -> bool:
    if str(entry.get("role") or "").strip().lower() != "user" or not poll.request_anchor:
        return False
    return f"{REQ_ID_PREFIX} {poll.request_anchor}" in str(entry.get("text") or "")


def entry_matches_bound_turn(poll: CodexPollState, entry: dict[str, object]) -> bool:
    entry_turn_id = str(entry.get("turn_id") or "").strip()
    return not entry_turn_id or not poll.bound_turn_id or entry_turn_id == poll.bound_turn_id


__all__ = ["entry_matches_bound_turn", "update_binding_refs"]
