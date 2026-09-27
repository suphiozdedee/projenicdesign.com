from __future__ import annotations

from ccbd.reload_additive_agents import window_agent_names


def removed_agent_targets(old_topology, new_topology) -> set[tuple[str, str]]:
    new_agents = _topology_agent_pairs(new_topology)
    return _topology_agent_pairs(old_topology) - new_agents


def moved_agent_targets(old_topology, new_topology) -> set[tuple[str, str, str]]:
    old_by_agent = _topology_agent_window_map(old_topology)
    new_by_agent = _topology_agent_window_map(new_topology)
    return {
        (old_by_agent[agent], new_by_agent[agent], agent)
        for agent in set(old_by_agent) & set(new_by_agent)
        if old_by_agent[agent] != new_by_agent[agent]
    }


def _topology_agent_pairs(topology) -> set[tuple[str, str]]:
    return {
        (str(window.name), str(agent_name))
        for window in tuple(getattr(topology, 'windows', ()) or ())
        for agent_name in window_agent_names(window)
    }


def _topology_agent_window_map(topology) -> dict[str, str]:
    return {
        str(agent_name): str(window.name)
        for window in tuple(getattr(topology, 'windows', ()) or ())
        for agent_name in window_agent_names(window)
    }


__all__ = ['moved_agent_targets', 'removed_agent_targets']
