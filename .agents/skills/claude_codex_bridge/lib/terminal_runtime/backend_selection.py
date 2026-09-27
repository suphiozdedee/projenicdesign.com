from __future__ import annotations

import os
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Callable, Mapping

from provider_runtime.session_payload import (
    backend_impl_from_session,
    namespace_ref_from_session,
    pane_id_from_session,
    pane_ref_from_session,
)
from terminal_runtime.backend_resolver import resolve_mux_backend_v2
from terminal_runtime.layouts import LayoutResult, create_tmux_auto_layout
from terminal_runtime.mux_backend_contract import MuxCommandErrorV2


@dataclass
class TerminalBackendSelection:
    detect_terminal_fn: Callable[[], object | None]
    tmux_backend_factory: Callable[[], object]
    herdr_backend_factory: Callable[[], object] | None = None
    platform_gate_fn: Callable[[], Mapping[str, object] | None] | None = None
    herdr_capability_report_fn: Callable[[], Mapping[str, object] | None] | None = None
    herdr_capability_report_ref_fn: Callable[[], str | None] | None = None
    cached_backend: object | None = None
    cached_backend_selection: str | None = None
    explicit_backend_cache: dict[str, object] = field(default_factory=dict)

    def get_backend(self, terminal_type: str | None = None) -> object | None:
        selected = terminal_type or self.detect_terminal_fn()
        cacheable_implicit = terminal_type is None and selected == 'tmux'
        if (
            cacheable_implicit
            and self.cached_backend is not None
            and self.cached_backend_selection == selected
        ):
            return self.cached_backend
        if terminal_type is not None and terminal_type in self.explicit_backend_cache:
            return self.explicit_backend_cache[terminal_type]
        if terminal_type is None and selected is None and self.herdr_backend_factory is not None:
            selected = 'auto'
        if selected == 'tmux':
            backend = self.tmux_backend_factory()
        elif selected in {'herdr', 'auto'}:
            backend = self._get_herdr_backend(str(selected))
        else:
            backend = None
        if cacheable_implicit and backend is not None:
            self.cached_backend = backend
            self.cached_backend_selection = str(selected)
        elif terminal_type is not None and backend is not None:
            self.explicit_backend_cache[terminal_type] = backend
        return backend

    def _get_herdr_backend(self, requested_backend: str) -> object | None:
        if self.herdr_backend_factory is None:
            return None
        platform_gate = self.platform_gate_fn() if self.platform_gate_fn is not None else None
        capability_report = (
            self.herdr_capability_report_fn()
            if self.herdr_capability_report_fn is not None
            else None
        )
        capability_report_ref = (
            self.herdr_capability_report_ref_fn()
            if self.herdr_capability_report_ref_fn is not None
            else None
        )
        selection = resolve_mux_backend_v2(
            requested_backend=requested_backend,  # type: ignore[arg-type]
            source='cli' if requested_backend == 'herdr' else 'platform_default',
            platform_gate=platform_gate,
            capability_report=capability_report,  # type: ignore[arg-type]
            capability_report_ref=capability_report_ref,
        )
        if selection.get('blocked') is True or selection.get('effective_backend') != 'herdr':
            if requested_backend == 'herdr':
                raise MuxCommandErrorV2(
                    category=_selection_error_category(selection.get('failure_reason')),
                    backend_impl='herdr',
                    operation='select_backend',
                    detail=str(selection.get('diagnostic') or 'Herdr backend selection failed'),
                    evidence={'selection': dict(selection)},
                )
            if selection.get('effective_backend') == 'tmux':
                return self.tmux_backend_factory()
            return None
        try:
            backend = self.herdr_backend_factory()
            prepare_server = getattr(backend, 'prepare_server', None)
            if callable(prepare_server):
                prepare_server()
        except MuxCommandErrorV2:
            if requested_backend == 'herdr':
                raise
            return None
        except Exception as exc:
            if requested_backend == 'herdr':
                raise MuxCommandErrorV2(
                    category='transient-unavailable',
                    backend_impl='herdr',
                    operation='select_backend',
                    detail=f'Herdr backend initialization failed: {exc}',
                ) from exc
            return None
        return backend

    def get_backend_for_session(self, session_data: dict) -> object:
        backend_impl = backend_impl_from_session(session_data)
        if backend_impl == 'herdr':
            if self.herdr_backend_factory is None:
                raise MuxCommandErrorV2(
                    category='unsupported',
                    backend_impl='herdr',
                    operation='select_backend_for_session',
                    detail='Herdr backend factory is not available for session backend_impl=herdr',
                )
            backend = self.herdr_backend_factory()
            namespace_ref = namespace_ref_from_session(session_data)
            if namespace_ref is not None:
                attach = getattr(backend, 'attach_persisted_session', None)
                if callable(attach):
                    attach(
                        namespace_ref,
                        pane_id=pane_id_from_session(session_data),
                        pane_ref=pane_ref_from_session(session_data),
                    )
                else:
                    setattr(backend, '_ccb_project_namespace_ref', namespace_ref)
            return backend
        if backend_impl not in {None, 'tmux', 'rmux', 'psmux'}:
            raise ValueError(f'unsupported session backend_impl: {backend_impl}')
        socket_name = str(session_data.get('tmux_socket_name') or '').strip() or None
        socket_path = str(session_data.get('tmux_socket_path') or '').strip() or None
        try:
            return self.tmux_backend_factory(socket_name=socket_name, socket_path=socket_path)
        except TypeError:
            return self.tmux_backend_factory()

    @staticmethod
    def get_pane_id_from_session(session_data: dict) -> str | None:
        return pane_id_from_session(session_data)


def _selection_error_category(failure_reason: object) -> str:
    if failure_reason == 'schema-mismatch':
        return 'schema-mismatch'
    if failure_reason in {'herdr-capability-missing', 'unsupported-capability'}:
        return 'unsupported'
    return 'transient-unavailable'


@dataclass
class TerminalLayoutService:
    tmux_backend_factory: Callable[[], object]
    detached_session_name_fn: Callable[..., str]
    os_getpid_fn: Callable[[], int] = os.getpid
    time_fn: Callable[[], float] = time.time
    env: dict[str, str] | None = None

    def create_auto_layout(
        self,
        providers: list[str],
        *,
        cwd: str,
        root_pane_id: str | None = None,
        tmux_session_name: str | None = None,
        percent: int = 50,
        set_markers: bool = True,
        marker_prefix: str = 'CCB',
    ) -> LayoutResult:
        env = self.env if self.env is not None else os.environ
        return create_tmux_auto_layout(
            providers,
            cwd=cwd,
            backend=self.tmux_backend_factory(),
            root_pane_id=root_pane_id,
            tmux_session_name=tmux_session_name,
            percent=percent,
            set_markers=set_markers,
            marker_prefix=marker_prefix,
            detached_session_name=self.detached_session_name_fn(
                cwd=cwd,
                pid=self.os_getpid_fn(),
                now_ts=self.time_fn(),
            ),
            inside_tmux=bool((env.get('TMUX') or '').strip()),
        )
