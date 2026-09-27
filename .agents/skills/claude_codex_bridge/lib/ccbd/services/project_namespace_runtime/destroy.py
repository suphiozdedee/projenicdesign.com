from __future__ import annotations

from terminal_runtime.mux_backend_contract import MuxCommandErrorV2

from .backend import kill_server, remember_namespace_state_ref
from .records import build_destroy_summary, build_destroyed_event, build_destroyed_state


def destroy_project_namespace(controller, *, reason: str):
    normalized_reason = str(reason or '').strip() or 'destroyed'
    controller._layout.ccbd_dir.mkdir(parents=True, exist_ok=True)
    current = controller._state_store.load()
    occurred_at = controller._clock()
    tmux_socket_path = str(current.tmux_socket_path) if current is not None else str(controller._layout.ccbd_tmux_socket_path)
    tmux_session_name = str(current.tmux_session_name) if current is not None else controller._layout.ccbd_tmux_session_name
    backend = controller._build_backend_for_destroy(
        socket_path=tmux_socket_path,
        namespace_state=current,
    )
    remember_namespace_state_ref(backend, current)
    destroyed = _kill_server_best_effort(backend, current)
    next_state = build_destroyed_state(
        current=current,
        project_id=controller._project_id,
        occurred_at=occurred_at,
        reason=normalized_reason,
        tmux_socket_path=tmux_socket_path,
        tmux_session_name=tmux_session_name,
        layout_version=controller._layout_version,
        control_window_name=(
            str(current.control_window_name)
            if current is not None and current.control_window_name
            else controller._layout.ccbd_tmux_control_window_name
        ),
        workspace_window_name=(
            str(current.workspace_window_name)
            if current is not None and current.workspace_window_name
            else controller._layout.ccbd_tmux_workspace_window_name
        ),
    )
    controller._state_store.save(next_state)
    controller._event_store.append(
        build_destroyed_event(
            project_id=controller._project_id,
            occurred_at=occurred_at,
            namespace_epoch=next_state.namespace_epoch,
            tmux_socket_path=tmux_socket_path,
            tmux_session_name=tmux_session_name,
            namespace_backend_family=next_state.namespace_backend_family,
            backend_impl=next_state.backend_impl,
            namespace_id=next_state.namespace_id,
            namespace_session_name=next_state.namespace_session_name,
            namespace_ipc_kind=next_state.namespace_ipc_kind,
            namespace_ipc_ref=next_state.namespace_ipc_ref,
            namespace_restore_token=next_state.namespace_restore_token,
            destroyed=destroyed,
            reason=normalized_reason,
        )
    )
    return build_destroy_summary(
        project_id=controller._project_id,
        namespace_epoch=next_state.namespace_epoch,
        tmux_socket_path=tmux_socket_path,
        tmux_session_name=tmux_session_name,
        destroyed=destroyed,
        reason=normalized_reason,
    )


def _kill_server_best_effort(backend, namespace_state) -> bool:
    try:
        return kill_server(backend)
    except MuxCommandErrorV2 as exc:
        if _teardown_target_is_unavailable(exc, namespace_state):
            return False
        raise


def _teardown_target_is_unavailable(exc: MuxCommandErrorV2, namespace_state) -> bool:
    backend_impl = str(getattr(namespace_state, 'backend_impl', '') or '').strip()
    backend_family = str(getattr(namespace_state, 'namespace_backend_family', '') or '').strip()
    if backend_impl != 'herdr' and backend_family != 'herdr-native':
        return False
    return exc.category in {'not-found', 'transient-unavailable'} or exc.operation in {
        'resolve_executable',
        'server_info',
    }


__all__ = ['destroy_project_namespace']
