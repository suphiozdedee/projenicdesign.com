from __future__ import annotations

from ccbd.reload_additive_agents import append_agent_windows, new_agent_targets, window_map

from .patch_validation_scope import step_proof_reason
from .patch_validation_steps import (
    has_view_only_step,
    patch_steps,
    planned_agent_targets,
    planned_create_windows,
    planned_kill_windows,
    planned_moved_agent_targets,
    planned_removed_agent_targets,
    planned_removed_tool_windows,
    planned_tool_windows,
)
from .patch_validation_targets import moved_agent_targets, removed_agent_targets


def unsupported_additive_patch_reason(
    patch_plan: dict[str, object],
    old_topology,
    new_topology,
) -> tuple[str, str] | None:
    reason = _patch_plan_status_reason(patch_plan)
    if reason is not None:
        return reason
    old_windows = set(window_map(old_topology))
    new_windows = set(window_map(new_topology))
    removed_windows = old_windows - new_windows
    append_windows = append_agent_windows(old_topology, new_topology)
    if append_windows is None:
        return ('non_append_agent_layout', 'namespace additive patch only supports appending agents at the end of an existing window')
    added_windows = new_windows - old_windows
    steps = patch_steps(patch_plan)
    expected_new_agents = new_agent_targets(old_topology, new_topology)
    expected_removed_agents = removed_agent_targets(old_topology, new_topology)
    expected_moved_agents = moved_agent_targets(old_topology, new_topology)
    expected_new_agents -= {(target, agent) for _source, target, agent in expected_moved_agents}
    expected_removed_agents -= {(source, agent) for source, _target, agent in expected_moved_agents}
    expected_new_tools = _new_tool_windows(old_topology, new_topology)
    expected_removed_tools = _removed_tool_windows(old_topology, new_topology)
    reason = _planned_target_reason(
        steps,
        added_windows,
        removed_windows,
        append_windows,
        expected_new_agents,
        expected_removed_agents,
        expected_moved_agents,
        expected_new_tools,
        expected_removed_tools,
    )
    if reason is not None:
        return reason
    return step_proof_reason(
        steps,
        added_windows=added_windows,
        removed_windows=removed_windows,
        append_windows=append_windows,
        expected_new_agents=expected_new_agents,
        expected_removed_agents=expected_removed_agents,
        expected_moved_agents=expected_moved_agents,
    )


def _patch_plan_status_reason(patch_plan: dict[str, object]) -> tuple[str, str] | None:
    if str((patch_plan or {}).get('status') or '') != 'planned':
        return ('patch_plan_not_planned', 'namespace patch plan is not planned')
    if tuple((patch_plan or {}).get('blocked_operations') or ()):
        return ('patch_plan_blocked', 'namespace patch plan has blocked operations')
    return None


def _planned_target_reason(
    steps: tuple[object, ...],
    added_windows: set[str],
    removed_windows: set[str],
    append_windows: dict[str, object],
    expected_new_agents: set[tuple[str, str]],
    expected_removed_agents: set[tuple[str, str]],
    expected_moved_agents: set[tuple[str, str, str]],
    expected_new_tools: set[str],
    expected_removed_tools: set[str],
) -> tuple[str, str] | None:
    planned_windows = planned_create_windows(steps)
    if planned_windows != added_windows:
        return ('patch_plan_mismatch', 'namespace patch plan windows do not match new topology windows')
    if planned_kill_windows(steps) != removed_windows:
        return ('patch_plan_mismatch', 'namespace patch plan removed windows do not match new topology windows')
    if planned_agent_targets(steps) != expected_new_agents:
        return ('patch_plan_mismatch', 'namespace patch plan agent panes do not match new topology agents')
    if planned_removed_agent_targets(steps) != expected_removed_agents:
        return ('patch_plan_mismatch', 'namespace patch plan removed agent panes do not match new topology agents')
    if planned_moved_agent_targets(steps) != expected_moved_agents:
        return ('patch_plan_mismatch', 'namespace patch plan moved agent panes do not match topology agent moves')
    if planned_tool_windows(steps) != expected_new_tools:
        return ('patch_plan_mismatch', 'namespace patch plan tool panes do not match new topology tools')
    if planned_removed_tool_windows(steps) != expected_removed_tools:
        return ('patch_plan_mismatch', 'namespace patch plan removed tool windows do not match removed topology tools')
    if (
        not planned_windows
        and not append_windows
        and not expected_removed_agents
        and not expected_moved_agents
        and not expected_removed_tools
        and not has_view_only_step(steps)
    ):
        return ('unsupported_patch_step', 'namespace additive patch has no supported namespace mutation steps')
    return None


def _tool_window_names(topology) -> set[str]:
    return {
        str(window.name)
        for window in tuple(getattr(topology, 'windows', ()) or ())
        if str(getattr(window, 'kind', '') or '') == 'tool'
    }


def _new_tool_windows(old_topology, new_topology) -> set[str]:
    return _tool_window_names(new_topology) - _tool_window_names(old_topology)


def _removed_tool_windows(old_topology, new_topology) -> set[str]:
    return _tool_window_names(old_topology) - _tool_window_names(new_topology)


__all__ = ['unsupported_additive_patch_reason']
