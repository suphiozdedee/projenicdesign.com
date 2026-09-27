from __future__ import annotations

import importlib
import os
from pathlib import Path

from agents.config_loader_runtime.role_lookup import RoleLookupError, installed_role_default_agent_name, looks_like_role_id, normalize_role_id
from agents.config_loader_runtime.dynamic_agent_overlays import apply_dynamic_agent_overlays
from agents.config_loader_runtime.loop_overlays import apply_loop_capacity_overlays
from agents.models import LayoutLeaf, LayoutNode, normalize_agent_name, parse_layout_spec

from ..common import (
    CONFIG_SOURCE_BUILTIN_DEFAULT,
    CONFIG_SOURCE_PROJECT,
    CONFIG_SOURCE_USER,
    ConfigLoadResult,
    ConfigValidationError,
)
from ..defaults import build_default_project_config
from ..parsing import validate_project_config
from ..paths import project_config_path, user_default_config_path

_ALLOWED_HYBRID_TOP_LEVEL_KEYS = {'agents', 'maintenance', 'loop', 'config_ui'}
_HYBRID_HEADER_OWNED_AGENT_KEYS = {'provider', 'workspace_mode'}


def _build_compact_agent_record(provider: str, *, workspace_mode: str) -> dict[str, str]:
    return {
        'provider': provider,
        'target': '.',
        'workspace_mode': workspace_mode,
        'restore': 'auto',
        'permission': 'manual',
    }


def _strip_layout_comments(line: str) -> str:
    return line.split('#', 1)[0].split('//', 1)[0].strip()


def _normalize_compact_layout_text(text: str) -> str:
    return '\n'.join(
        cleaned
        for cleaned in (_strip_layout_comments(line) for line in text.splitlines())
        if cleaned
    ).strip()


def _raise_invalid_compact_token(path: Path, token: str) -> None:
    raise ConfigValidationError(
        f"{path}: invalid token {token!r}; expected 'agent_name:provider' or 'cmd'"
    )


def _consume_compact_leaf(
    leaf,
    *,
    path: Path,
    project_root: Path | None,
    default_agents: list[str],
    agents: dict[str, dict[str, str]],
    cmd_enabled: bool,
) -> bool:
    token = leaf.name.strip()
    normalized_name = token.lower()
    if normalized_name == 'cmd':
        if leaf.provider is not None:
            raise ConfigValidationError(f"{path}: reserved token 'cmd' cannot declare a provider")
        if cmd_enabled:
            raise ConfigValidationError(f'{path}: compact config cannot define cmd more than once')
        return True
    if leaf.provider is None:
        _raise_invalid_compact_token(path, token)
    role_id = None
    if looks_like_role_id(token):
        role_id = normalize_role_id(token)
        try:
            token = normalize_agent_name(installed_role_default_agent_name(role_id, project_root=project_root))
        except RoleLookupError as exc:
            raise ConfigValidationError(f'{path}: {exc}') from exc
        except Exception as exc:
            raise ConfigValidationError(f'{path}: role {role_id} default agent name is invalid: {exc}') from exc
        normalized_name = token
    if normalized_name in agents:
        if role_id is not None:
            raise ConfigValidationError(
                f'{path}: role {role_id} resolves to existing agent {normalized_name!r}; '
                'use an explicit agent name with role binding'
            )
        raise ConfigValidationError(f'{path}: duplicate agent name in compact config: {token}')
    default_agents.append(token)
    record = _build_compact_agent_record(
        leaf.provider,
        workspace_mode='git-worktree' if str(leaf.workspace_mode or '').strip() == 'worktree' else 'inplace',
    )
    if role_id is not None:
        record['role'] = role_id
    agents[normalized_name] = record
    return cmd_enabled


def _parse_compact_config_document(text: str, *, path: Path, project_root: Path | None = None) -> dict[str, object]:
    layout_text = _normalize_compact_layout_text(text)
    if not layout_text:
        raise ConfigValidationError(f'{path}: config is empty')
    try:
        layout = parse_layout_spec(layout_text)
    except Exception as exc:
        raise ConfigValidationError(f'{path}: invalid compact layout: {exc}') from exc

    default_agents: list[str] = []
    agents: dict[str, dict[str, str]] = {}
    cmd_enabled = False
    for leaf in layout.iter_leaves():
        cmd_enabled = _consume_compact_leaf(
            leaf,
            path=path,
            project_root=project_root,
            default_agents=default_agents,
            agents=agents,
            cmd_enabled=cmd_enabled,
        )
    if not default_agents:
        raise ConfigValidationError(f'{path}: compact config must define at least one agent')

    return {
        'version': 2,
        'default_agents': default_agents,
        'agents': agents,
        'cmd_enabled': cmd_enabled,
        'layout': _expand_compact_role_layout(layout, project_root=project_root).render(),
    }


def _expand_compact_role_layout(node, *, project_root: Path | None):
    if node.kind == 'leaf':
        assert node.leaf is not None
        name = str(node.leaf.name or '').strip()
        if looks_like_role_id(name):
            role_id = normalize_role_id(name)
            try:
                name = normalize_agent_name(installed_role_default_agent_name(role_id, project_root=project_root))
            except RoleLookupError as exc:
                raise ConfigValidationError(str(exc)) from exc
            except Exception as exc:
                raise ConfigValidationError(f'role {role_id} default agent name is invalid: {exc}') from exc
        return LayoutNode(
            kind='leaf',
            leaf=LayoutLeaf(
                name=name,
                provider=node.leaf.provider,
                workspace_mode=node.leaf.workspace_mode,
                percent=node.leaf.percent,
            ),
        )
    assert node.left is not None
    assert node.right is not None
    return LayoutNode(
        kind=node.kind,
        left=_expand_compact_role_layout(node.left, project_root=project_root),
        right=_expand_compact_role_layout(node.right, project_root=project_root),
    )


def _classify_config_document(text: str) -> tuple[str, str, str | None]:
    lines = text.splitlines()
    first_meaningful_kind: str | None = None
    first_rich_index: int | None = None
    for index, line in enumerate(lines):
        body = line.split('#', 1)[0].strip()
        if not body:
            continue
        kind = 'rich' if body.startswith('[') or '=' in body else 'compact'
        if first_meaningful_kind is None:
            first_meaningful_kind = kind
        if kind == 'rich':
            first_rich_index = index
            break

    if first_meaningful_kind == 'rich':
        return 'rich', text, None
    if first_meaningful_kind == 'compact' and first_rich_index is None:
        return 'compact', text, None
    if first_meaningful_kind == 'compact' and first_rich_index is not None:
        compact_text = '\n'.join(lines[:first_rich_index])
        overlay_text = '\n'.join(lines[first_rich_index:])
        return 'hybrid', compact_text, overlay_text
    return 'compact', text, None


def _import_optional_toml_reader():
    for module_name in ('tomllib', 'tomli', 'toml'):
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
    return None


def _load_toml_reader(path: Path):
    reader = _import_optional_toml_reader()
    if reader is None:
        raise ConfigValidationError(
            f'{path}: rich TOML config requires Python 3.11+ or an installed tomli/toml parser'
        )
    loads = getattr(reader, 'loads', None)
    if not callable(loads):  # pragma: no cover - defensive guard for unexpected parser shims
        raise ConfigValidationError(f'{path}: TOML parser does not expose a supported loads() entrypoint')
    return loads


def _parse_toml_config_document(text: str, *, path: Path) -> dict[str, object]:
    try:
        document = _load_toml_reader(path)(text)
    except Exception as exc:
        if isinstance(exc, ConfigValidationError):
            raise
        raise ConfigValidationError(f'{path}: invalid TOML config: {exc}') from exc
    if not isinstance(document, dict):
        raise ConfigValidationError(f'{path}: TOML config must decode to a table/object')
    return dict(document)


def _parse_hybrid_config_document(
    text: str,
    overlay_text: str,
    *,
    path: Path,
    project_root: Path | None = None,
) -> dict[str, object]:
    base_document = _parse_compact_config_document(text, path=path, project_root=project_root)
    overlay_document = _parse_toml_config_document(overlay_text, path=path)
    return _merge_hybrid_overlay(base_document, overlay_document, path=path)


def _merge_hybrid_overlay(
    base_document: dict[str, object],
    overlay_document: dict[str, object],
    *,
    path: Path,
) -> dict[str, object]:
    unknown_top = sorted(set(overlay_document) - _ALLOWED_HYBRID_TOP_LEVEL_KEYS)
    if unknown_top:
        raise ConfigValidationError(
            f'{path}: hybrid overlay contains unsupported top-level fields: {", ".join(unknown_top)}'
        )

    merged_agents = {
        str(name): dict(spec)
        for name, spec in dict(base_document.get('agents') or {}).items()
    }
    raw_overlay_agents = overlay_document.get('agents') or {}
    if not isinstance(raw_overlay_agents, dict):
        raise ConfigValidationError(f'{path}: hybrid overlay agents must be a table/object')

    for raw_name, raw_spec in raw_overlay_agents.items():
        if not isinstance(raw_name, str):
            raise ConfigValidationError(f'{path}: hybrid overlay agent names must be strings')
        normalized_name = normalize_agent_name(raw_name)
        if normalized_name not in merged_agents:
            raise ConfigValidationError(
                f'{path}: hybrid overlay cannot define agent {normalized_name!r} outside the compact layout'
            )
        if not isinstance(raw_spec, dict):
            raise ConfigValidationError(f'{path}: agents.{raw_name} must be a table/object')
        forbidden = sorted(set(raw_spec) & _HYBRID_HEADER_OWNED_AGENT_KEYS)
        if forbidden:
            raise ConfigValidationError(
                f'{path}: hybrid overlay cannot redefine compact-header fields for agents.{normalized_name}: '
                + ', '.join(forbidden)
            )
        merged_agents[normalized_name] = _deep_merge_dicts(merged_agents[normalized_name], dict(raw_spec))

    merged = {
        **dict(base_document),
        'agents': merged_agents,
    }
    if 'maintenance' in overlay_document:
        merged['maintenance'] = overlay_document['maintenance']
    if 'loop' in overlay_document:
        merged['loop'] = overlay_document['loop']
    if 'config_ui' in overlay_document:
        merged['config_ui'] = overlay_document['config_ui']
    return merged


def _deep_merge_dicts(base: dict[str, object], overlay: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(dict(merged[key]), dict(value))
        else:
            merged[key] = value
    return merged


def _load_config_document(path: Path, *, project_root: Path | None = None) -> dict[str, object]:
    text = path.read_text(encoding='utf-8')
    return parse_config_document_text(text, path=path, project_root=project_root)


def parse_config_document_text(
    text: str,
    *,
    path: Path,
    project_root: Path | None = None,
) -> dict[str, object]:
    """Parse config text with the same compact/rich/hybrid rules as disk loading."""
    kind, primary_text, overlay_text = _classify_config_document(text)
    if kind == 'rich':
        return _parse_toml_config_document(primary_text, path=path)
    if kind == 'hybrid':
        assert overlay_text is not None
        return _parse_hybrid_config_document(primary_text, overlay_text, path=path, project_root=project_root)
    return _parse_compact_config_document(primary_text, path=path, project_root=project_root)


def load_project_config(project_root: Path, *, include_loop_overlays: bool = True) -> ConfigLoadResult:
    project_path = project_config_path(project_root)
    if project_path.exists():
        config = validate_project_config(
            _load_config_document(project_path, project_root=Path(project_root).expanduser().resolve()),
            source_path=project_path,
            project_root=project_root,
        )
        if include_loop_overlays:
            config = apply_loop_capacity_overlays(config, project_root)
            config = apply_dynamic_agent_overlays(config, project_root)
        _propagate_runtime_mux_backend(config)
        return ConfigLoadResult(
            config=config,
            source_path=project_path,
            source_kind=CONFIG_SOURCE_PROJECT,
            used_default=False,
        )
    user_default_path = user_default_config_path()
    if user_default_path.exists():
        config = validate_project_config(_load_config_document(user_default_path), source_path=user_default_path)
        _propagate_runtime_mux_backend(config)
        return ConfigLoadResult(
            config=config,
            source_path=user_default_path,
            source_kind=CONFIG_SOURCE_USER,
            used_default=False,
        )
    _propagate_runtime_mux_backend(None)
    return ConfigLoadResult(
        config=build_default_project_config(),
        source_path=None,
        source_kind=CONFIG_SOURCE_BUILTIN_DEFAULT,
        used_default=True,
    )


def _propagate_runtime_mux_backend(config) -> None:
    """将 config ``runtime.mux.backend`` 传播到 backend 选择层。

    design D2：config 为声明式单一事实源。
    两条传播路径：
    1. ``os.environ['CCB_RUNTIME_MUX_BACKEND']`` — 通过 env 桥梁驱动 ``get_backend()``
    2. ``set_backend_config_preference()`` — 直接设置模块级偏好，
       在 ``get_backend()`` 优先级链中高于 env var（config 显式 > env 检测）
    """
    from terminal_runtime.api import set_backend_config_preference

    backend = getattr(config, 'runtime_mux_backend', None) if config is not None else None
    key = 'CCB_RUNTIME_MUX_BACKEND'
    if backend and str(backend).strip():
        value = str(backend).strip().lower()
        os.environ[key] = value
        set_backend_config_preference(value)
    else:
        os.environ.pop(key, None)
        set_backend_config_preference(None)


__all__ = ['load_project_config', 'parse_config_document_text']
