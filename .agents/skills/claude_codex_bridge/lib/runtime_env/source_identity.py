from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import hashlib
import os
from pathlib import Path


IDENTITY_KIND = 'source-tree'


def current_source_runtime_identity(
    *,
    source_root: str | Path | None = None,
    environ: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    values = os.environ if environ is None else environ
    raw_root = (
        source_root if source_root is not None else values.get('CCB_SOURCE_ROOT')
    )
    root = _source_root(raw_root)
    if root is None:
        return None
    return {
        'kind': IDENTITY_KIND,
        'root': str(root),
        'digest': _source_tree_digest(str(root)),
    }


def source_runtime_identity_matches(
    expected: Mapping[str, object] | None,
    actual: object,
) -> bool:
    normalized_expected = _normalize_identity(expected)
    if normalized_expected is None:
        return True
    normalized_actual = _normalize_identity(actual)
    return normalized_actual == normalized_expected


def _source_root(value: object) -> Path | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    root = _resolve(Path(raw).expanduser())
    if not (root / 'ccb.py').is_file():
        return None
    return root


def _normalize_identity(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    if str(value.get('kind') or '').strip() != IDENTITY_KIND:
        return None
    root = _source_root(value.get('root'))
    digest = str(value.get('digest') or '').strip()
    if root is None or not digest:
        return None
    return {
        'kind': IDENTITY_KIND,
        'root': str(root),
        'digest': digest,
    }


@lru_cache(maxsize=8)
def _source_tree_digest(root_text: str) -> str:
    root = Path(root_text)
    hasher = hashlib.sha256()
    hasher.update(b'ccb-source-runtime-v1\n')
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix()
        hasher.update(relative.encode('utf-8', errors='surrogateescape'))
        hasher.update(b'\0')
        hasher.update(path.read_bytes())
        hasher.update(b'\0')
    return hasher.hexdigest()


def _source_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    entrypoint = root / 'ccb.py'
    if entrypoint.is_file():
        files.append(entrypoint)
    lib_root = root / 'lib'
    if lib_root.is_dir():
        files.extend(
            sorted(
                path
                for path in lib_root.rglob('*.py')
                if '__pycache__' not in path.parts
            )
        )
    return tuple(files)


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except Exception:
        return path.absolute()


__all__ = [
    'current_source_runtime_identity',
    'source_runtime_identity_matches',
]
