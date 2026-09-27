from __future__ import annotations


def ready_namespace_or_blocked(controller) -> tuple[object | None, tuple[str, str] | None]:
    current = controller._state_store.load()
    if current is None:
        return None, ('namespace_missing', 'project namespace state is not available')
    reason = _namespace_blocked_reason(controller, current)
    if reason is not None:
        return None, reason
    return current, None


def _namespace_blocked_reason(controller, current) -> tuple[str, str] | None:
    if not bool(getattr(current, 'ui_attachable', True)):
        return ('namespace_not_attachable', 'project namespace is not UI attachable')
    if str(getattr(current, 'project_id', '') or '').strip() != str(controller._project_id):
        return ('project_id_mismatch', 'project namespace project_id does not match controller project_id')
    if getattr(current, 'namespace_epoch', None) is None:
        return ('namespace_epoch_missing', 'project namespace epoch is missing')
    if str(getattr(current, 'tmux_session_name', '') or '').strip() == '':
        return ('tmux_session_name_missing', 'project namespace tmux session name is missing')
    if _is_herdr_namespace(current):
        if str(getattr(current, 'namespace_id', '') or '').strip() == '':
            return ('namespace_id_missing', 'project namespace id is missing')
        if str(getattr(current, 'namespace_ipc_ref', '') or '').strip() == '':
            return ('namespace_ipc_ref_missing', 'project namespace IPC reference is missing')
        if str(getattr(current, 'namespace_ipc_kind', '') or '').strip() == '':
            return ('namespace_ipc_kind_missing', 'project namespace IPC kind is missing')
        return None
    if str(getattr(current, 'tmux_socket_path', '') or '').strip() == '':
        return ('tmux_socket_path_missing', 'project namespace tmux socket path is missing')
    return None


def _is_herdr_namespace(current) -> bool:
    return (
        str(getattr(current, 'backend_impl', '') or '').strip() == 'herdr'
        or str(getattr(current, 'namespace_backend_family', '') or '').strip() == 'herdr-native'
    )


__all__ = ['ready_namespace_or_blocked']
