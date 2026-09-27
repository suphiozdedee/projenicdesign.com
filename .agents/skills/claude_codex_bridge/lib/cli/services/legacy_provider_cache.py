from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil

from project.ids import compute_project_id


_KNOWN_PROVIDERS = ('claude', 'gemini')
_MANIFEST_RECORD_TYPE = 'ccb_external_provider_cache_manifest'


@dataclass(frozen=True)
class LegacyProviderCacheRemoval:
    provider: str
    path: str
    bytes_removed: int
    reason: str = 'orphaned_legacy_project_provider_cache'


@dataclass(frozen=True)
class LegacyProviderCachePreserved:
    provider: str
    path: str
    reason: str
    project_root: str = ''


@dataclass(frozen=True)
class LegacyProviderCacheSweep:
    projects_root: str
    removals: tuple[LegacyProviderCacheRemoval, ...] = ()
    preserved: tuple[LegacyProviderCachePreserved, ...] = ()

    @property
    def removed_count(self) -> int:
        return len(self.removals)

    @property
    def removed_bytes(self) -> int:
        return sum(item.bytes_removed for item in self.removals)


def cleanup_orphaned_legacy_provider_caches(
    projects_root: Path,
    *,
    measure_bytes: bool = True,
) -> LegacyProviderCacheSweep:
    """Remove only manifest-valid known Provider caches for deleted projects."""

    root = Path(projects_root).expanduser()
    removals: list[LegacyProviderCacheRemoval] = []
    preserved: list[LegacyProviderCachePreserved] = []
    if not root.is_dir() or root.is_symlink():
        return LegacyProviderCacheSweep(projects_root=str(root))

    try:
        buckets = sorted(root.iterdir())
    except OSError:
        return LegacyProviderCacheSweep(
            projects_root=str(root),
            preserved=(
                LegacyProviderCachePreserved(
                    provider='',
                    path=str(root),
                    reason='projects_root_unreadable',
                ),
            ),
        )

    for bucket in buckets:
        if not _looks_like_project_cache_bucket(bucket):
            continue
        provider_cache = bucket / 'provider-cache'
        if not provider_cache.is_dir() or provider_cache.is_symlink():
            continue
        try:
            provider_entries = tuple(sorted(provider_cache.iterdir()))
        except OSError:
            preserved.append(
                LegacyProviderCachePreserved(
                    provider='',
                    path=str(provider_cache),
                    reason='provider_cache_unreadable',
                )
            )
            continue
        preserved.extend(
            LegacyProviderCachePreserved(
                provider=entry.name,
                path=str(entry),
                reason='unknown_provider',
            )
            for entry in provider_entries
            if entry.name not in _KNOWN_PROVIDERS
        )
        for provider in _KNOWN_PROVIDERS:
            cache_dir = provider_cache / provider
            if not cache_dir.exists() and not cache_dir.is_symlink():
                continue
            project_root, reason = _manifest_project_root(
                cache_dir,
                provider=provider,
                bucket_name=bucket.name,
            )
            if reason != 'orphaned_project_root':
                preserved.append(
                    LegacyProviderCachePreserved(
                        provider=provider,
                        path=str(cache_dir),
                        reason=reason,
                        project_root=str(project_root) if project_root is not None else '',
                    )
                )
                continue
            removed_bytes = _tree_size(cache_dir) if measure_bytes else 0
            try:
                _remove_cache_dir(cache_dir, provider_cache=provider_cache)
            except OSError:
                preserved.append(
                    LegacyProviderCachePreserved(
                        provider=provider,
                        path=str(cache_dir),
                        reason='remove_failed',
                        project_root=str(project_root),
                    )
                )
                continue
            removals.append(
                LegacyProviderCacheRemoval(
                    provider=provider,
                    path=str(cache_dir),
                    bytes_removed=removed_bytes,
                )
            )
        _remove_empty_dirs(provider_cache, stop_at=root)

    return LegacyProviderCacheSweep(
        projects_root=str(root),
        removals=tuple(removals),
        preserved=tuple(preserved),
    )


def _looks_like_project_cache_bucket(path: Path) -> bool:
    name = path.name
    return (
        path.is_dir()
        and not path.is_symlink()
        and len(name) == 16
        and all(character in '0123456789abcdef' for character in name)
    )


def _manifest_project_root(
    cache_dir: Path,
    *,
    provider: str,
    bucket_name: str,
) -> tuple[Path | None, str]:
    if cache_dir.is_symlink() or not cache_dir.is_dir():
        return None, 'cache_dir_not_owned_directory'
    manifest_path = cache_dir / 'MANIFEST.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None, 'manifest_not_owned_file'
    try:
        payload = json.loads(manifest_path.read_text(encoding='utf-8'))
    except Exception:
        return None, 'manifest_unreadable'
    if not isinstance(payload, dict):
        return None, 'manifest_invalid'
    if payload.get('schema_version') != 1:
        return None, 'manifest_schema_mismatch'
    if payload.get('record_type') != _MANIFEST_RECORD_TYPE:
        return None, 'manifest_record_type_mismatch'
    if str(payload.get('provider') or '') != provider:
        return None, 'manifest_provider_mismatch'
    project_id = str(payload.get('project_id') or '').strip().lower()
    if len(project_id) != 64 or project_id[:16] != bucket_name:
        return None, 'manifest_project_id_mismatch'
    raw_root = str(payload.get('project_root') or '').strip()
    if not raw_root:
        return None, 'manifest_project_root_missing'
    project_root = Path(raw_root).expanduser()
    if not project_root.is_absolute():
        return None, 'manifest_project_root_not_absolute'
    try:
        if compute_project_id(project_root) != project_id:
            return project_root, 'manifest_project_identity_mismatch'
    except Exception:
        return project_root, 'manifest_project_identity_invalid'
    if project_root.exists() or project_root.is_symlink():
        return project_root, 'project_root_exists'
    return project_root, 'orphaned_project_root'


def _remove_cache_dir(cache_dir: Path, *, provider_cache: Path) -> None:
    if cache_dir.is_symlink():
        raise OSError('legacy Provider cache directory is a symlink')
    try:
        cache_dir.resolve(strict=False).relative_to(provider_cache.resolve(strict=False))
    except Exception as exc:
        raise OSError('legacy Provider cache directory is out of bounds') from exc
    shutil.rmtree(cache_dir)


def _remove_empty_dirs(path: Path, *, stop_at: Path) -> None:
    current = path
    stop = stop_at.resolve(strict=False)
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _tree_size(path: Path) -> int:
    total = 0
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_file() or path.is_symlink():
        return _lstat_size(path)
    for child in path.rglob('*'):
        total += _lstat_size(child)
    return total


def _lstat_size(path: Path) -> int:
    try:
        return int(path.lstat().st_size)
    except OSError:
        return 0


__all__ = [
    'LegacyProviderCachePreserved',
    'LegacyProviderCacheRemoval',
    'LegacyProviderCacheSweep',
    'cleanup_orphaned_legacy_provider_caches',
]
