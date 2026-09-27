from __future__ import annotations

from pathlib import Path

from provider_backends.native_cli_support import (
    NativeCliExecutionConfig,
    NativeCliExecutionRequest,
    NativeCliSubprocessAdapter,
    observe_stdout_output,
)
from provider_core.runtime_shared import provider_start_parts


def build_execution_adapter() -> NativeCliSubprocessAdapter:
    return NativeCliSubprocessAdapter(
        NativeCliExecutionConfig(
            provider="crush",
            session_filename=".crush-session",
            command_builder=_build_command,
            observer=observe_stdout_output,
            output_kind="stdout",
            mode="crush_run",
            start_failed_reason="crush_run_start_failed",
            failed_reason="crush_run_failed",
            empty_reason="crush_empty_reply",
            run_error_reason="crush_run_error",
            complete_reason="crush_run_stop",
            process_exit_complete_reason="crush_run_exit",
            timeout_reason="crush_run_timeout",
        )
    )


def _build_command(request: NativeCliExecutionRequest) -> list[str]:
    data_dir = _state_path(request, "crush_data_dir", fallback="data")
    data_dir.mkdir(parents=True, exist_ok=True)
    return [
        *provider_start_parts("crush"),
        "--data-dir",
        str(data_dir),
        "--cwd",
        str(request.work_dir),
        "run",
        "--quiet",
        request.prompt,
    ]


def _state_path(request: NativeCliExecutionRequest, key: str, *, fallback: str) -> Path:
    raw = str(request.session_data.get(key) or "").strip()
    if raw:
        return Path(raw).expanduser()
    state_dir = Path(str(request.session_data.get("crush_state_dir") or request.work_dir / ".ccb" / "crush")).expanduser()
    return state_dir / fallback


__all__ = ["build_execution_adapter"]
