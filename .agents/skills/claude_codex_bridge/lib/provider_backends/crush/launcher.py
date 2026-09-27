from __future__ import annotations

from pathlib import Path

from provider_backends.native_cli_support import NativeCliLaunchConfig, build_native_cli_runtime_launcher
from provider_core.contracts import ProviderRuntimeLauncher


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return build_native_cli_runtime_launcher(
        NativeCliLaunchConfig(provider="crush", visible_args_builder=_crush_visible_args)
    )


def _crush_visible_args(prepared_state: dict[str, object]) -> tuple[str, ...]:
    data_dir = _path_from_prepared(prepared_state, "crush_data_dir")
    data_dir.mkdir(parents=True, exist_ok=True)
    return ("--data-dir", str(data_dir))


def _path_from_prepared(prepared_state: dict[str, object], key: str) -> Path:
    raw = str(prepared_state.get(key) or "").strip()
    if not raw:
        raise RuntimeError(f"crush launch requires {key} in prepared_state")
    return Path(raw).expanduser()


__all__ = ["build_runtime_launcher"]
