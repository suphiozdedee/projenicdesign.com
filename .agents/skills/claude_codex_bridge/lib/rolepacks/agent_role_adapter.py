from __future__ import annotations

from pathlib import Path
from typing import Any


AGENT_ROLE_SCHEMA_PREFIX = 'agent-role/preview-'
CCB_ADAPTER_SCHEMA_PREFIX = 'agent-role-adapter/ccb-preview-'


def is_agent_role_manifest(manifest: dict[str, Any]) -> bool:
    return str(manifest.get('schema') or '').strip().startswith(AGENT_ROLE_SCHEMA_PREFIX)


def translate_agent_role_manifest(
    root: Path,
    manifest: dict[str, Any],
    *,
    read_toml,
) -> dict[str, Any]:
    """Translate host-neutral AGENT-ROLE preview metadata into CCB's runtime shape."""
    translated = dict(manifest)
    translated['schema'] = 'rolepack/v1'

    identity = _table(translated, 'identity')
    contents = _table(translated, 'contents')
    adapter = _load_ccb_adapter(root, read_toml=read_toml)

    translated['identity'] = _translate_identity(identity, adapter=adapter)
    translated['compatibility'] = _translate_compatibility(adapter=adapter)
    translated['memory'] = _translate_memory(contents, adapter=adapter)
    translated['skills'] = _translate_skills(contents, adapter=adapter)
    translated['tools'] = _translate_tools(adapter=adapter)
    translated['permissions'] = _translate_permissions(translated, adapter=adapter)
    translated['activation'] = _translate_activation(translated, adapter=adapter)
    translated['adapters'] = _translate_adapters(translated, adapter=adapter)
    translated['source_schema'] = str(manifest.get('schema') or '')
    return translated


def _load_ccb_adapter(root: Path, *, read_toml) -> dict[str, Any]:
    path = Path(root) / 'adapters' / 'ccb' / 'adapter.toml'
    if not path.is_file():
        return {}
    adapter = read_toml(path)
    schema = str(adapter.get('schema') or '').strip()
    if schema and not schema.startswith(CCB_ADAPTER_SCHEMA_PREFIX):
        return {}
    return dict(adapter)


def _translate_identity(identity: dict[str, Any], *, adapter: dict[str, Any]) -> dict[str, Any]:
    translated = dict(identity)
    default_name = (
        adapter.get('default_agent_name')
        or identity.get('default_agent_name')
        or identity.get('default_name')
    )
    if default_name:
        translated['default_agent_name'] = str(default_name).strip()
    return translated


def _translate_compatibility(*, adapter: dict[str, Any]) -> dict[str, Any]:
    providers = _string_list(adapter.get('supported_providers'))
    if not providers:
        recommended = str(adapter.get('recommended_provider') or '').strip()
        providers = [recommended] if recommended else []
    compatibility: dict[str, Any] = {'hosts': ['ccb']}
    if providers:
        compatibility['providers'] = providers
    return compatibility


def _translate_memory(contents: dict[str, Any], *, adapter: dict[str, Any]) -> dict[str, Any]:
    files = _relative_paths(contents.get('memory'))
    files.extend(_relative_paths(adapter.get('memory')))
    merge_strategy = str(adapter.get('memory_merge_strategy') or 'append_after_project_memory').strip()
    return {'files': files, 'merge_strategy': merge_strategy}


def _translate_skills(contents: dict[str, Any], *, adapter: dict[str, Any]) -> dict[str, Any]:
    skills = _relative_paths(contents.get('skills'))
    skills.extend(_relative_paths(adapter.get('skills')))
    providers = _string_list(adapter.get('supported_providers'))
    if not providers:
        recommended = str(adapter.get('recommended_provider') or '').strip()
        providers = [recommended] if recommended else ['codex', 'claude']
    return {provider: list(skills) for provider in providers if provider}


def _translate_tools(*, adapter: dict[str, Any]) -> dict[str, Any]:
    tools = adapter.get('tools') or {}
    return dict(tools) if isinstance(tools, dict) else {}


def _translate_permissions(manifest: dict[str, Any], *, adapter: dict[str, Any]) -> dict[str, Any]:
    permissions = dict(manifest.get('permissions') or {})
    default = str(adapter.get('permission_default') or '').strip()
    if default:
        permissions.setdefault('default', default)
    return permissions


def _translate_activation(manifest: dict[str, Any], *, adapter: dict[str, Any]) -> dict[str, Any]:
    activation = dict(manifest.get('activation') or {})
    workspace_mode = str(adapter.get('recommended_workspace_mode') or '').strip()
    if workspace_mode:
        activation.setdefault('recommended_workspace_mode', workspace_mode)
    return activation


def _translate_adapters(manifest: dict[str, Any], *, adapter: dict[str, Any]) -> dict[str, Any]:
    adapters = dict(manifest.get('adapters') or {})
    ccb = dict(adapters.get('ccb') or {})
    for key in ('display_name', 'command_surface'):
        value = str(adapter.get(key) or '').strip()
        if value:
            ccb[key] = value
    if ccb:
        adapters['ccb'] = ccb
    return adapters


def _table(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key) or {}
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip().lower()
    return [text] if text else []


def _relative_paths(value: Any) -> list[str]:
    paths: list[str] = []
    for item in value if isinstance(value, (list, tuple)) else ([] if value is None else [value]):
        text = str(item).strip()
        if not text:
            continue
        path = Path(text)
        if path.is_absolute():
            continue
        paths.append(text)
    return paths


__all__ = ['is_agent_role_manifest', 'translate_agent_role_manifest']
