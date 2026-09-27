from __future__ import annotations

from agents.config_loader import load_project_config
from agents.store import AgentRuntimeStore
from cli.context import CliContext
from cli.models import ParsedPsCommand
from ccbd.services.project_namespace_state import ProjectNamespaceStateStore

from .daemon import ping_local_state
from platforms.windows.herdr.surface import herdr_surface_projection_from_namespace_state
from .provider_binding import binding_status


def ps_summary(context: CliContext, command: ParsedPsCommand) -> dict:
    del command
    config = load_project_config(context.project.project_root).config
    store = AgentRuntimeStore(context.paths)
    local = ping_local_state(context)
    ccbd_state = _effective_ccbd_state(local)
    agents: list[dict] = []
    for agent_name, spec in sorted(config.agents.items()):
        runtime = store.load(agent_name)
        agents.append(_agent_summary(context, agent_name=agent_name, spec=spec, runtime=runtime, ccbd_state=ccbd_state))
    herdr_projection = _namespace_herdr_surface_projection(context)
    return {
        'project_id': context.project.project_id,
        'ccbd_state': ccbd_state,
        'ccbd_mount_state': _local_attr(local, 'mount_state'),
        'ccbd_health': _local_attr(local, 'health'),
        'ccbd_reason': _local_attr(local, 'reason'),
        'ccbd_pid_alive': _local_attr(local, 'pid_alive'),
        'ccbd_socket_connectable': _local_attr(local, 'socket_connectable'),
        'ccbd_heartbeat_fresh': _local_attr(local, 'heartbeat_fresh'),
        **({'herdr_surface_projection': herdr_projection} if herdr_projection is not None else {}),
        'agents': agents,
    }


def _agent_summary(context: CliContext, *, agent_name: str, spec, runtime, ccbd_state: str) -> dict:
    workspace_path = _workspace_path(context, agent_name=agent_name, runtime=runtime)
    runtime_ref = _runtime_attr(runtime, 'runtime_ref')
    session_ref = _session_ref(runtime)
    base_binding_status = binding_status(runtime_ref, session_ref, workspace_path)
    state = _runtime_enum_value(runtime, 'state', 'stopped')
    pane_state = _runtime_attr(runtime, 'pane_state')
    if ccbd_state != 'mounted' and runtime is not None:
        if state not in {'failed', 'stopped', 'stopping'}:
            state = 'degraded'
        if pane_state not in {None, 'missing', 'dead'}:
            pane_state = 'missing'
        if base_binding_status == 'bound':
            base_binding_status = 'stale'
    return {
        'agent_name': agent_name,
        'provider': spec.provider,
        'runtime_mode': spec.runtime_mode.value,
        'workspace_mode': spec.workspace_mode.value,
        'state': state,
        'queue_depth': _runtime_attr(runtime, 'queue_depth', 0),
        'workspace_path': workspace_path,
        'runtime_ref': runtime_ref,
        'session_ref': session_ref,
        'binding_status': base_binding_status,
        'backend_type': _runtime_attr(runtime, 'backend_type', spec.runtime_mode.value),
        'binding_source': _runtime_enum_value(runtime, 'binding_source', 'provider-session'),
        'terminal': _runtime_attr(runtime, 'terminal_backend'),
        'tmux_socket_name': _runtime_attr(runtime, 'tmux_socket_name'),
        'tmux_socket_path': _runtime_attr(runtime, 'tmux_socket_path'),
        'tmux_window_name': _runtime_attr(runtime, 'tmux_window_name'),
        'tmux_window_id': _runtime_attr(runtime, 'tmux_window_id'),
        'pane_id': _runtime_attr(runtime, 'pane_id'),
        'active_pane_id': _runtime_attr(runtime, 'active_pane_id'),
        'pane_title_marker': _runtime_attr(runtime, 'pane_title_marker'),
        'pane_state': pane_state,
    }


def _effective_ccbd_state(local) -> str:
    mount_state = str(_local_attr(local, 'mount_state') or '').strip() or 'unknown'
    health = str(_local_attr(local, 'health') or '').strip()
    if mount_state == 'mounted':
        if health and health != 'healthy':
            return health
        for field in ('pid_alive', 'socket_connectable', 'heartbeat_fresh'):
            value = _local_attr(local, field)
            if value is False:
                return 'stale'
    return mount_state


def _local_attr(local, name: str, default=None):
    return getattr(local, name, default)


def _runtime_attr(runtime, name: str, default=None):
    if runtime is None:
        return default
    return getattr(runtime, name, default)


def _runtime_enum_value(runtime, name: str, default: str) -> str:
    value = _runtime_attr(runtime, name)
    return getattr(value, 'value', default)


def _workspace_path(context: CliContext, *, agent_name: str, runtime) -> str:
    workspace_path = _runtime_attr(runtime, 'workspace_path')
    if workspace_path:
        return runtime.workspace_path
    return str(context.paths.workspace_path(agent_name))


def _session_ref(runtime) -> str | None:
    if runtime is None:
        return None
    return runtime.session_file or runtime.session_id or runtime.session_ref


def _namespace_herdr_surface_projection(context: CliContext) -> dict[str, object] | None:
    try:
        namespace_state = ProjectNamespaceStateStore(context.paths).load()
    except Exception:
        return None
    return herdr_surface_projection_from_namespace_state(namespace_state)
