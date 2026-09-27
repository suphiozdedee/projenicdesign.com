from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from agents.config_loader import load_project_config
from rolepacks.manifest import normalize_role_id
from rolepacks.runtime_lookup import load_installed_role, project_role_lock_entry, project_role_lock_path, tree_digest
from rolepacks.sources import installed_role_metadata


@dataclass(frozen=True)
class ProjectRoleLockUpdate:
    role_id: str
    locked_version: str
    locked_digest: str
    current_version: str
    current_digest: str
    lock_path: Path


def find_project_role_lock_updates(project_root: Path) -> tuple[ProjectRoleLockUpdate, ...]:
    root = Path(project_root).expanduser().resolve()
    config = load_project_config(root).config
    updates: list[ProjectRoleLockUpdate] = []
    seen: set[str] = set()
    for spec in config.agents.values():
        role_text = str(getattr(spec, 'role', '') or '').strip()
        if not role_text:
            continue
        role_id = normalize_role_id(role_text)
        if role_id in seen:
            continue
        seen.add(role_id)
        try:
            entry = project_role_lock_entry(root, role_id)
        except ValueError:
            continue
        if entry is None:
            continue
        installed = load_installed_role(role_id)
        if installed is None:
            continue
        locked_version = str(entry.get('version') or '').strip()
        locked_digest = str(entry.get('digest') or '').strip()
        current_digest = _installed_current_digest(installed)
        if locked_version == installed.version and locked_digest == current_digest:
            continue
        updates.append(
            ProjectRoleLockUpdate(
                role_id=installed.id,
                locked_version=locked_version,
                locked_digest=locked_digest,
                current_version=installed.version,
                current_digest=current_digest,
                lock_path=project_role_lock_path(root),
            )
        )
    return tuple(updates)


def confirm_project_role_lock_refresh(
    project_root: Path,
    *,
    out: TextIO,
    stdin,
    stream_is_tty_fn,
) -> tuple[ProjectRoleLockUpdate, ...]:
    updates = find_project_role_lock_updates(project_root)
    if not updates:
        return ()
    if not stream_is_tty_fn(stdin):
        for update in updates:
            print(_format_update_available(update), file=out)
        print('role_lock_legacy_check: skipped_noninteractive', file=out)
        return updates

    print('Legacy project role-lock residue differs from installed Role Packs:', file=out)
    for update in updates:
        print(f'  {update.role_id}: {_format_versions(update)}', file=out)
    print(
        f'Show legacy diagnostic for {updates[0].lock_path} without changing the file? [y/N] ',
        end='',
        file=out,
        flush=True,
    )
    reply = stdin.readline()
    if str(reply or '').strip().lower() not in {'y', 'yes'}:
        print('role_lock_legacy_check: declined', file=out)
        return updates

    print('role_lock_legacy_check: confirmed_noop', file=out)
    print('  project role locks are legacy diagnostics and no longer control provider restart adoption', file=out)
    print('  existing .ccb/role-lock.json remains unchanged in this release', file=out)
    for update in updates:
        print(
            f'role_lock_legacy_notice: {update.role_id} '
            f'locked version={update.locked_version} digest={update.locked_digest} -> '
            f'installed version={update.current_version} digest={update.current_digest}',
            file=out,
        )
    return updates


def _installed_current_digest(role) -> str:
    metadata = installed_role_metadata(role.id)
    digest = str(metadata.get('digest') or '').strip()
    if digest:
        return digest
    return f'sha256:{tree_digest(role.root)}'


def _format_update_available(update: ProjectRoleLockUpdate) -> str:
    return f'role_lock_update_available: {update.role_id} {_format_versions(update)}'


def _format_versions(update: ProjectRoleLockUpdate) -> str:
    locked_version = update.locked_version or 'unknown'
    locked_digest = update.locked_digest or 'unknown'
    return (
        f'locked version={locked_version} digest={locked_digest} -> '
        f'installed version={update.current_version} digest={update.current_digest}'
    )


__all__ = [
    'ProjectRoleLockUpdate',
    'confirm_project_role_lock_refresh',
    'find_project_role_lock_updates',
]
