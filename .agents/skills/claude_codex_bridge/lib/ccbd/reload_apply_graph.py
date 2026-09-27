from __future__ import annotations

from ccbd.app_runtime.request_guard import lifecycle_is_stopping
from ccbd.app_runtime.service_graph import (
    CcbdServiceGraphDependencies,
    build_ccbd_service_graph,
)


def build_reload_service_graph(app, new_config):
    current = app.current_service_graph()
    return build_ccbd_service_graph(
        CcbdServiceGraphDependencies(
            project_root=app.project_root,
            project_id=app.project_id,
            paths=app.paths,
            config=new_config,
            provider_catalog=app.provider_catalog,
            mount_manager=app.mount_manager,
            lifecycle_store=app.lifecycle_store,
            restore_store=app.restore_store,
            namespace_state_store=app.namespace_state_store,
            project_view_state_store=app.project_view_state_store,
            project_namespace=app.project_namespace,
            ownership_guard=app.ownership_guard,
            startup_report_store=app.startup_report_store,
            shutdown_report_store=app.shutdown_report_store,
            start_policy_store=app.start_policy_store,
            execution_service=app.execution_service,
            snapshot_writer=app.snapshot_writer,
            control_plane_metrics=app.control_plane_metrics,
            clock=app.clock,
            request_timeout_s=0.0,
            daemon_generation_getter=_daemon_generation_getter(app),
            mount_agent_fn=getattr(app, '_mount_agent_from_policy', None),
            remount_project_fn=getattr(app, '_remount_project_from_policy', None),
            mount_missing_runtime_fn=_mount_missing_runtime_fn(app),
            supervision_suspended_fn=_supervision_suspended_fn(app),
            version=_next_graph_version(current),
        )
    )


def _daemon_generation_getter(app):
    return lambda: app.lease.generation if app.lease is not None else None


def _mount_missing_runtime_fn(app):
    return lambda agent_name: app._mount_missing_runtime_requested(agent_name)


def _supervision_suspended_fn(app):
    return lambda: lifecycle_is_stopping(_safe_load_lifecycle(app))


def _next_graph_version(current) -> int:
    try:
        return int(getattr(current, 'version', 0) or 0) + 1
    except Exception:
        return 1


def _safe_load_lifecycle(app):
    try:
        return app.lifecycle_store.load()
    except Exception:
        return None


__all__ = ['build_reload_service_graph']
