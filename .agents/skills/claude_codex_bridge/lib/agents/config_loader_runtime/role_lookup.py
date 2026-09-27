from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

from runtime_env.source_home import current_provider_source_home
from role_aliases import canonical_role_id, role_id_candidates


class RoleLookupError(ValueError):
    pass


def role_store_root() -> Path:
    data_home = os.environ.get('XDG_DATA_HOME')
    base = Path(data_home).expanduser() if data_home else Path.home() / '.local' / 'share'
    return base / 'ccb' / 'roles'


def agent_roles_store_root() -> Path:
    value = str(os.environ.get('AGENT_ROLES_STORE') or '').strip()
    if value:
        return Path(value).expanduser()
    return current_provider_source_home() / '.roles'


def agent_roles_installed_root() -> Path:
    return agent_roles_store_root() / 'installed'


def project_role_store_roots(project_root: Path | None) -> tuple[Path, ...]:
    if project_root is None:
        return ()
    root = Path(project_root)
    candidates = (
        root / 'roles' / 'installed',
        root / '.ccb' / 'roles' / 'installed',
    )
    return tuple(candidate for candidate in candidates if candidate.exists())


def role_store_roots(project_root: Path | None = None) -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[str] = set()
    for root in (*project_role_store_roots(project_root), agent_roles_installed_root()):
        key = str(root.expanduser())
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return tuple(roots)


def normalize_role_id(value: str) -> str:
    role_id = str(value or '').strip().lower()
    if not role_id or '.' not in role_id:
        raise RoleLookupError('role id must use publisher.role form, for example agentroles.archi')
    allowed = set('abcdefghijklmnopqrstuvwxyz0123456789._-')
    if any(ch not in allowed for ch in role_id):
        raise RoleLookupError(f'invalid role id: {value!r}')
    return canonical_role_id(role_id)


def looks_like_role_id(value: str) -> bool:
    try:
        normalize_role_id(value)
        return True
    except RoleLookupError:
        return False


def load_installed_role_manifest(role_id: str, *, project_root: Path | None = None) -> tuple[Path, dict[str, Any]]:
    role_id = normalize_role_id(role_id)
    role_root = None
    root = None
    roots = role_store_roots(project_root)
    for store_root in roots:
        for candidate_id in role_id_candidates(role_id):
            candidate_root = store_root / candidate_id
            current = candidate_root / 'current'
            if current.exists():
                role_root = current.resolve()
                root = candidate_root
                break
            if (candidate_root / 'role.toml').is_file():
                role_root = candidate_root
                root = candidate_root
                break
        if role_root is not None:
            break
    if role_root is None or root is None:
        root = roots[0] / role_id
        role_root = root
    manifest_path = role_root / 'role.toml'
    if not manifest_path.exists():
        raise RoleLookupError(
            f'role {role_id} is not installed in role store {roots[0]}; '
            f'run `ccb roles install {role_id}`'
        )
    if not manifest_path.is_file():
        raise RoleLookupError(f'role {role_id} has invalid manifest path: {manifest_path}')
    manifest = _load_toml(manifest_path)
    actual_id = normalize_role_id(str(manifest.get('id') or ''))
    if actual_id != role_id:
        raise RoleLookupError(f'role {role_id} manifest id mismatch: {actual_id}')
    return role_root, manifest


def installed_role_default_agent_name(role_id: str, *, project_root: Path | None = None) -> str:
    role_id = normalize_role_id(role_id)
    root, manifest = load_installed_role_manifest(role_id, project_root=project_root)
    return _default_agent_name_from_manifest(role_id, root, manifest)


def _default_agent_name_from_manifest(role_id: str, root: Path, manifest: dict[str, Any]) -> str:
    identity = manifest.get('identity') or {}
    if not isinstance(identity, dict):
        raise RoleLookupError(f'role {role_id} identity must be a table')
    default_name = str(
        identity.get('default_agent_name')
        or identity.get('default_name')
        or _ccb_adapter_default_agent_name(root)
        or role_id.rsplit('.', 1)[-1]
    ).strip()
    if not default_name:
        raise RoleLookupError(f'role {role_id} identity.default_agent_name cannot be empty')
    return default_name


def _ccb_adapter_default_agent_name(role_root: Path) -> str | None:
    adapter_path = Path(role_root) / 'adapters' / 'ccb' / 'adapter.toml'
    if not adapter_path.is_file():
        return None
    try:
        adapter = _load_toml(adapter_path)
    except RoleLookupError:
        return None
    default_name = str(adapter.get('default_agent_name') or '').strip()
    return default_name or None


def _load_toml(path: Path) -> dict[str, Any]:
    for module_name in ('tomllib', 'tomli', 'toml'):
        try:
            module = importlib.import_module(module_name)
            break
        except ModuleNotFoundError:
            module = None
    if module is None:
        raise RoleLookupError('TOML parsing requires Python 3.11+ or tomli/toml')
    try:
        payload = module.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise RoleLookupError(f'invalid role manifest {path}: {exc}') from exc
    if not isinstance(payload, dict):
        raise RoleLookupError(f'role manifest must decode to a table: {path}')
    return dict(payload)


__all__ = [
    'RoleLookupError',
    'agent_roles_installed_root',
    'agent_roles_store_root',
    'installed_role_default_agent_name',
    'load_installed_role_manifest',
    'project_role_store_roots',
    'looks_like_role_id',
    'normalize_role_id',
    'role_store_root',
    'role_store_roots',
]
