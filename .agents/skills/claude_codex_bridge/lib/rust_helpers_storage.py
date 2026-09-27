from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Mapping, Sequence

from rust_helpers import (
    DEFAULT_TIMEOUT_S,
    RUST_HELPER_BIN_ENV,
    RUST_HELPER_BINARY,
    RUST_HELPERS_ENV,
    RustHelperCallResult,
    RustHelperDiagnostic,
    call_rust_helper_or_fallback,
)


RUST_STORAGE_SCAN_ENV = 'CCB_RUST_STORAGE_SCAN'
RUST_STORAGE_SUMMARY_ENV = 'CCB_RUST_STORAGE_SUMMARY'
STORAGE_SCAN_INVENTORY_CAPABILITY = 'storage.scan.inventory'
STORAGE_SCAN_SUMMARY_CAPABILITY = 'storage.scan.summary'


def scan_storage_inventory(
    roots: Sequence[Mapping[str, object]],
    *,
    env: Mapping[str, str] | None = None,
    helper_bin: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
    which=shutil.which,
    run=subprocess.run,
) -> RustHelperCallResult[list[dict[str, object]]]:
    started = time.monotonic()
    normalized = _normalize_roots(roots)
    required = _storage_helper_required(env)

    def fallback():
        if required:
            _raise_required_storage_helper_unavailable(STORAGE_SCAN_INVENTORY_CAPABILITY)
        return _python_scan_inventory(normalized)

    helper_env = _storage_helper_env(env=env, helper_bin=helper_bin)

    result = call_rust_helper_or_fallback(
        capability=STORAGE_SCAN_INVENTORY_CAPABILITY,
        payload={'roots': [root.copy() for root in normalized]},
        fallback=fallback,
        env=helper_env,
        timeout_s=timeout_s,
        script_root=script_root,
        which=which,
        run=run,
    )
    if not result.helper_used:
        if required:
            _raise_required_storage_helper_unavailable(STORAGE_SCAN_INVENTORY_CAPABILITY)
        return RustHelperCallResult(
            value=_coerce_inventory(result.value, fallback),
            helper_used=False,
            diagnostics=result.diagnostics,
            helper_path=result.helper_path,
        )

    value = _validate_inventory(result.value)
    if value is None:
        if required:
            _raise_required_storage_helper_unavailable(STORAGE_SCAN_INVENTORY_CAPABILITY)
        return RustHelperCallResult(
            value=fallback(),
            helper_used=False,
            diagnostics=(
                RustHelperDiagnostic(
                    helper=Path(result.helper_path or RUST_HELPER_BINARY).name,
                    failure_kind='unknown_schema',
                    elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                ),
            ),
            helper_path=result.helper_path,
        )
    return RustHelperCallResult(value=value, helper_used=True, diagnostics=result.diagnostics, helper_path=result.helper_path)


def scan_storage_summary(
    roots: Sequence[Mapping[str, object]],
    *,
    ccb_dir: str | os.PathLike[str],
    runtime_state_root: str | os.PathLike[str],
    top_entries_limit: int = 50,
    env: Mapping[str, str] | None = None,
    helper_bin: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
    which=shutil.which,
    run=subprocess.run,
) -> RustHelperCallResult[dict[str, object]]:
    started = time.monotonic()
    normalized = _normalize_roots(roots)
    required = _summary_helper_required(env)

    def fallback():
        if required:
            _raise_required_storage_helper_unavailable(STORAGE_SCAN_SUMMARY_CAPABILITY)
        return {}

    helper_env = _summary_helper_env(env=env, helper_bin=helper_bin)
    result = call_rust_helper_or_fallback(
        capability=STORAGE_SCAN_SUMMARY_CAPABILITY,
        payload={
            'roots': [root.copy() for root in normalized],
            'ccb_dir': str(ccb_dir),
            'runtime_state_root': str(runtime_state_root),
            'top_entries_limit': max(0, int(top_entries_limit)),
        },
        fallback=fallback,
        env=helper_env,
        timeout_s=timeout_s,
        script_root=script_root,
        which=which,
        run=run,
    )
    if not result.helper_used:
        if required:
            _raise_required_storage_helper_unavailable(STORAGE_SCAN_SUMMARY_CAPABILITY)
        return RustHelperCallResult(
            value=_coerce_summary(result.value, fallback),
            helper_used=False,
            diagnostics=result.diagnostics,
            helper_path=result.helper_path,
        )

    value = _validate_summary(result.value)
    if value is None:
        if required:
            _raise_required_storage_helper_unavailable(STORAGE_SCAN_SUMMARY_CAPABILITY)
        return RustHelperCallResult(
            value=fallback(),
            helper_used=False,
            diagnostics=(
                RustHelperDiagnostic(
                    helper=Path(result.helper_path or RUST_HELPER_BINARY).name,
                    failure_kind='unknown_schema',
                    elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                ),
            ),
            helper_path=result.helper_path,
        )
    return RustHelperCallResult(value=value, helper_used=True, diagnostics=result.diagnostics, helper_path=result.helper_path)


def _normalize_roots(roots: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for root in roots:
        if not isinstance(root, Mapping):
            raise TypeError('storage scan root must be a mapping')
        root_kind = str(root.get('root_kind') or '').strip()
        path = str(root.get('path') or '').strip()
        if not root_kind:
            raise ValueError('storage scan root requires root_kind')
        if not path:
            raise ValueError('storage scan root requires path')
        normalized.append({'root_kind': root_kind, 'path': path})
    return normalized


def _storage_helper_env(
    *,
    env: Mapping[str, str] | None,
    helper_bin: str | os.PathLike[str] | None,
) -> dict[str, str]:
    base = dict(env if env is not None else os.environ)
    mode = str(base.get(RUST_STORAGE_SCAN_ENV, '')).strip().lower()
    global_mode = str(base.get(RUST_HELPERS_ENV, '')).strip().lower()
    if mode in {'0', 'false', 'no', 'off', 'disabled'}:
        base[RUST_HELPERS_ENV] = '0'
    elif mode in {'1', 'true', 'yes', 'on', 'auto', 'required'}:
        base[RUST_HELPERS_ENV] = '1'
    elif global_mode in {'0', 'false', 'no', 'off', 'disabled'}:
        base[RUST_HELPERS_ENV] = '0'
    else:
        base[RUST_HELPERS_ENV] = '1'
    if helper_bin is not None:
        base[RUST_HELPER_BIN_ENV] = str(helper_bin)
    return base


def _summary_helper_env(
    *,
    env: Mapping[str, str] | None,
    helper_bin: str | os.PathLike[str] | None,
) -> dict[str, str]:
    base = dict(env if env is not None else os.environ)
    mode = str(base.get(RUST_STORAGE_SUMMARY_ENV, '')).strip().lower()
    if mode in {'1', 'true', 'yes', 'on', 'auto', 'required'}:
        base[RUST_HELPERS_ENV] = '1'
    else:
        base[RUST_HELPERS_ENV] = '0'
    if helper_bin is not None:
        base[RUST_HELPER_BIN_ENV] = str(helper_bin)
    return base


def _storage_helper_required(env: Mapping[str, str] | None) -> bool:
    base = env if env is not None else os.environ
    return str(base.get(RUST_STORAGE_SCAN_ENV, '')).strip().lower() == 'required'


def _summary_helper_required(env: Mapping[str, str] | None) -> bool:
    base = env if env is not None else os.environ
    return str(base.get(RUST_STORAGE_SUMMARY_ENV, '')).strip().lower() == 'required'


def _python_scan_inventory(roots: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    import os as _os

    records: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for root in roots:
        root_kind = str(root['root_kind'])
        root_path = Path(str(root['path']))
        if not root_path.exists():
            continue
        for path in _walk_files(root_path):
            identity = _scan_identity(path)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                relative_path = str(path.relative_to(root_path))
            except Exception:
                relative_path = str(path)
            try:
                stat = path.lstat()
                size_bytes = int(stat.st_size)
                is_symlink = path.is_symlink()
            except OSError:
                size_bytes = 0
                is_symlink = False
            records.append(
                {
                    'path': str(path),
                    'relative_path': relative_path,
                    'root_kind': root_kind,
                    'size_bytes': size_bytes,
                    'is_symlink': is_symlink,
                }
            )
    return records


def _walk_files(root: Path):
    import os as _os

    for current, dirnames, filenames in _os.walk(root, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for dirname in dirnames:
            candidate = current_path / dirname
            try:
                if candidate.is_symlink():
                    yield candidate
                    continue
            except OSError:
                yield candidate
                continue
            safe_dirs.append(dirname)
        dirnames[:] = safe_dirs
        for filename in filenames:
            yield current_path / filename


def _scan_identity(path: Path) -> tuple[object, ...]:
    try:
        stat = path.lstat()
        return ('inode', stat.st_dev, stat.st_ino)
    except OSError:
        return ('path', str(path.absolute()))


def _coerce_inventory(value: object, fallback) -> list[dict[str, object]]:
    valid = _validate_inventory(value)
    if valid is not None:
        return valid
    return fallback()


def _coerce_summary(value: object, fallback) -> dict[str, object]:
    valid = _validate_summary(value)
    if valid is not None:
        return valid
    return fallback()


def _validate_inventory(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, list):
        return None
    records: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        path = item.get('path')
        relative_path = item.get('relative_path')
        root_kind = item.get('root_kind')
        size_bytes = item.get('size_bytes')
        if not isinstance(path, str) or not isinstance(relative_path, str) or not isinstance(root_kind, str):
            return None
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            return None
        records.append(
            {
                'path': path,
                'relative_path': relative_path,
                'root_kind': root_kind,
                'size_bytes': size_bytes,
                'is_symlink': bool(item.get('is_symlink')),
            }
        )
    return records


def _validate_summary(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    total_bytes = value.get('total_bytes')
    total_count = value.get('total_count')
    if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 0:
        return None
    if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
        return None
    summary: dict[str, object] = {
        'total_bytes': total_bytes,
        'total_count': total_count,
        'by_class': _validate_summary_buckets(value.get('by_class')),
        'by_provider': _validate_summary_buckets(value.get('by_provider')),
        'by_agent': _validate_summary_buckets(value.get('by_agent')),
        'entries': _validate_summary_entries(value.get('entries')),
    }
    if summary['by_class'] is None or summary['by_provider'] is None or summary['by_agent'] is None:
        return None
    if summary['entries'] is None:
        return None
    if 'by_root_kind' in value:
        by_root_kind = _validate_summary_buckets(value.get('by_root_kind'))
        if by_root_kind is None:
            return None
        summary['by_root_kind'] = by_root_kind
    return summary


def _validate_summary_buckets(value: object) -> dict[str, dict[str, int]] | None:
    if not isinstance(value, Mapping):
        return None
    buckets: dict[str, dict[str, int]] = {}
    for key, bucket in value.items():
        if not isinstance(key, str) or not isinstance(bucket, Mapping):
            return None
        bytes_value = bucket.get('bytes')
        count_value = bucket.get('count')
        if isinstance(bytes_value, bool) or not isinstance(bytes_value, int) or bytes_value < 0:
            return None
        if isinstance(count_value, bool) or not isinstance(count_value, int) or count_value < 0:
            return None
        buckets[key] = {'bytes': bytes_value, 'count': count_value}
    return buckets


def _validate_summary_entries(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, list):
        return None
    entries: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        relative_path = item.get('relative_path')
        storage_class = item.get('storage_class')
        size_bytes = item.get('size_bytes')
        if not isinstance(relative_path, str) or not isinstance(storage_class, str):
            return None
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            return None
        entries.append(dict(item))
    return entries


def _raise_required_storage_helper_unavailable(capability: str):
    raise RuntimeError(f'{capability} requires ccb-rs-helper; no Python fallback is available for this path')


__all__ = [
    'RUST_STORAGE_SCAN_ENV',
    'RUST_STORAGE_SUMMARY_ENV',
    'STORAGE_SCAN_INVENTORY_CAPABILITY',
    'STORAGE_SCAN_SUMMARY_CAPABILITY',
    'scan_storage_inventory',
    'scan_storage_summary',
]
