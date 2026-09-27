from __future__ import annotations

from ccbd.reload_runtime_mount_state import clean_text, optional_int
from ccbd.services.start_policy import recovery_start_options


def call_start_flow_for_additive_mount(
    supervisor,
    namespace,
    *,
    agent_panes: dict[str, str],
    requested_agents: tuple[str, ...],
    restore: bool,
    auto_permission: bool,
    run_start_flow_fn,
):
    return run_start_flow_fn(
        project_root=supervisor._project_root,
        project_id=supervisor._project_id,
        paths=supervisor._paths,
        config=supervisor._config,
        runtime_service=supervisor._runtime_service,
        requested_agents=requested_agents,
        restore=restore,
        auto_permission=auto_permission,
        cleanup_tmux_orphans=False,
        interactive_tmux_layout=True,
        tmux_socket_path=str(getattr(namespace, 'tmux_socket_path')),
        tmux_session_name=str(getattr(namespace, 'tmux_session_name')),
        tmux_workspace_window_name=clean_text(
            getattr(namespace, 'workspace_window_name', None)
        ),
        namespace_epoch=int(getattr(namespace, 'namespace_epoch')),
        workspace_window_id=clean_text(getattr(namespace, 'workspace_window_id', None)),
        workspace_epoch=optional_int(getattr(namespace, 'workspace_epoch', None)),
        namespace_agent_panes=dict(agent_panes),
        namespace_active_panes=tuple(agent_panes.values()),
        fresh_namespace=False,
        fresh_workspace=False,
        clock=supervisor._clock,
    )


def start_options(supervisor, *, fallback_app=None) -> tuple[bool, bool]:
    store = getattr(supervisor, '_start_policy_store', None)
    if store is None and fallback_app is not None:
        store = getattr(fallback_app, 'start_policy_store', None)
    try:
        policy = store.load() if store is not None else None
    except Exception:
        policy = None
    return recovery_start_options(policy)


__all__ = ['call_start_flow_for_additive_mount', 'start_options']
