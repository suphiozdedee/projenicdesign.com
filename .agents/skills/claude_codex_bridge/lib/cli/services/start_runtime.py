from __future__ import annotations

from dataclasses import dataclass, replace
import time

from ccbd.startup_identity import new_startup_run_id


@dataclass(frozen=True)
class StartSummary:
    project_root: str
    project_id: str
    started: tuple[str, ...]
    daemon_started: bool
    socket_path: str
    cleanup_summaries: tuple[object, ...] = ()
    worktree_warnings: tuple[object, ...] = ()
    worktree_retired: tuple[object, ...] = ()
    maintenance_heartbeat: dict | None = None
    layout_summary: dict | None = None
    sidebar_helper_refresh: dict | None = None
    startup_run_id: str | None = None
    cli_timings_ms: dict[str, float] | None = None
    process_bootstrap_trace_id: str | None = None
    process_bootstrap_timings_ms: dict[str, float] | None = None


def start_agents(
    context,
    command,
    *,
    terminal_size: tuple[int, int] | None = None,
    process_trace_id: str | None = None,
    readiness_origin_ns: int | None = None,
    readiness_attach_mode: str | None = None,
    ensure_daemon_started_fn,
    cleanup_summary_cls,
    startup_report_store_cls=None,
    before_client_start_fn=None,
    enrich_summary_fn=None,
    start_rpc_timeout_s: float | None = None,
) -> StartSummary:
    del startup_report_store_cls  # Compatibility-only injection; the daemon is report authority.
    cli_started_ns = time.monotonic_ns()
    startup_run_id = new_startup_run_id()
    stage_started_ns = time.monotonic_ns()
    pre_start_result = before_client_start_fn(context) if before_client_start_fn is not None else None
    cli_pre_rpc_ms = _elapsed_ms(stage_started_ns)
    stage_started_ns = time.monotonic_ns()
    handle = ensure_daemon_started_fn(context)
    daemon_ensure_ms = _elapsed_ms(stage_started_ns)
    control_plane_ready_ns = time.perf_counter_ns()
    assert handle.client is not None
    start_kwargs = {
        'agent_names': command.agent_names,
        'restore': command.restore,
        'auto_permission': command.auto_permission,
        'startup_run_id': startup_run_id,
        'daemon_started': bool(handle.started),
    }
    if terminal_size is not None:
        start_kwargs['terminal_size'] = terminal_size
    readiness_trace = _readiness_trace_payload(
        trace_id=process_trace_id,
        origin_ns=readiness_origin_ns,
        attach_mode=readiness_attach_mode,
        handle=handle,
        control_plane_ready_ns=control_plane_ready_ns,
    )
    if readiness_trace is not None:
        start_kwargs['readiness_trace'] = readiness_trace
    stage_started_ns = time.monotonic_ns()
    payload = _start_rpc_client(handle.client, timeout_s=start_rpc_timeout_s).start(**start_kwargs)
    start_rpc_ms = _elapsed_ms(stage_started_ns)
    response_run_id = str(payload.get('startup_run_id') or '').strip()
    if response_run_id and response_run_id != startup_run_id:
        raise RuntimeError(
            f'ccbd start response correlation mismatch: expected {startup_run_id}, got {response_run_id}'
        )
    stage_started_ns = time.monotonic_ns()
    summary = _summary_from_start_payload(
        context,
        payload,
        daemon_started=handle.started,
        cleanup_summary_cls=cleanup_summary_cls,
    )
    if enrich_summary_fn is not None:
        summary = enrich_summary_fn(context, summary, pre_start_result)
    cli_post_rpc_ms = _elapsed_ms(stage_started_ns)
    return replace(
        summary,
        startup_run_id=startup_run_id,
        cli_timings_ms={
            'cli_pre_rpc': cli_pre_rpc_ms,
            'daemon_ensure': daemon_ensure_ms,
            'start_rpc': start_rpc_ms,
            'cli_post_rpc': cli_post_rpc_ms,
            'cli_total': _elapsed_ms(cli_started_ns),
        },
    )


def _start_rpc_client(client, *, timeout_s: float | None):
    if timeout_s is None:
        return client
    with_timeout = getattr(client, 'with_timeout', None)
    if not callable(with_timeout):
        return client
    return with_timeout(timeout_s)


def _summary_from_start_payload(context, payload: dict, *, daemon_started: bool, cleanup_summary_cls) -> StartSummary:
    return StartSummary(
        project_root=str(payload.get("project_root") or context.project.project_root),
        project_id=str(payload.get("project_id") or context.project.project_id),
        started=_started_agents(payload),
        daemon_started=daemon_started,
        socket_path=str(payload.get("socket_path") or context.paths.ccbd_socket_path),
        cleanup_summaries=_cleanup_summaries(payload, cleanup_summary_cls=cleanup_summary_cls),
    )


def _started_agents(payload: dict) -> tuple[str, ...]:
    return tuple(
        str(item).strip()
        for item in (payload.get("started") or ())
        if str(item).strip()
    )


def _cleanup_summaries(payload: dict, *, cleanup_summary_cls) -> tuple[object, ...]:
    return tuple(
        cleanup_summary_cls(
            socket_name=item.get("socket_name"),
            owned_panes=tuple(item.get("owned_panes") or ()),
            active_panes=tuple(item.get("active_panes") or ()),
            orphaned_panes=tuple(item.get("orphaned_panes") or ()),
            killed_panes=tuple(item.get("killed_panes") or ()),
        )
        for item in (payload.get("cleanup_summaries") or ())
        if isinstance(item, dict)
    )


def _elapsed_ms(started_ns: int) -> float:
    return (time.monotonic_ns() - started_ns) / 1_000_000


def _readiness_trace_payload(
    *,
    trace_id: str | None,
    origin_ns: int | None,
    attach_mode: str | None,
    handle,
    control_plane_ready_ns: int,
) -> dict[str, object] | None:
    if not trace_id or origin_ns is None or origin_ns <= 0:
        return None
    if control_plane_ready_ns < origin_ns:
        return None
    inspection = getattr(handle, 'inspection', None)
    generation = getattr(inspection, 'generation', None)
    if generation is None:
        lease = getattr(inspection, 'lease', None)
        generation = getattr(lease, 'generation', None)
    try:
        expected_generation = int(generation) if generation is not None else None
    except (TypeError, ValueError):
        expected_generation = None
    if expected_generation is None or expected_generation <= 0:
        return None
    keeper_startup_id = str(getattr(inspection, 'startup_id', '') or '').strip() or None
    t2_elapsed_ms = (control_plane_ready_ns - origin_ns) / 1_000_000.0
    daemon_started = bool(getattr(handle, 'started', False))
    return {
        'schema_version': 1,
        'trace_id': trace_id,
        'origin_monotonic_ns': origin_ns,
        'attach_mode': 'no_attach' if attach_mode == 'no_attach' else 'interactive',
        'expected_daemon_generation': expected_generation,
        'keeper_startup_id': keeper_startup_id,
        'T1_lifecycle_intent': {
            'status': (
                'observed_upper_bound' if daemon_started else 'not_required_already_mounted'
            ),
            'elapsed_ms': t2_elapsed_ms if daemon_started else None,
            'source': (
                'cli_compatible_daemon_observation'
                if daemon_started
                else 'cli_existing_mounted_generation'
            ),
        },
        'T2_control_plane_ready': {
            'status': 'reached',
            'elapsed_ms': t2_elapsed_ms,
            'source': 'cli_compatible_daemon_handle',
        },
    }


__all__ = ["StartSummary", "start_agents"]
