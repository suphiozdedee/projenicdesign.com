from __future__ import annotations

from dataclasses import dataclass, replace

from .backend import build_backend, remember_namespace_state_ref, session_alive
from .records import normalized_layout_signature


@dataclass(frozen=True)
class NamespaceEnsureContext:
    current: object | None
    backend: object
    session_is_alive: bool
    desired_socket_path: str
    desired_session_name: str
    desired_layout_signature: str | None
    desired_control_window_name: str
    desired_workspace_window_name: str
    topology_plan: object | None
    recreate_cause: str | None

    def with_updates(
        self,
        *,
        backend=...,
        session_is_alive=...,
        recreate_cause=...,
    ) -> 'NamespaceEnsureContext':
        updates = {}
        if backend is not ...:
            updates['backend'] = backend
        if session_is_alive is not ...:
            updates['session_is_alive'] = bool(session_is_alive)
        if recreate_cause is not ...:
            updates['recreate_cause'] = recreate_cause
        return replace(self, **updates)


def desired_namespace_state(
    controller,
    *,
    layout_signature: str | None,
    topology_plan: object | None = None,
) -> tuple[str, str, str | None, str, str]:
    desired_socket_path = str(controller._layout.ccbd_tmux_socket_path)
    desired_session_name = controller._layout.ccbd_tmux_session_name
    desired_layout_signature = normalized_layout_signature(
        getattr(topology_plan, 'signature', None) or layout_signature
    )
    desired_control_window_name = controller._layout.ccbd_tmux_control_window_name
    desired_workspace_window_name = (
        str(getattr(topology_plan, 'entry_window', '') or '').strip()
        or controller._layout.ccbd_tmux_workspace_window_name
    )
    return (
        desired_socket_path,
        desired_session_name,
        desired_layout_signature,
        desired_control_window_name,
        desired_workspace_window_name,
    )


def load_namespace_context(
    controller,
    *,
    layout_signature: str | None,
    topology_plan: object | None = None,
    recreate_reason: str | None,
) -> NamespaceEnsureContext:
    (
        desired_socket_path,
        desired_session_name,
        desired_layout_signature,
        desired_control_window_name,
        desired_workspace_window_name,
    ) = (
        desired_namespace_state(
            controller,
            layout_signature=layout_signature,
            topology_plan=topology_plan,
        )
    )
    current = controller._state_store.load()
    backend = build_backend(
        controller._backend_factory,
        socket_path=desired_socket_path,
        namespace_state=current,
    )
    remember_namespace_state_ref(backend, current)
    return NamespaceEnsureContext(
        current=current,
        backend=backend,
        session_is_alive=False,
        desired_socket_path=desired_socket_path,
        desired_session_name=desired_session_name,
        desired_layout_signature=desired_layout_signature,
        desired_control_window_name=desired_control_window_name,
        desired_workspace_window_name=desired_workspace_window_name,
        topology_plan=topology_plan,
        recreate_cause=str(recreate_reason or '').strip() or None,
    )


def refresh_session_liveness(
    controller,
    context: NamespaceEnsureContext,
    *,
    timeout_s: float | None = None,
) -> NamespaceEnsureContext:
    del controller
    if context.current is None:
        return context.with_updates(session_is_alive=False)
    return context.with_updates(
        session_is_alive=session_alive(
            context.backend,
            context.desired_session_name,
            timeout_s=timeout_s,
        )
    )


def rebuild_namespace_backend(controller, *, socket_path: str, namespace_state=None):
    return build_backend(
        controller._backend_factory,
        socket_path=socket_path,
        namespace_state=namespace_state,
    )


__all__ = [
    'NamespaceEnsureContext',
    'desired_namespace_state',
    'load_namespace_context',
    'refresh_session_liveness',
    'rebuild_namespace_backend',
]
