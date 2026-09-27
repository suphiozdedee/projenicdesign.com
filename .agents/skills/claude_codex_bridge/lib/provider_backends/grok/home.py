from __future__ import annotations

from pathlib import Path

from provider_core.one_way_inheritance import (
    copy_regular_file,
    ensure_private_directory,
    ensure_private_inheritance_directory,
)
from provider_core.source_home import current_provider_source_home

from .skills import materialize_grok_skills


_GROK_AUTH_FILES = (
    Path('.grok') / 'auth.json',
)
_GROK_CONFIG_FILES = (
    Path('.grok') / 'config.toml',
)


def materialize_grok_home(
    target_home: Path,
    *,
    profile=None,
    source_home: Path | None = None,
) -> Path:
    target = Path(target_home).expanduser()
    source = Path(source_home).expanduser() if source_home is not None else current_provider_source_home()
    target = ensure_private_inheritance_directory(target, source)
    ensure_private_directory(target / '.grok')

    if _inherits_auth(profile):
        for relative in _GROK_AUTH_FILES:
            copy_regular_file(source / relative, target / relative)
    if _inherits_config(profile):
        for relative in _GROK_CONFIG_FILES:
            copy_regular_file(source / relative, target / relative)
    materialize_grok_skills(target, profile=profile)
    return target


def _inherits_auth(profile) -> bool:
    return bool(getattr(profile, 'inherit_auth', True))


def _inherits_config(profile) -> bool:
    return bool(getattr(profile, 'inherit_config', True))

__all__ = ['materialize_grok_home']
