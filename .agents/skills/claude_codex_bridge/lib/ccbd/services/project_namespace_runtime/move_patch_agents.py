from __future__ import annotations

from ccbd.reload_additive_agents import append_agent_plan_for_window, window_agent_names, window_map

from .backend import apply_pane_identity, kill_pane, move_pane
from .remove_patch_agents import reflow_window_after_agent_change


def move_agent_panes(
    controller,
    backend,
    *,
    old_topology,
    new_topology,
    existing_agent_panes: dict[str, str],
    current,
    result,
    timeout_s: float | None,
) -> None:
    old_windows = window_map(old_topology)
    new_windows = window_map(new_topology)
    moved = _moved_agents(old_topology, new_topology)
    if not moved:
        return
    expected_moved_by_target = _expected_moved_by_target(moved)
    moved_by_target: dict[str, list[str]] = {}
    touched_windows: set[str] = set()
    for agent_name, source_window, target_window in moved:
        old_target = old_windows.get(target_window)
        new_target = new_windows.get(target_window)
        if new_target is None:
            raise RuntimeError(f'target window missing for moved agent {agent_name!r}: {target_window!r}')
        source_pane = existing_agent_panes.get(agent_name)
        if not source_pane:
            raise RuntimeError(f'pane missing for moved agent {agent_name!r}')
        if old_target is None:
            prior_moved = tuple(moved_by_target.get(target_window) or ())
            if prior_moved:
                anchor = _moved_target_anchor(target_window, prior_moved, result.moved_agents)
                direction = 'bottom'
                placeholder_pane = None
            else:
                anchor = _new_window_anchor(
                    agent_name,
                    target_window,
                    new_target,
                    moved_target_agents=expected_moved_by_target.get(target_window, ()),
                    result=result,
                )
                direction = 'right'
                placeholder_pane = anchor
        else:
            anchor = _target_anchor(agent_name, target_window, old_target, moved_by_target, existing_agent_panes, result.moved_agents)
            direction = _move_direction(agent_name, old_target, new_target)
            placeholder_pane = None
        move_pane(backend, source_pane=source_pane, anchor_pane=anchor, direction=direction, timeout_s=timeout_s)
        if placeholder_pane:
            _kill_placeholder_pane(backend, placeholder_pane, result=result, timeout_s=timeout_s)
        order_index = _agent_order_index(new_target, agent_name)
        apply_pane_identity(
            backend,
            pane_id=source_pane,
            title=agent_name,
            agent_label=agent_name,
            project_id=controller._project_id,
            order_index=order_index,
            role='agent',
            slot_key=agent_name,
            window_name=target_window,
            namespace_epoch=current.namespace_epoch,
            managed_by='ccbd',
        )
        result.moved_agents[agent_name] = source_pane
        result.moved_agent_windows[agent_name] = target_window
        moved_by_target.setdefault(target_window, []).append(agent_name)
        touched_windows.update({source_window, target_window})
    for window_name in sorted(touched_windows):
        if window_name not in new_windows:
            continue
        reflow_window_after_agent_change(
            controller,
            backend,
            current=current,
            topology_plan=new_topology,
            window_name=window_name,
            result=result,
            timeout_s=timeout_s,
        )


def _moved_agents(old_topology, new_topology) -> tuple[tuple[str, str, str], ...]:
    old_by_agent = _agent_window_map(old_topology)
    moved = []
    for window in tuple(getattr(new_topology, 'windows', ()) or ()):
        target = str(window.name)
        for agent_name in window_agent_names(window):
            source = old_by_agent.get(agent_name)
            if source is not None and source != target:
                moved.append((agent_name, source, target))
    return tuple(moved)


def _agent_window_map(topology) -> dict[str, str]:
    return {
        str(agent_name): str(window.name)
        for window in tuple(getattr(topology, 'windows', ()) or ())
        for agent_name in window_agent_names(window)
    }


def _target_anchor(
    agent_name: str,
    target_window: str,
    old_target,
    moved_by_target: dict[str, list[str]],
    existing_agent_panes: dict[str, str],
    moved_agent_panes: dict[str, str],
) -> str:
    prior_moved = tuple(moved_by_target.get(target_window) or ())
    if prior_moved:
        pane_id = moved_agent_panes.get(prior_moved[-1])
        if pane_id:
            return pane_id
    old_agents = window_agent_names(old_target)
    if not old_agents:
        raise RuntimeError(f'cannot move {agent_name!r} into empty existing target window {target_window!r}')
    anchor_agent = old_agents[-1]
    pane_id = existing_agent_panes.get(anchor_agent)
    if not pane_id:
        raise RuntimeError(f'anchor pane missing for moved agent {agent_name!r}: {anchor_agent!r}')
    return pane_id


def _moved_target_anchor(target_window: str, prior_moved: tuple[str, ...], moved_agent_panes: dict[str, str]) -> str:
    pane_id = moved_agent_panes.get(prior_moved[-1])
    if not pane_id:
        raise RuntimeError(f'anchor pane missing for moved agent target window {target_window!r}: {prior_moved[-1]!r}')
    return pane_id


def _new_window_anchor(agent_name: str, target_window: str, new_target, *, moved_target_agents: tuple[str, ...], result) -> str:
    target_agents = window_agent_names(new_target)
    moved_count = len(moved_target_agents)
    if (
        not moved_count
        or tuple(target_agents[:moved_count]) != moved_target_agents
        or target_agents[0] != agent_name
    ):
        raise RuntimeError(f'new target window must start with moved agents starting with {agent_name!r}')
    panes = dict(getattr(result, 'move_anchor_panes', {}) or {})
    pane_id = str(panes.get(target_window) or '').strip()
    if not pane_id:
        raise RuntimeError(f'placeholder anchor missing for moved agent {agent_name!r}: {target_window!r}')
    return pane_id


def _move_direction(agent_name: str, old_target, new_target) -> str:
    append_plan = append_agent_plan_for_window(old_target, new_target)
    if append_plan is None:
        raise RuntimeError(f'target window does not append moved agent {agent_name!r}')
    for item in append_plan:
        if item.agent == agent_name:
            return item.direction
    raise RuntimeError(f'target append plan does not include moved agent {agent_name!r}')


def _agent_order_index(window, agent_name: str) -> int | None:
    try:
        return window_agent_names(window).index(agent_name)
    except ValueError:
        return None


def _kill_placeholder_pane(backend, pane_id: str, *, result, timeout_s: float | None) -> None:
    kill_pane(backend, pane_id=pane_id, timeout_s=timeout_s)
    try:
        result.created_panes.remove(pane_id)
    except ValueError:
        pass


def _expected_moved_by_target(moved: tuple[tuple[str, str, str], ...]) -> dict[str, tuple[str, ...]]:
    collected: dict[str, list[str]] = {}
    for agent_name, _source_window, target_window in moved:
        collected.setdefault(target_window, []).append(agent_name)
    return {target_window: tuple(agent_names) for target_window, agent_names in collected.items()}


__all__ = ['move_agent_panes']
