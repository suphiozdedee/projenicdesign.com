from __future__ import annotations

import os

from storage.paths import PathLayout
from terminal_runtime import (
    TmuxBackend,
    get_backend as resolve_terminal_backend,
    get_backend_for_namespace_teardown,
)
from terminal_runtime.mux_backend_contract import MuxCommandErrorV2

from ccbd.system import utc_now

from .backend import build_backend, session_root_pane, session_window_target, window_root_pane
from .controller_state import ProjectNamespaceControllerState, ProjectNamespaceControllerStateMixin
from .destroy import destroy_project_namespace
from .ensure import ensure_project_namespace
from .additive_patch import apply_additive_patch, apply_reload_patch
from .models import ProjectNamespace
from .reflow import reflow_project_workspace
from .records import namespace_from_state
from ..project_namespace_state import ProjectNamespaceEventStore, ProjectNamespaceStateStore
from ..project_namespace_pane import snapshot_project_namespace_panes


def default_project_namespace_backend(*, socket_path: str | None = None, namespace_state=None):
    backend_impl = str(getattr(namespace_state, 'backend_impl', '') or '').strip()
    backend_family = str(getattr(namespace_state, 'namespace_backend_family', '') or '').strip()
    if (
        namespace_state is not None
        and backend_family != 'herdr-native'
        and backend_impl != 'herdr'
        and not _herdr_runtime_configured()
    ):
        return TmuxBackend(socket_path=socket_path)
    requested_backend = backend_impl if backend_impl in {'tmux', 'herdr'} else None
    if namespace_state is not None and backend_family == 'herdr-native':
        requested_backend = 'herdr'
    if _herdr_runtime_configured():
        requested_backend = 'herdr'
    if requested_backend == 'herdr':
        return _select_herdr_backend()
    backend = resolve_terminal_backend(requested_backend)
    if (
        namespace_state is None
        and _herdr_runtime_configured()
        and str(getattr(backend, 'backend_impl', '') or '').strip() != 'herdr'
    ):
        return _select_herdr_backend()
    if backend is None and namespace_state is None:
        return _select_herdr_backend()
    # ``terminal_runtime.get_backend()`` may return its process-global cached
    # TmuxBackend.  Project namespaces must never reuse that backend because
    # its socket is either the ambient tmux server or another project's
    # socket.  Keep backend auto-selection for Herdr and test doubles, but
    # always bind the real tmux implementation to this project's socket.
    if isinstance(backend, TmuxBackend):
        return TmuxBackend(socket_path=socket_path)
    if backend is not None:
        return backend
    return TmuxBackend(socket_path=socket_path)


def backend_for_namespace_teardown(*, socket_path: str | None = None, namespace_state=None):
    """Build a backend that can tear down an already-persisted namespace.

    Unlike backend selection, teardown must not re-run the Herdr
    capability-evidence gate: the persisted namespace state already proves Herdr
    was validated at creation time, and teardown runs from processes (``ccb
    kill``) that do not carry the ambient capability-evidence env.  Re-attach
    directly from the persisted namespace ref with a teardown-only gate.
    """
    backend_impl = str(getattr(namespace_state, 'backend_impl', '') or '').strip()
    backend_family = str(getattr(namespace_state, 'namespace_backend_family', '') or '').strip()
    if (
        namespace_state is not None
        and (backend_family == 'herdr-native' or backend_impl == 'herdr')
        and callable(getattr(namespace_state, 'namespace_ref', None))
    ):
        return get_backend_for_namespace_teardown(namespace_state.namespace_ref())
    return TmuxBackend(socket_path=socket_path)


def _select_herdr_backend():
    try:
        backend = resolve_terminal_backend('herdr')
    except MuxCommandErrorV2 as exc:
        raise _herdr_selection_error(cause=exc) from exc
    if backend is not None:
        return backend
    raise _herdr_selection_error()


def _herdr_selection_error(*, cause: MuxCommandErrorV2 | None = None) -> MuxCommandErrorV2:
    evidence: dict[str, object] = {}
    if cause is not None:
        evidence = {
            'cause_category': cause.category,
            'cause_operation': cause.operation,
            'cause_detail': cause.detail,
        }
    return MuxCommandErrorV2(
        category='unsupported',
        backend_impl='herdr',
        operation='select_backend',
        detail='Herdr project namespace backend selection failed',
        evidence=evidence,
    )


def _herdr_runtime_configured() -> bool:
    return any(
        os.environ.get(name, '').strip()
        for name in (
            'CCB_HERDR_CAPABILITY_REPORT',
            'CCB_HERDR_SOCKET_REF',
        )
    )


class ProjectNamespaceController(ProjectNamespaceControllerStateMixin):
    def __init__(
        self,
        layout: PathLayout,
        project_id: str,
        *,
        clock=utc_now,
        backend_factory=None,
        state_store: ProjectNamespaceStateStore | None = None,
        event_store: ProjectNamespaceEventStore | None = None,
        layout_version: int = 3,
    ) -> None:
        resolved_project_id = str(project_id or '').strip()
        if not resolved_project_id:
            raise ValueError('project_id cannot be empty')
        resolved_layout_version = int(layout_version)
        if resolved_layout_version <= 0:
            raise ValueError('layout_version must be positive')
        self._runtime_state = ProjectNamespaceControllerState(
            layout=layout,
            project_id=resolved_project_id,
            clock=clock,
            backend_factory=backend_factory or default_project_namespace_backend,
            state_store=state_store or ProjectNamespaceStateStore(layout),
            event_store=event_store or ProjectNamespaceEventStore(layout),
            layout_version=resolved_layout_version,
        )

    def load(self) -> ProjectNamespace | None:
        state = self._state_store.load()
        if state is None:
            return None
        return namespace_from_state(state)

    def ensure(
        self,
        *,
        layout_signature: str | None = None,
        topology_plan=None,
        force_recreate: bool = False,
        recreate_reason: str | None = None,
        session_probe_timeout_s: float | None = None,
        terminal_size: tuple[int, int] | None = None,
    ) -> ProjectNamespace:
        return ensure_project_namespace(
            self,
            layout_signature=layout_signature,
            topology_plan=topology_plan,
            force_recreate=force_recreate,
            recreate_reason=recreate_reason,
            session_probe_timeout_s=session_probe_timeout_s,
            terminal_size=terminal_size,
        )

    def destroy(self, *, reason: str, force: bool = False):
        del force
        return destroy_project_namespace(self, reason=reason)

    def _build_backend_for_destroy(self, *, socket_path: str, namespace_state=None):
        """Build the backend used to tear down this project's namespace.

        Honors an injected backend factory (e.g. test doubles) unchanged, but
        routes the default factory through the teardown path so destroy does not
        re-run the Herdr capability-evidence selection gate on a namespace that
        persisted state already proves is Herdr.
        """
        factory = self._backend_factory
        if factory is default_project_namespace_backend:
            return backend_for_namespace_teardown(socket_path=socket_path, namespace_state=namespace_state)
        return build_backend(factory, socket_path=socket_path, namespace_state=namespace_state)

    def reflow_workspace(
        self,
        *,
        layout_signature: str | None = None,
        reason: str | None = None,
        session_probe_timeout_s: float | None = None,
    ) -> ProjectNamespace:
        return reflow_project_workspace(
            self,
            layout_signature=layout_signature,
            reason=reason,
            session_probe_timeout_s=session_probe_timeout_s,
        )

    def apply_additive_patch(
        self,
        *,
        patch_plan: dict[str, object],
        old_topology,
        new_topology,
        timeout_s: float | None = None,
    ):
        return apply_additive_patch(
            self,
            patch_plan=patch_plan,
            old_topology=old_topology,
            new_topology=new_topology,
            timeout_s=timeout_s,
        )

    def apply_reload_patch(
        self,
        *,
        patch_plan: dict[str, object],
        old_topology,
        new_topology,
        timeout_s: float | None = None,
    ):
        return apply_reload_patch(
            self,
            patch_plan=patch_plan,
            old_topology=old_topology,
            new_topology=new_topology,
            timeout_s=timeout_s,
        )

    def root_pane_id(
        self,
        namespace: ProjectNamespace | None = None,
        *,
        timeout_s: float | None = None,
    ) -> str:
        current = namespace or self.load()
        if current is None:
            raise RuntimeError('project namespace is not available')
        backend = build_backend(
            self._backend_factory,
            socket_path=current.tmux_socket_path,
            namespace_state=current,
        )
        workspace_window_name = str(current.workspace_window_name or '').strip()
        pane_records = snapshot_project_namespace_panes(backend)
        if pane_records is not None:
            cmd_panes = [
                pane_id
                for pane_id, record in pane_records.items()
                if record.matches_authoritative_topology(
                    tmux_session_name=current.tmux_session_name,
                    project_id=self._project_id,
                    role='cmd',
                    slot_key='cmd',
                    window_name=workspace_window_name or None,
                    managed_by='ccbd',
                    namespace_epoch=current.namespace_epoch,
                )
            ]
            if len(cmd_panes) == 1:
                return cmd_panes[0]
            if len(cmd_panes) > 1:
                raise RuntimeError('project namespace has multiple authoritative cmd panes')
        if workspace_window_name:
            return window_root_pane(
                backend,
                target_window=session_window_target(current.tmux_session_name, workspace_window_name),
                timeout_s=timeout_s,
            )
        return session_root_pane(backend, current.tmux_session_name, timeout_s=timeout_s)


__all__ = [
    'ProjectNamespaceController',
    'backend_for_namespace_teardown',
    'default_project_namespace_backend',
]
