from __future__ import annotations

from pathlib import Path

from provider_backends.native_cli_support import NativeCliLaunchConfig, build_native_cli_runtime_launcher
from provider_core.contracts import ProviderRuntimeLauncher


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return build_native_cli_runtime_launcher(
        NativeCliLaunchConfig(
            provider="zai",
            home_env="HOME",
            visible_args_builder=_zai_visible_args,
        )
    )


def _zai_visible_args(prepared_state: dict[str, object]) -> tuple[str, ...]:
    workspace = _path_from_prepared(prepared_state, "workspace_path")
    return ("--directory", str(workspace))


def _path_from_prepared(prepared_state: dict[str, object], key: str) -> Path:
    raw = str(prepared_state.get(key) or "").strip()
    if not raw:
        raise RuntimeError(f"zai launch requires {key} in prepared_state")
    return Path(raw).expanduser()


__all__ = ["build_runtime_launcher"]
