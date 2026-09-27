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
            provider="kiro",
            session_filename=".kiro-session",
            command_builder=_build_command,
            env_builder=_build_env,
            private_path_env_names=("KIRO_HOME",),
            observer=observe_stdout_output,
            output_kind="stdout",
            mode="kiro_run",
            start_failed_reason="kiro_run_start_failed",
            failed_reason="kiro_run_failed",
            empty_reason="kiro_empty_reply",
            run_error_reason="kiro_run_error",
            complete_reason="kiro_run_stop",
            process_exit_complete_reason="kiro_run_exit",
            timeout_reason="kiro_run_timeout",
        )
    )


def _build_command(request: NativeCliExecutionRequest) -> list[str]:
    return [
        *provider_start_parts("kiro"),
        "chat",
        "--no-interactive",
        "--wrap",
        "never",
        request.prompt,
    ]


def _build_env(request: NativeCliExecutionRequest) -> dict[str, str]:
    kiro_home = _state_path(request, "kiro_home", fallback="home")
    kiro_home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(kiro_home),
        "KIRO_HOME": str(kiro_home / ".kiro"),
    }


def _state_path(request: NativeCliExecutionRequest, key: str, *, fallback: str) -> Path:
    raw = str(request.session_data.get(key) or "").strip()
    if raw:
        return Path(raw).expanduser()
    state_dir = Path(str(request.session_data.get("kiro_state_dir") or request.work_dir / ".ccb" / "kiro")).expanduser()
    return state_dir / fallback


__all__ = ["build_execution_adapter"]
