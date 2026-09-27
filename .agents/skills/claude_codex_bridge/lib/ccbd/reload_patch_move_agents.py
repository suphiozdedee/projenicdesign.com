from __future__ import annotations

from ccbd.reload_additive_agents import append_agent_plan_for_window, window_agent_names, window_map


def move_agent_steps(old_topology, new_topology, *, step_factory) -> dict[str, object]:
    old_windows = window_map(old_topology)
    new_windows = window_map(new_topology)
    moved = _moved_agents(old_topology, new_topology)
    moved_by_target = _moved_by_target(moved)
    moved_by_source = _moved_by_source(moved)
    steps = []
    blocked: list[dict[str, object]] = []
    for agent_name, source_window, target_window in moved:
        result = _move_agent_step(
            agent_name,
            source_window,
            target_window,
            old_windows=old_windows,
            new_windows=new_windows,
            moved_source_agents=moved_by_source.get(source_window, ()),
            moved_target_agents=moved_by_target.get(target_window, ()),
            step_factory=step_factory,
        )
        steps.extend(result['steps'])
        blocked.extend(result['blocked'])
    return {'steps': steps, 'blocked': blocked, 'moved_agents': tuple(agent for agent, _source, _target in moved)}


def _move_agent_step(
    agent_name: str,
    source_window: str,
    target_window: str,
    *,
    old_windows: dict[str, object],
    new_windows: dict[str, object],
    moved_source_agents: tuple[str, ...],
    moved_target_agents: tuple[str, ...],
    step_factory,
) -> dict[str, object]:
    old_source = old_windows.get(source_window)
    new_source = new_windows.get(source_window)
    old_target = old_windows.get(target_window)
    new_target = new_windows.get(target_window)
    if old_source is None:
        return {'steps': [], 'blocked': [_blocked_move(agent_name, source_window, target_window, 'source window must remain present')]}
    if new_source is None:
        source_agents = window_agent_names(old_source)
        if set(source_agents) != set(moved_source_agents):
            return {
                'steps': [],
                'blocked': [
                    _blocked_move(
                        agent_name,
                        source_window,
                        target_window,
                        'source window can be removed only when all source agents move in the same transaction',
                    )
                ],
            }
    if new_target is None:
        return {'steps': [], 'blocked': [_blocked_move(agent_name, source_window, target_window, 'target window must exist in new topology')]}
    moved_source_set = set(moved_source_agents)
    if new_source is not None and tuple(item for item in window_agent_names(old_source) if item not in moved_source_set) != window_agent_names(new_source):
        return {
            'steps': [],
            'blocked': [_blocked_move(agent_name, source_window, target_window, 'source window order must be preserved after move')],
        }
    if old_target is None:
        target_agents = window_agent_names(new_target)
        moved_count = len(moved_target_agents)
        if (
            not moved_count
            or tuple(target_agents[:moved_count]) != moved_target_agents
            or agent_name not in moved_target_agents
        ):
            return {
                'steps': [],
                'blocked': [_blocked_move(agent_name, source_window, target_window, 'new target window must start with moved agents')],
            }
        return _move_step(agent_name, source_window, target_window, step_factory=step_factory)
    append_plan = append_agent_plan_for_window(old_target, new_target)
    if append_plan is None:
        return {
            'steps': [],
            'blocked': [_blocked_move(agent_name, source_window, target_window, 'target window must append moved agents at the end')],
        }
    appended = tuple(item.agent for item in append_plan)
    if agent_name not in appended:
        return {'steps': [], 'blocked': [_blocked_move(agent_name, source_window, target_window, 'target append plan does not include moved agent')]}
    return _move_step(agent_name, source_window, target_window, step_factory=step_factory)


def _move_step(agent_name: str, source_window: str, target_window: str, *, step_factory) -> dict[str, object]:
    return {
        'steps': [
            step_factory(
                action='move_agent_pane',
                window=source_window,
                target_window=target_window,
                agent=agent_name,
                role='agent',
                slot_key=agent_name,
                reason='existing dynamic agent window membership changed',
            )
        ],
        'blocked': [],
    }


def _moved_by_target(moved: tuple[tuple[str, str, str], ...]) -> dict[str, tuple[str, ...]]:
    collected: dict[str, list[str]] = {}
    for agent_name, _source_window, target_window in moved:
        collected.setdefault(target_window, []).append(agent_name)
    return {target_window: tuple(agent_names) for target_window, agent_names in collected.items()}


def _moved_by_source(moved: tuple[tuple[str, str, str], ...]) -> dict[str, tuple[str, ...]]:
    collected: dict[str, list[str]] = {}
    for agent_name, source_window, _target_window in moved:
        collected.setdefault(source_window, []).append(agent_name)
    return {source_window: tuple(agent_names) for source_window, agent_names in collected.items()}


def _moved_agents(old_topology, new_topology) -> tuple[tuple[str, str, str], ...]:
    old_by_agent = _agent_window_map(old_topology)
    moved = []
    for window in tuple(getattr(new_topology, 'windows', ()) or ()):
        new_window = str(window.name)
        for agent_name in window_agent_names(window):
            old_window = old_by_agent.get(agent_name)
            if old_window is not None and old_window != new_window:
                moved.append((agent_name, old_window, new_window))
    return tuple(moved)


def _agent_window_map(topology) -> dict[str, str]:
    return {
        str(agent_name): str(window.name)
        for window in tuple(getattr(topology, 'windows', ()) or ())
        for agent_name in window_agent_names(window)
    }


def _blocked_move(agent: str, source_window: str, target_window: str, reason: str) -> dict[str, object]:
    return {
        'op': 'move_agent',
        'agent': agent,
        'from_window': source_window,
        'to_window': target_window,
        'reason': reason,
    }


__all__ = ['move_agent_steps']
