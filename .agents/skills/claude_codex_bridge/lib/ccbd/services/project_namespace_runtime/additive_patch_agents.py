from __future__ import annotations

from types import SimpleNamespace

from agents.models import AgentValidationError, LayoutNode, normalize_agent_name, parse_layout_spec
from ccbd.reload_additive_agents import append_agent_plan_for_window, append_agent_windows, window_agent_names, window_map

from .backend import apply_pane_identity, reflow_window, split_pane
from .remove_patch_agents import reflow_window_after_agent_change


def append_agent_panes(
    controller,
    backend,
    *,
    old_topology,
    new_topology,
    existing_agent_panes: dict[str, str],
    current,
    result,
    namespace_epoch: int,
    created_panes: list[str],
    timeout_s: float | None,
    excluded_agents: tuple[str, ...] | set[str] = (),
) -> dict[str, str]:
    excluded = {str(agent) for agent in tuple(excluded_agents or ())}
    append_windows = append_agent_windows(old_topology, new_topology) or {}
    append_windows.update(_new_window_append_agent_windows(old_topology, new_topology, excluded_agents=excluded))
    old_windows = window_map(old_topology)
    new_windows = window_map(new_topology)
    agent_panes: dict[str, str] = {}
    for window_name, appended_agents in append_windows.items():
        if not tuple(item for item in tuple(appended_agents or ()) if str(item.agent) not in excluded):
            continue
        old_window = old_windows.get(window_name)
        old_agents = window_agent_names(old_window) if old_window is not None else _moved_prefix(new_windows[window_name], excluded)
        agent_panes.update(
            _append_window_agent_panes(
                controller,
                backend,
                window_name=window_name,
                old_agents=old_agents,
                new_window=new_windows[window_name],
                appended_agents=tuple(appended_agents or ()),
                existing_agent_panes={**existing_agent_panes, **dict(getattr(result, 'moved_agents', {}) or {})},
                current=current,
                namespace_epoch=namespace_epoch,
                created_panes=created_panes,
                timeout_s=timeout_s,
                excluded_agents=excluded,
            )
        )
        reflow_window_after_agent_change(
            controller,
            backend,
            current=current,
            topology_plan=new_topology,
            window_name=window_name,
            result=result,
            timeout_s=timeout_s,
        )
    return agent_panes


def _append_window_agent_panes(
    controller,
    backend,
    *,
    window_name: str,
    old_agents: tuple[str, ...],
    new_window,
    appended_agents,
    existing_agent_panes: dict[str, str],
    current,
    namespace_epoch: int,
    created_panes: list[str],
    timeout_s: float | None,
    excluded_agents: set[str],
) -> dict[str, str]:
    if not old_agents:
        raise RuntimeError(f'cannot append agents to empty existing window {window_name!r}')
    target = _anchor_pane(existing_agent_panes, old_agents[-1])
    style_index_by_agent = {agent: index for index, agent in enumerate(window_agent_names(new_window))}
    agent_panes: dict[str, str] = {}
    appended_sequence = tuple(appended_agents)
    for index, appended in enumerate(appended_sequence):
        # 物化 token / agent_panes key 统一用 normalized 名（layout 可能保留原始大小写
        # 如 Main_Code，而 CCB 内部 agent 标识是 main_code）——否则大小写敏感匹配下
        # agent 拿不到 pane（2026-08-06 采集暴露）。
        try:
            agent_name = normalize_agent_name(str(appended.agent))
        except AgentValidationError:
            agent_name = str(appended.agent)
        if agent_name in excluded_agents:
            target = _anchor_pane(existing_agent_panes, agent_name)
            continue
        target = _append_single_agent_pane(
            controller,
            backend,
            appended=appended,
            target=target,
            window_name=window_name,
            order_index=style_index_by_agent.get(agent_name),
            namespace_epoch=namespace_epoch,
            created_panes=created_panes,
            timeout_s=timeout_s,
        )
        agent_panes[agent_name] = target
        if any(str(item.agent) not in excluded_agents for item in appended_sequence[index + 1 :]):
            _prepare_window_for_next_append(
                backend,
                session_name=current.tmux_session_name,
                window_name=window_name,
                timeout_s=timeout_s,
            )
    return agent_panes


def _prepare_window_for_next_append(
    backend,
    *,
    session_name: str,
    window_name: str,
    timeout_s: float | None,
) -> None:
    reflow_window(
        backend,
        session_name=session_name,
        window_name=window_name,
        timeout_s=timeout_s,
    )


def _anchor_pane(existing_agent_panes: dict[str, str], anchor_agent: str) -> str:
    pane_id = existing_agent_panes.get(anchor_agent)
    if not pane_id:
        raise RuntimeError(f'anchor pane missing for preserved agent {anchor_agent!r}')
    return pane_id


def _append_single_agent_pane(
    controller,
    backend,
    *,
    appended,
    target: str,
    window_name: str,
    order_index: int | None,
    namespace_epoch: int,
    created_panes: list[str],
    timeout_s: float | None,
) -> str:
    pane_id = split_pane(
        backend,
        target=target,
        direction=appended.direction,
        percent=50,
        project_root=controller._layout.project_root,
        timeout_s=timeout_s,
    )
    _append_unique(created_panes, pane_id)
    try:
        agent_label = normalize_agent_name(str(appended.agent))
    except AgentValidationError:
        agent_label = str(appended.agent)
    apply_pane_identity(
        backend,
        pane_id=pane_id,
        title=appended.agent,
        agent_label=agent_label,
        project_id=controller._project_id,
        order_index=order_index,
        role='agent',
        slot_key=agent_label,
        window_name=window_name,
        namespace_epoch=namespace_epoch,
        managed_by='ccbd',
    )
    return pane_id


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _new_window_append_agent_windows(old_topology, new_topology, *, excluded_agents: set[str]) -> dict[str, tuple[object, ...]]:
    if not excluded_agents:
        return {}
    old_windows = window_map(old_topology)
    plans: dict[str, tuple[object, ...]] = {}
    for window_name, new_window in window_map(new_topology).items():
        if window_name in old_windows:
            continue
        moved_prefix = _moved_prefix(new_window, excluded_agents)
        if not moved_prefix:
            continue
        agent_names = window_agent_names(new_window)
        if len(moved_prefix) == len(agent_names):
            continue
        synthetic_old = SimpleNamespace(
            agent_names=moved_prefix,
            user_layout=_prefix_layout_spec(new_window, len(moved_prefix)),
        )
        plan = append_agent_plan_for_window(synthetic_old, new_window)
        if plan is None:
            raise RuntimeError(f'new window {window_name!r} cannot append new agents after moved panes')
        if plan:
            plans[window_name] = plan
    return plans


def _moved_prefix(window, excluded_agents: set[str]) -> tuple[str, ...]:
    prefix = []
    for agent_name in window_agent_names(window):
        if agent_name not in excluded_agents:
            break
        prefix.append(agent_name)
    return tuple(prefix)


def _prefix_layout_spec(window, leaf_count: int) -> str:
    layout = parse_layout_spec(str(getattr(window, 'user_layout', '') or ''))
    prefix = _prefix_layout_node(layout, leaf_count)
    if prefix is None:
        return ', '.join(window_agent_names(window)[:leaf_count])
    return prefix.render()


def _prefix_layout_node(node: LayoutNode, leaf_count: int) -> LayoutNode | None:
    if leaf_count <= 0:
        return None
    if node.kind == 'leaf':
        return node if leaf_count == 1 else None
    assert node.left is not None
    assert node.right is not None
    if leaf_count == node.leaf_count:
        return node
    left_count = node.left.leaf_count
    if leaf_count <= left_count:
        return _prefix_layout_node(node.left, leaf_count)
    right_prefix = _prefix_layout_node(node.right, leaf_count - left_count)
    if right_prefix is None:
        return None
    return LayoutNode(kind=node.kind, left=node.left, right=right_prefix)


__all__ = ['append_agent_panes']
