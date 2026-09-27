from __future__ import annotations

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


def apply_loop_capacity_overlays(config: ProjectConfig, project_root: Path) -> ProjectConfig:
    states = _active_loop_capacity_states(project_root)
    if not states:
        return config
    generated_specs: dict[str, AgentSpec] = {}
    generated_order: list[str] = []
    records_by_name: dict[str, dict[str, object]] = {}
    for state_path, state in states:
        for agent in _active_agents_from_state(state_path, state):
            spec = _agent_spec_from_record(agent)
            if spec.name in config.agents:
                raise ConfigValidationError(
                    f'{state_path}: loop generated agent {spec.name!r} conflicts with configured agent'
                )
            if spec.name in generated_specs:
                raise ConfigValidationError(
                    f'{state_path}: duplicate loop generated agent {spec.name!r} across active loop capacity states'
                )
            generated_specs[spec.name] = spec
            records_by_name[spec.name] = agent
            generated_order.append(spec.name)
    if not generated_specs:
        return config
    agents = dict(config.agents)
    agents.update(generated_specs)
    if getattr(config, 'windows_explicit', False):
        window_agents, entry_agents = _plan_window_placement(config, records_by_name, generated_order)
        windows = (
            _apply_window_placement(config, window_agents, agents=agents)
            if window_agents
            else _append_agents_to_entry_window(config, entry_agents, agents=agents)
        )
        return _copy_config(config, agents=agents, default_agents=config.default_agents, windows=windows)
    default_agents = (*tuple(config.default_agents), *tuple(generated_order))
    layout_spec = _append_agents_to_layout(config.layout_spec, generated_order, agents)
    return _copy_config(config, agents=agents, default_agents=default_agents, layout_spec=layout_spec)


def _active_loop_capacity_states(project_root: Path) -> tuple[tuple[Path, dict[str, object]], ...]:
    loops_dir = PathLayout(project_root).runtime_state_root / 'runtime' / 'loops'
    if not loops_dir.is_dir():
        return ()
    states: list[tuple[Path, dict[str, object]]] = []
    for state_path in sorted(loops_dir.glob('*/capacity.json')):
        try:
            payload = json.loads(state_path.read_text(encoding='utf-8'))
        except Exception as exc:
            raise ConfigValidationError(f'{state_path}: invalid loop capacity state: {exc}') from exc
        if not isinstance(payload, dict):
            raise ConfigValidationError(f'{state_path}: loop capacity state must be a JSON object')
        if str(payload.get('loop_capacity_status') or '') != 'ensured':
            continue
        states.append((state_path, dict(payload)))
    return tuple(states)


def _active_agents_from_state(state_path: Path, state: dict[str, object]) -> tuple[dict[str, object], ...]:
    agents = state.get('agents')
    if not isinstance(agents, list):
        raise ConfigValidationError(f'{state_path}: loop capacity state agents must be a list')
    active: list[dict[str, object]] = []
    for raw_agent in agents:
        if not isinstance(raw_agent, dict):
            raise ConfigValidationError(f'{state_path}: loop capacity state agent entries must be objects')
        agent = dict(raw_agent)
        if str(agent.get('state') or '') == 'released':
            continue
        active.append(agent)
    return tuple(active)


def _plan_window_placement(
    config: ProjectConfig,
    records_by_name: dict[str, dict[str, object]],
    generated_order: list[str],
) -> tuple[dict[str, tuple[str, ...]], list[str]]:
    entry_agents: list[str] = []
    window_agents: dict[str, list[str]] = {}
    any_window_placement = False
    for agent_name in generated_order:
        window_name = _placement_window_name(records_by_name.get(agent_name) or {})
        if window_name is None:
            entry_agents.append(agent_name)
            continue
        any_window_placement = True
        window_agents.setdefault(window_name, []).append(agent_name)
    if any_window_placement and entry_agents:
        entry = str(config.entry_window or 'main')
        window_agents.setdefault(entry, []).extend(entry_agents)
        entry_agents = []
    return {name: tuple(values) for name, values in window_agents.items()}, entry_agents


def _placement_window_name(agent: dict[str, object]) -> str | None:
    window_name = _optional_string(agent.get('window_name'))
    if window_name is not None:
        return window_name
    placement = agent.get('placement') if isinstance(agent.get('placement'), dict) else {}
    window_name = _optional_string(dict(placement).get('window_name')) if placement else None
    if window_name is not None:
        return window_name
    loop_id = _optional_string(agent.get('loop_id')) or (_optional_string(dict(placement).get('loop_id')) if placement else None)
    node_id = _optional_string(agent.get('node_id')) or (_optional_string(dict(placement).get('node_id')) if placement else None)
    if loop_id is not None or node_id is not None:
        return _execution_node_window_name(loop_id=loop_id, node_id=node_id)
    return None


def _execution_node_window_name(*, loop_id: str | None, node_id: str | None) -> str:
    return f'node-{_window_slug(loop_id or "loop")}-{_window_slug(node_id or "node")}'


def _window_slug(value: str) -> str:
    text = str(value or '').strip().replace('_', '-')
    cleaned = ''.join(ch if ch.isalnum() or ch == '-' else '-' for ch in text)
    cleaned = '-'.join(part for part in cleaned.split('-') if part)
    if not cleaned:
        cleaned = 'window'
    if not cleaned[0].isalpha():
        cleaned = f'w-{cleaned}'
    return cleaned


def _agent_spec_from_record(agent: dict[str, object]) -> AgentSpec:
    return AgentSpec(
        name=normalize_agent_name(str(agent.get('name') or '')),
        provider=str(agent.get('provider') or ''),
        target='.',
        workspace_mode=WorkspaceMode(str(agent.get('workspace_mode') or WorkspaceMode.INPLACE.value)),
        workspace_root=None,
        workspace_path=None,
        workspace_group=_optional_string(agent.get('workspace_group')),
        runtime_mode=RuntimeMode.PANE_BACKED,
        restore_default=RestoreMode.AUTO,
        permission_default=PermissionMode.MANUAL,
        queue_policy=QueuePolicy.SERIAL_PER_AGENT,
        model=_optional_string(agent.get('model')),
        thinking=_optional_string(agent.get('thinking')),
        startup_args=tuple(str(item) for item in tuple(agent.get('startup_args') or ())),
        provider_profile=dict(agent.get('provider_profile') or {}),
        role=_optional_string(agent.get('role')),
        labels=('ccb-loop', f'loop-profile:{agent.get("profile") or ""}'),
        description='CCB loop capacity generated agent',
    )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _append_agents_to_layout(layout_spec: str | None, agent_names: list[str], agents: dict[str, AgentSpec]) -> str:
    if not agent_names:
        return str(layout_spec or '')
    node = parse_layout_spec(str(layout_spec or ''))
    for agent_name in agent_names:
        spec = agents[agent_name]
        node = _append_layout_leaf(node, agent_name, provider=spec.provider, workspace_mode=spec.workspace_mode.value)
    return node.render()


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
        windows.append(_new_loop_window(window_name, agent_names, order=len(windows), agents=agents))
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


def _new_loop_window(
    window_name: str,
    agent_names: tuple[str, ...],
    *,
    order: int,
    agents: dict[str, AgentSpec],
) -> WindowSpec:
    return WindowSpec(
        name=window_name,
        order=order,
        layout_spec=_append_compatible_layout_spec(agent_names, agents=agents),
        agent_names=agent_names,
    )


def _append_compatible_layout_spec(agent_names: tuple[str, ...], *, agents: dict[str, AgentSpec]) -> str:
    if not agent_names:
        raise ValueError('at least one agent is required for loop window layout')
    first = agent_names[0]
    spec = agents[first]
    node = _layout_leaf(first, provider=spec.provider, workspace_mode=spec.workspace_mode.value)
    for agent_name in agent_names[1:]:
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
        raise ConfigValidationError('loop capacity overlay could not find entry window for generated agents')
    return tuple(windows)


def _copy_config(
    config: ProjectConfig,
    *,
    agents: dict[str, AgentSpec],
    default_agents: tuple[str, ...],
    layout_spec: str | None = None,
    windows: tuple[WindowSpec, ...] | None = None,
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
        windows_explicit=config.windows_explicit,
        maintenance_heartbeat=config.maintenance_heartbeat,
        loop_capacity=config.loop_capacity,
        workflow=config.workflow,
        runtime_mux_backend=config.runtime_mux_backend,
    )


__all__ = ['apply_loop_capacity_overlays']
