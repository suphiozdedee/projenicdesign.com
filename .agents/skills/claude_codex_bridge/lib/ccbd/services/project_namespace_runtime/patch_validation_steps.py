from __future__ import annotations

from collections.abc import Mapping


def patch_steps(patch_plan: dict[str, object]) -> tuple[Mapping[str, object] | object, ...]:
    return tuple((patch_plan or {}).get('steps') or ())


def planned_create_windows(steps: tuple[Mapping[str, object] | object, ...]) -> set[str]:
    return _planned_windows_for_action(steps, 'create_window')


def planned_kill_windows(steps: tuple[Mapping[str, object] | object, ...]) -> set[str]:
    return _planned_windows_for_action(steps, 'kill_window') | _planned_windows_for_action(steps, 'kill_tool_window')


def planned_agent_targets(steps: tuple[Mapping[str, object] | object, ...]) -> set[tuple[str, str]]:
    return _planned_agent_targets_for_action(steps, 'create_agent_pane')


def planned_removed_agent_targets(steps: tuple[Mapping[str, object] | object, ...]) -> set[tuple[str, str]]:
    return _planned_agent_targets_for_action(steps, 'kill_agent_pane')


def planned_moved_agent_targets(steps: tuple[Mapping[str, object] | object, ...]) -> set[tuple[str, str, str]]:
    return {
        (str(step.get('window') or ''), str(step.get('target_window') or ''), str(step.get('agent') or ''))
        for step in steps
        if isinstance(step, Mapping) and step.get('action') == 'move_agent_pane'
    }


def planned_tool_windows(steps: tuple[Mapping[str, object] | object, ...]) -> set[str]:
    return _planned_windows_for_action(steps, 'create_tool_pane')


def planned_removed_tool_windows(steps: tuple[Mapping[str, object] | object, ...]) -> set[str]:
    return _planned_windows_for_action(steps, 'kill_tool_window')


def has_view_only_step(steps: tuple[Mapping[str, object] | object, ...]) -> bool:
    return any(
        isinstance(step, Mapping) and step.get('action') == 'refresh_project_view'
        for step in steps
    )


def _planned_windows_for_action(
    steps: tuple[Mapping[str, object] | object, ...],
    action: str,
) -> set[str]:
    return {
        str(step.get('window') or '')
        for step in steps
        if isinstance(step, Mapping) and step.get('action') == action
    }


def _planned_agent_targets_for_action(
    steps: tuple[Mapping[str, object] | object, ...],
    action: str,
) -> set[tuple[str, str]]:
    return {
        (str(step.get('window') or ''), str(step.get('agent') or ''))
        for step in steps
        if isinstance(step, Mapping) and step.get('action') == action
    }


__all__ = [
    'has_view_only_step',
    'patch_steps',
    'planned_agent_targets',
    'planned_create_windows',
    'planned_kill_windows',
    'planned_moved_agent_targets',
    'planned_removed_tool_windows',
    'planned_removed_agent_targets',
    'planned_tool_windows',
]
