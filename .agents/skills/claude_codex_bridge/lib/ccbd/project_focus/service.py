from __future__ import annotations

from dataclasses import dataclass

from agents.models import AgentValidationError, normalize_agent_name
from agents.models_runtime.config_runtime.topology import validate_window_name
from ccbd.services.project_namespace_runtime.controller import ProjectNamespaceController

from .models import FocusErrorCode, ProjectFocusError, focus_success
from .tmux import backend_for_namespace, find_agent_pane, refresh_sidebar_panes, select_pane, select_window


@dataclass(frozen=True)
class ProjectFocusDependencies:
    project_id: str
    config: object
    namespace_controller: ProjectNamespaceController
    project_view_service: object | None = None


class ProjectFocusService:
    def __init__(self, deps: ProjectFocusDependencies) -> None:
        self._deps = deps

    def focus_window(self, *, window: str, namespace_epoch: int | None = None) -> dict[str, object]:
        window_name = _valid_window_name(window)
        window_spec = _window_spec(self._deps.config, window_name)
        namespace = _namespace(self._deps.namespace_controller)
        _validate_epoch(namespace_epoch, namespace.namespace_epoch)
        backend = backend_for_namespace(self._deps.namespace_controller._backend_factory, namespace)
        agent_names = tuple(getattr(window_spec, 'agent_names', ()) or ())
        agent_name = agent_names[0] if agent_names else None
        pane_id = (
            find_agent_pane(backend, project_id=self._deps.project_id, window_name=window_name, agent_name=agent_name)
            if agent_name is not None
            else None
        )
        try:
            select_window(backend, session_name=namespace.tmux_session_name, window_name=window_name)
        except ProjectFocusError as exc:
            if exc.code != FocusErrorCode.TARGET_MISSING or pane_id is None:
                raise
        if pane_id is not None:
            select_pane(backend, pane_id=pane_id)
        _invalidate_and_refresh_project_view(self._deps, backend, namespace)
        return focus_success(
            kind='window',
            window=window_name,
            agent=agent_name,
            namespace_epoch=namespace.namespace_epoch,
        )

    def focus_agent(self, *, agent: str, namespace_epoch: int | None = None) -> dict[str, object]:
        agent_name = _valid_agent_name(agent)
        window = _agent_window(self._deps.config, agent_name)
        namespace = _namespace(self._deps.namespace_controller)
        _validate_epoch(namespace_epoch, namespace.namespace_epoch)
        backend = backend_for_namespace(self._deps.namespace_controller._backend_factory, namespace)
        pane_id = find_agent_pane(
            backend,
            project_id=self._deps.project_id,
            window_name=window.name,
            agent_name=agent_name,
        )
        if pane_id is None:
            raise ProjectFocusError(FocusErrorCode.TARGET_MISSING, f'agent pane {agent_name} is not available')
        _select_window_if_available(backend, session_name=namespace.tmux_session_name, window_name=window.name)
        select_pane(backend, pane_id=pane_id)
        _invalidate_and_refresh_project_view(self._deps, backend, namespace)
        return focus_success(
            kind='agent',
            window=window.name,
            agent=agent_name,
            namespace_epoch=namespace.namespace_epoch,
        )


def _namespace(controller: ProjectNamespaceController):
    namespace = controller.load()
    if namespace is None:
        raise ProjectFocusError(FocusErrorCode.TARGET_MISSING, 'project namespace is not available')
    return namespace


def _validate_epoch(requested: int | None, actual: int) -> None:
    if requested is None:
        return
    if int(requested) != int(actual):
        raise ProjectFocusError(FocusErrorCode.STALE_VIEW, 'ProjectView namespace epoch is stale')


def _invalidate_project_view_cache(project_view_service: object | None) -> None:
    invalidator = getattr(project_view_service, 'invalidate_cache', None)
    if callable(invalidator):
        invalidator()


def _invalidate_and_refresh_project_view(deps: ProjectFocusDependencies, backend, namespace) -> None:
    project_view_service = deps.project_view_service
    _invalidate_project_view_cache(project_view_service)
    if _request_project_view_sidebar_refresh(project_view_service):
        return
    try:
        refresh_sidebar_panes(
            backend,
            project_id=deps.project_id,
            session_name=namespace.tmux_session_name,
        )
    except Exception:
        return


def _select_window_if_available(backend, *, session_name: str, window_name: str) -> None:
    try:
        select_window(backend, session_name=session_name, window_name=window_name)
    except ProjectFocusError as exc:
        if exc.code == FocusErrorCode.TARGET_MISSING:
            return
        raise


def _request_project_view_sidebar_refresh(project_view_service: object | None) -> bool:
    requester = getattr(project_view_service, 'request_sidebar_refresh', None)
    if not callable(requester):
        return False
    try:
        requester()
    except Exception:
        return False
    return True


def _valid_window_name(value: str) -> str:
    try:
        return validate_window_name(value)
    except AgentValidationError as exc:
        raise ProjectFocusError(FocusErrorCode.INVALID_REQUEST, str(exc)) from exc


def _valid_agent_name(value: str) -> str:
    try:
        return normalize_agent_name(value)
    except AgentValidationError as exc:
        raise ProjectFocusError(FocusErrorCode.INVALID_REQUEST, str(exc)) from exc


def _window_spec(config, window_name: str):
    for window in tuple(getattr(config, 'windows', ()) or ()):
        if window.name == window_name:
            return window
    for window in tuple(getattr(config, 'tool_windows', ()) or ()):
        if window.name == window_name:
            return window
    raise ProjectFocusError(FocusErrorCode.UNKNOWN_WINDOW, f'unknown window: {window_name}')


def _agent_window(config, agent_name: str):
    if agent_name not in config.agents:
        raise ProjectFocusError(FocusErrorCode.UNKNOWN_AGENT, f'unknown agent: {agent_name}')
    for window in config.windows:
        if agent_name in window.agent_names:
            return window
    raise ProjectFocusError(FocusErrorCode.UNKNOWN_AGENT, f'agent is not assigned to a window: {agent_name}')


__all__ = ['ProjectFocusDependencies', 'ProjectFocusService']
