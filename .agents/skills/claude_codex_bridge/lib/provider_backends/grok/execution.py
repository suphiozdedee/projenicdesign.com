from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from provider_backends.native_cli_support import (
    NativeCliExecutionConfig,
    NativeCliExecutionRequest,
    NativeCliObservation,
    NativeCliSubprocessAdapter,
    observe_jsonl_output,
)
from provider_core.runtime_shared import provider_start_parts
from provider_core.caller_env import caller_context_env
from provider_profiles import load_resolved_provider_profile

from .home import materialize_grok_home
from .skills import grok_ccb_skills_ready, grok_skill_permission_args


def build_execution_adapter():
    from .pane_execution import GrokPaneExecutionAdapter

    return GrokPaneExecutionAdapter()


def build_headless_execution_adapter() -> NativeCliSubprocessAdapter:
    return NativeCliSubprocessAdapter(
        NativeCliExecutionConfig(
            provider="grok",
            session_filename=".grok-session",
            command_builder=_build_command,
            env_builder=_build_env,
            observer=observe_grok_output,
            output_kind="jsonl",
            mode="grok_run",
            start_failed_reason="grok_run_start_failed",
            failed_reason="grok_run_failed",
            empty_reason="grok_empty_reply",
            run_error_reason="grok_run_error",
            complete_reason="grok_run_stop",
            missing_terminal_reason="grok_native_terminal_missing",
            timeout_reason="grok_run_timeout",
            terminal_on_process_exit=False,
        )
    )


def _build_command(request: NativeCliExecutionRequest) -> list[str]:
    grok_home = _materialize_request_home(request)
    skill_permissions_enabled = bool(request.session_data.get('grok_skill_permissions_enabled'))
    cmd = [
        *provider_start_parts("grok"),
        "--no-auto-update",
        *(
            grok_skill_permission_args()
            if skill_permissions_enabled and grok_ccb_skills_ready(grok_home)
            else ()
        ),
        "-p",
        request.prompt,
        "--cwd",
        str(request.work_dir),
        "--output-format",
        "streaming-json",
        "--session-id",
        _grok_session_id_for_job(request.job.job_id),
    ]
    model = _setting(request, "grok_model", "CCB_GROK_MODEL")
    if model:
        cmd.extend(["-m", model])
    effort = _setting(request, "grok_effort", "CCB_GROK_EFFORT")
    if effort:
        cmd.extend(["--reasoning-effort", effort])
    return cmd


def _build_env(request: NativeCliExecutionRequest) -> dict[str, str]:
    grok_home = _materialize_request_home(request)
    env = {"HOME": str(grok_home)}
    runtime_dir = _path_from_text(request.session_data.get("runtime_dir"))
    actor = str(request.session_data.get('agent_name') or getattr(request.job, 'agent_name', '') or '').strip()
    launch_session_id = str(
        request.session_data.get('ccb_session_id')
        or request.session_data.get('grok_session_id')
        or ''
    ).strip()
    if runtime_dir is not None and actor and launch_session_id:
        env.update(
            caller_context_env(
                actor=actor,
                runtime_dir=runtime_dir,
                launch_session_id=launch_session_id,
            )
        )
    return env


def _materialize_request_home(request: NativeCliExecutionRequest) -> Path:
    grok_home = _state_path(request, "grok_home", fallback="home")
    runtime_dir = _path_from_text(request.session_data.get("runtime_dir"))
    materialize_grok_home(
        grok_home,
        profile=load_resolved_provider_profile(runtime_dir) if runtime_dir is not None else None,
    )
    return grok_home


def _grok_session_id_for_job(job_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ccb:grok:{job_id}"))


def _setting(request: NativeCliExecutionRequest, session_key: str, env_key: str) -> str | None:
    value = str(request.session_data.get(session_key) or "").strip()
    if value:
        return value
    return str(os.environ.get(env_key) or "").strip() or None


def observe_grok_output(path: Path) -> NativeCliObservation:
    if not path or not path.is_file():
        return NativeCliObservation()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return NativeCliObservation(error=f"read_stdout_failed:{exc}")
    aggregate = _observe_grok_aggregate(raw)
    if aggregate is not None:
        return aggregate
    lines = raw.splitlines()

    chunk_text: list[str] = []
    final_text = ""
    finished = False
    finish_reason = ""
    turn_ref: str | None = None
    completed_at: object | None = None
    error = ""
    intermediate = False
    saw_grok_shape = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        method = _string(event.get("method")).strip()
        event_type = _event_type(event)
        if method or event_type:
            saw_grok_shape = True
        if isinstance(event.get("error"), dict):
            error = _text_from(event["error"]) or "grok_error"
            continue
        if event_type in {"error", "failed", "failure", "unauthorized", "auth_failed"}:
            error = _text_from(event) or event_type
            continue
        if event_type == "thought":
            intermediate = True
            continue

        update = _grok_update(event)
        update_kind = _update_kind(update)
        if update_kind:
            saw_grok_shape = True
        if _is_error_update(update, update_kind):
            error = _text_from(update) or update_kind or "grok_error"
            continue
        if _is_tool_update(update, update_kind):
            intermediate = True

        text = _grok_text(event, update)
        if text:
            if _is_final_update(update, update_kind):
                final_text = text
            elif update_kind in {"agent_message", "assistant_message"} and chunk_text and text == "".join(chunk_text):
                final_text = text
            else:
                chunk_text.append(text)
            turn_ref = turn_ref or _event_ref(event) or _event_ref(update)
            completed_at = completed_at or _event_time(event) or _event_time(update)

        reason = _normalize_grok_reason(_finish_reason(event) or _finish_reason(update))
        if reason:
            finish_reason = reason
        if _is_final_update(update, update_kind) or _is_final_event(event, method):
            finished = True
            turn_ref = turn_ref or _event_ref(event) or _event_ref(update)
            completed_at = completed_at or _event_time(event) or _event_time(update)

    generic = observe_jsonl_output(path)
    if not saw_grok_shape:
        return generic

    text = (final_text or "".join(chunk_text) or generic.text).strip()
    return NativeCliObservation(
        text=text,
        finished=finished or generic.finished,
        finish_reason=finish_reason or generic.finish_reason,
        turn_ref=turn_ref or generic.turn_ref,
        completed_at=completed_at or generic.completed_at,
        error=error or generic.error,
        intermediate=intermediate or generic.intermediate,
    )


def _observe_grok_aggregate(raw: str) -> NativeCliObservation | None:
    stripped = str(raw or "").strip()
    if not stripped:
        return NativeCliObservation()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not _looks_like_grok_aggregate(payload):
        return None

    text = _text_from(payload.get("text")).strip()
    finish_reason = _normalize_grok_reason(_finish_reason(payload))
    turn_ref = _event_ref(payload)
    error = ""
    error_field = payload.get("error")
    if error_field:
        error = _text_from(error_field) or "grok_error"

    return NativeCliObservation(
        text=text,
        finished=bool(finish_reason),
        finish_reason=finish_reason,
        turn_ref=turn_ref,
        completed_at=_event_time(payload),
        error=error,
    )


def _looks_like_grok_aggregate(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("text", "stopReason", "sessionId", "requestId", "thought"))


def _state_path(request: NativeCliExecutionRequest, key: str, *, fallback: str) -> Path:
    raw = str(request.session_data.get(key) or "").strip()
    if raw:
        return Path(raw).expanduser()
    state_dir = Path(str(request.session_data.get("grok_state_dir") or request.work_dir / ".ccb" / "grok")).expanduser()
    return state_dir / fallback


def _path_from_text(value: object) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser()
    except Exception:
        return None


def _grok_update(event: dict[str, Any]) -> Any:
    params = event.get("params")
    if isinstance(params, dict) and isinstance(params.get("update"), dict):
        return params["update"]
    update = event.get("update")
    if isinstance(update, dict):
        return update
    return {}


def _update_kind(update: Any) -> str:
    if not isinstance(update, dict):
        return ""
    return _string(
        update.get("sessionUpdate")
        or update.get("type")
        or update.get("kind")
        or update.get("event")
        or update.get("name")
    ).strip().lower().replace("-", "_")


def _grok_text(event: dict[str, Any], update: Any) -> str:
    if isinstance(update, dict):
        for key in ("content", "message", "delta", "part", "result", "data", "payload"):
            text = _text_from(update.get(key))
            if text:
                return text
        text = _text_from(update)
        if text:
            return text
    event_type = _event_type(event)
    if event_type in {"text", "assistant_message", "agent_message", "message_delta", "content_delta"}:
        text = _text_from(event.get("data"))
        if text:
            return text
    return _text_from(event.get("content") or event.get("message") or event.get("delta") or event.get("result"))


def _text_from(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text_from(item) for item in value)
    if not isinstance(value, dict):
        return ""
    for key in ("text", "merged_text", "final_answer", "answer", "reply", "output", "response", "message"):
        text = _text_from(value.get(key))
        if text:
            return text
    for key in ("content", "delta", "part", "payload", "data", "result", "error"):
        text = _text_from(value.get(key))
        if text:
            return text
    return ""


def _finish_reason(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("stopReason", "stop_reason", "finish_reason", "reason", "status", "state"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    for key in ("params", "update", "payload", "data", "result", "message"):
        reason = _finish_reason(value.get(key))
        if reason:
            return reason
    return ""


def _event_ref(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("requestId", "id", "sessionId", "session_id", "messageId", "message_id", "turnId", "turn_id", "request_id"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    for key in ("params", "update", "payload", "data", "result", "message"):
        ref = _event_ref(value.get(key))
        if ref:
            return ref
    return None


def _event_time(value: Any) -> object | None:
    if not isinstance(value, dict):
        return None
    for key in ("completed_at", "timestamp", "time", "created_at", "updated_at"):
        raw = value.get(key)
        if raw:
            return raw
    for key in ("params", "update", "payload", "data", "result", "message"):
        found = _event_time(value.get(key))
        if found:
            return found
    return None


def _is_tool_update(update: Any, update_kind: str) -> bool:
    if not isinstance(update, dict):
        return False
    haystack = " ".join(
        item
        for item in (
            update_kind,
            _string(update.get("role")).strip().lower().replace("-", "_"),
            _string(update.get("status")).strip().lower().replace("-", "_"),
            _string(update.get("state")).strip().lower().replace("-", "_"),
        )
        if item
    )
    return "tool" in haystack or "permission" in haystack or "function_call" in haystack


def _is_error_update(update: Any, update_kind: str) -> bool:
    if not isinstance(update, dict):
        return False
    haystack = " ".join(
        item
        for item in (
            update_kind,
            _normalize_grok_reason(_finish_reason(update)),
        )
        if item
    )
    return any(token in haystack for token in ("error", "failed", "failure", "unauthorized", "auth_failed"))


def _is_final_update(update: Any, update_kind: str) -> bool:
    if not isinstance(update, dict):
        return False
    haystack = " ".join(
        item
        for item in (
            update_kind,
            _normalize_grok_reason(_finish_reason(update)),
        )
        if item
    )
    return any(token in haystack for token in ("final", "result", "completion", "completed", "done", "finished", "turn_end", "end_turn", "stop"))


def _is_final_event(event: dict[str, Any], method: str) -> bool:
    if _event_type(event) == "end":
        return True
    haystack = " ".join(
        item
        for item in (
            method.strip().lower().replace("-", "_").replace("/", "_"),
            _event_type(event),
            _normalize_grok_reason(_finish_reason(event)),
        )
        if item
    )
    return any(token in haystack for token in ("final", "result", "completion", "completed", "done", "finished", "turn_end", "end_turn", "stop"))


def _event_type(event: dict[str, Any]) -> str:
    return _string(event.get("type")).strip().lower().replace("-", "_")


def _normalize_grok_reason(reason: str) -> str:
    normalized = str(reason or "").strip().lower().replace("-", "_")
    if normalized == "endturn":
        return "end_turn"
    return normalized


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


__all__ = ["build_execution_adapter", "build_headless_execution_adapter", "observe_grok_output"]
