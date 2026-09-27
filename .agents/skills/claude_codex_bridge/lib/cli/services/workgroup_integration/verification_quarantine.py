from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Iterable


def preserve_verification_delta(
    *,
    project_root: Path,
    quarantine_root: Path,
    transaction_key: str,
    signature: dict[str, object],
    changed_paths: Iterable[str],
    deleted_paths: Iterable[str],
    untracked_paths: Iterable[str],
    evidence_kind: str = 'root-verification',
) -> dict[str, object]:
    project_root = Path(project_root).resolve()
    quarantine_root = Path(quarantine_root).resolve()
    try:
        quarantine_root.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise ValueError('verification quarantine must be outside the project root')
    if evidence_kind == 'root-verification':
        schema = 'ccb.loop.root_verification_quarantine.v1'
    elif evidence_kind == 'node-failure':
        schema = 'ccb.loop.node_failure_quarantine.v1'
    else:
        raise ValueError(f'unsupported verification quarantine evidence kind: {evidence_kind}')
    destination = quarantine_root / transaction_key / evidence_kind
    manifest = {
        'schema': schema,
        'transaction_key': transaction_key,
        'project_root': str(project_root),
        'signature': signature,
        'changed_paths': sorted(set(changed_paths)),
        'deleted_paths': sorted(set(deleted_paths)),
        'untracked_paths': sorted(set(untracked_paths)),
    }
    if evidence_kind != 'root-verification':
        manifest['evidence_kind'] = evidence_kind
    manifest['digest'] = _digest(manifest)
    manifest_path = destination / 'manifest.json'
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding='utf-8'))
        if existing != manifest:
            raise RuntimeError('verification quarantine authority mismatch')
        return {
            'status': 'preserved',
            'path': str(destination),
            'manifest_path': str(manifest_path),
            'manifest_digest': manifest['digest'],
        }

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f'.{destination.name}.tmp-{os.getpid()}')
    if temporary.exists():
        shutil.rmtree(temporary)
    files_root = temporary / 'files'
    files_root.mkdir(parents=True)
    try:
        for relative in manifest['changed_paths']:
            source = _project_path(project_root, relative)
            if not source.exists() and not source.is_symlink():
                continue
            target = files_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
            elif source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target, follow_symlinks=False)
        (temporary / 'manifest.json').write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
        )
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        'status': 'preserved',
        'path': str(destination),
        'manifest_path': str(manifest_path),
        'manifest_digest': manifest['digest'],
    }


def remove_captured_untracked(project_root: Path, paths: Iterable[str]) -> None:
    project_root = Path(project_root).resolve()
    parents: set[Path] = set()
    for relative in sorted(set(paths), reverse=True):
        target = _project_path(project_root, relative)
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        parent = target.parent
        while parent != project_root:
            parents.add(parent)
            parent = parent.parent
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass


def _project_path(project_root: Path, relative: str) -> Path:
    value = str(relative)
    if not value or value.startswith('/'):
        raise ValueError(f'invalid verification delta path: {value!r}')
    target = (project_root / value).resolve(strict=False)
    try:
        target.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(f'verification delta path escapes project root: {value!r}') from exc
    return target


def _digest(value: dict[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return f'sha256:{hashlib.sha256(encoded).hexdigest()}'


__all__ = ['preserve_verification_delta', 'remove_captured_untracked']
