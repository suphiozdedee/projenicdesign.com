from __future__ import annotations

from .session_file import declared_binding_tmux_socket_path
from .validation_context import (
    BindingValidationContext,
    binding_matches_project_socket,
    binding_matches_assigned_pane,
    binding_runtime_ref,
    binding_pane_state,
    binding_with_namespace_record,
    declares_current_project_socket,
    has_acceptable_provider_runtime_identity,
    has_no_provider_runtime_identity_mismatch,
    has_project_tmux_session_name,
    has_reusable_tmux_pane,
    is_live_tmux_binding,
    matching_namespace_binding,
)


def usable_project_namespace_binding_for_context(binding, *, context: BindingValidationContext):
    if not is_live_tmux_binding(binding):
        return None
    if not has_acceptable_provider_runtime_identity(binding):
        return None
    if not binding_matches_project_socket(binding, context=context):
        return None
    if binding_matches_assigned_pane(binding, context=context):
        if str(binding_runtime_ref(binding)).strip().split(':', 1)[0].lower() != 'tmux':
            return binding
    record = matching_namespace_binding(binding=binding, context=context)
    if record is None:
        return None
    return binding_with_namespace_record(binding, record)


def usable_agent_only_project_binding_for_context(binding, *, context: BindingValidationContext):
    if not has_reusable_tmux_pane(binding):
        return None
    if not has_no_provider_runtime_identity_mismatch(binding):
        return None

    pane_state = binding_pane_state(binding)
    binding_socket_declared, binding_socket_path = declared_binding_tmux_socket_path(binding)
    if not binding_socket_declared:
        return binding

    if binding_socket_path and not declares_current_project_socket(binding_socket_path, context=context):
        return None
    if pane_state in {'dead', 'foreign'}:
        return None

    if declares_current_project_socket(binding_socket_path, context=context):
        record = matching_namespace_binding(binding=binding, context=context)
        if record is not None:
            return binding_with_namespace_record(binding, record)
        if has_project_tmux_session_name(context):
            return None

    if pane_state in {'', 'alive', 'unknown'}:
        return binding
    return None


__all__ = [
    'usable_agent_only_project_binding_for_context',
    'usable_project_namespace_binding_for_context',
]
