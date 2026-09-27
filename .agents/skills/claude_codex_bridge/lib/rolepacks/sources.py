from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
import urllib.request
import zipfile

from agents.config_loader_runtime.role_lookup import agent_roles_installed_root, role_store_root, role_store_roots
from role_aliases import role_id_candidates
from storage.atomic import atomic_write_text

from .manifest import load_role_manifest, normalize_role_id


SOURCE_REGISTRY_SCHEMA = 'rolepack-source-registry/v1'
DEFAULT_AGENT_ROLES_SPEC_GIT_URL = 'https://github.com/SeemSeam/agent-roles-spec'
DEFAULT_AGENT_ROLES_SPEC_ARCHIVE_URL = (
    'https://github.com/SeemSeam/agent-roles-spec/archive/refs/heads/main.zip'
)
SYSTEM_ROLE_SOURCE_NAMES = ('systemroles', 'dotroles')
SOURCE_CHECKOUT_DRAFT_ROLE_SOURCE_NAME = 'ccb-source-drafts'


@dataclass(frozen=True)
class RoleSource:
    name: str
    path: Path
    source_type: str = 'path'


@dataclass(frozen=True)
class SourceRole:
    source: str
    role_id: str
    version: str
    digest: str
    path: Path
    name: str
    description: str
    duplicates: tuple[str, ...] = ()


def source_registry_path() -> Path:
    path = agent_roles_installed_root().parent / 'sources.json'
    _migrate_legacy_source_registry(path)
    return path


def _migrate_legacy_source_registry(target: Path) -> None:
    legacy = role_store_root() / 'sources.json'
    if _same_path(target, legacy) or target.is_file() or not legacy.is_file():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
    except Exception:
        return


def default_agent_roles_source(
    *,
    refresh: bool = False,
    download_if_missing: bool = True,
) -> Path | None:
    env_path = os.environ.get('AGENT_ROLES_SPEC_HOME') or os.environ.get('CCB_AGENT_ROLES_SPEC_HOME')
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(Path.home() / 'yunwei' / 'agent-roles-spec')
    for candidate in candidates:
        if _looks_like_agent_roles_spec(candidate):
            if refresh:
                _refresh_git_agent_roles_source(candidate)
            return candidate.resolve()
    remote = _ensure_remote_agent_roles_source(
        refresh=refresh,
        download_if_missing=download_if_missing,
    )
    if remote is not None:
        return remote
    return None


def system_role_sources() -> tuple[RoleSource, ...]:
    env_path = os.environ.get('CCB_SYSTEM_ROLES_HOME') or os.environ.get('CCB_ROLES_HOME')
    candidates: list[tuple[str, Path]] = []
    if env_path:
        candidates.append(('systemroles', Path(env_path).expanduser()))
    candidates.extend(
        (
            ('systemroles', Path.home() / '.ccb' / 'roles'),
            ('dotroles', Path.home() / '.roles'),
        )
    )
    sources: list[RoleSource] = []
    seen_names: set[str] = set()
    seen_paths: set[Path] = set()
    for name, candidate in candidates:
        if name in seen_names:
            continue
        if not _looks_like_role_source(candidate):
            continue
        resolved = candidate.resolve()
        if resolved in seen_paths:
            continue
        sources.append(RoleSource(name=name, path=resolved, source_type='system'))
        seen_names.add(name)
        seen_paths.add(resolved)
    return tuple(sources)


def load_role_sources(
    *,
    include_default: bool = True,
    refresh_default: bool = False,
    download_missing_default: bool = True,
) -> tuple[RoleSource, ...]:
    sources: list[RoleSource] = []
    seen: set[str] = set()
    if include_default:
        source_checkout_drafts = source_checkout_draft_role_source()
        if source_checkout_drafts is not None:
            sources.append(source_checkout_drafts)
            seen.add(source_checkout_drafts.name)
        for source in system_role_sources():
            if source.name in seen:
                continue
            sources.append(source)
            seen.add(source.name)
        default = default_agent_roles_source(
            refresh=refresh_default,
            download_if_missing=download_missing_default,
        )
        if default is not None:
            sources.append(RoleSource(name='agentroles', path=default))
            seen.add('agentroles')
    path = source_registry_path()
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        payload = {}
    for item in payload.get('sources') or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        raw_path = str(item.get('path') or '').strip()
        if not name or not raw_path or name in seen:
            continue
        sources.append(RoleSource(name=name, path=Path(raw_path).expanduser()))
        seen.add(name)
    return tuple(sources)


def source_checkout_draft_role_source() -> RoleSource | None:
    if not _source_checkout_draft_roles_enabled():
        return None
    source_root = Path(__file__).resolve().parents[2]
    drafts = source_root / 'docs' / 'plantree' / 'plans' / 'agentic-loop-workflow' / 'drafts'
    if not _looks_like_role_source(drafts):
        return None
    return RoleSource(
        name=SOURCE_CHECKOUT_DRAFT_ROLE_SOURCE_NAME,
        path=drafts.resolve(),
        source_type='source-checkout',
    )


def add_role_source(name: str, path: Path) -> dict[str, object]:
    source_name = _normalize_source_name(name)
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_dir():
        raise ValueError(f'role source path is not a directory: {source_path}')
    sources = {
        source.name: {'name': source.name, 'path': str(source.path), 'type': source.source_type}
        for source in load_role_sources(include_default=False)
    }
    sources[source_name] = {'name': source_name, 'path': str(source_path), 'type': 'path'}
    _write_sources(tuple(sources.values()))
    return {'source_status': 'added', 'name': source_name, 'path': str(source_path)}


def remove_role_source(name: str) -> dict[str, object]:
    source_name = _normalize_source_name(name)
    sources = [
        {'name': source.name, 'path': str(source.path), 'type': source.source_type}
        for source in load_role_sources(include_default=False)
        if source.name != source_name
    ]
    _write_sources(tuple(sources))
    return {'source_status': 'removed', 'name': source_name}


def discover_source_roles(
    *,
    include_default: bool = True,
    include_reference: bool | None = None,
    refresh_default: bool = False,
    download_missing_default: bool = True,
) -> tuple[SourceRole, ...]:
    return _discover_roles_from_sources(
        load_role_sources(
            include_default=include_default,
            refresh_default=refresh_default,
            download_missing_default=download_missing_default,
        ),
        include_reference=include_reference,
    )


def discover_system_source_roles(*, include_reference: bool | None = None) -> tuple[SourceRole, ...]:
    return _discover_roles_from_sources(system_role_sources(), include_reference=include_reference)


def discover_path_roles(path: Path, *, include_reference: bool | None = None) -> tuple[SourceRole, ...]:
    source_path = Path(path).expanduser().resolve()
    return _discover_roles_from_sources(
        (RoleSource(name='path', path=source_path, source_type='path'),),
        include_reference=include_reference,
    )


def find_system_source_role(role_id: str) -> SourceRole | None:
    normalized = normalize_role_id(role_id)
    for role in discover_system_source_roles():
        if role.role_id == normalized:
            return role
    return None


def _discover_roles_from_sources(
    sources: tuple[RoleSource, ...],
    *,
    include_reference: bool | None = None,
) -> tuple[SourceRole, ...]:
    discovered: dict[str, SourceRole] = {}
    duplicates: dict[str, list[str]] = {}
    include_reference_roles = _include_reference_roles_default() if include_reference is None else include_reference
    for source in sources:
        for role_path in _iter_role_paths(source.path, include_reference=include_reference_roles):
            try:
                role = load_role_manifest(role_path)
            except Exception:
                continue
            source_role = SourceRole(
                source=source.name,
                role_id=role.id,
                version=role.version,
                digest=tree_digest(role.root),
                path=role.root,
                name=role.name,
                description=role.description,
            )
            existing = discovered.get(role.id)
            if existing is None:
                discovered[role.id] = source_role
                continue
            if _same_source_reference_to_roles_upgrade(existing, source_role):
                duplicates.setdefault(role.id, []).append(f'{existing.source}:{existing.path}')
                discovered[role.id] = source_role
                continue
            duplicates.setdefault(role.id, []).append(f'{source_role.source}:{source_role.path}')
    roles: list[SourceRole] = []
    for role_id, role in discovered.items():
        duplicate_paths = tuple(duplicates.get(role_id) or ())
        if duplicate_paths:
            role = SourceRole(
                source=role.source,
                role_id=role.role_id,
                version=role.version,
                digest=role.digest,
                path=role.path,
                name=role.name,
                description=role.description,
                duplicates=duplicate_paths,
            )
        roles.append(role)
    return tuple(sorted(roles, key=lambda item: item.role_id))


def find_source_role(role_id: str, *, refresh_default: bool = False) -> SourceRole | None:
    normalized = normalize_role_id(role_id)
    for role in discover_source_roles(refresh_default=refresh_default):
        if role.role_id == normalized:
            return role
    return None


def installed_role_metadata(role_id: str) -> dict[str, Any]:
    for store_root in role_store_roots():
        for candidate_id in role_id_candidates(normalize_role_id(role_id)):
            path = store_root / candidate_id / 'install.json'
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                continue
            return dict(payload) if isinstance(payload, dict) else {}
    return {}


def installed_role_ids() -> tuple[str, ...]:
    ids: list[str] = []
    seen: set[str] = set()
    for root in role_store_roots():
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if not child.is_dir():
                continue
            try:
                role_id = normalize_role_id(child.name)
            except Exception:
                continue
            if role_id in seen:
                continue
            ids.append(role_id)
            seen.add(role_id)
    return tuple(ids)


def migrate_legacy_installed_roles(role_id: str | None = None) -> dict[str, object]:
    legacy_root = role_store_root()
    target_root = agent_roles_installed_root()
    if _same_path(legacy_root, target_root):
        return {'migration_status': 'skipped_same_store', 'migrated': 0, 'skipped': 0, 'failed': 0}
    if not legacy_root.is_dir():
        return {'migration_status': 'ok', 'migrated': 0, 'skipped': 0, 'failed': 0}

    role_dirs = _legacy_role_dirs_for_migration(legacy_root, role_id)
    migrated = 0
    skipped = 0
    failed = 0
    for legacy_dir in role_dirs:
        try:
            canonical_id = normalize_role_id(legacy_dir.name)
        except Exception:
            skipped += 1
            continue
        target_dir = target_root / canonical_id
        try:
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            if not target_dir.exists():
                shutil.copytree(legacy_dir, target_dir, symlinks=True)
                _rewrite_migrated_install_metadata(target_dir, canonical_id)
                migrated += 1
                continue
            else:
                copied_versions = _merge_missing_legacy_versions(legacy_dir, target_dir)
                if (target_dir / 'install.json').is_file() and (target_dir / 'current').exists():
                    if copied_versions:
                        migrated += 1
                    else:
                        skipped += 1
                    continue
                _copy_missing_legacy_install_files(legacy_dir, target_dir)
                if copied_versions:
                    _repair_current_pointer(target_dir, _load_install_metadata(target_dir))
            _rewrite_migrated_install_metadata(target_dir, canonical_id)
            migrated += 1
        except Exception:
            failed += 1
            continue
    status = 'partial' if failed else 'ok'
    return {'migration_status': status, 'migrated': migrated, 'skipped': skipped, 'failed': failed}


def _legacy_role_dirs_for_migration(legacy_root: Path, role_id: str | None) -> tuple[Path, ...]:
    if role_id:
        normalized = normalize_role_id(role_id)
        candidates = [legacy_root / candidate_id for candidate_id in role_id_candidates(normalized)]
        return tuple(candidate for candidate in candidates if candidate.is_dir())
    role_dirs: list[Path] = []
    for child in sorted(legacy_root.iterdir(), key=lambda item: item.name):
        if not child.is_dir():
            continue
        try:
            normalize_role_id(child.name)
        except Exception:
            continue
        role_dirs.append(child)
    return tuple(role_dirs)


def _rewrite_migrated_install_metadata(role_dir: Path, canonical_id: str) -> None:
    path = role_dir / 'install.json'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload['schema'] = 'agent-roles-install/v1'
    payload['id'] = canonical_id
    payload.setdefault('source', 'migrated-ccb')
    payload['migrated_from'] = 'ccb'

    role_root = _installed_role_root_from_metadata(role_dir, payload)
    if role_root is not None:
        try:
            role = load_role_manifest(role_root)
        except Exception:
            role = None
        if role is not None:
            payload['id'] = role.id
            payload['version'] = role.version
            payload['digest'] = f'sha256:{tree_digest(role.root)}'
    atomic_write_text(path, json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + '\n')
    _repair_current_pointer(role_dir, payload)


def _installed_role_root_from_metadata(role_dir: Path, metadata: dict[str, Any]) -> Path | None:
    version = str(metadata.get('version') or '').strip()
    digest = str(metadata.get('digest') or '').strip().removeprefix('sha256:')
    if version and digest:
        target = role_dir / 'versions' / version / digest
        if (target / 'role.toml').is_file():
            return target
    current = role_dir / 'current'
    try:
        if current.exists() and (current.resolve() / 'role.toml').is_file():
            return current.resolve()
    except Exception:
        return None
    return None


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except Exception:
        return left.expanduser() == right.expanduser()


def _copy_missing_legacy_install_files(legacy_dir: Path, canonical_dir: Path) -> None:
    canonical_dir.mkdir(parents=True, exist_ok=True)
    for name in ('install.json', 'versions', 'current'):
        source = legacy_dir / name
        target = canonical_dir / name
        if not source.exists() and not source.is_symlink():
            continue
        if target.exists() or target.is_symlink():
            continue
        if source.is_symlink():
            target.symlink_to(source.readlink(), target_is_directory=True)
        elif source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)


def _merge_missing_legacy_versions(legacy_dir: Path, canonical_dir: Path) -> int:
    legacy_versions = legacy_dir / 'versions'
    if not legacy_versions.is_dir():
        return 0
    copied = 0
    target_versions = canonical_dir / 'versions'
    target_versions.mkdir(parents=True, exist_ok=True)
    for legacy_version in sorted(legacy_versions.iterdir(), key=lambda item: item.name):
        if not legacy_version.is_dir():
            continue
        target_version = target_versions / legacy_version.name
        if (legacy_version / 'role.toml').is_file():
            if not target_version.exists():
                shutil.copytree(legacy_version, target_version, symlinks=True)
                copied += 1
            continue
        target_version.mkdir(parents=True, exist_ok=True)
        for legacy_digest in sorted(legacy_version.iterdir(), key=lambda item: item.name):
            if not legacy_digest.is_dir():
                continue
            target_digest = target_version / legacy_digest.name
            if target_digest.exists():
                continue
            shutil.copytree(legacy_digest, target_digest, symlinks=True)
            copied += 1
    return copied


def _load_install_metadata(role_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((role_dir / 'install.json').read_text(encoding='utf-8'))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _repair_current_pointer(role_dir: Path, metadata: dict[str, Any]) -> None:
    version = str(metadata.get('version') or '').strip()
    digest = str(metadata.get('digest') or '').strip().removeprefix('sha256:')
    if not version or not digest:
        return
    target = role_dir / 'versions' / version / digest
    if not target.is_dir():
        return
    current = role_dir / 'current'
    try:
        if current.resolve() == target.resolve():
            return
    except Exception:
        pass
    if current.exists() or current.is_symlink():
        if current.is_symlink() or current.is_file():
            current.unlink()
        else:
            shutil.rmtree(current)
    try:
        current.symlink_to(target, target_is_directory=True)
    except OSError:
        shutil.copytree(target, current)


def role_catalog_status(
    *,
    refresh_default: bool = False,
    download_missing_default: bool = True,
) -> tuple[dict[str, object], ...]:
    source_roles = {
        role.role_id: role
        for role in discover_source_roles(
            refresh_default=refresh_default,
            download_missing_default=download_missing_default,
        )
    }
    migrate_legacy_installed_roles()
    installed = set(installed_role_ids())
    rows: list[dict[str, object]] = []
    for role_id, source_role in sorted(source_roles.items()):
        metadata = installed_role_metadata(role_id)
        installed_version = str(metadata.get('version') or '') if role_id in installed else ''
        installed_digest = str(metadata.get('digest') or '') if role_id in installed else ''
        source_digest = f'sha256:{source_role.digest}'
        if role_id not in installed:
            status = 'available'
        elif installed_version != source_role.version or installed_digest != source_digest:
            status = 'update_available'
        else:
            status = 'current'
        rows.append(
            {
                'role_id': role_id,
                'source': source_role.source,
                'version': source_role.version,
                'installed_version': installed_version,
                'digest': source_digest,
                'installed_digest': installed_digest,
                'status': status,
                'path': str(source_role.path),
                'name': source_role.name,
                'description': source_role.description,
                'duplicates': source_role.duplicates,
                'warning': _duplicate_warning(source_role),
            }
        )
    for role_id in sorted(installed - set(source_roles)):
        metadata = installed_role_metadata(role_id)
        rows.append(
            {
                'role_id': role_id,
                'source': str(metadata.get('source') or ''),
                'version': '',
                'installed_version': str(metadata.get('version') or ''),
                'digest': '',
                'installed_digest': str(metadata.get('digest') or ''),
                'status': 'installed_source_missing',
                'path': str(metadata.get('source_path') or ''),
                'name': '',
                'description': '',
            }
        )
    return tuple(rows)


def _iter_role_paths(source_root: Path, *, include_reference: bool = False) -> tuple[Path, ...]:
    root = Path(source_root).expanduser()
    candidates: list[Path] = []
    base_names = ('reference_roles', 'roles') if include_reference else ('roles',)
    for base_name in base_names:
        base = root / base_name
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir(), key=lambda item: item.name):
            if child.is_dir() and (child / 'role.toml').is_file():
                candidates.append(child)
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if child.is_dir() and (child / 'role.toml').is_file():
                candidates.append(child)
    if (root / 'role.toml').is_file():
        candidates.append(root)
    deduped: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        deduped.append(candidate)
        seen.add(resolved)
    return tuple(deduped)


def _same_source_reference_to_roles_upgrade(existing: SourceRole, candidate: SourceRole) -> bool:
    return (
        existing.source == candidate.source
        and _catalog_base_name(existing.path) == 'reference_roles'
        and _catalog_base_name(candidate.path) == 'roles'
    )


def _catalog_base_name(role_path: Path) -> str:
    for parent in (Path(role_path), *Path(role_path).parents):
        if parent.name in {'roles', 'reference_roles'}:
            return parent.name
    return ''


def _duplicate_warning(source_role: SourceRole) -> str:
    if not source_role.duplicates:
        return ''
    return 'duplicate_source_roles: kept ' + f'{source_role.source}:{source_role.path}; ignored ' + ', '.join(source_role.duplicates)


def _include_reference_roles_default() -> bool:
    value = str(os.environ.get('CCB_AGENT_ROLES_INCLUDE_REFERENCE') or '').strip().lower()
    return value in {'1', 'true', 'yes', 'on'}


def _source_checkout_draft_roles_enabled() -> bool:
    if os.environ.get('CCB_TEST_ENTRYPOINT') == '1':
        return True
    value = str(os.environ.get('CCB_INCLUDE_SOURCE_DRAFT_ROLEPACKS') or '').strip().lower()
    return value in {'1', 'true', 'yes', 'on'}


def _looks_like_agent_roles_spec(path: Path) -> bool:
    root = Path(path).expanduser()
    return (root / 'roles').is_dir() or (root / 'reference_roles').is_dir()


def _looks_like_role_source(path: Path) -> bool:
    root = Path(path).expanduser()
    if (root / 'role.toml').is_file():
        return True
    if (root / 'roles').is_dir() or (root / 'reference_roles').is_dir():
        return True
    if not root.is_dir():
        return False
    try:
        return any(child.is_dir() and (child / 'role.toml').is_file() for child in root.iterdir())
    except OSError:
        return False


def _ensure_remote_agent_roles_source(
    *,
    refresh: bool = False,
    download_if_missing: bool = True,
) -> Path | None:
    if _remote_agent_roles_disabled():
        return None
    target = _remote_agent_roles_cache_path()
    if _looks_like_agent_roles_spec(target):
        if refresh:
            refreshed = _refresh_remote_agent_roles_source(target)
            if refreshed is not None:
                return refreshed.resolve()
        return target.resolve()
    if target.exists():
        return None
    if not download_if_missing:
        return None
    return _clone_or_download_remote_agent_roles_source(target)


def _clone_or_download_remote_agent_roles_source(target: Path) -> Path | None:
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = ['git', 'clone', '--depth', '1', _remote_agent_roles_git_url(), str(target)]
    try:
        result = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_remote_agent_roles_git_timeout(),
        )
    except Exception:
        return _download_remote_agent_roles_source_archive(target)
    if result.returncode != 0 or not _looks_like_agent_roles_spec(target):
        shutil.rmtree(target, ignore_errors=True)
        return _download_remote_agent_roles_source_archive(target)
    return target.resolve()


def _download_remote_agent_roles_source_archive(target: Path) -> Path | None:
    if target.exists():
        return target.resolve() if _looks_like_agent_roles_spec(target) else None
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=f'{target.name}-', dir=str(target.parent)) as temp:
            temp_dir = Path(temp)
            archive_path = temp_dir / 'agent-roles-spec.zip'
            with urllib.request.urlopen(
                _remote_agent_roles_archive_url(),
                timeout=_remote_agent_roles_git_timeout(),
            ) as response:
                archive_path.write_bytes(response.read())
            extract_dir = temp_dir / 'extract'
            extract_dir.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(extract_dir)
            source_root = _find_extracted_agent_roles_source(extract_dir)
            if source_root is None:
                return None
            shutil.move(str(source_root), str(target))
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        return None
    return target.resolve() if _looks_like_agent_roles_spec(target) else None


def _find_extracted_agent_roles_source(root: Path) -> Path | None:
    if _looks_like_agent_roles_spec(root):
        return root
    try:
        children = tuple(root.iterdir())
    except OSError:
        return None
    for child in children:
        if child.is_dir() and _looks_like_agent_roles_spec(child):
            return child
    return None


def _refresh_git_agent_roles_source(target: Path) -> Path | None:
    if not (target / '.git').is_dir():
        return None
    cmd = ['git', '-C', str(target), 'pull', '--ff-only']
    try:
        subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_remote_agent_roles_git_timeout(),
        )
    except Exception:
        return None
    return target.resolve() if _looks_like_agent_roles_spec(target) else None


def _refresh_remote_agent_roles_source(target: Path) -> Path | None:
    refreshed = _refresh_git_agent_roles_source(target)
    if refreshed is not None:
        return refreshed
    return _replace_remote_agent_roles_source(target)


def _replace_remote_agent_roles_source(target: Path) -> Path | None:
    if (target / '.git').is_dir():
        return target.resolve() if _looks_like_agent_roles_spec(target) else None
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=f'{target.name}-refresh-', dir=str(parent)) as temp:
            replacement = Path(temp) / target.name
            refreshed = _clone_or_download_remote_agent_roles_source(replacement)
            if refreshed is None or not _looks_like_agent_roles_spec(replacement):
                return None
            backup = parent / f'.{target.name}.previous'
            shutil.rmtree(backup, ignore_errors=True)
            try:
                target.rename(backup)
            except OSError:
                return None
            try:
                shutil.move(str(replacement), str(target))
            except Exception:
                if not target.exists() and backup.exists():
                    backup.rename(target)
                return None
            shutil.rmtree(backup, ignore_errors=True)
            return target.resolve() if _looks_like_agent_roles_spec(target) else None
    except Exception:
        return None


def _remote_agent_roles_cache_path() -> Path:
    return _user_cache_home() / 'ccb' / 'role-catalogs' / 'agent-roles-spec'


def _remote_agent_roles_git_url() -> str:
    for env_name in ('CCB_AGENT_ROLES_SPEC_GIT_URL', 'AGENT_ROLES_SPEC_GIT_URL'):
        value = str(os.environ.get(env_name) or '').strip()
        if value:
            return value
    return DEFAULT_AGENT_ROLES_SPEC_GIT_URL


def _remote_agent_roles_archive_url() -> str:
    for env_name in ('CCB_AGENT_ROLES_SPEC_ARCHIVE_URL', 'AGENT_ROLES_SPEC_ARCHIVE_URL'):
        value = str(os.environ.get(env_name) or '').strip()
        if value:
            return value
    git_url = _remote_agent_roles_git_url().rstrip('/')
    if git_url.endswith('.git'):
        git_url = git_url[:-4]
    if git_url.startswith('https://github.com/'):
        return f'{git_url}/archive/refs/heads/main.zip'
    return DEFAULT_AGENT_ROLES_SPEC_ARCHIVE_URL


def _remote_agent_roles_disabled() -> bool:
    value = str(
        os.environ.get('CCB_AGENT_ROLES_SPEC_NO_REMOTE')
        or os.environ.get('CCB_AGENT_ROLES_NO_REMOTE')
        or ''
    ).strip().lower()
    return value in {'1', 'true', 'yes', 'on'}


def _remote_agent_roles_git_timeout() -> float:
    raw = str(os.environ.get('CCB_AGENT_ROLES_GIT_TIMEOUT_SECONDS') or '60').strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 60.0


def _user_cache_home() -> Path:
    xdg_cache_home = str(os.environ.get('XDG_CACHE_HOME') or '').strip()
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser()
    return Path.home() / '.cache'


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob('*')):
        rel = path.relative_to(root)
        digest.update(str(rel).encode('utf-8'))
        digest.update(b'\0')
        if path.is_file():
            digest.update(path.read_bytes())
        elif path.is_symlink():
            digest.update(str(path.readlink()).encode('utf-8'))
        digest.update(b'\0')
    return digest.hexdigest()


def _write_sources(sources: tuple[dict[str, object], ...]) -> None:
    payload = {'schema': SOURCE_REGISTRY_SCHEMA, 'sources': list(sources)}
    atomic_write_text(source_registry_path(), json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + '\n')


def _normalize_source_name(value: str) -> str:
    name = str(value or '').strip().lower()
    allowed = set('abcdefghijklmnopqrstuvwxyz0123456789._-')
    if not name or any(ch not in allowed for ch in name):
        raise ValueError(f'invalid role source name: {value!r}')
    return name


__all__ = [
    'RoleSource',
    'SourceRole',
    'DEFAULT_AGENT_ROLES_SPEC_GIT_URL',
    'SOURCE_CHECKOUT_DRAFT_ROLE_SOURCE_NAME',
    'SYSTEM_ROLE_SOURCE_NAMES',
    'add_role_source',
    'default_agent_roles_source',
    'discover_path_roles',
    'discover_source_roles',
    'discover_system_source_roles',
    'find_source_role',
    'find_system_source_role',
    'installed_role_ids',
    'installed_role_metadata',
    'load_role_sources',
    'remove_role_source',
    'role_catalog_status',
    'source_registry_path',
    'source_checkout_draft_role_source',
    'system_role_sources',
    'tree_digest',
]
