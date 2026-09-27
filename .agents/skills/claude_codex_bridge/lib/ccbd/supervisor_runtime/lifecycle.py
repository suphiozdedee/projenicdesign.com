from __future__ import annotations

from dataclasses import replace
import os
import time

from ccbd.models import CcbdStartupAgentResult
from ccbd.services import CcbdLifecycleStore
from ccbd.services.project_namespace_runtime import build_namespace_topology_plan

from .namespace import ensure_project_namespace
from .reporting import record_startup_report


def start_supervisor(
    supervisor,
    *,
    agent_names: tuple[str, ...],
    restore: bool,
    auto_permission: bool,
    terminal_size: tuple[int, int] | None,
    cleanup_tmux_orphans: bool,
    interactive_tmux_layout: bool,
    recreate_namespace: bool,
    reflow_workspace: bool,
    recreate_reason: str | None,
    background_maintenance: bool,
    startup_run_id: str | None,
    daemon_started: bool | None,
    readiness_recorder=None,
    restart_agent_panes: dict[str, str] | None,
    run_start_flow_fn,
):
    supervisor_started_ns = time.monotonic_ns()
    namespace_started_ns = supervisor_started_ns
    try:
        topology_plan = None
        namespace_layout_signature = None
        if (
            supervisor._project_namespace is not None
            and _uses_namespace_topology(
                supervisor._config,
                interactive_tmux_layout=interactive_tmux_layout,
                project_namespace=supervisor._project_namespace,
            )
        ):
            topology_plan = build_namespace_topology_plan(
                supervisor._config,
                ccbd_socket_path=str(supervisor._paths.ccbd_socket_path),
                project_root=str(supervisor._project_root),
            )
            namespace_layout_signature = topology_plan.signature
        elif supervisor._project_namespace is not None and interactive_tmux_layout:
            namespace_layout_signature = str(getattr(supervisor._config, 'topology_signature', '') or '') or None
        namespace = (
            ensure_project_namespace(
                supervisor._project_namespace,
                layout_signature=namespace_layout_signature,
                topology_plan=topology_plan,
                recreate_namespace=recreate_namespace,
                reflow_workspace=reflow_workspace,
                recreate_reason=recreate_reason,
                background_maintenance=background_maintenance,
                terminal_size=terminal_size,
            )
            if supervisor._project_namespace is not None
            else None
        )
        if (
            topology_plan is None
            and supervisor._project_namespace is not None
            and namespace is not None
            and _namespace_is_herdr(namespace)
            and _config_wants_namespace_topology(
                supervisor._config,
                interactive_tmux_layout=interactive_tmux_layout,
            )
        ):
            topology_plan = build_namespace_topology_plan(
                supervisor._config,
                ccbd_socket_path=str(supervisor._paths.ccbd_socket_path),
                project_root=str(supervisor._project_root),
            )
            namespace_layout_signature = topology_plan.signature
            namespace = ensure_project_namespace(
                supervisor._project_namespace,
                layout_signature=namespace_layout_signature,
                topology_plan=topology_plan,
                recreate_namespace=False,
                reflow_workspace=False,
                recreate_reason=None,
                background_maintenance=background_maintenance,
                terminal_size=terminal_size,
            )
        namespace_ms = (time.monotonic_ns() - namespace_started_ns) / 1_000_000
        if readiness_recorder is not None:
            if (
                namespace is not None
                and bool(getattr(namespace, 'ui_attachable', True))
                and str(getattr(namespace, 'tmux_session_name', '') or '').strip()
            ):
                readiness_recorder.mark(
                    'T3_namespace_attachable',
                    source='ccbd_namespace_ensure_current_generation',
                    now_ns=time.perf_counter_ns(),
                )
            else:
                readiness_recorder.mark(
                    'T3_namespace_attachable',
                    status='not_applicable_no_namespace',
                    source='ccbd_namespace_unavailable',
                    now_ns=time.perf_counter_ns(),
                )
        summary = run_start_flow_fn(
            project_root=supervisor._project_root,
            project_id=supervisor._project_id,
            paths=supervisor._paths,
            config=supervisor._config,
            runtime_service=supervisor._runtime_service,
            requested_agents=agent_names,
            restore=restore,
            auto_permission=auto_permission,
            cleanup_tmux_orphans=cleanup_tmux_orphans,
            interactive_tmux_layout=interactive_tmux_layout,
            tmux_socket_path=namespace.tmux_socket_path if namespace is not None else None,
            tmux_session_name=namespace.tmux_session_name if namespace is not None else None,
            namespace_session_name=getattr(namespace, 'namespace_session_name', None) if namespace is not None else None,
            tmux_workspace_window_name=getattr(namespace, 'workspace_window_name', None) if namespace is not None else None,
            namespace_id=getattr(namespace, 'namespace_id', None) if namespace is not None else None,
            namespace_ipc_kind=getattr(namespace, 'namespace_ipc_kind', None) if namespace is not None else None,
            namespace_ipc_ref=getattr(namespace, 'namespace_ipc_ref', None) if namespace is not None else None,
            namespace_restore_token=getattr(namespace, 'namespace_restore_token', None) if namespace is not None else None,
            namespace_epoch=namespace.namespace_epoch if namespace is not None else None,
            workspace_window_id=getattr(namespace, 'workspace_window_id', None) if namespace is not None else None,
            workspace_epoch=getattr(namespace, 'workspace_epoch', None) if namespace is not None else None,
            namespace_agent_panes=getattr(supervisor._project_namespace, '_last_materialized_agent_panes', None),
            namespace_cmd_pane=getattr(supervisor._project_namespace, '_last_materialized_cmd_pane', None),
            namespace_pane_records=getattr(supervisor._project_namespace, '_last_topology_pane_records', None),
            namespace_active_panes=getattr(supervisor._project_namespace, '_last_topology_active_panes', None),
            namespace_topology_managed=(
                topology_plan is not None
                and hasattr(supervisor._project_namespace, '_last_materialized_cmd_pane')
            ),
            namespace_backend_family=(
                getattr(namespace, 'namespace_backend_family', None) if namespace is not None else None
            ),
            namespace_backend_impl=getattr(namespace, 'backend_impl', None) if namespace is not None else None,
            fresh_namespace=bool(getattr(namespace, 'created_this_call', False)),
            fresh_workspace=bool(getattr(namespace, 'workspace_recreated_this_call', False)),
            restart_agent_panes=restart_agent_panes,
            clock=supervisor._clock,
            readiness_recorder=readiness_recorder,
        )
        summary = replace(
            summary,
            timings_ms={
                'namespace_ensure': namespace_ms,
                **dict(getattr(summary, 'timings_ms', {}) or {}),
                'supervisor_total': (time.monotonic_ns() - supervisor_started_ns) / 1_000_000,
            },
        )
        _sync_lifecycle_namespace_epoch(supervisor, namespace=namespace)
    except Exception as exc:
        record_startup_report(
            supervisor,
            requested_agents=agent_names,
            restore=restore,
            auto_permission=auto_permission,
            status='failed',
            actions_taken=('start_flow_failed',),
            cleanup_summaries=(),
            agent_results=_failure_agent_results(exc),
            failure_reason=str(exc),
            timings_ms={'supervisor_total': (time.monotonic_ns() - supervisor_started_ns) / 1_000_000},
            startup_run_id=startup_run_id,
            daemon_started=daemon_started,
            readiness_recorder=readiness_recorder,
        )
        raise

    record_startup_report(
        supervisor,
        requested_agents=agent_names,
        restore=restore,
        auto_permission=auto_permission,
        status='ok',
        actions_taken=summary.actions_taken,
        cleanup_summaries=summary.cleanup_summaries,
        agent_results=summary.agent_results,
        failure_reason=None,
        timings_ms=summary.timings_ms,
        startup_run_id=startup_run_id,
        daemon_started=daemon_started,
        readiness_recorder=readiness_recorder,
    )
    return summary


def _sync_lifecycle_namespace_epoch(supervisor, *, namespace) -> None:
    if namespace is None:
        return
    epoch = getattr(namespace, 'namespace_epoch', None)
    if epoch is None:
        return
    lifecycle_store = CcbdLifecycleStore(supervisor._paths)
    lifecycle = lifecycle_store.load()
    if lifecycle is None:
        return
    inspection = supervisor._ownership_guard.inspect()
    current_generation = inspection.generation
    if current_generation is None:
        return
    if lifecycle.generation != int(current_generation):
        return
    if lifecycle.phase == 'unmounted':
        return
    if lifecycle.namespace_epoch == int(epoch):
        return
    with supervisor._ownership_guard.startup_lock():
        lifecycle = lifecycle_store.load()
        if lifecycle is None or lifecycle.desired_state != 'running':
            return
        inspection = supervisor._ownership_guard.inspect()
        current_generation = inspection.generation
        if current_generation is None:
            return
        if lifecycle.generation != int(current_generation):
            return
        if lifecycle.phase in {'unmounted', 'stopping'}:
            return
        if lifecycle.namespace_epoch == int(epoch):
            return
        lifecycle_store.save(
            lifecycle.with_updates(namespace_epoch=int(epoch))
        )


def _failure_agent_results(exc: Exception) -> tuple[CcbdStartupAgentResult, ...]:
    value = getattr(exc, 'ccb_startup_agent_results', ())
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(item for item in value if isinstance(item, CcbdStartupAgentResult))


def _uses_explicit_windows_topology(config, *, interactive_tmux_layout: bool) -> bool:
    if not interactive_tmux_layout:
        return False
    if bool(getattr(config, 'windows_explicit', False)):
        return True
    sidebar = getattr(config, 'sidebar', None)
    if str(getattr(sidebar, 'mode', '') or '').strip() == 'every_window':
        return True
    return bool(tuple(getattr(config, 'tool_windows', ()) or ()))


def _uses_namespace_topology(config, *, interactive_tmux_layout: bool, project_namespace) -> bool:
    if not _config_wants_namespace_topology(config, interactive_tmux_layout=interactive_tmux_layout):
        return False
    if _uses_explicit_windows_topology(config, interactive_tmux_layout=interactive_tmux_layout):
        return True
    if not _project_namespace_prefers_herdr(project_namespace):
        return False
    return True


def _config_wants_namespace_topology(config, *, interactive_tmux_layout: bool) -> bool:
    if not interactive_tmux_layout:
        return False
    if _uses_explicit_windows_topology(config, interactive_tmux_layout=interactive_tmux_layout):
        return True
    return bool(
        str(getattr(config, 'layout_spec', '') or '').strip()
        or bool(getattr(config, 'cmd_enabled', False))
        or tuple(getattr(config, 'windows', ()) or ())
    )


def _project_namespace_prefers_herdr(project_namespace) -> bool:
    load = getattr(project_namespace, 'load', None)
    if callable(load):
        try:
            current = load()
        except Exception:
            current = None
        if (
            str(getattr(current, 'backend_impl', '') or '').strip() == 'herdr'
            or str(getattr(current, 'namespace_backend_family', '') or '').strip() == 'herdr-native'
        ):
            return True
    return any(
        str(os.environ.get(name) or '').strip()
        for name in (
            'CCB_HERDR_CAPABILITY_REPORT',
            'CCB_HERDR_SOCKET_REF',
        )
    )


def _namespace_is_herdr(namespace) -> bool:
    return (
        str(getattr(namespace, 'backend_impl', '') or '').strip() == 'herdr'
        or str(getattr(namespace, 'namespace_backend_family', '') or '').strip() == 'herdr-native'
    )


def stop_all_supervisor(
    supervisor,
    *,
    force: bool,
    cleanup_project_tmux_orphans_by_socket_fn,
    tmux_cleanup_history_store_cls,
    stop_all_project_fn,
):
    return stop_all_project_fn(
        project_root=supervisor._project_root,
        project_id=supervisor._project_id,
        paths=supervisor._paths,
        registry=supervisor._registry,
        project_namespace=supervisor._project_namespace,
        clock=supervisor._clock,
        force=force,
        cleanup_project_tmux_orphans_by_socket_fn=cleanup_project_tmux_orphans_by_socket_fn,
        tmux_cleanup_history_store_cls=tmux_cleanup_history_store_cls,
    )


__all__ = ['start_supervisor', 'stop_all_supervisor']
