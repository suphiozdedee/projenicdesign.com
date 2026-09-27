from __future__ import annotations

import json
from pathlib import Path
import uuid
from typing import Any

from provider_backends.native_cli_support import (
    NativeCliExecutionConfig,
    NativeCliExecutionRequest,
    NativeCliObservation,
    NativeCliSubprocessAdapter,
)
from provider_core.runtime_shared import provider_start_parts


_NORMAL_STOP_REASONS = {"completed", "end_turn", "stop", "stop_sequence", "success"}
_PERMISSION_OPTIONS = {"--dangerously-skip-permissions", "--permission-mode", "--yolo"}


def build_execution_adapter() -> NativeCliSubprocessAdapter:
    return NativeCliSubprocessAdapter(
        NativeCliExecutionConfig(
            provider="qoder",
            session_filename=".qoder-session",
            command_builder=_build_command,
            observer=observe_qoder_output,
            output_kind="jsonl",
            mode="qoder_run",
            start_failed_reason="qoder_run_start_failed",
            failed_reason="qoder_run_failed",
            empty_reason="qoder_empty_reply",
            run_error_reason="qoder_run_error",
            complete_reason="qoder_run_stop",
            process_exit_complete_reason="qoder_run_exit",
            missing_terminal_reason="qoder_native_terminal_missing",
            timeout_reason="qoder_run_timeout",
            terminal_on_process_exit=False,
        )
    )


def _build_command(request: NativeCliExecutionRequest) -> list[str]:
    return _build_qoder_command(request, provider="qoder")


def _build_qoder_command(
    request: NativeCliExecutionRequest,
    *,
    provider: str,
) -> list[str]:
    base = provider_start_parts(provider)
    command = [*base]
    if not _has_option(base, "--config-dir"):
        command.extend(["--config-dir", str(_qoder_config_dir(request, provider=provider))])
    if not any(_has_option(base, option) for option in _PERMISSION_OPTIONS):
        permission_mode = str(
            request.session_data.get(f"{provider}_headless_permission_mode") or "dont_ask"
        ).strip()
        if permission_mode not in {
            "accept_edits",
            "auto",
            "bypass_permissions",
            "default",
            "dont_ask",
            "plan",
        }:
            permission_mode = "dont_ask"
        command.extend(["--permission-mode", permission_mode])
    command.extend(
        [
            "-w",
            str(request.work_dir),
            "-p",
            "--output-format",
            "stream-json",
            "--session-id",
            _qoder_session_id_for_job(request.job.job_id, provider=provider),
            request.prompt,
        ]
    )
    return command


def _qoder_config_dir(
    request: NativeCliExecutionRequest,
    *,
    provider: str = "qoder",
) -> Path:
    raw = str(
        request.session_data.get(f"{provider}_config_dir")
        or request.session_data.get(f"{provider}_home")
        or ""
    ).strip()
    if raw:
        path = Path(raw).expanduser()
    else:
        state_dir = Path(
            str(
                request.session_data.get(f"{provider}_state_dir")
                or request.work_dir / ".ccb" / provider
            )
        ).expanduser()
        path = state_dir / "home"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _qoder_session_id_for_job(job_id: str, *, provider: str = "qoder") -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ccb:{provider}:{job_id}"))


def observe_qoder_output(path: Path) -> NativeCliObservation:
    return _observe_qoder_output(
        path,
        result_error="qoder_result_error",
        assistant_error_terminal=True,
    )


def _observe_qoder_output(
    path: Path,
    *,
    result_error: str,
    assistant_error_terminal: bool = False,
    require_explicit_success: bool = False,
    require_stop_reason: bool = False,
) -> NativeCliObservation:
    if not path or not path.is_file():
        return NativeCliObservation()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return NativeCliObservation(error=f"read_stdout_failed:{exc}")

    assistant_text = ""
    result_text = ""
    finished = False
    finish_reason = ""
    turn_ref: str | None = None
    error = ""
    assistant_error = ""
    intermediate = False

    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").strip().lower()
        turn_ref = turn_ref or _text_value(event.get("session_id"))

        if event_type == "system":
            intermediate = True
            continue
        if event_type == "assistant":
            event_error = _text_value(event.get("error"))
            text = _message_text(event.get("message"))
            if event_error:
                assistant_error = text or event_error
                if assistant_error_terminal:
                    error = assistant_error
                elif text:
                    assistant_text = text
                continue
            if text:
                assistant_text = text
            continue
        if event_type != "result":
            continue

        finished = True
        native_reason = _text_value(event.get("stop_reason")) or _text_value(
            event.get("subtype")
        )
        is_error = event.get("is_error")
        if bool(is_error):
            error = (
                _text_value(event.get("result"))
                or assistant_error
                or native_reason
                or result_error
            )
            result_text = ""
            assistant_text = ""
            continue
        if require_explicit_success and is_error is not False:
            finish_reason = "missing_result_status"
            continue
        result_text = _text_value(event.get("result"))
        normalized_reason = native_reason.strip().lower().replace("-", "_")
        finish_reason = (
            "completed" if normalized_reason in _NORMAL_STOP_REASONS else normalized_reason
        )
        if not finish_reason and require_stop_reason:
            finish_reason = "missing_stop_reason"
        elif not finish_reason:
            finish_reason = "completed"

    return NativeCliObservation(
        text=result_text or assistant_text,
        finished=finished,
        finish_reason=finish_reason,
        turn_ref=turn_ref,
        error=error,
        intermediate=intermediate,
    )


def _message_text(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return _content_text(value.get("content"))


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_content_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    if str(value.get("type") or "").strip().lower() in {"text", "output_text"}:
        return _text_value(value.get("text"))
    for key in ("text", "content", "message"):
        text = _content_text(value.get(key))
        if text:
            return text
    return ""


def _text_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _has_option(parts: list[str], option: str) -> bool:
    return any(part == option or part.startswith(f"{option}=") for part in parts)


__all__ = [
    "build_execution_adapter",
    "observe_qoder_output",
]
