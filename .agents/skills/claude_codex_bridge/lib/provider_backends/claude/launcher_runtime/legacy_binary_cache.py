from __future__ import annotations

import json
import os
from pathlib import Path


_PROJECTION_LABEL = 'claude-binary-versions'


def detach_legacy_claude_binary_cache(
    home: Path,
    *,
    cache_roots: tuple[Path, ...],
) -> dict[str, object]:
    """Detach only CCB-owned legacy Claude cache links from a managed home."""

    managed_home = Path(home).expanduser()
    versions_dir = managed_home / '.local' / 'share' / 'claude' / 'versions'
    if not versions_dir.is_symlink():
        return _result(status='skipped', reason='versions_dir_not_symlink', versions_dir=versions_dir)

    versions_target = _symlink_target(versions_dir)
    matched_root = _matching_versions_root(versions_target, cache_roots)
    if matched_root is None:
        return _result(
            status='skipped',
            reason='versions_dir_not_legacy_ccb_cache',
            versions_dir=versions_dir,
            versions_target=versions_target,
        )

    removed: list[str] = []
    executable_link = managed_home / '.local' / 'bin' / 'claude'
    try:
        if executable_link.is_symlink():
            executable_target = _symlink_target(executable_link)
            if executable_target is not None and _is_within(executable_target, matched_root):
                executable_link.unlink(missing_ok=True)
                removed.append(str(executable_link))

        versions_dir.unlink(missing_ok=True)
        removed.append(str(versions_dir))
    except OSError:
        return _result(
            status='skipped',
            reason='legacy_ccb_binary_cache_detach_failed',
            versions_dir=versions_dir,
            versions_target=versions_target,
            removed=tuple(removed),
        )
    marker = Path(f'{versions_dir}.ccb-projection.json')
    if _is_legacy_projection_marker(marker, source=matched_root):
        try:
            marker.unlink(missing_ok=True)
            removed.append(str(marker))
        except OSError:
            pass

    return _result(
        status='ok',
        reason='legacy_ccb_binary_cache_detached',
        versions_dir=versions_dir,
        versions_target=versions_target,
        removed=tuple(removed),
    )


def _matching_versions_root(target: Path | None, cache_roots: tuple[Path, ...]) -> Path | None:
    if target is None:
        return None
    for cache_root in cache_roots:
        expected = _normalize(Path(cache_root).expanduser() / 'versions')
        if expected is not None and target == expected:
            return expected
    return None


def _symlink_target(path: Path) -> Path | None:
    try:
        raw = Path(os.readlink(path))
    except OSError:
        return None
    if not raw.is_absolute():
        raw = path.parent / raw
    return _normalize(raw)


def _normalize(path: Path) -> Path | None:
    try:
        return Path(path).resolve(strict=False)
    except Exception:
        try:
            return Path(path).absolute()
        except Exception:
            return None


def _is_within(path: Path, root: Path) -> bool:
    normalized_path = _normalize(path)
    normalized_root = _normalize(root)
    if normalized_path is None or normalized_root is None:
        return False
    try:
        normalized_path.relative_to(normalized_root)
        return True
    except ValueError:
        return False


def _is_legacy_projection_marker(marker: Path, *, source: Path) -> bool:
    try:
        payload = json.loads(marker.read_text(encoding='utf-8'))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get('record_type') != 'ccb_projected_asset':
        return False
    if str(payload.get('label') or '') != _PROJECTION_LABEL:
        return False
    recorded_source = _normalize(Path(str(payload.get('source') or '')).expanduser())
    return recorded_source == _normalize(source)


def _result(
    *,
    status: str,
    reason: str,
    versions_dir: Path,
    versions_target: Path | None = None,
    removed: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        'status': status,
        'reason': reason,
        'versions_dir': str(versions_dir),
        'versions_target': str(versions_target) if versions_target is not None else '',
        'removed': removed,
    }


__all__ = ['detach_legacy_claude_binary_cache']
