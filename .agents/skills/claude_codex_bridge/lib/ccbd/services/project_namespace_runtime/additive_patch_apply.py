from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from ccbd.reload_sensitive_diagnostics import (
    redact_sensitive_diagnostic_value,
    redact_sensitive_diagnostics,
)

from .backend import build_backend, remember_namespace_state_ref, session_alive
from .additive_patch_agents import append_agent_panes
from .additive_patch_namespace import ready_namespace_or_blocked
from .additive_patch_preservation import assert_preserved_agent_panes, snapshot_preserved_agent_panes
from .additive_patch_validation import unsupported_additive_patch_reason
from .additive_patch_windows import WindowPatchResult, create_new_windows
from .move_patch_agents import move_agent_panes
from .patch_validation_targets import moved_agent_targets
from .remove_patch_agents import remove_agent_panes
from .remove_patch_tools import remove_tool_windows


@dataclass(frozen=True)
class NamespacePatchApplyResult:
    status: str
    created_windows: tuple[str, ...] = ()
    created_panes: tuple[str, ...] = ()
    agent_panes: dict[str, str] = field(default_factory=dict)
    sidebar_panes: dict[str, str] = field(default_factory=dict)
    removed_windows: tuple[str, ...] = ()
    removed_panes: tuple[str, ...] = ()
    removed_agents: dict[str, str] = field(default_factory=dict)
    replaced_agents: dict[str, str] = field(default_factory=dict)
    moved_agents: dict[str, str] = field(default_factory=dict)
    moved_agent_windows: dict[str, str] = field(default_factory=dict)
    reflowed_windows: tuple[str, ...] = ()
    reflow_errors: dict[str, str] = field(default_factory=dict)
    tool_panes: dict[str, str] = field(default_factory=dict)
    preserved_before: dict[str, str] = field(default_factory=dict)
    preserved_after: dict[str, str] = field(default_factory=dict)
    partial: bool = False
    rollback_actions: tuple[str, ...] = ()
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_record(self) -> dict[str, object]:
        return {
            'status': self.status,
            'created_windows': list(self.created_windows),
            'created_panes': list(self.created_panes),
            'agent_panes': dict(self.agent_panes),
            'sidebar_panes': dict(self.sidebar_panes),
            'removed_windows': list(self.removed_windows),
            'removed_panes': list(self.removed_panes),
            'removed_agents': dict(self.removed_agents),
            'replaced_agents': dict(self.replaced_agents),
            'moved_agents': dict(self.moved_agents),
            'moved_agent_windows': dict(self.moved_agent_windows),
            'reflowed_windows': list(self.reflowed_windows),
            'reflow_errors': dict(self.reflow_errors),
            'tool_panes': dict(self.tool_panes),
            'preserved_before': dict(self.preserved_before),
            'preserved_after': dict(self.preserved_after),
            'partial': bool(self.partial),
            'rollback_actions': list(self.rollback_actions),
            'diagnostics': redact_sensitive_diagnostics(self.diagnostics),
        }


def apply_additive_patch(
    controller,
    *,
    patch_plan: dict[str, object],
    old_topology,
    new_topology,
    timeout_s: float | None = None,
) -> NamespacePatchApplyResult:
    return apply_reload_patch(
        controller,
        patch_plan=patch_plan,
        old_topology=old_topology,
        new_topology=new_topology,
        timeout_s=timeout_s,
    )


def apply_reload_patch(
    controller,
    *,
    patch_plan: dict[str, object],
    old_topology,
    new_topology,
    timeout_s: float | None = None,
) -> NamespacePatchApplyResult:
    current, blocked = ready_namespace_or_blocked(controller)
    if blocked is not None:
        return _blocked(*blocked)
    assert current is not None
    unsupported = unsupported_additive_patch_reason(patch_plan, old_topology, new_topology)
    if unsupported is not None:
        return _blocked(*unsupported)

    backend = build_backend(
        controller._backend_factory,
        socket_path=current.tmux_socket_path,
        namespace_state=current,
    )
    remember_namespace_state_ref(backend, current)
    if not session_alive(backend, current.tmux_session_name, timeout_s=timeout_s):
        return _blocked('session_unavailable', 'project namespace tmux session is not alive')

    context = SimpleNamespace(
        backend=backend,
        current=current,
        desired_session_name=current.tmux_session_name,
    )
    preserved_agents = tuple(str(item) for item in tuple((patch_plan or {}).get('preserved_agents') or ()))
    removed_agents = _patch_plan_removed_agents(patch_plan)
    tracked_agents = tuple(dict.fromkeys((*preserved_agents, *removed_agents)))
    tracked_before = snapshot_preserved_agent_panes(controller, context, topology_plan=old_topology, agents=tracked_agents)
    preserved_before = _select_agents(tracked_before, preserved_agents)
    state = WindowPatchResult()
    mutation_error = _apply_mutations(
        controller,
        backend,
        current=current,
        old_topology=old_topology,
        new_topology=new_topology,
        preserved_before=tracked_before,
        state=state,
        timeout_s=timeout_s,
    )
    preserved_after = snapshot_preserved_agent_panes(controller, context, topology_plan=new_topology, agents=preserved_agents)
    if mutation_error is not None:
        return _failure_result('namespace_patch_failed', mutation_error, state, preserved_before, preserved_after)
    preservation_error = _preservation_error(preserved_before, preserved_after, preserved_agents)
    if preservation_error is not None:
        return _failure_result('preserved_agent_pane_changed', preservation_error, state, preserved_before, preserved_after)
    return _applied_result(state, preserved_before, preserved_after)


def _apply_mutations(
    controller,
    backend,
    *,
    current,
    old_topology,
    new_topology,
    preserved_before: dict[str, str],
    state: WindowPatchResult,
    timeout_s: float | None,
) -> Exception | None:
    try:
        moved_agents = tuple(sorted(agent for _source, _target, agent in moved_agent_targets(old_topology, new_topology)))
        create_new_windows(
            controller,
            backend,
            current=current,
            old_topology=old_topology,
            new_topology=new_topology,
            result=state,
            excluded_agents=moved_agents,
            timeout_s=timeout_s,
        )
        move_agent_panes(
            controller,
            backend,
            old_topology=old_topology,
            new_topology=new_topology,
            existing_agent_panes=preserved_before,
            current=current,
            result=state,
            timeout_s=timeout_s,
        )
        state.agent_panes.update(
            append_agent_panes(
                controller,
                backend,
                old_topology=old_topology,
                new_topology=new_topology,
                existing_agent_panes=preserved_before,
                current=current,
                result=state,
                namespace_epoch=current.namespace_epoch,
                created_panes=state.created_panes,
                timeout_s=timeout_s,
                excluded_agents=moved_agents,
            )
        )
        remove_agent_panes(
            controller,
            backend,
            old_topology=old_topology,
            new_topology=new_topology,
            existing_agent_panes=preserved_before,
            current=current,
            result=state,
            timeout_s=timeout_s,
            excluded_agents=moved_agents,
        )
        remove_tool_windows(
            backend,
            old_topology=old_topology,
            new_topology=new_topology,
            current=current,
            result=state,
            timeout_s=timeout_s,
        )
    except Exception as exc:
        return exc
    return None


def _preservation_error(
    preserved_before: dict[str, str],
    preserved_after: dict[str, str],
    preserved_agents: tuple[str, ...],
) -> Exception | None:
    try:
        assert_preserved_agent_panes(preserved_before, preserved_after, expected_agents=preserved_agents)
    except Exception as exc:
        return exc
    return None


def _failure_result(
    reason: str,
    exc: Exception,
    state: WindowPatchResult,
    preserved_before: dict[str, str],
    preserved_after: dict[str, str],
) -> NamespacePatchApplyResult:
    diagnostics = {
        'reason': reason,
        'error_type': type(exc).__name__,
        'error': str(exc),
        'graph_published': False,
        'runtime_authority_written': False,
        'lease_or_lifecycle_written': False,
    }
    for attr in ('category', 'operation', 'backend_impl', 'ipc_ref', 'evidence'):
        value = getattr(exc, attr, None)
        if value:
            diagnostics[f'error_{attr}'] = redact_sensitive_diagnostic_value(value)
    return NamespacePatchApplyResult(
        status='failed',
        created_windows=tuple(state.created_windows),
        created_panes=tuple(state.created_panes),
        agent_panes=state.agent_panes,
        sidebar_panes=state.sidebar_panes,
        removed_windows=tuple(state.removed_windows),
        removed_panes=tuple(state.removed_panes),
        removed_agents=state.removed_agents,
        moved_agents=state.moved_agents,
        moved_agent_windows=state.moved_agent_windows,
        reflowed_windows=tuple(state.reflowed_windows),
        reflow_errors=state.reflow_errors,
        tool_panes=state.tool_panes,
        preserved_before=preserved_before,
        preserved_after=preserved_after,
        partial=bool(
            state.created_windows
            or state.created_panes
            or state.removed_windows
            or state.removed_panes
            or state.moved_agents
        ),
        rollback_actions=(
            tuple(f'created_pane:{pane}' for pane in state.created_panes)
            + tuple(f'removed_pane:{pane}' for pane in state.removed_panes)
            + tuple(f'removed_window:{window}' for window in state.removed_windows)
            + tuple(f'moved_agent:{agent}' for agent in state.moved_agents)
        ),
        diagnostics=diagnostics,
    )


def _applied_result(
    state: WindowPatchResult,
    preserved_before: dict[str, str],
    preserved_after: dict[str, str],
) -> NamespacePatchApplyResult:
    return NamespacePatchApplyResult(
        status='applied',
        created_windows=tuple(state.created_windows),
        created_panes=tuple(state.created_panes),
        agent_panes=state.agent_panes,
        sidebar_panes=state.sidebar_panes,
        removed_windows=tuple(state.removed_windows),
        removed_panes=tuple(state.removed_panes),
        removed_agents=state.removed_agents,
        moved_agents=state.moved_agents,
        moved_agent_windows=state.moved_agent_windows,
        reflowed_windows=tuple(state.reflowed_windows),
        reflow_errors=state.reflow_errors,
        tool_panes=state.tool_panes,
        preserved_before=preserved_before,
        preserved_after=preserved_after,
        partial=False,
        diagnostics={
            'supported_operations': ['add_window', 'add_agent', 'remove_agent', 'move_agent', 'add_tool_window', 'remove_tool_window'],
            'namespace_state_written': False,
            'graph_published': False,
            'runtime_authority_written': False,
            'lease_or_lifecycle_written': False,
        },
    )


def _blocked(reason: str, message: str) -> NamespacePatchApplyResult:
    return NamespacePatchApplyResult(
        status='blocked',
        diagnostics={
            'reason': reason,
            'message': message,
            'graph_published': False,
            'runtime_authority_written': False,
            'lease_or_lifecycle_written': False,
        },
    )


def _patch_plan_removed_agents(patch_plan: dict[str, object]) -> tuple[str, ...]:
    agents = []
    for step in tuple((patch_plan or {}).get('steps') or ()):
        if not isinstance(step, dict):
            continue
        if str(step.get('action') or '') != 'kill_agent_pane':
            continue
        agent_name = str(step.get('agent') or '').strip()
        if agent_name:
            agents.append(agent_name)
    return tuple(dict.fromkeys(agents))


def _select_agents(panes: dict[str, str], agents: tuple[str, ...]) -> dict[str, str]:
    return {agent: panes[agent] for agent in agents if agent in panes}


__all__ = ['NamespacePatchApplyResult', 'apply_additive_patch', 'apply_reload_patch']
