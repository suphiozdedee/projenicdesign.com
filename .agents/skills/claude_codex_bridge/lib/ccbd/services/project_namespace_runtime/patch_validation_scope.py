from __future__ import annotations

from collections.abc import Mapping

_SUPPORTED_ACTIONS = {
    'create_window',
    'create_sidebar_pane',
    'create_agent_pane',
    'create_tool_pane',
    'move_agent_pane',
    'kill_agent_pane',
    'kill_tool_window',
    'kill_window',
}


def step_proof_reason(
    steps: tuple[Mapping[str, object] | object, ...],
    *,
    added_windows: set[str],
    removed_windows: set[str],
    append_windows: dict[str, object],
    expected_new_agents: set[tuple[str, str]],
    expected_removed_agents: set[tuple[str, str]],
    expected_moved_agents: set[tuple[str, str, str]],
) -> tuple[str, str] | None:
    for step in steps:
        reason = single_step_reason(
            step,
            added_windows=added_windows,
            removed_windows=removed_windows,
            append_windows=append_windows,
            expected_new_agents=expected_new_agents,
            expected_removed_agents=expected_removed_agents,
            expected_moved_agents=expected_moved_agents,
        )
        if reason is not None:
            return reason
    return None


def single_step_reason(
    step: Mapping[str, object] | object,
    *,
    added_windows: set[str],
    removed_windows: set[str],
    append_windows: dict[str, object],
    expected_new_agents: set[tuple[str, str]],
    expected_removed_agents: set[tuple[str, str]],
    expected_moved_agents: set[tuple[str, str, str]],
) -> tuple[str, str] | None:
    if not isinstance(step, Mapping):
        return ('invalid_patch_step', 'namespace patch plan step must be an object')
    action = str(step.get('action') or '')
    if action == 'refresh_project_view':
        return None
    if action not in _SUPPORTED_ACTIONS:
        return ('unsupported_patch_step', f'unsupported namespace patch step: {action}')
    reason = _step_scope_reason(
        action,
        str(step.get('window') or ''),
        step,
        added_windows=added_windows,
        removed_windows=removed_windows,
        append_windows=append_windows,
        expected_new_agents=expected_new_agents,
        expected_removed_agents=expected_removed_agents,
        expected_moved_agents=expected_moved_agents,
    )
    if reason is not None:
        return reason
    return _step_identity_reason(action, step)


def _step_scope_reason(
    action: str,
    window: str,
    step: Mapping[str, object],
    *,
    added_windows: set[str],
    removed_windows: set[str],
    append_windows: dict[str, object],
    expected_new_agents: set[tuple[str, str]],
    expected_removed_agents: set[tuple[str, str]],
    expected_moved_agents: set[tuple[str, str, str]],
) -> tuple[str, str] | None:
    if action in {'create_window', 'create_sidebar_pane'} and window not in added_windows:
        return ('unsupported_patch_step', 'window/sidebar patch steps are only supported for newly-added windows')
    if action in {'kill_window', 'kill_tool_window'} and window not in removed_windows:
        return ('patch_plan_mismatch', 'window removal step does not match removed topology window')
    if action == 'kill_agent_pane':
        if (window, str(step.get('agent') or '')) not in expected_removed_agents:
            return ('patch_plan_mismatch', 'agent pane removal step does not match a removed topology agent')
        return None
    if action == 'move_agent_pane':
        target_window = str(step.get('target_window') or '')
        if (window, target_window, str(step.get('agent') or '')) not in expected_moved_agents:
            return ('patch_plan_mismatch', 'agent pane move step does not match a moved topology agent')
        return None
    if action == 'create_tool_pane':
        if window not in added_windows:
            return ('patch_plan_mismatch', 'tool pane patch step window is not an added window')
        return None
    if action != 'create_agent_pane':
        return None
    if window not in added_windows and window not in append_windows:
        return ('patch_plan_mismatch', 'agent pane patch step window is not an added window or append-only existing window')
    if (window, str(step.get('agent') or '')) not in expected_new_agents:
        return ('patch_plan_mismatch', 'agent pane patch step does not match a new topology agent')
    return None


def _step_identity_reason(action: str, step: Mapping[str, object]) -> tuple[str, str] | None:
    if str(step.get('managed_by') or '') != 'ccbd':
        return ('scope_proof_missing', 'namespace patch step is missing managed_by=ccbd proof')
    if action not in {'create_sidebar_pane', 'create_agent_pane', 'create_tool_pane', 'move_agent_pane', 'kill_agent_pane', 'kill_tool_window'}:
        return None
    role = str(step.get('role') or '')
    slot_key = str(step.get('slot_key') or '')
    if not role or not slot_key:
        return ('scope_proof_missing', 'namespace patch pane step is missing role or slot_key proof')
    if action == 'create_sidebar_pane':
        expected_role = 'sidebar'
    elif action in {'create_tool_pane', 'kill_tool_window'}:
        expected_role = 'tool'
    else:
        expected_role = 'agent'
    if role != expected_role:
        return ('scope_proof_mismatch', f'namespace patch pane step role must be {expected_role}')
    return None


__all__ = ['single_step_reason', 'step_proof_reason']
