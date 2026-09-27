from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provider_backends.native_cli_support import (
    NativeCliExecutionConfig,
    NativeCliExecutionRequest,
    NativeCliObservation,
    NativeCliSubprocessAdapter,
)
from provider_core.runtime_shared import provider_start_parts


def build_execution_adapter() -> NativeCliSubprocessAdapter:
    return NativeCliSubprocessAdapter(
        NativeCliExecutionConfig(
            provider="zai",
            session_filename=".zai-session",
            command_builder=_build_command,
            env_builder=_build_env,
            observer=observe_zai_output,
            output_kind="stdout",
            mode="zai_run",
            start_failed_reason="zai_run_start_failed",
            failed_reason="zai_run_failed",
            empty_reason="zai_empty_reply",
            run_error_reason="zai_run_error",
            complete_reason="zai_run_stop",
            process_exit_complete_reason="zai_run_exit",
            timeout_reason="zai_run_timeout",
        )
    )


def observe_zai_output(path: Path) -> NativeCliObservation:
    if not path or not path.is_file():
        return NativeCliObservation()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return NativeCliObservation(error=f"read_stdout_failed:{exc}")

    assistant_chunks: list[str] = []
    raw_chunks: list[str] = []
    turn_ref: str | None = None
    completed_at: object | None = None
    error = ""
    saw_json = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            raw_chunks.append(stripped)
            continue
        if not isinstance(event, dict):
            continue
        saw_json = True
        role = _nested_text(event, ("role", "sender", "author")).strip().lower()
        if role == "user":
            continue
        event_type = _nested_text(event, ("type", "event", "kind", "name")).strip().lower()
        if role in {"error", "system_error"} or "error" in event_type:
            error = _content_text(event) or event_type or "zai_error"
            continue
        if role in {"assistant", "agent", "model"} or "assistant" in event_type:
            text = _content_text(event)
            if _is_progress_text(text):
                continue
            if text:
                assistant_chunks.append(text)
                turn_ref = turn_ref or _event_ref(event)
                completed_at = completed_at or _event_time(event)

    text = "".join(assistant_chunks).strip()
    if not text and not saw_json:
        text = "\n".join(raw_chunks).strip()
    return NativeCliObservation(
        text=text,
        turn_ref=turn_ref,
        completed_at=completed_at,
        error=error,
    )


def _is_progress_text(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().split()).lower()
    if not normalized:
        return False
    return normalized in {
        "using tools to help you...",
        "thinking...",
    }


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_content_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    for key in ("content", "text", "reply", "answer", "output", "response", "message", "data", "payload"):
        nested = value.get(key)
        text = _content_text(nested)
        if text:
            return text
    return ""


def _nested_text(value: Any, keys: tuple[str, ...]) -> str:
    if isinstance(value, list):
        for item in value:
            text = _nested_text(item, keys)
            if text:
                return text
        return ""
    if not isinstance(value, dict):
        return ""
    for key in keys:
        nested = value.get(key)
        if isinstance(nested, str) and nested:
            return nested
    for key in ("message", "payload", "data", "result"):
        text = _nested_text(value.get(key), keys)
        if text:
            return text
    return ""


def _event_ref(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("id", "message_id", "session_id", "turn_id", "request_id"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    for key in ("message", "payload", "data", "result"):
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
    for key in ("message", "payload", "data", "result"):
        found = _event_time(value.get(key))
        if found:
            return found
    return None


def _build_command(request: NativeCliExecutionRequest) -> list[str]:
    return [
        *provider_start_parts("zai"),
        "--directory",
        str(request.work_dir),
        "--no-color",
        "--prompt",
        request.prompt,
    ]


def _build_env(request: NativeCliExecutionRequest) -> dict[str, str]:
    zai_home = _state_path(request, "zai_home", fallback="home")
    zai_home.mkdir(parents=True, exist_ok=True)
    return {"HOME": str(zai_home)}


def _state_path(request: NativeCliExecutionRequest, key: str, *, fallback: str) -> Path:
    raw = str(request.session_data.get(key) or "").strip()
    if raw:
        return Path(raw).expanduser()
    state_dir = Path(str(request.session_data.get("zai_state_dir") or request.work_dir / ".ccb" / "zai")).expanduser()
    return state_dir / fallback


__all__ = ["build_execution_adapter", "observe_zai_output"]
