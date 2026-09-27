from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path
from typing import Any

from role_aliases import canonical_role_id

from .agent_role_adapter import is_agent_role_manifest, translate_agent_role_manifest


SUPPORTED_ROLE_SCHEMA = 'rolepack/v1'


class RoleManifestError(ValueError):
    pass


@dataclass(frozen=True)
class RoleManifest:
    id: str
    name: str
    version: str
    description: str
    root: Path
    manifest: dict[str, Any]

    @property
    def default_agent_name(self) -> str:
        identity = self.table('identity')
        return str(identity.get('default_agent_name') or self.id.rsplit('.', 1)[-1]).strip()

    @property
    def providers(self) -> tuple[str, ...]:
        compatibility = self.table('compatibility')
        return tuple(
            str(item).strip().lower()
            for item in compatibility.get('providers', ())
            if str(item).strip()
        )

    def table(self, key: str) -> dict[str, Any]:
        value = self.manifest.get(key) or {}
        if not isinstance(value, dict):
            raise RoleManifestError(f'{self.root}: role manifest {key} must be a table')
        return dict(value)

    def to_summary(self) -> dict[str, object]:
        return {
            'id': self.id,
            'name': self.name,
            'version': self.version,
            'description': self.description,
            'default_agent_name': self.default_agent_name,
            'providers': ','.join(self.providers),
            'root': str(self.root),
        }


def normalize_role_id(value: str) -> str:
    role_id = str(value or '').strip().lower()
    if not role_id or '.' not in role_id:
        raise RoleManifestError('role id must use publisher.role form, for example agentroles.archi')
    allowed = set('abcdefghijklmnopqrstuvwxyz0123456789._-')
    if any(ch not in allowed for ch in role_id):
        raise RoleManifestError(f'invalid role id: {value!r}')
    return canonical_role_id(role_id)


def load_role_manifest(path: Path) -> RoleManifest:
    root = Path(path).expanduser()
    manifest_path = root / 'role.toml'
    if not manifest_path.is_file():
        raise RoleManifestError(f'role manifest not found: {manifest_path}')
    return role_manifest_from_mapping(root, read_toml_manifest(manifest_path))


def role_manifest_from_mapping(root: Path, manifest: dict[str, Any]) -> RoleManifest:
    schema = str(manifest.get('schema') or '').strip()
    if is_agent_role_manifest(manifest):
        manifest = translate_agent_role_manifest(root, manifest, read_toml=read_toml_manifest)
        schema = str(manifest.get('schema') or '').strip()
    if schema != SUPPORTED_ROLE_SCHEMA:
        raise RoleManifestError(f'{root}: unsupported role schema: {schema!r}')
    role_id = normalize_role_id(str(manifest.get('id') or ''))
    name = str(manifest.get('name') or '').strip()
    version = str(manifest.get('version') or '').strip()
    description = str(manifest.get('description') or '').strip()
    if not name or not version or not description:
        raise RoleManifestError(f'{root}: role manifest requires name, version, and description')
    return RoleManifest(
        id=role_id,
        name=name,
        version=version,
        description=description,
        root=root,
        manifest=manifest,
    )


def read_toml_manifest(path: Path) -> dict[str, Any]:
    for module_name in ('tomllib', 'tomli', 'toml'):
        try:
            module = importlib.import_module(module_name)
            break
        except ModuleNotFoundError:
            module = None
    if module is None:
        raise RoleManifestError('TOML parsing requires Python 3.11+ or tomli/toml')
    text = Path(path).read_text(encoding='utf-8')
    try:
        payload = module.loads(text)
    except Exception as exc:
        raise RoleManifestError(f'invalid role manifest {path}: {exc}') from exc
    if not isinstance(payload, dict):
        raise RoleManifestError(f'role manifest must decode to a table: {path}')
    return dict(payload)


__all__ = [
    'RoleManifest',
    'RoleManifestError',
    'SUPPORTED_ROLE_SCHEMA',
    'load_role_manifest',
    'normalize_role_id',
    'read_toml_manifest',
    'role_manifest_from_mapping',
]
