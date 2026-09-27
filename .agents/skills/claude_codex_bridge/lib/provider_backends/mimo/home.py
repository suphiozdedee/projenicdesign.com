from __future__ import annotations

from pathlib import Path

from provider_core.one_way_inheritance import (
    copy_regular_file,
    ensure_private_inheritance_directory,
)
from provider_core.source_home import current_provider_source_home


def materialize_mimo_home(
    target_home: Path,
    *,
    profile=None,
    source_home: Path | None = None,
) -> Path:
    source_root = (
        Path(source_home).expanduser()
        if source_home is not None
        else current_provider_source_home()
    ) / '.mimocode'
    target = ensure_private_inheritance_directory(target_home, source_root)
    if profile is None or bool(getattr(profile, 'inherit_auth', True)):
        for filename in ('auth.json', 'credentials.json', 'token.json', '.env'):
            copy_regular_file(source_root / filename, target / filename)
    return target


__all__ = ['materialize_mimo_home']
