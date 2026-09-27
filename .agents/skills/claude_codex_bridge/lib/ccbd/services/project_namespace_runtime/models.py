from __future__ import annotations

from dataclasses import dataclass

from ..project_namespace_state_runtime.namespace_projection import (
    HERDR_BACKEND_FAMILY,
    HERDR_IPC_KIND,
    NAMESPACE_BACKEND_FAMILY,
    resolved_namespace_backend_family,
)


@dataclass(frozen=True)
class ProjectNamespace:
    project_id: str
    namespace_epoch: int
    tmux_socket_path: str
    tmux_session_name: str
    layout_version: int
    layout_signature: str | None
    control_window_name: str | None
    control_window_id: str | None
    workspace_window_name: str | None
    workspace_window_id: str | None
    workspace_epoch: int
    ui_attachable: bool
    namespace_backend_family: str = NAMESPACE_BACKEND_FAMILY
    backend_impl: str = 'tmux'
    namespace_id: str | None = None
    namespace_session_name: str | None = None
    namespace_ipc_kind: str | None = None
    namespace_ipc_ref: str | None = None
    namespace_restore_token: str | None = None
    created_this_call: bool = False
    workspace_recreated_this_call: bool = False

    @classmethod
    def from_state(cls, state) -> ProjectNamespace:
        return cls(
            project_id=state.project_id,
            namespace_epoch=state.namespace_epoch,
            tmux_socket_path=state.tmux_socket_path,
            tmux_session_name=state.tmux_session_name,
            namespace_backend_family=state.namespace_backend_family,
            backend_impl=state.backend_impl,
            namespace_id=state.namespace_id,
            namespace_session_name=state.namespace_session_name,
            namespace_ipc_kind=state.namespace_ipc_kind,
            namespace_ipc_ref=state.namespace_ipc_ref,
            namespace_restore_token=state.namespace_restore_token,
            layout_version=state.layout_version,
            layout_signature=state.layout_signature,
            control_window_name=state.control_window_name,
            control_window_id=state.control_window_id,
            workspace_window_name=state.workspace_window_name,
            workspace_window_id=state.workspace_window_id,
            workspace_epoch=state.workspace_epoch,
            ui_attachable=state.ui_attachable,
            created_this_call=False,
            workspace_recreated_this_call=False,
        )

    def namespace_ref(self) -> dict[str, object]:
        if resolved_namespace_backend_family(self.backend_impl, self.namespace_backend_family) == HERDR_BACKEND_FAMILY:
            return {
                'backend_family': HERDR_BACKEND_FAMILY,
                'backend_impl': 'herdr',
                'namespace_id': str(self.namespace_id or ''),
                'session_name': str(self.namespace_session_name or self.tmux_session_name),
                'ipc_kind': HERDR_IPC_KIND,
                'ipc_ref': str(self.namespace_ipc_ref or ''),
                'restore_token': self.namespace_restore_token,
            }
        return {
            'backend_family': NAMESPACE_BACKEND_FAMILY,
            'backend_impl': self.backend_impl,
            'namespace_id': self.tmux_session_name,
            'session_name': self.tmux_session_name,
            'ipc_kind': self.namespace_ipc_kind or 'socket_path',
            'ipc_ref': self.namespace_ipc_ref or self.tmux_socket_path,
            'restore_token': None,
        }


@dataclass(frozen=True)
class ProjectNamespaceDestroySummary:
    project_id: str
    namespace_epoch: int | None
    tmux_socket_path: str
    tmux_session_name: str
    destroyed: bool
    reason: str


__all__ = ['ProjectNamespace', 'ProjectNamespaceDestroySummary']
