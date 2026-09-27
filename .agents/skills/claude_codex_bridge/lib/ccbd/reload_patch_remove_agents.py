from __future__ import annotations

from ccbd.reload_additive_agents import window_agent_names, window_map


def remove_agent_steps(old_topology, new_topology, *, step_factory, excluded_agents: tuple[str, ...] = ()) -> dict[str, object]:
    old_windows = window_map(old_topology)
    new_windows = window_map(new_topology)
    removed_windows = set(old_windows) - set(new_windows)
    excluded = {str(agent) for agent in tuple(excluded_agents or ())}
    steps = []
    blocked: list[dict[str, object]] = []
    for window_name, old_window in old_windows.items():
        if window_name in removed_windows:
            if str(getattr(old_window, 'kind', '') or '') == 'tool':
                continue
            agents = window_agent_names(old_window)
            if agents and set(agents).issubset(excluded):
                result = _remove_window_after_move_steps(window_name, step_factory=step_factory)
                steps.extend(result['steps'])
                blocked.extend(result['blocked'])
                continue
            if set(agents) & excluded:
                result = {
                    'steps': [],
                    'blocked': [
                        _blocked_remove(window_name, 'move_agent does not support removing the source window in the same transaction')
                    ],
                }
                steps.extend(result['steps'])
                blocked.extend(result['blocked'])
                continue
            result = _remove_window_steps(window_name, old_window, step_factory=step_factory)
        else:
            new_window = new_windows.get(window_name)
            if new_window is None:
                continue
            result = _remove_agent_steps(window_name, old_window, new_window, step_factory=step_factory, excluded_agents=excluded)
        steps.extend(result['steps'])
        blocked.extend(result['blocked'])
    return {'steps': steps, 'blocked': blocked}


def _remove_agent_steps(window_name: str, old_window, new_window, *, step_factory, excluded_agents: set[str]) -> dict[str, object]:
    old_agents = window_agent_names(old_window)
    new_agents = window_agent_names(new_window)
    removed = tuple(agent for agent in old_agents if agent not in set(new_agents) and agent not in excluded_agents)
    if not removed:
        return {'steps': [], 'blocked': []}
    if tuple(agent for agent in old_agents if agent in set(new_agents)) != new_agents:
        return {
            'steps': [],
            'blocked': [
                _blocked_remove(
                    window_name,
                    'Phase 7 remove_agent only supports deleting agents while preserving remaining order',
                )
            ],
        }
    return {
        'steps': [
            step_factory(
                action='kill_agent_pane',
                window=window_name,
                agent=agent,
                role='agent',
                slot_key=agent,
                reason='agent exists only in current published config',
            )
            for agent in removed
        ],
        'blocked': [],
    }


def _remove_window_steps(window_name: str, old_window, *, step_factory) -> dict[str, object]:
    removed_agents = window_agent_names(old_window)
    if not removed_agents:
        return {'steps': [], 'blocked': [_blocked_remove(window_name, 'cannot remove an empty managed window')]}
    steps = [
        step_factory(
            action='kill_agent_pane',
            window=window_name,
            agent=agent,
            role='agent',
            slot_key=agent,
            reason='agent window exists only in current published config',
        )
        for agent in removed_agents
    ]
    steps.append(
        step_factory(
            action='kill_window',
            window=window_name,
            reason='window exists only in current published config',
        )
    )
    return {'steps': steps, 'blocked': []}


def _remove_window_after_move_steps(window_name: str, *, step_factory) -> dict[str, object]:
    return {
        'steps': [
            step_factory(
                action='kill_window',
                window=window_name,
                reason='window emptied by moved agents',
            )
        ],
        'blocked': [],
    }


def _blocked_remove(window_name: str, reason: str) -> dict[str, object]:
    return {'op': 'remove_agent', 'window': window_name, 'reason': reason}


__all__ = ['remove_agent_steps']
