from __future__ import annotations

from provider_backends.native_cli_support import NativeCliLaunchConfig, build_native_cli_runtime_launcher
from provider_core.contracts import ProviderRuntimeLauncher


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return build_native_cli_runtime_launcher(
        NativeCliLaunchConfig(
            provider="qwen",
            home_env="QWEN_HOME",
            visible_env_builder=lambda _state: {
                # Keep MCP OAuth and extension-secret writes out of the user's
                # OS keychain and inside the managed QWEN_HOME projection.
                "QWEN_CODE_FORCE_FILE_STORAGE": "true",
                "QWEN_CODE_FORCE_ENCRYPTED_FILE_STORAGE": "true",
            },
            visible_raw_env_names=(
                "QWEN_CODE_FORCE_FILE_STORAGE",
                "QWEN_CODE_FORCE_ENCRYPTED_FILE_STORAGE",
            ),
        )
    )


__all__ = ["build_runtime_launcher"]
