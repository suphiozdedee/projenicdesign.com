from __future__ import annotations

from provider_backends.native_cli_support import build_native_cli_manifest
from provider_core.manifests import ProviderManifest


def build_manifest() -> ProviderManifest:
    return build_native_cli_manifest(provider="grok", supports_subagents=True)


__all__ = ["build_manifest"]
