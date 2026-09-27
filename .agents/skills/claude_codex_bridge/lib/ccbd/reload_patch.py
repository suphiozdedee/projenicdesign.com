from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ccbd.reload_patch_additive_agents import additive_agent_steps
from ccbd.reload_patch_move_agents import move_agent_steps
from ccbd.reload_patch_remove_agents import remove_agent_steps
from ccbd.services.project_namespace_runtime import build_namespace_topology_plan


_SUPPORTED_OPS = {
    'no_change',
    'view_only_change',
    'maintenance_change',
    'add_agent',
    'add_window',
    'remove_agent',
    'replace_agent',
    'move_agent',
    'add_tool_window',
    'remove_tool_window',
}
_MUTATING_OPS = {'add_agent', 'add_window', 'remove_agent', 'replace_agent', 'move_agent', 'add_tool_window', 'remove_tool_window'}
_REQUIRED_PROOFS = (
    'project_id',
    'namespace_transport_ref',
    'tmux_session_name',
    'namespace_epoch',
    'window',
    'role',
    'slot_key',
    'managed_by=ccbd',
)
_MANAGED_BY = 'ccbd'


@dataclass(frozen=True)
class NamespacePatchStep:
    action: str
    window: str | None = None
    target_window: str | None = None
    agent: str | None = None
    role: str | None = None
    slot_key: str | None = None
    managed_by: str = _MANAGED_BY
    anchor_agent: str | None = None
    reason: str | None = None

    def to_record(self) -> dict[str, object]:
        payload = {'action': self.action}
        for key in ('window', 'target_window', 'agent', 'role', 'slot_key', 'managed_by', 'anchor_agent', 'reason'):
            value = getattr(self, key)
            if value not in (None, ''):
                payload[key] = value
        return payload


def build_namespace_patch_plan(
    current_config,
    new_config,
    operations: list[Mapping[str, object]] | tuple[Mapping[str, object], ...],
    *,
    project_id: str | None = None,
    current_namespace=None,
) -> dict[str, object]:
    op_records = tuple(dict(item) for item in tuple(operations or ()))
    blocked = _blocked_unsupported_operations(op_records)
    old_topology = build_namespace_topology_plan(current_config)
    new_topology = build_namespace_topology_plan(new_config)
    scope = _scope_payload(project_id=project_id, current_namespace=current_namespace)
    steps: list[NamespacePatchStep] = []
    replace_agents = _replace_agent_names(op_records)

    if any(str(item.get('op') or '') in _MUTATING_OPS for item in op_records) and not scope['verified']:
        blocked.append(
            {
                'op': 'namespace_scope',
                'reason': 'current project namespace scope is unavailable or mismatched',
            }
        )

    if not blocked:
        steps.extend(_view_refresh_steps(op_records))
        steps.extend(_replace_agent_steps(op_records, step_factory=NamespacePatchStep))
        move_result = move_agent_steps(old_topology, new_topology, step_factory=NamespacePatchStep)
        moved_agents = tuple(move_result.get('moved_agents') or ())
        steps.extend(_additive_window_steps(old_topology, new_topology, excluded_agents=moved_agents))
        steps.extend(move_result['steps'])
        blocked.extend(move_result['blocked'])
        additive_result = additive_agent_steps(old_topology, new_topology, step_factory=NamespacePatchStep, excluded_agents=moved_agents)
        steps.extend(additive_result['steps'])
        blocked.extend(additive_result['blocked'])
        remove_result = remove_agent_steps(old_topology, new_topology, step_factory=NamespacePatchStep, excluded_agents=moved_agents)
        steps.extend(remove_result['steps'])
        blocked.extend(remove_result['blocked'])
        steps.extend(_remove_tool_window_steps(old_topology, new_topology))
        blocked.extend(_missing_additive_agent_steps(op_records, steps))
        blocked.extend(_missing_remove_agent_steps(op_records, steps))
        blocked.extend(_missing_replace_agent_steps(op_records, steps))
        blocked.extend(_missing_move_agent_steps(op_records, steps))
        blocked.extend(_missing_tool_window_steps(op_records, steps))

    status = 'blocked' if blocked else ('no_op' if not steps else 'planned')
    return {
        'status': status,
        'mutation_enabled': False,
        'apply_deferred': True,
        'scope': scope,
        'supported_operations': sorted(_SUPPORTED_OPS),
        'required_proofs': list(_REQUIRED_PROOFS),
        'preserved_agents': _preserved_agents(old_topology, new_topology, excluded_agents=replace_agents),
        'steps': [step.to_record() for step in steps],
        'blocked_operations': blocked,
        'warnings': _warnings_for_status(status),
    }


def build_invalid_namespace_patch_plan(error: object) -> dict[str, object]:
    return {
        'status': 'not_planned',
        'mutation_enabled': False,
        'apply_deferred': True,
        'scope': {'verified': False},
        'supported_operations': sorted(_SUPPORTED_OPS),
        'required_proofs': list(_REQUIRED_PROOFS),
        'preserved_agents': [],
        'steps': [],
        'blocked_operations': [
            {
                'op': 'invalid_config',
                'reason': str(error),
            }
        ],
        'warnings': ['namespace patch planning requires a valid new config'],
    }


def _blocked_unsupported_operations(operations: tuple[dict[str, object], ...]) -> list[dict[str, object]]:
    blocked = []
    for operation in operations:
        op = str(operation.get('op') or '').strip() or 'no_change'
        if op == 'layout_change' and str(operation.get('change') or '') == 'remove_window':
            continue
        if op not in _SUPPORTED_OPS:
            blocked.append(
                {
                    'op': op,
                    'agent': operation.get('agent'),
                    'window': operation.get('window'),
                    'reason': 'namespace patch planner supports config-only, additive, idle remove_agent, and guarded move_agent operations',
                }
            )
    return blocked


def _scope_payload(*, project_id: str | None, current_namespace) -> dict[str, object]:
    expected_project_id = _clean_text(project_id)
    if current_namespace is None:
        return {
            'verified': False,
            'project_id': expected_project_id,
            'reason': 'namespace unavailable',
        }
    namespace_project_id = _clean_text(getattr(current_namespace, 'project_id', None))
    backend_family = _clean_text(getattr(current_namespace, 'namespace_backend_family', None)) or 'tmux-family'
    backend_impl = _clean_text(getattr(current_namespace, 'backend_impl', None))
    socket_path = _clean_text(getattr(current_namespace, 'tmux_socket_path', None))
    session_name = _clean_text(getattr(current_namespace, 'tmux_session_name', None))
    namespace_id = _clean_text(getattr(current_namespace, 'namespace_id', None))
    namespace_ipc_kind = _clean_text(getattr(current_namespace, 'namespace_ipc_kind', None))
    namespace_ipc_ref = _clean_text(getattr(current_namespace, 'namespace_ipc_ref', None))
    namespace_epoch = getattr(current_namespace, 'namespace_epoch', None)
    has_namespace_epoch = namespace_epoch is not None
    ui_attachable = bool(getattr(current_namespace, 'ui_attachable', True))
    has_transport_ref = bool(
        namespace_id and namespace_ipc_kind and namespace_ipc_ref
        if backend_family == 'herdr-native' or backend_impl == 'herdr'
        else socket_path
    )
    verified = bool(
        namespace_project_id
        and has_transport_ref
        and session_name
        and has_namespace_epoch
        and ui_attachable
        and (expected_project_id is None or expected_project_id == namespace_project_id)
    )
    payload = {
        'verified': verified,
        'project_id': namespace_project_id or expected_project_id,
        'tmux_socket_path': socket_path,
        'tmux_session_name': session_name,
        'namespace_backend_family': backend_family,
        'namespace_backend_impl': backend_impl,
        'namespace_id': namespace_id,
        'namespace_ipc_kind': namespace_ipc_kind,
        'namespace_ipc_ref': namespace_ipc_ref,
        'namespace_epoch': namespace_epoch,
        'ui_attachable': ui_attachable,
    }
    if not verified:
        payload['reason'] = 'namespace project/transport/session scope is incomplete or mismatched'
    return payload


def _view_refresh_steps(operations: tuple[dict[str, object], ...]) -> list[NamespacePatchStep]:
    refresh_ops = {
        str(item.get('op') or '')
        for item in operations
        if str(item.get('op') or '') in {'view_only_change', 'maintenance_change'}
    }
    if not refresh_ops:
        return []
    if refresh_ops == {'view_only_change'}:
        reason = 'presentation-only config changed; no tmux namespace mutation is required'
    elif refresh_ops == {'maintenance_change'}:
        reason = 'maintenance heartbeat policy changed; no tmux namespace mutation is required'
    else:
        reason = 'presentation/config-only fields changed; no tmux namespace mutation is required'
    return [
        NamespacePatchStep(
            action='refresh_project_view',
            reason=reason,
        )
    ]


def _replace_agent_steps(operations: tuple[dict[str, object], ...], *, step_factory) -> list[NamespacePatchStep]:
    steps: list[NamespacePatchStep] = []
    for item in operations:
        if str(item.get('op') or '') != 'replace_agent':
            continue
        agent_name = str(item.get('agent') or '').strip()
        if not agent_name:
            continue
        steps.append(
            step_factory(
                action='reuse_agent_pane_for_replace',
                window=_clean_text(item.get('window')),
                agent=agent_name,
                role='agent',
                slot_key=agent_name,
                reason='agent spec changed; existing pane will be respawned for replacement',
            )
        )
    return steps


def _additive_window_steps(old_topology, new_topology, *, excluded_agents: tuple[str, ...] = ()) -> list[NamespacePatchStep]:
    old_windows = _window_map(old_topology)
    excluded = {str(agent) for agent in tuple(excluded_agents or ())}
    steps: list[NamespacePatchStep] = []
    for window in tuple(getattr(new_topology, 'windows', ()) or ()):
        window_name = str(window.name)
        if window_name in old_windows:
            continue
        steps.append(
            NamespacePatchStep(
                action='create_window',
                window=window_name,
                reason='window exists only in new config',
            )
        )
        if getattr(window, 'sidebar', None) is not None:
            steps.append(
                NamespacePatchStep(
                    action='create_sidebar_pane',
                    window=window_name,
                    role='sidebar',
                    slot_key=f'sidebar:{window_name}',
                    reason='new managed window needs a sidebar pane',
                )
            )
        for agent_name in tuple(getattr(window, 'agent_names', ()) or ()):
            if str(agent_name) in excluded:
                continue
            steps.append(
                NamespacePatchStep(
                    action='create_agent_pane',
                    window=window_name,
                    agent=str(agent_name),
                    role='agent',
                    slot_key=str(agent_name),
                    reason='new managed window needs an agent pane',
                )
            )
        if str(getattr(window, 'kind', '') or '') == 'tool':
            steps.append(
                NamespacePatchStep(
                    action='create_tool_pane',
                    window=window_name,
                    role='tool',
                    slot_key=f'tool:{window_name}',
                    reason='new managed tool window needs a tool pane',
                )
            )
    return steps


def _remove_tool_window_steps(old_topology, new_topology) -> list[NamespacePatchStep]:
    old_windows = _window_map(old_topology)
    new_windows = _window_map(new_topology)
    steps: list[NamespacePatchStep] = []
    for window_name, window in old_windows.items():
        if window_name in new_windows:
            continue
        if str(getattr(window, 'kind', '') or '') != 'tool':
            continue
        steps.append(
            NamespacePatchStep(
                action='kill_tool_window',
                window=window_name,
                role='tool',
                slot_key=f'tool:{window_name}',
                reason='managed tool window exists only in current published config',
            )
        )
    return steps


def _missing_additive_agent_steps(
    operations: tuple[dict[str, object], ...],
    steps: list[NamespacePatchStep],
) -> list[dict[str, object]]:
    expected = {
        str(item.get('agent') or '').strip()
        for item in operations
        if str(item.get('op') or '') == 'add_agent' and str(item.get('agent') or '').strip()
    }
    planned = {str(step.agent) for step in steps if step.action == 'create_agent_pane' and step.agent}
    return [
        {
            'op': 'add_agent',
            'agent': agent_name,
            'reason': 'add_agent operation was not covered by an additive namespace patch step',
        }
        for agent_name in sorted(expected - planned)
    ]


def _missing_remove_agent_steps(
    operations: tuple[dict[str, object], ...],
    steps: list[NamespacePatchStep],
) -> list[dict[str, object]]:
    expected = {
        str(item.get('agent') or '').strip()
        for item in operations
        if str(item.get('op') or '') == 'remove_agent' and str(item.get('agent') or '').strip()
    }
    planned = {str(step.agent) for step in steps if step.action == 'kill_agent_pane' and step.agent}
    return [
        {
            'op': 'remove_agent',
            'agent': agent_name,
            'reason': 'remove_agent operation was not covered by a namespace pane removal step',
        }
        for agent_name in sorted(expected - planned)
    ]


def _missing_replace_agent_steps(
    operations: tuple[dict[str, object], ...],
    steps: list[NamespacePatchStep],
) -> list[dict[str, object]]:
    expected = {
        str(item.get('agent') or '').strip()
        for item in operations
        if str(item.get('op') or '') == 'replace_agent' and str(item.get('agent') or '').strip()
    }
    planned = {str(step.agent) for step in steps if step.action == 'reuse_agent_pane_for_replace' and step.agent}
    return [
        {
            'op': 'replace_agent',
            'agent': agent_name,
            'reason': 'replace_agent operation was not covered by a same-slot runtime replacement step',
        }
        for agent_name in sorted(expected - planned)
    ]


def _missing_move_agent_steps(
    operations: tuple[dict[str, object], ...],
    steps: list[NamespacePatchStep],
) -> list[dict[str, object]]:
    expected = {
        str(item.get('agent') or '').strip()
        for item in operations
        if str(item.get('op') or '') == 'move_agent' and str(item.get('agent') or '').strip()
    }
    planned = {str(step.agent) for step in steps if step.action == 'move_agent_pane' and step.agent}
    return [
        {
            'op': 'move_agent',
            'agent': agent_name,
            'reason': 'move_agent operation was not covered by a namespace pane move step',
        }
        for agent_name in sorted(expected - planned)
    ]


def _missing_tool_window_steps(
    operations: tuple[dict[str, object], ...],
    steps: list[NamespacePatchStep],
) -> list[dict[str, object]]:
    created = {
        str(step.window)
        for step in steps
        if step.action == 'create_tool_pane' and str(step.window or '').strip()
    }
    removed = {
        str(step.window)
        for step in steps
        if step.action == 'kill_tool_window' and str(step.window or '').strip()
    }
    missing: list[dict[str, object]] = []
    for item in operations:
        op = str(item.get('op') or '')
        window = str(item.get('window') or '').strip()
        if op == 'add_tool_window' and window and window not in created:
            missing.append(
                {
                    'op': 'add_tool_window',
                    'window': window,
                    'reason': 'add_tool_window operation was not covered by a tool pane creation step',
                }
            )
        if op == 'remove_tool_window' and window and window not in removed:
            missing.append(
                {
                    'op': 'remove_tool_window',
                    'window': window,
                    'reason': 'remove_tool_window operation was not covered by a tool window removal step',
                }
            )
    return missing


def _replace_agent_names(operations: tuple[dict[str, object], ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(item.get('agent') or '').strip()
                for item in operations
                if str(item.get('op') or '') == 'replace_agent' and str(item.get('agent') or '').strip()
            }
        )
    )


def _preserved_agents(old_topology, new_topology, *, excluded_agents: tuple[str, ...] = ()) -> list[str]:
    excluded = set(excluded_agents)
    old_agents = {
        str(agent_name)
        for window in tuple(getattr(old_topology, 'windows', ()) or ())
        for agent_name in tuple(getattr(window, 'agent_names', ()) or ())
    }
    new_agents = {
        str(agent_name)
        for window in tuple(getattr(new_topology, 'windows', ()) or ())
        for agent_name in tuple(getattr(window, 'agent_names', ()) or ())
    }
    return sorted((old_agents & new_agents) - excluded)


def _warnings_for_status(status: str) -> list[str]:
    if status == 'planned':
        return ['Namespace patch apply is explicit and only supports additive, idle remove_agent, idle replace_agent, or guarded move_agent operations.']
    if status == 'blocked':
        return ['Namespace patch plan is blocked; reload must remain dry-run/rejected.']
    return []


def _window_map(topology) -> dict[str, object]:
    return {
        str(window.name): window
        for window in tuple(getattr(topology, 'windows', ()) or ())
    }


def _clean_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


__all__ = [
    'NamespacePatchStep',
    'build_invalid_namespace_patch_plan',
    'build_namespace_patch_plan',
]
