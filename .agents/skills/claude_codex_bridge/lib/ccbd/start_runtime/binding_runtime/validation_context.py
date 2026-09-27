from __future__ import annotations

from dataclasses import dataclass, replace

from .common import (
    binding_pane_id,
    is_pane_runtime_ref,
    matching_project_namespace_record,
    runtime_ref_backend,
)


@dataclass(frozen=True)
class BindingValidationContext:
    tmux_socket_path: str
    tmux_session_name: str | None
    workspace_window_id: str | None
    agent_name: str
    project_id: str
    window_name: str | None
    namespace_epoch: int | None
    assigned_pane_id: str | None
    namespace_pane_records: dict[str, object] | None
    tmux_backend_factory: object
    inspect_project_namespace_pane_fn: object
    same_tmux_socket_path_fn: object


def binding_runtime_ref(binding) -> str:
    return str(getattr(binding, 'runtime_ref', None) or '').strip()


def binding_pane_state(binding) -> str:
    return str(getattr(binding, 'pane_state', None) or '').strip().lower()


def is_tmux_binding(binding) -> bool:
    return is_pane_runtime_ref(binding_runtime_ref(binding))


def build_binding_validation_context(
    *,
    tmux_socket_path: str,
    tmux_session_name: str | None,
    workspace_window_id: str | None,
    agent_name: str,
    project_id: str,
    tmux_backend_factory,
    inspect_project_namespace_pane_fn,
    same_tmux_socket_path_fn,
    window_name: str | None = None,
    namespace_epoch: int | None = None,
    assigned_pane_id: str | None = None,
    namespace_pane_records: dict[str, object] | None = None,
) -> BindingValidationContext:
    return BindingValidationContext(
        tmux_socket_path=tmux_socket_path,
        tmux_session_name=tmux_session_name,
        workspace_window_id=workspace_window_id,
        agent_name=agent_name,
        project_id=project_id,
        window_name=window_name,
        namespace_epoch=namespace_epoch,
        assigned_pane_id=str(assigned_pane_id or '').strip() or None,
        namespace_pane_records=namespace_pane_records,
        tmux_backend_factory=tmux_backend_factory,
        inspect_project_namespace_pane_fn=inspect_project_namespace_pane_fn,
        same_tmux_socket_path_fn=same_tmux_socket_path_fn,
    )


def matching_namespace_binding(binding, *, context: BindingValidationContext):
    return matching_project_namespace_record(
        binding=binding,
        tmux_socket_path=context.tmux_socket_path,
        tmux_session_name=context.tmux_session_name,
        workspace_window_id=context.workspace_window_id,
        agent_name=context.agent_name,
        project_id=context.project_id,
        window_name=context.window_name,
        namespace_epoch=context.namespace_epoch,
        tmux_backend_factory=context.tmux_backend_factory,
        inspect_project_namespace_pane_fn=context.inspect_project_namespace_pane_fn,
        namespace_pane_records=context.namespace_pane_records,
    )


def binding_matches_assigned_pane(binding, *, context: BindingValidationContext) -> bool:
    assigned_pane_id = str(context.assigned_pane_id or '').strip()
    if not assigned_pane_id:
        return False
    return binding_pane_id(binding) == assigned_pane_id


def binding_with_namespace_record(binding, record):
    updates = {}
    if hasattr(binding, 'tmux_window_id'):
        updates['tmux_window_id'] = getattr(record, 'window_id', None)
    if hasattr(binding, 'tmux_window_name'):
        updates['tmux_window_name'] = (
            getattr(record, 'ccb_window', None)
            or getattr(record, 'window_name', None)
        )
    if not updates:
        return binding
    try:
        return replace(binding, **updates)
    except (TypeError, ValueError):
        return binding


def binding_matches_project_socket(binding, *, context: BindingValidationContext) -> bool:
    if runtime_ref_backend(binding_runtime_ref(binding)) != 'tmux':
        return True
    return context.same_tmux_socket_path_fn(
        getattr(binding, 'tmux_socket_path', None),
        context.tmux_socket_path,
    )


def binding_has_live_namespace_record(binding, *, context: BindingValidationContext) -> bool:
    return matching_namespace_binding(binding=binding, context=context) is not None


def is_live_tmux_binding(binding) -> bool:
    if binding is None or not is_tmux_binding(binding):
        return False
    pane_state = binding_pane_state(binding)
    if runtime_ref_backend(binding_runtime_ref(binding)) == 'tmux':
        return pane_state == 'alive'
    return pane_state in {'', 'alive', 'unknown'}


def has_reusable_tmux_pane(binding) -> bool:
    return binding is not None and is_tmux_binding(binding) and binding_pane_id(binding) is not None


def has_acceptable_provider_runtime_identity(binding) -> bool:
    state = str(getattr(binding, 'provider_identity_state', None) or '').strip().lower()
    if not state:
        return True
    return state in {'match', 'rotated_in_process'}


def has_no_provider_runtime_identity_mismatch(binding) -> bool:
    state = str(getattr(binding, 'provider_identity_state', None) or '').strip().lower()
    return state != 'mismatch'


def declares_current_project_socket(binding_socket_path: str | None, *, context: BindingValidationContext) -> bool:
    return context.same_tmux_socket_path_fn(binding_socket_path, context.tmux_socket_path)


def has_project_tmux_session_name(context: BindingValidationContext) -> bool:
    return bool(str(context.tmux_session_name or '').strip())


__all__ = [
    'BindingValidationContext',
    'binding_has_live_namespace_record',
    'binding_matches_assigned_pane',
    'binding_with_namespace_record',
    'binding_matches_project_socket',
    'binding_pane_state',
    'build_binding_validation_context',
    'declares_current_project_socket',
    'has_acceptable_provider_runtime_identity',
    'has_no_provider_runtime_identity_mismatch',
    'has_project_tmux_session_name',
    'has_reusable_tmux_pane',
    'is_live_tmux_binding',
]
