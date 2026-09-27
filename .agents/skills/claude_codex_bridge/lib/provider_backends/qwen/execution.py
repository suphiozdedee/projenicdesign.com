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
            provider="qwen",
            session_filename=".qwen-session",
            command_builder=_build_command,
            env_builder=_build_env,
            private_path_env_names=("QWEN_HOME",),
            private_raw_env_names=(
                "QWEN_CODE_FORCE_FILE_STORAGE",
                "QWEN_CODE_FORCE_ENCRYPTED_FILE_STORAGE",
            ),
            output_kind="jsonl",
            mode="qwen_run",
            start_failed_reason="qwen_run_start_failed",
            failed_reason="qwen_run_failed",
            empty_reason="qwen_empty_reply",
            run_error_reason="qwen_run_error",
            complete_reason="qwen_run_stop",
            process_exit_complete_reason="qwen_run_exit",
            timeout_reason="qwen_run_timeout",
        )
    )


def _build_command(request: NativeCliExecutionRequest) -> list[str]:
    return [
        *provider_start_parts("qwen"),
        "--bare",
        "--output-format",
        "stream-json",
        "--session-id",
        request.job.job_id,
        request.prompt,
    ]


def _build_env(request: NativeCliExecutionRequest) -> dict[str, str]:
    qwen_home = _state_path(request, "qwen_home", fallback="home")
    qwen_home.mkdir(parents=True, exist_ok=True)
    return {
        "QWEN_HOME": str(qwen_home),
        "QWEN_CODE_FORCE_FILE_STORAGE": "true",
        "QWEN_CODE_FORCE_ENCRYPTED_FILE_STORAGE": "true",
    }


def _state_path(request: NativeCliExecutionRequest, key: str, *, fallback: str) -> Path:
    raw = str(request.session_data.get(key) or "").strip()
    if raw:
        return Path(raw).expanduser()
    state_dir = Path(str(request.session_data.get("qwen_state_dir") or request.work_dir / ".ccb" / "qwen")).expanduser()
    return state_dir / fallback


__all__ = ["build_execution_adapter"]
