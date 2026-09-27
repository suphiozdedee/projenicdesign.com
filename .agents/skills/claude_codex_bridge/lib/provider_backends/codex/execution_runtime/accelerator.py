from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from completion.models import CompletionItemKind
from provider_execution.base import ProviderPollResult, ProviderSubmission
from provider_execution.common import build_item, request_anchor_from_runtime_state
from runtime_accelerator import client as accelerator_client
from runtime_accelerator.config import (
    accelerator_socket_path,
    accelerator_timeout_s,
    codex_accelerator_enabled,
)

from .start import state_session_path
from .state_machine_runtime import CodexPollState, finalize_poll_result


@dataclass(frozen=True)
class AcceleratorPollOutcome:
    used: bool
    result: ProviderPollResult | None


def poll_with_accelerator(submission: ProviderSubmission, *, now: str) -> AcceleratorPollOutcome | None:
    if not codex_accelerator_enabled():
        return None
    descriptor = codex_job_descriptor(submission)
    socket_path = socket_path_from_submission(submission)
    if descriptor is None or socket_path is None:
        return None
    try:
        response = accelerator_client.call(
            socket_path,
            "codex_observe",
            {"jobs": [descriptor]},
            timeout_s=accelerator_timeout_s(),
        )
    except accelerator_client.AcceleratorError:
        return None
    result = poll_result_from_response(submission, response, now=now)
    if result is _FALLBACK:
        return None
    return AcceleratorPollOutcome(used=True, result=result)


def socket_path_from_submission(submission: ProviderSubmission):
    return accelerator_socket_path(project_root_from_submission(submission))


def project_root_from_submission(submission: ProviderSubmission) -> str:
    runtime_state = submission.runtime_state
    raw = runtime_state.get("workspace_path")
    if not raw and isinstance(submission.diagnostics, dict):
        raw = submission.diagnostics.get("workspace_path")
    return str(raw or "").strip()


def codex_job_descriptor(submission: ProviderSubmission) -> dict[str, Any] | None:
    runtime_state = submission.runtime_state
    reader_state = runtime_state.get("state") if isinstance(runtime_state.get("state"), dict) else {}
    session_path = str(state_session_path(reader_state) or runtime_state.get("session_path") or "").strip()
    if not session_path:
        return None
    return {
        "job_id": submission.job_id,
        "session_path": session_path,
        "request_anchor": request_anchor_from_runtime_state(runtime_state, fallback=submission.job_id),
        "state": {
            "offset": int_value(reader_state.get("offset"), 0),
            "next_seq": int_value(runtime_state.get("next_seq"), 1),
            "anchor_seen": bool_value(runtime_state.get("anchor_seen"), False),
            "bound_turn_id": str(runtime_state.get("bound_turn_id") or ""),
            "bound_task_id": str(runtime_state.get("bound_task_id") or ""),
            "reply_buffer": str(runtime_state.get("reply_buffer") or ""),
            "last_agent_message": str(runtime_state.get("last_agent_message") or ""),
            "last_final_answer": str(runtime_state.get("last_final_answer") or ""),
            "last_assistant_message": str(runtime_state.get("last_assistant_message") or ""),
            "last_assistant_signature": str(runtime_state.get("last_assistant_signature") or ""),
            "session_path": session_path,
        },
    }


_FALLBACK = object()


def poll_result_from_response(
    submission: ProviderSubmission,
    response: dict[str, Any],
    *,
    now: str,
) -> ProviderPollResult | None | object:
    observation = matching_observation(response, submission.job_id)
    if observation is None or str(observation.get("error") or "").strip():
        return _FALLBACK
    state = observation.get("state")
    if not isinstance(state, dict):
        return _FALLBACK
    session_path = str(observation.get("session_path") or state.get("session_path") or "").strip()
    reader_state = accelerator_reader_state(submission, state, session_path=session_path)
    poll = CodexPollState(
        request_anchor=request_anchor_from_runtime_state(submission.runtime_state, fallback=submission.job_id),
        next_seq=int_value(state.get("next_seq"), int_value(submission.runtime_state.get("next_seq"), 1)),
        anchor_seen=bool_value(state.get("anchor_seen"), False),
        bound_turn_id=str(state.get("bound_turn_id") or ""),
        bound_task_id=str(state.get("bound_task_id") or ""),
        reply_buffer=str(state.get("reply_buffer") or ""),
        last_agent_message=str(state.get("last_agent_message") or ""),
        last_final_answer=str(state.get("last_final_answer") or ""),
        last_assistant_message=str(state.get("last_assistant_message") or ""),
        last_assistant_signature=str(state.get("last_assistant_signature") or ""),
        session_path=session_path,
        reached_terminal=bool_value(observation.get("reached_terminal"), False),
    )
    raw_items = observation.get("items")
    if not isinstance(raw_items, list):
        return _FALLBACK
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            return _FALLBACK
        try:
            kind = CompletionItemKind(str(raw_item.get("kind") or ""))
        except ValueError:
            return _FALLBACK
        payload = raw_item.get("payload")
        poll.items.append(
            build_item(
                submission,
                kind=kind,
                timestamp=now,
                seq=int_value(raw_item.get("seq"), poll.next_seq),
                payload=payload if isinstance(payload, dict) else {},
                cursor_kwargs={
                    "session_path": session_path or None,
                    "offset": int_value(state.get("offset"), 0),
                },
            )
        )
    return finalize_poll_result(submission, poll, state=reader_state, now=now)


def matching_observation(response: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    observations = response.get("observations")
    if not isinstance(observations, list):
        return None
    for observation in observations:
        if isinstance(observation, dict) and str(observation.get("job_id") or "") == job_id:
            return observation
    return None


def accelerator_reader_state(
    submission: ProviderSubmission,
    state: dict[str, Any],
    *,
    session_path: str,
) -> dict[str, object]:
    prior = submission.runtime_state.get("state")
    reader_state = dict(prior) if isinstance(prior, dict) else {}
    reader_state["offset"] = int_value(state.get("offset"), int_value(reader_state.get("offset"), 0))
    if session_path:
        reader_state["log_path"] = session_path
    return reader_state


def int_value(value: object, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def bool_value(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


__all__ = [
    "AcceleratorPollOutcome",
    "codex_accelerator_enabled",
    "codex_job_descriptor",
    "poll_with_accelerator",
]
