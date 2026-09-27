from __future__ import annotations

from pathlib import Path

from provider_core.one_way_inheritance import (
    copy_regular_file,
    copy_regular_tree,
    ensure_private_inheritance_directory,
)
from provider_core.source_home import current_provider_source_home


_AUTH_BEARING_SHARE_FILES = ('config.toml', 'kimi.json', 'device_id')
_AUTH_BEARING_CODE_FILES = ('config.toml', 'device_id')
_CONFIG_ONLY_CODE_FILES = ('tui.toml',)


def materialize_kimi_home(
    share_dir: Path,
    code_home: Path,
    *,
    profile=None,
    source_home: Path | None = None,
) -> tuple[Path, Path]:
    """Create private Kimi roots and inherit account state in one direction."""
    source = (
        Path(source_home).expanduser()
        if source_home is not None
        else current_provider_source_home()
    )
    source_share = source / '.kimi'
    source_code = source / '.kimi-code'
    share = ensure_private_inheritance_directory(share_dir, source_share)
    code = ensure_private_inheritance_directory(code_home, source_code)

    if _inherits_auth(profile):
        for filename in _AUTH_BEARING_SHARE_FILES:
            copy_regular_file(source_share / filename, share / filename)
        for filename in _AUTH_BEARING_CODE_FILES:
            copy_regular_file(source_code / filename, code / filename)
        copy_regular_tree(source_code / 'credentials', code / 'credentials')
    if _inherits_config(profile):
        for filename in _CONFIG_ONLY_CODE_FILES:
            copy_regular_file(source_code / filename, code / filename)
    return share, code


def _inherits_auth(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_auth', True))


def _inherits_config(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_config', True))


__all__ = ['materialize_kimi_home']
