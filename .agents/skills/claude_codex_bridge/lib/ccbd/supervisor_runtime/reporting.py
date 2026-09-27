from __future__ import annotations

from ccbd.models import CcbdShutdownReport, CcbdStartupReport, cleanup_summaries_from_objects
from ccbd.stop_flow import build_shutdown_runtime_snapshots
from runtime_observability import record_startup_operation, startup_operation_counts
from storage.path_helpers import socket_placement_payload


def record_startup_report(
    supervisor,
    *,
    requested_agents: tuple[str, ...],
    restore: bool,
    auto_permission: bool,
    status: str,
    actions_taken: tuple[str, ...],
    cleanup_summaries,
    agent_results,
    failure_reason: str | None,
    timings_ms: dict[str, float] | None = None,
    startup_run_id: str | None = None,
    daemon_started: bool | None = None,
    readiness_recorder=None,
) -> None:
    try:
        # The enclosing report write is intentionally represented only by this
        # attempt marker. Its atomic write cannot be added to the same payload
        # without recursively rewriting the report.
        record_startup_operation('startup_report_write_attempt_count')
        inspection = supervisor._ownership_guard.inspect()
        readiness_timeline = (
            readiness_recorder.to_record(
                startup_run_id=startup_run_id,
                daemon_generation=inspection.generation,
            )
            if readiness_recorder is not None
            else {}
        )
        report = CcbdStartupReport(
            project_id=supervisor._project_id,
            generated_at=supervisor._clock(),
            trigger='start_command',
            status=status,
            requested_agents=tuple(requested_agents),
            desired_agents=tuple(sorted(supervisor._config.agents)),
            restore_requested=restore,
            auto_permission=auto_permission,
            daemon_generation=inspection.generation,
            daemon_started=daemon_started,
            startup_run_id=startup_run_id,
            config_signature=str(supervisor._config_identity.get('config_signature') or '').strip() or None,
            inspection=inspection.to_record(),
            socket_placement={
                **supervisor._paths.runtime_state_payload(),
                **socket_placement_payload(supervisor._paths.ccbd_socket_placement),
                **socket_placement_payload(supervisor._paths.ccbd_tmux_socket_placement, prefix='tmux'),
            },
            restore_summary={},
            actions_taken=tuple(actions_taken),
            cleanup_summaries=cleanup_summaries_from_objects(cleanup_summaries),
            agent_results=tuple(agent_results),
            failure_reason=failure_reason,
            timings_ms=dict(timings_ms or {}),
            operation_counts=startup_operation_counts(),
            readiness_timeline=dict(readiness_timeline or {}),
        )
        supervisor._startup_report_store.save(report)
    except Exception:
        return


def record_shutdown_report(
    supervisor,
    *,
    trigger: str,
    status: str,
    forced: bool,
    reason: str,
    stopped_agents: tuple[str, ...],
    actions_taken: tuple[str, ...],
    cleanup_summaries,
    failure_reason: str | None,
) -> None:
    try:
        inspection = supervisor._ownership_guard.inspect()
        runtime_snapshots = build_shutdown_runtime_snapshots(
            paths=supervisor._paths,
            config=supervisor._config,
            registry=supervisor._registry,
        )
        report = CcbdShutdownReport(
            project_id=supervisor._project_id,
            generated_at=supervisor._clock(),
            trigger=trigger,
            status=status,
            forced=forced,
            stopped_agents=stopped_agents,
            daemon_generation=inspection.generation,
            reason=reason,
            inspection_after=inspection.to_record(),
            actions_taken=actions_taken,
            cleanup_summaries=cleanup_summaries_from_objects(cleanup_summaries),
            runtime_snapshots=runtime_snapshots,
            failure_reason=failure_reason,
        )
        supervisor._shutdown_report_store.save(report)
    except Exception:
        return


__all__ = ['record_shutdown_report', 'record_startup_report']
