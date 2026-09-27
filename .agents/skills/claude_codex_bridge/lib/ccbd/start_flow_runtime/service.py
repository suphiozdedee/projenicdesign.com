from __future__ import annotations

from pathlib import Path
import time

from ccbd.models import CcbdStartupAgentResult

from .binding import launch_binding_hint, relabel_project_namespace_pane
from .service_agents import prepare_agents
from .service_context import build_start_context, record_namespace_action
from .service_tmux import (
    bootstrap_cmd_pane_if_needed,
    cleanup_tmux_orphans_if_needed,
    project_socket_active_panes,
    record_active_panes,
    tmux_layout_for_start,
    tmux_namespace_runtime,
)
from .summary import StartFlowSummary


def run_start_flow(
    *,
    project_root: Path,
    project_id: str,
    paths,
    config,
    runtime_service,
    requested_agents: tuple[str, ...],
    restore: bool,
    auto_permission: bool,
    cleanup_tmux_orphans: bool,
    interactive_tmux_layout: bool,
    tmux_socket_path: str | None,
    tmux_session_name: str | None,
    namespace_session_name: str | None = None,
    tmux_workspace_window_name: str | None,
    namespace_id: str | None = None,
    namespace_ipc_kind: str | None = None,
    namespace_ipc_ref: str | None = None,
    namespace_restore_token: str | None = None,
    namespace_epoch: int | None,
    workspace_window_id: str | None,
    workspace_epoch: int | None,
    namespace_agent_panes: dict[str, str] | None,
    namespace_cmd_pane: str | None,
    namespace_pane_records: dict[str, object] | None,
    namespace_active_panes: tuple[str, ...] | None,
    namespace_topology_managed: bool,
    namespace_backend_family: str | None,
    namespace_backend_impl: str | None,
    fresh_namespace: bool,
    fresh_workspace: bool,
    restart_agent_panes: dict[str, str] | None,
    clock,
    readiness_recorder=None,
    deps,
) -> StartFlowSummary:
    flow_started_ns = time.monotonic_ns()
    timings_ms: dict[str, float] = {}
    tmux_socket_path = str(tmux_socket_path or '').strip() or None
    stage_started_ns = flow_started_ns
    command, context = build_start_context(
        project_root=project_root,
        project_id=project_id,
        paths=paths,
        requested_agents=requested_agents,
        restore=restore,
        auto_permission=auto_permission,
    )
    layout_plan = deps.build_project_layout_plan_fn(config, requested_agents=command.agent_names)
    timings_ms['context_and_layout_plan'] = _elapsed_ms(stage_started_ns)
    targets = layout_plan.target_agent_names
    if readiness_recorder is not None:
        readiness_recorder.set_agent_scopes(targets, tuple(sorted(config.agents)))
    actions_taken: list[str] = []
    agent_results: list[object] = []
    stage_started_ns = time.monotonic_ns()
    tmux_backend, root_pane_id = tmux_namespace_runtime(
        deps,
        tmux_socket_path=tmux_socket_path,
        tmux_session_name=tmux_session_name,
        tmux_workspace_window_name=tmux_workspace_window_name,
        namespace_cmd_pane=namespace_cmd_pane,
        namespace_topology_managed=namespace_topology_managed,
        cmd_enabled=bool(getattr(config, 'cmd_enabled', False)),
    )
    timings_ms['tmux_namespace_runtime'] = _elapsed_ms(stage_started_ns)

    record_namespace_action(
        actions_taken,
        tmux_socket_path=tmux_socket_path,
        tmux_session_name=tmux_session_name,
        namespace_epoch=namespace_epoch,
    )

    stage_started_ns = time.monotonic_ns()
    prepared_agents = prepare_agents(
        deps,
        targets=targets,
        config=config,
        paths=paths,
        context=context,
        project_root=project_root,
        project_id=project_id,
        tmux_socket_path=tmux_socket_path,
        tmux_session_name=tmux_session_name,
        workspace_window_id=workspace_window_id,
        namespace_epoch=namespace_epoch,
        namespace_agent_panes=namespace_agent_panes,
        namespace_pane_records=namespace_pane_records,
        force_restart_agents=tuple((restart_agent_panes or {}).keys()),
    )
    timings_ms['agent_prepare_and_classify'] = _elapsed_ms(stage_started_ns)
    prepared_by_agent = {item.agent_name: item for item in prepared_agents}

    stage_started_ns = time.monotonic_ns()
    tmux_layout = tmux_layout_for_start(
        deps,
        context,
        config=config,
        prepared_agents=prepared_agents,
        interactive_tmux_layout=interactive_tmux_layout,
        tmux_backend=tmux_backend,
        root_pane_id=root_pane_id,
        namespace_agent_panes={
            **dict(namespace_agent_panes or {}),
            **dict(restart_agent_panes or {}),
        },
        actions_taken=actions_taken,
        namespace_backend_family=namespace_backend_family,
        namespace_backend_impl=namespace_backend_impl,
    )
    timings_ms['tmux_layout'] = _elapsed_ms(stage_started_ns)

    stage_started_ns = time.monotonic_ns()
    active_panes_by_socket: dict[str | None, list[str]] = {}
    active_project_panes, cmd_pane_id = project_socket_active_panes(
        tmux_layout=tmux_layout,
        tmux_socket_path=tmux_socket_path,
        config=config,
        root_pane_id=root_pane_id,
        namespace_active_panes=namespace_active_panes,
    )
    bootstrap_cmd_pane_if_needed(
        deps,
        fresh_namespace=(fresh_namespace or fresh_workspace),
        cmd_pane_id=cmd_pane_id,
        project_root=project_root,
        project_id=project_id,
        tmux_socket_path=tmux_socket_path,
        namespace_epoch=namespace_epoch,
        actions_taken=actions_taken,
    )
    timings_ms['active_panes_and_cmd'] = _elapsed_ms(stage_started_ns)

    agents_started_ns = time.monotonic_ns()
    namespace_ref = _herdr_namespace_ref(
        namespace_backend_family=namespace_backend_family,
        namespace_backend_impl=namespace_backend_impl,
        namespace_id=namespace_id,
        session_name=namespace_session_name or tmux_session_name,
        ipc_kind=namespace_ipc_kind,
        ipc_ref=namespace_ipc_ref,
        restore_token=namespace_restore_token,
    )
    provider_runtime_deferred = _namespace_is_herdr(
        namespace_backend_family=namespace_backend_family,
        namespace_backend_impl=namespace_backend_impl,
    ) and not _herdr_runtime_launch_ready(
        targets=targets,
        assigned_panes=tmux_layout.agent_panes,
        namespace_ref=namespace_ref,
    )
    if provider_runtime_deferred:
        for agent_name in targets:
            prepared = prepared_by_agent[agent_name]
            agent_results.append(_herdr_deferred_agent_result(agent_name, prepared=prepared))
            actions_taken.append(f'provider_runtime_deferred_on_herdr:{agent_name}')
    else:
        for style_index, agent_name in enumerate(targets):
            prepared = prepared_by_agent[agent_name]
            try:
                assigned_pane_id = tmux_layout.agent_panes.get(agent_name)
                execution = deps.start_agent_runtime_impl(
                    context=context,
                    command=command,
                    runtime_service=runtime_service,
                    agent_name=agent_name,
                    spec=prepared.spec,
                    plan=prepared.plan,
                    binding=prepared.binding,
                    raw_binding=prepared.raw_binding,
                    stale_binding=prepared.stale_binding,
                    assigned_pane_id=assigned_pane_id,
                    assigned_pane_ref=_herdr_assigned_pane_ref(
                        agent_name=agent_name,
                        assigned_pane_id=assigned_pane_id,
                        namespace_ref=namespace_ref,
                        namespace_pane_records=namespace_pane_records,
                    ),
                    style_index=style_index,
                    project_id=project_id,
                    tmux_socket_path=tmux_socket_path,
                    namespace_backend_impl=namespace_backend_impl,
                    namespace_ref=namespace_ref,
                    namespace_epoch=namespace_epoch,
                    workspace_window_id=workspace_window_id,
                    workspace_epoch=workspace_epoch,
                    window_name=prepared.window_name,
                    namespace_pane_records=namespace_pane_records,
                    provider_prepared=prepared.provider_prepared,
                    effective_command=getattr(prepared, 'effective_command', None),
                    provider_prepare_ms=prepared.provider_prepare_ms,
                    binding_reject_reason=prepared.binding_reject_reason,
                    ensure_agent_runtime_fn=deps.ensure_agent_runtime_fn,
                    launch_binding_hint_fn=lambda **kwargs: launch_binding_hint(deps, **kwargs),
                    relabel_project_namespace_pane_fn=lambda **kwargs: relabel_project_namespace_pane(deps, **kwargs),
                    same_tmux_socket_path_fn=deps.same_tmux_socket_path_fn,
                    pane_runtime_pid_resolver=(
                        (lambda pane_id: _best_effort_query_pane_pid(tmux_backend, namespace_ref, pane_id))
                        if hasattr(tmux_backend, 'pane_process_info')
                        else None
                    ),
                )
            except Exception as exc:
                if readiness_recorder is not None:
                    completed_agents = tuple(
                        str(getattr(item, 'agent_name', '') or '').strip()
                        for item in agent_results
                        if str(getattr(item, 'agent_name', '') or '').strip()
                    )
                    readiness_recorder.mark(
                        'T4_requested_agents_ready',
                        status='failed_before_ready',
                        source='ccbd_start_flow_agent_failure',
                        agents=completed_agents,
                        now_ns=time.perf_counter_ns(),
                    )
                    readiness_recorder.mark(
                        'T6_fully_warm',
                        status='failed_before_ready',
                        source='ccbd_start_flow_agent_failure',
                        agents=completed_agents,
                        now_ns=time.perf_counter_ns(),
                    )
                failed_result = getattr(exc, 'ccb_startup_agent_result', None)
                failure_results = tuple(
                    item for item in agent_results
                    if isinstance(item, CcbdStartupAgentResult)
                )
                if isinstance(failed_result, CcbdStartupAgentResult):
                    failure_results = (*failure_results, failed_result)
                try:
                    setattr(exc, 'ccb_startup_agent_results', failure_results)
                except Exception:
                    pass
                raise
            actions_taken.extend(execution.actions_taken)
            record_active_panes(
                active_panes_by_socket,
                active_project_panes,
                execution=execution,
            )
            agent_results.append(execution.agent_result)
            # 方向 B：binding 恢复失败（degraded，binding 无法解析）时清理已启动
            # 的 pane，避免 codex/claude 在 pane 里空转（如登录界面）而 runtime 记
            # degraded + pid=None。正常 binding（launched/attached/relaunched）不受影响。
            if str(getattr(execution.agent_result, 'action', '') or '').strip() == 'degraded':
                degraded_pane_id = getattr(execution, 'runtime_pane_id', None)
                if degraded_pane_id and hasattr(tmux_backend, 'kill_pane'):
                    try:
                        tmux_backend.kill_pane(degraded_pane_id)
                    except Exception:
                        pass
    if readiness_recorder is not None:
        if provider_runtime_deferred:
            readiness_recorder.mark(
                'T4_requested_agents_ready',
                status='not_reached_at_rpc_return',
                source='ccbd_start_flow_provider_runtime_deferred_on_herdr',
                agents=targets,
                now_ns=time.perf_counter_ns(),
            )
        else:
            readiness_recorder.mark(
                'T4_requested_agents_ready',
                source='ccbd_start_flow_authority_committed',
                agents=targets,
                now_ns=time.perf_counter_ns(),
            )
        desired_agents = tuple(sorted(config.agents))
        if provider_runtime_deferred:
            readiness_recorder.mark(
                'T6_fully_warm',
                status='not_reached_at_rpc_return',
                source='ccbd_start_flow_provider_runtime_deferred_on_herdr',
                agents=targets,
                now_ns=time.perf_counter_ns(),
            )
        elif set(targets) == set(desired_agents):
            readiness_recorder.mark(
                'T6_fully_warm',
                source='ccbd_start_flow_all_desired_agents_committed',
                agents=desired_agents,
                now_ns=time.perf_counter_ns(),
            )
        else:
            readiness_recorder.mark(
                'T6_fully_warm',
                status='not_reached_at_rpc_return',
                source='ccbd_start_flow_requested_subset',
                agents=targets,
                now_ns=time.perf_counter_ns(),
            )
    timings_ms['agent_runtime_commit'] = _elapsed_ms(agents_started_ns)
    agent_duration_sum_ms = sum(
        float(getattr(item, 'duration_ms', 0.0) or 0.0)
        for item in agent_results
    )
    timings_ms['agent_runtime_duration_sum'] = agent_duration_sum_ms
    for field_name in (
        'prepare_launch_context',
        'build_start_cmd',
        'tmux_respawn',
        'pane_identity',
        'pane_agent_report',
        'session_write',
        'provider_post_launch',
        'binding_resolve',
        'pane_and_runtime_facts',
        'authority_commit',
        'restore_bookkeeping',
        'unattributed',
    ):
        timings_ms[f'agent_runtime_{field_name}'] = sum(
            float((getattr(item, 'timings_ms', None) or {}).get(field_name, 0.0))
            for item in agent_results
        )
    timings_ms['agent_runtime_loop_overhead'] = max(
        0.0,
        timings_ms['agent_runtime_commit'] - agent_duration_sum_ms,
    )

    stage_started_ns = time.monotonic_ns()
    cleanup_summaries = cleanup_tmux_orphans_if_needed(
        deps,
        cleanup_tmux_orphans=cleanup_tmux_orphans,
        project_id=project_id,
        paths=paths,
        active_panes_by_socket=active_panes_by_socket,
        project_socket_active_panes=active_project_panes,
        tmux_socket_path=tmux_socket_path,
        clock=clock,
        actions_taken=actions_taken,
    )
    timings_ms['tmux_cleanup'] = _elapsed_ms(stage_started_ns)
    timings_ms['flow_total'] = _elapsed_ms(flow_started_ns)
    return StartFlowSummary(
        project_root=str(project_root),
        project_id=project_id,
        started=targets,
        socket_path=str(paths.ccbd_socket_path),
        cleanup_summaries=tuple(cleanup_summaries),
        actions_taken=tuple(actions_taken),
        agent_results=tuple(agent_results),
        timings_ms=timings_ms,
    )


def _elapsed_ms(started_ns: int) -> float:
    return (time.monotonic_ns() - started_ns) / 1_000_000


def _namespace_is_herdr(
    *,
    namespace_backend_family: str | None,
    namespace_backend_impl: str | None,
) -> bool:
    return (
        str(namespace_backend_impl or '').strip() == 'herdr'
        or str(namespace_backend_family or '').strip() == 'herdr-native'
    )


def _herdr_namespace_ref(
    *,
    namespace_backend_family: str | None,
    namespace_backend_impl: str | None,
    namespace_id: str | None,
    session_name: str | None,
    ipc_kind: str | None,
    ipc_ref: str | None,
    restore_token: str | None,
) -> dict[str, object] | None:
    if not _namespace_is_herdr(
        namespace_backend_family=namespace_backend_family,
        namespace_backend_impl=namespace_backend_impl,
    ):
        return None
    clean_namespace_id = str(namespace_id or '').strip()
    clean_session_name = str(session_name or '').strip()
    clean_ipc_kind = str(ipc_kind or '').strip()
    clean_ipc_ref = str(ipc_ref or '').strip()
    if not clean_namespace_id or not clean_session_name or not clean_ipc_kind or not clean_ipc_ref:
        return None
    return {
        'backend_family': 'herdr-native',
        'backend_impl': 'herdr',
        'namespace_id': clean_namespace_id,
        'session_name': clean_session_name,
        'ipc_kind': clean_ipc_kind,
        'ipc_ref': clean_ipc_ref,
        'restore_token': restore_token,
    }


def _herdr_runtime_launch_ready(
    *,
    targets: tuple[str, ...],
    assigned_panes: dict[str, str],
    namespace_ref: dict[str, object] | None,
) -> bool:
    if namespace_ref is None:
        return False
    for agent_name in targets:
        if not str(assigned_panes.get(agent_name) or '').strip():
            return False
    return True


def _herdr_assigned_pane_ref(
    *,
    agent_name: str,
    assigned_pane_id: str | None,
    namespace_ref: dict[str, object] | None,
    namespace_pane_records: dict[str, object] | None,
) -> dict[str, object] | None:
    pane_id = str(assigned_pane_id or '').strip()
    if not pane_id or namespace_ref is None:
        return None
    record = (namespace_pane_records or {}).get(pane_id)
    session_name = str(
        getattr(record, 'session_name', None)
        or namespace_ref.get('session_name')
        or ''
    ).strip()
    if not session_name:
        return None
    window_name = (
        getattr(record, 'ccb_window', None)
        or getattr(record, 'window_name', None)
    )
    return {
        'backend_impl': 'herdr',
        'pane_id': pane_id,
        'session_name': session_name,
        'window_name': str(window_name).strip() if str(window_name or '').strip() else None,
        'agent_slug': agent_name,
    }


def _best_effort_query_pane_pid(tmux_backend, namespace_ref, pane_id: str) -> int | None:
    """方向 A：查询 herdr pane 前台进程 pid（best-effort，不抛异常）。

    供 agent_runtime 回填 runtime_pid 使用；pane 前台进程即 codex/claude
    （respawn 后 codex 启动前可能是 shell，best-effort 单次查询）。
    """
    if not hasattr(tmux_backend, 'pane_process_info'):
        return None
    session_name = str((namespace_ref or {}).get('session_name') or '').strip() or None
    pane_ref = {'backend_impl': 'herdr', 'pane_id': pane_id, 'session_name': session_name}
    try:
        info = tmux_backend.pane_process_info(pane_ref)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    pid = info.get('foreground_pid')
    return int(pid) if pid else None


def _herdr_deferred_agent_result(agent_name: str, *, prepared) -> CcbdStartupAgentResult:
    spec = getattr(prepared, 'spec', None)
    plan = getattr(prepared, 'plan', None)
    provider = str(getattr(spec, 'provider', '') or '').strip() or None
    return CcbdStartupAgentResult(
        agent_name=agent_name,
        provider=provider,
        action='deferred',
        health='deferred',
        workspace_path=str(getattr(plan, 'workspace_path', '') or '').strip() or None,
        lifecycle_state='deferred',
        desired_state='provider_runtime_required',
        reconcile_state='deferred',
        terminal_backend='herdr',
        failure_reason='deferred_to_provider_runtime_on_herdr',
        provider_prepare_ms=getattr(prepared, 'provider_prepare_ms', None),
        provider_prepare_count=1 if getattr(prepared, 'provider_prepared', None) is not None else 0,
        timings_ms={},
    )


__all__ = ['run_start_flow']
