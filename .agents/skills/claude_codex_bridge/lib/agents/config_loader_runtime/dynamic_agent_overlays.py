from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from agents.models import (
    AgentSpec,
    LayoutLeaf,
    LayoutNode,
    PermissionMode,
    ProjectConfig,
    QueuePolicy,
    RestoreMode,
    RuntimeMode,
    WorkspaceMode,
    WindowSpec,
    normalize_agent_name,
    parse_layout_spec,
)
from storage.paths import PathLayout

from .common import ConfigValidationError


ACTIVE_DYNAMIC_AGENT_STATES = frozenset({'visible', 'hidden', 'parked'})
DEFAULT_MAX_PANES_PER_DYNAMIC_WINDOW = 6


@dataclass(frozen=True)
class DynamicPlacementPlan:
    entry_agents: tuple[str, ...]
    window_agents: dict[str, tuple[str, ...]]


def apply_dynamic_agent_overlays(config: ProjectConfig, project_root: Path) -> ProjectConfig:
    states = _active_dynamic_agent_states(project_root)
    if not states:
        return config
    generated_specs: dict[str, AgentSpec] = {}
    generated_order: list[str] = []
    for state_path, state in states:
        spec = _agent_spec_from_state(state_path, state)
        if spec.name in config.agents:
            raise ConfigValidationError(f'{state_path}: dynamic agent {spec.name!r} conflicts with configured agent')
        if spec.name in generated_specs:
            raise ConfigValidationError(f'{state_path}: duplicate dynamic agent {spec.name!r}')
        generated_specs[spec.name] = spec
        generated_order.append(spec.name)
    agents = dict(config.agents)
    agents.update(generated_specs)
    placement = _plan_dynamic_placement(config, states, generated_order)
    if placement.window_agents:
        windows = _apply_window_placement(config, placement.window_agents, agents=agents)
        return _copy_config(
            config,
            agents=agents,
            default_agents=config.default_agents,
            windows=windows,
            windows_explicit=True,
        )
    if getattr(config, 'windows_explicit', False):
        windows = _append_agents_to_entry_window(config, placement.entry_agents, agents=agents)
        return _copy_config(config, agents=agents, default_agents=config.default_agents, windows=windows)
    default_agents = (*tuple(config.default_agents), *tuple(generated_order))
    layout_spec = _append_agents_to_layout(config.layout_spec, placement.entry_agents, agents)
    return _copy_config(config, agents=agents, default_agents=default_agents, layout_spec=layout_spec)


def _plan_dynamic_placement(
    config: ProjectConfig,
    states: tuple[tuple[Path, dict[str, object]], ...],
    generated_order: list[str],
) -> DynamicPlacementPlan:
    state_by_agent = {normalize_agent_name(str(state.get('agent') or '')): state for _path, state in states}
    entry_agents: list[str] = []
    window_agents: dict[str, list[str]] = {}
    window_counts = _initial_window_counts(config)
    any_window_placement = False
    for agent_name in generated_order:
        state = state_by_agent[agent_name]
        window_name = _placement_window_name(config, state, window_counts=window_counts)
        if window_name is None:
            entry_agents.append(agent_name)
            continue
        any_window_placement = True
        window_agents.setdefault(window_name, []).append(agent_name)
        window_counts[window_name] = window_counts.get(window_name, 0) + 1
    if any_window_placement and entry_agents:
        entry = str(config.entry_window or 'main')
        window_agents.setdefault(entry, []).extend(entry_agents)
        entry_agents = []
    return DynamicPlacementPlan(
        entry_agents=tuple(entry_agents),
        window_agents={name: tuple(agents) for name, agents in window_agents.items()},
    )


def _initial_window_counts(config: ProjectConfig) -> dict[str, int]:
    return {
        str(window.name): len(tuple(window.agent_names or ()))
        for window in tuple(config.windows or ())
    }


def resolve_dynamic_placement_window(
    config: ProjectConfig,
    state: dict[str, object],
    *,
    window_counts: dict[str, int] | None = None,
) -> str | None:
    counts = dict(window_counts) if window_counts is not None else _initial_window_counts(config)
    return _placement_window_name(config, state, window_counts=counts)


def _placement_window_name(
    config: ProjectConfig,
    state: dict[str, object],
    *,
    window_counts: dict[str, int],
) -> str | None:
    window_name = _optional_string(state.get('window_name'))
    if window_name is not None:
        return window_name
    placement = state.get('placement') if isinstance(state.get('placement'), dict) else {}
    window_name = _optional_string(dict(placement).get('window_name')) if placement else None
    if window_name is not None:
        return window_name
    loop_id = _optional_string(state.get('loop_id')) or (_optional_string(dict(placement).get('loop_id')) if placement else None)
    node_id = _optional_string(state.get('node_id')) or (_optional_string(dict(placement).get('node_id')) if placement else None)
    if loop_id is not None or node_id is not None:
        return _execution_node_window_name(loop_id=loop_id, node_id=node_id)
    window_class = _optional_string(state.get('window_class')) or (
        _optional_string(dict(placement).get('window_class')) if placement else None
    )
    if window_class is None:
        return None
    return _window_for_class(config, window_class, window_counts=window_counts)


def _execution_node_window_name(*, loop_id: str | None, node_id: str | None) -> str:
    loop = _window_slug(loop_id or 'loop')
    node = _window_slug(node_id or 'node')
    return f'node-{loop}-{node}'


def _window_for_class(config: ProjectConfig, window_class: str, *, window_counts: dict[str, int]) -> str:
    prefix = _window_slug(window_class)
    candidates = _class_window_candidates(config, prefix, window_counts)
    for name in candidates:
        if window_counts.get(name, 0) < DEFAULT_MAX_PANES_PER_DYNAMIC_WINDOW:
            return name
    index = 2
    while True:
        candidate = f'{prefix}-{index}'
        if candidate not in window_counts:
            return candidate
        index += 1


def _class_window_candidates(config: ProjectConfig, prefix: str, window_counts: dict[str, int]) -> tuple[str, ...]:
    names = {str(window.name) for window in tuple(config.windows or ())}
    names.update(window_counts)
    candidates = [
        name
        for name in names
        if name == prefix or name.startswith(f'{prefix}-')
    ]
    return tuple(sorted(candidates, key=lambda name: _class_window_sort_key(prefix, name))) or (prefix,)


def _class_window_sort_key(prefix: str, name: str) -> tuple[int, str]:
    if name == prefix:
        return (1, name)
    suffix = name[len(prefix) + 1 :]
    if suffix.isdigit():
        return (int(suffix), name)
    return (10_000, name)


def _window_slug(value: str) -> str:
    text = str(value or '').strip().replace('_', '-')
    cleaned = ''.join(ch if ch.isalnum() or ch == '-' else '-' for ch in text)
    cleaned = '-'.join(part for part in cleaned.split('-') if part)
    if not cleaned:
        cleaned = 'window'
    if not cleaned[0].isalpha():
        cleaned = f'w-{cleaned}'
    return cleaned


def _apply_window_placement(
    config: ProjectConfig,
    window_agents: dict[str, tuple[str, ...]],
    *,
    agents: dict[str, AgentSpec],
) -> tuple[WindowSpec, ...]:
    windows: list[WindowSpec] = []
    remaining = {name: tuple(values) for name, values in window_agents.items() if values}
    for window in tuple(config.windows or ()):
        agent_names = remaining.pop(window.name, ())
        if not agent_names:
            windows.append(window)
            continue
        windows.append(_append_agents_to_window(window, agent_names, agents=agents))
    for window_name, agent_names in remaining.items():
        windows.append(_new_dynamic_window(window_name, agent_names, order=len(windows), agents=agents))
    return tuple(windows)


def _append_agents_to_window(
    window: WindowSpec,
    agent_names: tuple[str, ...],
    *,
    agents: dict[str, AgentSpec],
) -> WindowSpec:
    layout_spec = _append_agents_to_layout(window.layout_spec, list(agent_names), agents)
    return WindowSpec(
        name=window.name,
        order=window.order,
        layout_spec=layout_spec,
        agent_names=(*window.agent_names, *agent_names),
        tool_names=window.tool_names,
    )


def _new_dynamic_window(
    window_name: str,
    agent_names: tuple[str, ...],
    *,
    order: int,
    agents: dict[str, AgentSpec],
) -> WindowSpec:
    layout_spec = _append_compatible_layout_spec(agent_names, agents=agents)
    return WindowSpec(
        name=window_name,
        order=order,
        layout_spec=layout_spec,
        agent_names=agent_names,
    )


def _append_compatible_layout_spec(agent_names: tuple[str, ...], *, agents: dict[str, AgentSpec]) -> str:
    if not agent_names:
        raise ValueError('at least one agent is required for dynamic window layout')
    first = agent_names[0]
    spec = agents[first]
    node = _layout_leaf(first, provider=spec.provider, workspace_mode=spec.workspace_mode.value)
    for agent_name in agent_names[1:]:
        spec = agents[agent_name]
        node = _append_layout_leaf(node, agent_name, provider=spec.provider, workspace_mode=spec.workspace_mode.value)
    return node.render()


def _active_dynamic_agent_states(project_root: Path) -> tuple[tuple[Path, dict[str, object]], ...]:
    agents_dir = PathLayout(project_root).runtime_state_root / 'runtime' / 'agents'
    if not agents_dir.is_dir():
        return ()
    states: list[tuple[Path, dict[str, object]]] = []
    for state_path in sorted(agents_dir.glob('*/lifecycle.json')):
        try:
            payload = json.loads(state_path.read_text(encoding='utf-8'))
        except Exception as exc:
            raise ConfigValidationError(f'{state_path}: invalid dynamic agent lifecycle state: {exc}') from exc
        if not isinstance(payload, dict):
            raise ConfigValidationError(f'{state_path}: dynamic agent lifecycle state must be a JSON object')
        if str(payload.get('lifecycle_state') or '') not in ACTIVE_DYNAMIC_AGENT_STATES:
            continue
        states.append((state_path, dict(payload)))
    return tuple(sorted(states, key=_dynamic_state_sort_key))


def _dynamic_state_sort_key(item: tuple[Path, dict[str, object]]) -> tuple[int, str, int, str]:
    path, state = item
    sequence = _optional_int(state.get('placement_sequence'))
    if sequence is None:
        sequence = _optional_int(state.get('created_sequence'))
    if sequence is not None:
        return (0, '', sequence, str(path))
    timestamp = _optional_string(state.get('created_at')) or _optional_string(state.get('updated_at')) or ''
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return (1, timestamp, mtime_ns, str(path))


def _agent_spec_from_state(state_path: Path, state: dict[str, object]) -> AgentSpec:
    try:
        return AgentSpec(
            name=normalize_agent_name(str(state.get('agent') or '')),
            provider=str(state.get('provider') or ''),
            target=str(state.get('target') or '.'),
            workspace_mode=WorkspaceMode(str(state.get('workspace_mode') or WorkspaceMode.INPLACE.value)),
            workspace_root=_optional_string(state.get('workspace_root')),
            workspace_path=_optional_string(state.get('workspace_path')),
            workspace_group=_optional_string(state.get('workspace_group')),
            runtime_mode=RuntimeMode.PANE_BACKED,
            restore_default=RestoreMode.AUTO,
            permission_default=PermissionMode.MANUAL,
            queue_policy=QueuePolicy.SERIAL_PER_AGENT,
            model=_optional_string(state.get('model')),
            thinking=_optional_string(state.get('thinking')),
            startup_args=tuple(str(item) for item in tuple(state.get('startup_args') or ())),
            provider_profile=dict(state.get('provider_profile') or {}),
            role=_optional_string(state.get('role')),
            labels=tuple(str(item) for item in tuple(state.get('labels') or ())),
            description=_optional_string(state.get('description')) or 'CCB dynamic agent',
            dispatch_disabled=_dispatch_disabled(state),
        )
    except Exception as exc:
        raise ConfigValidationError(f'{state_path}: invalid dynamic agent spec: {exc}') from exc


def _dispatch_disabled(state: dict[str, object]) -> bool:
    if str(state.get('lifecycle_state') or '') == 'parked':
        return True
    value = state.get('dispatch_disabled')
    if isinstance(value, bool):
        return value
    return False


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _append_agents_to_layout(layout_spec: str | None, agent_names: list[str], agents: dict[str, AgentSpec]) -> str:
    if not agent_names:
        return str(layout_spec or '')
    node = parse_layout_spec(str(layout_spec or ''))
    for agent_name in agent_names:
        spec = agents[agent_name]
        node = _append_layout_leaf(node, agent_name, provider=spec.provider, workspace_mode=spec.workspace_mode.value)
    return node.render()


def _append_layout_leaf(node, agent_name: str, *, provider: str, workspace_mode: str):
    leaf = _layout_leaf(agent_name, provider=provider, workspace_mode=workspace_mode)
    if node.kind == 'leaf':
        return LayoutNode(kind='horizontal', left=node, right=leaf)
    assert node.left is not None
    assert node.right is not None
    return LayoutNode(
        kind=node.kind,
        left=node.left,
        right=_append_layout_leaf(node.right, agent_name, provider=provider, workspace_mode=workspace_mode),
    )


def _layout_leaf(agent_name: str, *, provider: str, workspace_mode: str):
    return LayoutNode(
        kind='leaf',
        leaf=LayoutLeaf(
            name=agent_name,
            provider=provider,
            workspace_mode='worktree' if workspace_mode == WorkspaceMode.GIT_WORKTREE.value else None,
        ),
    )


def _append_agents_to_entry_window(
    config: ProjectConfig,
    agent_names: list[str],
    *,
    agents: dict[str, AgentSpec],
) -> tuple[WindowSpec, ...]:
    entry = str(config.entry_window or '')
    windows: list[WindowSpec] = []
    matched = False
    for window in tuple(config.windows or ()):
        if window.name != entry:
            windows.append(window)
            continue
        matched = True
        layout_spec = _append_agents_to_layout(window.layout_spec, agent_names, agents)
        windows.append(
            WindowSpec(
                name=window.name,
                order=window.order,
                layout_spec=layout_spec,
                agent_names=(*window.agent_names, *tuple(agent_names)),
                tool_names=window.tool_names,
            )
        )
    if not matched:
        raise ConfigValidationError('dynamic agent overlay could not find entry window for generated agents')
    return tuple(windows)


def _copy_config(
    config: ProjectConfig,
    *,
    agents: dict[str, AgentSpec],
    default_agents: tuple[str, ...],
    layout_spec: str | None = None,
    windows: tuple[WindowSpec, ...] | None = None,
    windows_explicit: bool | None = None,
) -> ProjectConfig:
    return ProjectConfig(
        version=config.version,
        default_agents=default_agents,
        agents=agents,
        cmd_enabled=config.cmd_enabled,
        layout_spec=layout_spec if layout_spec is not None else config.layout_spec,
        windows=windows if windows is not None else (config.windows if getattr(config, 'windows_explicit', False) else None),
        tool_windows=config.tool_windows,
        entry_window=config.entry_window,
        sidebar=config.sidebar,
        sidebar_view=config.sidebar_view,
        source_path=config.source_path,
        windows_explicit=config.windows_explicit if windows_explicit is None else windows_explicit,
        maintenance_heartbeat=config.maintenance_heartbeat,
        loop_capacity=config.loop_capacity,
        workflow=config.workflow,
        runtime_mux_backend=config.runtime_mux_backend,
    )


__all__ = [
    'ACTIVE_DYNAMIC_AGENT_STATES',
    'DEFAULT_MAX_PANES_PER_DYNAMIC_WINDOW',
    'apply_dynamic_agent_overlays',
    'resolve_dynamic_placement_window',
]
