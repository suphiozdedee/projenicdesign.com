from __future__ import annotations

from pathlib import Path

from provider_core.one_way_inheritance import (
    copy_regular_file,
    ensure_private_directory,
    ensure_private_inheritance_directory,
)
from provider_core.source_home import current_provider_source_home


_AUTH_BEARING_FILES = ('settings.json', 'credentials.json', 'auth.json', '.env')
_CONFIG_FILES = ('config.json',)


def materialize_deepseek_home(
    target_home: Path,
    *,
    profile=None,
    source_home: Path | None = None,
) -> Path:
    source = (
        Path(source_home).expanduser()
        if source_home is not None
        else current_provider_source_home()
    ) / '.deepcode'
    target = ensure_private_directory(target_home)
    target_root = ensure_private_inheritance_directory(target / '.deepcode', source)
    if _inherits_auth(profile):
        for filename in _AUTH_BEARING_FILES:
            copy_regular_file(source / filename, target_root / filename)
    if _inherits_config(profile):
        for filename in _CONFIG_FILES:
            copy_regular_file(source / filename, target_root / filename)
    return target


def _inherits_auth(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_auth', True))


def _inherits_config(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_config', True))


__all__ = ['materialize_deepseek_home']
