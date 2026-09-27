from __future__ import annotations

from ccbd.reload_additive_agents import append_agent_plan_for_window, window_agent_names


def additive_agent_steps(old_topology, new_topology, *, step_factory, excluded_agents: tuple[str, ...] = ()) -> dict[str, object]:
    old_windows = _window_map(old_topology)
    new_windows = _window_map(new_topology)
    added_windows = set(new_windows) - set(old_windows)
    excluded = {str(agent) for agent in tuple(excluded_agents or ())}
    steps = []
    blocked: list[dict[str, object]] = []
    for window_name, new_window in new_windows.items():
        if window_name in added_windows:
            continue
        old_window = old_windows.get(window_name)
        if old_window is None:
            continue
        result = _steps_for_window(window_name, old_window, new_window, step_factory=step_factory, excluded_agents=excluded)
        steps.extend(result['steps'])
        blocked.extend(result['blocked'])
    return {'steps': steps, 'blocked': blocked}


def _steps_for_window(window_name: str, old_window, new_window, *, step_factory, excluded_agents: set[str]) -> dict[str, object]:
    old_agents = window_agent_names(old_window)
    new_agents = window_agent_names(new_window)
    if old_agents == new_agents:
        return {'steps': [], 'blocked': []}
    if len(new_agents) < len(old_agents):
        return {'steps': [], 'blocked': []}
    if tuple(new_agents[: len(old_agents)]) != old_agents:
        return {'steps': [], 'blocked': [_blocked_append(window_name, 'Phase 5 additive patch only supports appending new agents after existing panes')]}
    append_plan = append_agent_plan_for_window(old_window, new_window)
    if append_plan is None:
        return {'steps': [], 'blocked': [_blocked_append(window_name, 'Phase 5 additive patch only supports expanding the last existing agent pane')]}
    return {'steps': _append_steps(window_name, old_agents, append_plan, step_factory=step_factory, excluded_agents=excluded_agents), 'blocked': []}


def _append_steps(window_name: str, old_agents: tuple[str, ...], append_plan, *, step_factory, excluded_agents: set[str]) -> list[object]:
    anchor = old_agents[-1] if old_agents else None
    steps = []
    for append in append_plan:
        if append.agent in excluded_agents:
            anchor = append.agent
            continue
        steps.append(
            step_factory(
                action='create_agent_pane',
                window=window_name,
                agent=append.agent,
                role='agent',
                slot_key=append.agent,
                anchor_agent=anchor,
                reason='new agent appended to existing managed window',
            )
        )
        anchor = append.agent
    return steps


def _blocked_append(window_name: str, reason: str) -> dict[str, object]:
    return {'op': 'add_agent', 'window': window_name, 'reason': reason}


def _window_map(topology) -> dict[str, object]:
    return {str(window.name): window for window in tuple(getattr(topology, 'windows', ()) or ())}


__all__ = ['additive_agent_steps']
