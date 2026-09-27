from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any

from agents.config_loader_runtime.role_lookup import RoleLookupError, installed_role_default_agent_name, looks_like_role_id, normalize_role_id
from agents.models import (
    AgentValidationError,
    LayoutLeaf,
    LayoutNode,
    MaintenanceHeartbeatConfig,
    ProjectConfig,
    normalize_agent_name,
    parse_layout_spec,
)

from ..common import (
    ALLOWED_TOP_LEVEL_KEYS,
    CONFIG_FILENAME,
    ConfigValidationError,
    StructuredConfigValidationError,
)
from .agent_specs import parse_agents
from .expectations import expect_bool, expect_mapping, expect_string, expect_string_list
from .loop_capacity import parse_loop_capacity
from .topology import agents_from_topology_windows, parse_sidebar, parse_sidebar_view, parse_tool_windows, parse_topology_windows

_MAINTENANCE_TOP_LEVEL_KEYS = {'heartbeat'}
_CONFIG_UI_KEYS = {'port', 'token_env', 'token_file'}
_ENVIRONMENT_VARIABLE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_MAINTENANCE_HEARTBEAT_KEYS = {
    'enabled',
    'assessor',
    'interval_s',
    'min_interval_s',
    'unknown_streak_cap',
    'escalation_policy',
    'startup_ensure',
}
# runtime.mux.backend —— 原生 Windows 后端声明（herdr/rmux），驱动 backend 选择与 launch_mode
_RUNTIME_TOP_LEVEL_KEYS = {'mux'}
_RUNTIME_MUX_KEYS = {'backend'}
_ALLOWED_MUX_BACKENDS = {'herdr', 'rmux'}


def validate_project_config(
    document: dict[str, Any],
    *,
    source_path: Path | None = None,
    project_root: Path | None = None,
) -> ProjectConfig:
    resolved_project_root = (
        Path(project_root).expanduser().resolve() if project_root is not None else _project_root_from_source_path(source_path)
    )
    version = document.get('version')
    if version == 3:
        from .workflow_v3 import validate_v3_project_config

        try:
            return validate_v3_project_config(
                document,
                source_path=source_path,
                project_root=resolved_project_root,
                maintenance_heartbeat=_parse_maintenance_heartbeat(document),
            )
        except StructuredConfigValidationError:
            raise
        except (ConfigValidationError, AgentValidationError, ValueError) as exc:
            raise StructuredConfigValidationError(
                code='v3_validation_invalid',
                path='config',
                message=str(exc),
            ) from exc
    if version != 2:
        raise ConfigValidationError('version must be 2 or 3')
    document = _expand_role_id_shorthand(
        document,
        project_root=resolved_project_root,
    )
    _validate_document_shape(document, source_path=source_path)
    runtime_mux_backend = _parse_runtime_mux_backend(document)
    validate_config_ui_settings(document.get('config_ui'))
    windows = parse_topology_windows(document.get('windows'))
    tool_windows = parse_tool_windows(document.get('tool_windows'))
    _validate_topology_presence(document, windows=windows, tool_windows=tool_windows)
    if windows is not None:
        default_agents = _parse_topology_default_agents(document, windows=windows)
        parsed_agents = agents_from_topology_windows(windows, raw_agents=document.get('agents', {}))
    else:
        parsed_agents = parse_agents(document.get('agents'))
        default_agents = _parse_default_agents(document)
    cmd_enabled = _parse_cmd_enabled(document)
    layout_spec = _parse_layout_spec(document)
    sidebar = parse_sidebar(document.get('ui'))
    sidebar_view = parse_sidebar_view(document.get('ui'))
    maintenance_heartbeat = _parse_maintenance_heartbeat(document)
    loop_capacity = parse_loop_capacity(document.get('loop'), project_root=resolved_project_root)
    entry_window = _parse_entry_window(document)
    _validate_legacy_and_windows_fields(document, windows=windows, tool_windows=tool_windows)
    return _build_project_config(
        default_agents=default_agents,
        parsed_agents=parsed_agents,
        cmd_enabled=cmd_enabled,
        layout_spec=layout_spec,
        windows=windows,
        tool_windows=tool_windows,
        entry_window=entry_window,
        sidebar=sidebar,
        sidebar_view=sidebar_view,
        maintenance_heartbeat=maintenance_heartbeat,
        loop_capacity=loop_capacity,
        runtime_mux_backend=runtime_mux_backend,
        source_path=source_path,
    )


def _validate_document_shape(document: dict[str, Any], *, source_path: Path | None = None) -> None:
    unknown_top = sorted(set(document) - ALLOWED_TOP_LEVEL_KEYS)
    if unknown_top:
        location = f' at {source_path}' if source_path is not None else ''
        raise ConfigValidationError(
            f'config contains unknown top-level fields: {", ".join(unknown_top)}{location}'
        )
    if document.get('version') != 2:
        raise ConfigValidationError('version must be 2')


def _parse_runtime_mux_backend(document: dict[str, Any]) -> str | None:
    """解析 v2 顶层 `runtime.mux.backend`（原生 Windows 后端声明）。

    白名单最小面：`runtime` 只接受 `mux`，`mux` 只接受 `backend`，其余 fail-closed。
    backend ∈ {herdr, rmux}；rmux 后端未实现，fail-closed 提示。
    """
    raw_runtime = document.get('runtime')
    if raw_runtime is None:
        return None
    runtime = expect_mapping(raw_runtime, field_name='runtime')
    unknown = sorted(set(runtime) - _RUNTIME_TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigValidationError(f'runtime contains unknown fields: {", ".join(unknown)}')
    raw_mux = runtime.get('mux')
    if raw_mux is None:
        return None
    mux = expect_mapping(raw_mux, field_name='runtime.mux')
    unknown_mux = sorted(set(mux) - _RUNTIME_MUX_KEYS)
    if unknown_mux:
        raise ConfigValidationError(
            f'runtime.mux contains unknown fields: {", ".join(unknown_mux)}'
        )
    backend = mux.get('backend')
    if backend is None:
        return None
    if not isinstance(backend, str) or backend not in _ALLOWED_MUX_BACKENDS:
        raise ConfigValidationError('runtime.mux.backend must be one of: herdr, rmux')
    if backend == 'rmux':
        # rmux backend 未实现（独立 epic）：schema 保留类型预留，但 fail-closed 提示
        raise ConfigValidationError(
            "runtime.mux.backend='rmux' requires rmux backend which is not implemented yet"
        )
    return backend


def validate_config_ui_settings(value: object) -> dict[str, object]:
    if value is None:
        return {}
    settings = expect_mapping(value, field_name='config_ui')
    unknown = sorted(set(settings) - _CONFIG_UI_KEYS)
    if unknown:
        raise ConfigValidationError(
            f'config_ui contains unknown fields: {", ".join(unknown)}'
        )
    port = settings.get('port')
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535):
        raise ConfigValidationError('config_ui.port must be an integer between 0 and 65535')
    for key in ('token_env', 'token_file'):
        if key in settings:
            value = expect_string(settings[key], field_name=f'config_ui.{key}').strip()
            if not value:
                raise ConfigValidationError(f'config_ui.{key} must not be empty')
            if key == 'token_env' and not _ENVIRONMENT_VARIABLE_NAME.fullmatch(value):
                raise ConfigValidationError('config_ui.token_env must be a valid environment variable name')
    if 'token_env' in settings and 'token_file' in settings:
        raise ConfigValidationError('config_ui.token_env and config_ui.token_file are mutually exclusive')
    return dict(settings)


def _parse_default_agents(document: dict[str, Any]) -> tuple[str, ...]:
    raw_default_agents = document.get('default_agents')
    if raw_default_agents is None:
        raise ConfigValidationError('default_agents is required')
    try:
        return tuple(
            normalize_agent_name(item)
            for item in expect_string_list(raw_default_agents, field_name='default_agents')
        )
    except AgentValidationError as exc:
        raise ConfigValidationError(str(exc)) from exc


def _parse_topology_default_agents(document: dict[str, Any], *, windows) -> tuple[str, ...]:
    raw_default_agents = document.get('default_agents')
    if raw_default_agents is not None:
        raise ConfigValidationError('default_agents is not supported with windows topology')
    return tuple(agent_name for window in windows for agent_name in window.agent_names)


def _parse_cmd_enabled(document: dict[str, Any]) -> bool:
    if 'cmd_enabled' not in document:
        return False
    return expect_bool(document['cmd_enabled'], field_name='cmd_enabled')


def _parse_layout_spec(document: dict[str, Any]) -> str | None:
    if document.get('layout') is None:
        return None
    return expect_string(document['layout'], field_name='layout')


def _parse_entry_window(document: dict[str, Any]) -> str | None:
    if document.get('entry_window') is None:
        return None
    return expect_string(document['entry_window'], field_name='entry_window')


def _parse_maintenance_heartbeat(document: dict[str, Any]) -> MaintenanceHeartbeatConfig:
    raw_maintenance = document.get('maintenance')
    if raw_maintenance is None:
        return MaintenanceHeartbeatConfig()
    maintenance = expect_mapping(raw_maintenance, field_name='maintenance')
    unknown_top = sorted(set(maintenance) - _MAINTENANCE_TOP_LEVEL_KEYS)
    if unknown_top:
        raise ConfigValidationError(
            f'maintenance contains unknown fields: {", ".join(unknown_top)}'
        )
    raw_heartbeat = maintenance.get('heartbeat')
    if raw_heartbeat is None:
        return MaintenanceHeartbeatConfig()
    heartbeat = expect_mapping(raw_heartbeat, field_name='maintenance.heartbeat')
    unknown_heartbeat = sorted(set(heartbeat) - _MAINTENANCE_HEARTBEAT_KEYS)
    if unknown_heartbeat:
        raise ConfigValidationError(
            'maintenance.heartbeat contains unknown fields: '
            + ', '.join(unknown_heartbeat)
        )
    try:
        return MaintenanceHeartbeatConfig(
            enabled=_optional_bool(heartbeat, 'enabled', default=False),
            assessor=_optional_string(heartbeat, 'assessor', default='ccb_self'),
            interval_s=_optional_positive_int(heartbeat, 'interval_s', default=3600),
            min_interval_s=_optional_positive_int(heartbeat, 'min_interval_s', default=300),
            unknown_streak_cap=_optional_positive_int(heartbeat, 'unknown_streak_cap', default=3),
            escalation_policy=_optional_string(heartbeat, 'escalation_policy', default='report_only'),
            startup_ensure=_optional_bool(heartbeat, 'startup_ensure', default=True),
        )
    except AgentValidationError as exc:
        raise ConfigValidationError(str(exc)) from exc


def _optional_bool(table: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in table:
        return bool(default)
    return expect_bool(table[key], field_name=f'maintenance.heartbeat.{key}')


def _optional_string(table: dict[str, Any], key: str, *, default: str) -> str:
    if key not in table:
        return default
    return expect_string(table[key], field_name=f'maintenance.heartbeat.{key}')


def _optional_positive_int(table: dict[str, Any], key: str, *, default: int) -> int:
    if key not in table:
        return int(default)
    value = table[key]
    if isinstance(value, bool):
        raise ConfigValidationError(f'maintenance.heartbeat.{key} must be a positive integer')
    if not isinstance(value, int):
        raise ConfigValidationError(f'maintenance.heartbeat.{key} must be a positive integer')
    if value <= 0:
        raise ConfigValidationError(f'maintenance.heartbeat.{key} must be a positive integer')
    return int(value)


def _validate_legacy_and_windows_fields(
    document: dict[str, Any],
    *,
    windows,
    tool_windows,
) -> None:
    if windows is None:
        if 'entry_window' in document:
            raise ConfigValidationError('entry_window requires windows topology')
        if 'ui' in document:
            raise ConfigValidationError('ui.sidebar requires windows topology')
        return
    if 'layout' in document:
        raise ConfigValidationError('layout is not supported with windows topology')
    if 'cmd_enabled' in document:
        raise ConfigValidationError('cmd_enabled is not supported with windows topology')


def _validate_topology_presence(document: dict[str, Any], *, windows, tool_windows) -> None:
    if windows is not None:
        return
    if tuple(tool_windows or ()):
        raise ConfigValidationError('tool_windows requires windows topology')


def _build_project_config(
    *,
    default_agents: tuple[str, ...],
    parsed_agents,
    cmd_enabled: bool,
    layout_spec: str | None,
    windows,
    tool_windows,
    entry_window: str | None,
    sidebar,
    sidebar_view,
    maintenance_heartbeat: MaintenanceHeartbeatConfig,
    loop_capacity,
    runtime_mux_backend: str | None,
    source_path: Path | None,
) -> ProjectConfig:
    try:
        return ProjectConfig(
            version=2,
            default_agents=default_agents,
            agents=parsed_agents,
            cmd_enabled=cmd_enabled,
            layout_spec=layout_spec,
            windows=windows,
            tool_windows=tool_windows,
            entry_window=entry_window,
            sidebar=sidebar,
            sidebar_view=sidebar_view,
            maintenance_heartbeat=maintenance_heartbeat,
            loop_capacity=loop_capacity,
            runtime_mux_backend=runtime_mux_backend,
            source_path=str(source_path) if source_path else None,
            windows_explicit=windows is not None,
        )
    except AgentValidationError as exc:
        raise ConfigValidationError(str(exc)) from exc


def _expand_role_id_shorthand(document: dict[str, Any], *, project_root: Path | None) -> dict[str, Any]:
    if not isinstance(document.get('windows'), dict):
        return document
    expanded = deepcopy(document)
    windows = dict(expanded.get('windows') or {})
    agents = dict(expanded.get('agents') or {})
    for window_name, layout_text in windows.items():
        layout = parse_layout_spec(str(layout_text))
        role_bindings: dict[str, str] = {}
        resolved = _expand_role_layout(layout, role_bindings=role_bindings, project_root=project_root)
        if role_bindings:
            windows[window_name] = resolved.render()
            for agent_name, role_id in role_bindings.items():
                raw_spec = dict(agents.get(agent_name) or {})
                existing_role = str(raw_spec.get('role') or '').strip().lower()
                if existing_role and existing_role != role_id:
                    raise ConfigValidationError(
                        f'agent {agent_name!r} role conflicts with shorthand role {role_id}'
                    )
                raw_spec['role'] = role_id
                agents[agent_name] = raw_spec
    expanded['windows'] = windows
    if agents:
        expanded['agents'] = agents
    return expanded


def _expand_role_layout(node, *, role_bindings: dict[str, str], project_root: Path | None):
    if node.kind == 'leaf':
        assert node.leaf is not None
        name = str(node.leaf.name or '').strip()
        if looks_like_role_id(name):
            role_id = normalize_role_id(name)
            try:
                agent_name = normalize_agent_name(installed_role_default_agent_name(role_id, project_root=project_root))
            except RoleLookupError as exc:
                raise ConfigValidationError(str(exc)) from exc
            except AgentValidationError as exc:
                raise ConfigValidationError(f'role {role_id} default agent name is invalid: {exc}') from exc
            existing = role_bindings.get(agent_name)
            if existing is not None and existing != role_id:
                raise ConfigValidationError(
                    f'agent {agent_name!r} cannot be derived from multiple roles: {existing}, {role_id}'
                )
            role_bindings[agent_name] = role_id
            return LayoutNode(
                kind='leaf',
                leaf=LayoutLeaf(
                    name=agent_name,
                    provider=node.leaf.provider,
                    workspace_mode=node.leaf.workspace_mode,
                    percent=node.leaf.percent,
                ),
            )
        return node
    assert node.left is not None
    assert node.right is not None
    return LayoutNode(
        kind=node.kind,
        left=_expand_role_layout(node.left, role_bindings=role_bindings, project_root=project_root),
        right=_expand_role_layout(node.right, role_bindings=role_bindings, project_root=project_root),
    )


def _project_root_from_source_path(source_path: Path | None) -> Path | None:
    if source_path is None:
        return None
    path = Path(source_path).expanduser().resolve()
    if path == Path.home().expanduser().resolve() / '.ccb' / CONFIG_FILENAME:
        return None
    if path.name == CONFIG_FILENAME and path.parent.name == '.ccb':
        return path.parent.parent
    return None


__all__ = ['validate_project_config']
