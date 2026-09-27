from __future__ import annotations

from ccbd.reload_append_layout import AppendAgentPlan, rightmost_leaf_append_plan


def append_agent_windows(old_topology, new_topology) -> dict[str, tuple[AppendAgentPlan, ...]] | None:
    old_windows = window_map(old_topology)
    new_windows = window_map(new_topology)
    added_windows = set(new_windows) - set(old_windows)
    append: dict[str, tuple[AppendAgentPlan, ...]] = {}
    for window_name, new_window in new_windows.items():
        if window_name in added_windows:
            continue
        old_window = old_windows.get(window_name)
        if old_window is None:
            continue
        plan = append_agent_plan_for_window(old_window, new_window)
        if plan is None:
            return None
        if plan:
            append[window_name] = plan
    return append


def append_agent_plan_for_window(old_window, new_window) -> tuple[AppendAgentPlan, ...] | None:
    old_agents = window_agent_names(old_window)
    new_agents = window_agent_names(new_window)
    if old_agents == new_agents:
        return ()
    if len(new_agents) < len(old_agents):
        return ()
    if tuple(new_agents[: len(old_agents)]) != old_agents:
        return None
    append_plan = rightmost_leaf_append_plan(old_window, new_window)
    if append_plan is None:
        return None
    if tuple(item.agent for item in append_plan) != new_agents[len(old_agents) :]:
        return None
    return append_plan


def new_agent_targets(old_topology, new_topology) -> set[tuple[str, str]]:
    old_agents = agent_window_pairs(old_topology)
    return {
        (str(window.name), str(agent_name))
        for window in tuple(getattr(new_topology, 'windows', ()) or ())
        for agent_name in window_agent_names(window)
        if (str(window.name), str(agent_name)) not in old_agents
    }


def agent_window_pairs(topology) -> set[tuple[str, str]]:
    return {
        (str(window.name), str(agent_name))
        for window in tuple(getattr(topology, 'windows', ()) or ())
        for agent_name in window_agent_names(window)
    }


def window_agent_names(window) -> tuple[str, ...]:
    return tuple(str(item) for item in tuple(getattr(window, 'agent_names', ()) or ()))


def window_map(topology) -> dict[str, object]:
    return {str(window.name): window for window in tuple(getattr(topology, 'windows', ()) or ())}


__all__ = [
    'AppendAgentPlan',
    'agent_window_pairs',
    'append_agent_plan_for_window',
    'append_agent_windows',
    'new_agent_targets',
    'window_agent_names',
    'window_map',
]
