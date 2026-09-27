from __future__ import annotations

from pathlib import Path

from provider_backends.native_cli_support import NativeCliLaunchConfig, build_native_cli_runtime_launcher
from provider_core.contracts import ProviderRuntimeLauncher


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return build_native_cli_runtime_launcher(
        NativeCliLaunchConfig(
            provider="kiro",
            home_env="KIRO_HOME",
            visible_env_builder=_kiro_visible_env,
        )
    )


def _kiro_visible_env(prepared_state: dict[str, object]) -> dict[str, str]:
    raw_home = str(prepared_state.get("kiro_home") or "").strip()
    if not raw_home:
        return {}
    return {"KIRO_HOME": str(Path(raw_home).expanduser() / ".kiro")}


__all__ = ["build_runtime_launcher"]
