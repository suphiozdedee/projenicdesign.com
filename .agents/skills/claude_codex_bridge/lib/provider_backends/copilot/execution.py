from __future__ import annotations

from pathlib import Path

from provider_backends.native_cli_support import (
    NativeCliExecutionConfig,
    NativeCliExecutionRequest,
    NativeCliSubprocessAdapter,
)
from provider_core.runtime_shared import provider_start_parts


def build_execution_adapter() -> NativeCliSubprocessAdapter:
    return NativeCliSubprocessAdapter(
        NativeCliExecutionConfig(
            provider="copilot",
            session_filename=".copilot-session",
            command_builder=_build_command,
            env_builder=_build_env,
            private_path_env_names=("COPILOT_HOME", "COPILOT_CACHE_HOME"),
            private_raw_env_names=("COPILOT_DISABLE_KEYTAR",),
            output_kind="jsonl",
            mode="copilot_run",
            start_failed_reason="copilot_run_start_failed",
            failed_reason="copilot_run_failed",
            empty_reason="copilot_empty_reply",
            run_error_reason="copilot_run_error",
            complete_reason="copilot_run_stop",
            process_exit_complete_reason="copilot_run_exit",
            timeout_reason="copilot_run_timeout",
        )
    )


def _build_command(request: NativeCliExecutionRequest) -> list[str]:
    return [
        *provider_start_parts("copilot"),
        "-C",
        str(request.work_dir),
        "-p",
        request.prompt,
        "--output-format",
        "json",
        "--session-id",
        request.job.job_id,
    ]


def _build_env(request: NativeCliExecutionRequest) -> dict[str, str]:
    copilot_home = _state_path(request, "copilot_home", fallback="home")
    copilot_data = _state_path(request, "copilot_data_dir", fallback="data")
    copilot_cache = copilot_data / 'cache'
    copilot_home.mkdir(parents=True, exist_ok=True)
    copilot_cache.mkdir(parents=True, exist_ok=True)
    return {
        "COPILOT_HOME": str(copilot_home),
        "COPILOT_CACHE_HOME": str(copilot_cache),
        "COPILOT_DISABLE_KEYTAR": "1",
    }


def _state_path(request: NativeCliExecutionRequest, key: str, *, fallback: str) -> Path:
    raw = str(request.session_data.get(key) or "").strip()
    if raw:
        return Path(raw).expanduser()
    state_dir = Path(str(request.session_data.get("copilot_state_dir") or request.work_dir / ".ccb" / "copilot")).expanduser()
    return state_dir / fallback


__all__ = ["build_execution_adapter"]
