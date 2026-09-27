from __future__ import annotations

from pathlib import Path

from provider_backends.native_cli_support import NativeCliLaunchConfig, build_native_cli_runtime_launcher
from provider_core.contracts import ProviderRuntimeLauncher


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return build_native_cli_runtime_launcher(
        NativeCliLaunchConfig(
            provider="copilot",
            home_env="COPILOT_HOME",
            visible_env_builder=_visible_env,
            visible_path_env_names=("COPILOT_CACHE_HOME",),
            visible_raw_env_names=("COPILOT_DISABLE_KEYTAR",),
        )
    )


def _visible_env(launch_context: dict[str, object]) -> dict[str, str]:
    state_dir = _path_or_none(launch_context.get('copilot_state_dir'))
    data_dir = _path_or_none(launch_context.get('copilot_data_dir'))
    cache_root = (data_dir or ((state_dir / 'data') if state_dir is not None else None))
    if cache_root is None:
        return {}
    return {
        'COPILOT_CACHE_HOME': str(cache_root / 'cache'),
        # The Copilot SDK documents this switch for avoiding keytar.  Managed
        # auth then remains in the agent-local COPILOT_HOME projection.
        'COPILOT_DISABLE_KEYTAR': '1',
    }


def _path_or_none(value: object) -> Path | None:
    raw = str(value or '').strip()
    return Path(raw).expanduser() if raw else None


__all__ = ["build_runtime_launcher"]
