from __future__ import annotations

from pathlib import Path
from typing import Any

from .api_shortcuts import provider_api_shortcut_env, supported_provider_api_shortcuts
from .models import ProviderProfileSpec, ResolvedProviderProfile, SkillOverlaySpec


def load_resolved_provider_profile(runtime_dir: Path):
    from .materializer import load_resolved_provider_profile as _impl

    return _impl(runtime_dir)


def materialize_provider_profile(
    *,
    layout: Any,
    spec: Any,
    workspace_path: Path,
    materialize_home: bool = True,
):
    from .materializer import materialize_provider_profile as _impl

    return _impl(
        layout=layout,
        spec=spec,
        workspace_path=workspace_path,
        materialize_home=materialize_home,
    )


def provider_api_env_keys(provider: str) -> set[str]:
    from .materializer import provider_api_env_keys as _impl

    return _impl(provider)


def validate_provider_runtime_home_uniqueness(*, layout: Any, specs: Any) -> None:
    from .materializer import validate_provider_runtime_home_uniqueness as _impl

    _impl(layout=layout, specs=specs)


__all__ = [
    'ProviderProfileSpec',
    'ResolvedProviderProfile',
    'SkillOverlaySpec',
    'load_resolved_provider_profile',
    'materialize_provider_profile',
    'provider_api_env_keys',
    'provider_api_shortcut_env',
    'supported_provider_api_shortcuts',
    'validate_provider_runtime_home_uniqueness',
]
