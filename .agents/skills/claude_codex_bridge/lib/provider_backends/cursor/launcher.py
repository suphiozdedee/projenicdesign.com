from __future__ import annotations

from provider_backends.native_cli_support import NativeCliLaunchConfig, build_native_cli_runtime_launcher
from provider_core.contracts import ProviderRuntimeLauncher


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return build_native_cli_runtime_launcher(
        NativeCliLaunchConfig(
            provider="cursor",
            home_env="HOME",
            visible_env_builder=lambda _state: {
                # Cursor's default credential store may be an OS keychain.
                # File mode keeps refresh/logout writes inside managed XDG
                # config, where CCB projects a private auth.json copy.
                "AGENT_CLI_CREDENTIAL_STORE": "file",
            },
            visible_raw_env_names=("AGENT_CLI_CREDENTIAL_STORE",),
        )
    )


__all__ = ["build_runtime_launcher"]
