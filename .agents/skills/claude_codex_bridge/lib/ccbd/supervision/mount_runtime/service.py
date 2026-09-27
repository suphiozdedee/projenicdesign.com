from __future__ import annotations

from agents.models import AgentState
from terminal_runtime.tmux_readiness import TmuxTransientServerUnavailable

from .events import record_mount_started, record_mount_succeeded
from .transitions import (
    SUCCESS_RUNTIME_HEALTHS,
    in_backoff_window,
    missing_mount_action_health,
    mount_actions_missing,
    mount_or_reflow,
    persist_mount_exception,
    persist_mount_superseded,
    persist_mount_transient,
    persist_mount_success,
    start_mount_attempt,
)


def ensure_mounted(
    *,
    project_id: str,
    agent_name: str,
    runtime,
    registry,
    runtime_service,
    mount_agent_fn,
    remount_project_fn,
    clock,
    event_store,
    upsert_if_changed_fn,
    build_starting_runtime_fn,
    persist_mount_failure_fn,
    is_in_backoff_window_fn,
    should_reflow_project_mount_fn,
    align_runtime_authority_fn,
    normalized_runtime_health_fn,
) -> str:
    if mount_actions_missing(mount_agent_fn=mount_agent_fn, remount_project_fn=remount_project_fn):
        return missing_mount_action_health(runtime)

    attempted_at = clock()
    if in_backoff_window(runtime, attempted_at=attempted_at, is_in_backoff_window_fn=is_in_backoff_window_fn):
        return runtime.health

    starting, prior_health, next_restart_count = start_mount_attempt(
        agent_name=agent_name,
        runtime=runtime,
        attempted_at=attempted_at,
        build_starting_runtime_fn=build_starting_runtime_fn,
    )
    attempt_id = starting.mount_attempt_id
    record_mount_started(
        event_store,
        project_id=project_id,
        agent_name=agent_name,
        attempted_at=attempted_at,
        prior_health=prior_health,
        runtime=starting,
    )

    try:
        mount_or_reflow(
            agent_name,
            mount_agent_fn=mount_agent_fn,
            remount_project_fn=remount_project_fn,
            should_reflow_project_mount_fn=should_reflow_project_mount_fn,
        )
    except TmuxTransientServerUnavailable as exc:
        finalized, applied = runtime_service.finalize_mount_attempt_failure(
            agent_name,
            attempt_id=attempt_id,
            attempted_at=attempted_at,
            state=AgentState.FAILED if runtime is None else runtime.state,
            health='start-deferred' if runtime is None else (runtime.health or 'start-deferred'),
            reconcile_state='deferred',
            restart_count=next_restart_count,
            reason=f'{type(exc).__name__}: {exc}',
            lifecycle_state='degraded' if runtime is None else runtime.lifecycle_state,
        )
        if not applied:
            return persist_mount_superseded(
                stabilize_superseded_runtime(
                    align_runtime_authority_fn(finalized or registry.get(agent_name) or starting),
                    attempted_at=attempted_at,
                    runtime_service=runtime_service,
                ),
                project_id=project_id,
                agent_name=agent_name,
                attempted_at=attempted_at,
                prior_health=prior_health,
                event_store=event_store,
                attempt_id=attempt_id,
            )
        return persist_mount_transient(
            finalized,
            project_id=project_id,
            agent_name=agent_name,
            attempted_at=attempted_at,
            prior_health=prior_health,
            reason=f'{type(exc).__name__}: {exc}',
            event_store=event_store,
        )
    except Exception as exc:
        finalized, applied = runtime_service.finalize_mount_attempt_failure(
            agent_name,
            attempt_id=attempt_id,
            attempted_at=attempted_at,
            state=AgentState.FAILED,
            health='start-failed',
            reconcile_state='failed',
            restart_count=next_restart_count,
            reason=f'{type(exc).__name__}: {exc}',
            lifecycle_state='failed',
        )
        if not applied:
            return persist_mount_superseded(
                stabilize_superseded_runtime(
                    align_runtime_authority_fn(finalized or registry.get(agent_name) or starting),
                    attempted_at=attempted_at,
                    runtime_service=runtime_service,
                ),
                project_id=project_id,
                agent_name=agent_name,
                attempted_at=attempted_at,
                prior_health=prior_health,
                event_store=event_store,
                attempt_id=attempt_id,
            )
        return persist_mount_exception(
            finalized,
            project_id=project_id,
            agent_name=agent_name,
            attempted_at=attempted_at,
            prior_health=prior_health,
            event_store=event_store,
            reason=f'{type(exc).__name__}: {exc}',
        )

    refreshed = registry.get(agent_name)
    if refreshed is None:
        finalized, applied = runtime_service.finalize_mount_attempt_failure(
            agent_name,
            attempt_id=attempt_id,
            attempted_at=attempted_at,
            state=AgentState.FAILED,
            health='start-failed',
            reconcile_state='failed',
            restart_count=next_restart_count,
            reason='runtime-missing-after-mount',
            lifecycle_state='failed',
        )
        if not applied:
            return persist_mount_superseded(
                stabilize_superseded_runtime(
                    align_runtime_authority_fn(finalized or starting),
                    attempted_at=attempted_at,
                    runtime_service=runtime_service,
                ),
                project_id=project_id,
                agent_name=agent_name,
                attempted_at=attempted_at,
                prior_health=prior_health,
                event_store=event_store,
                attempt_id=attempt_id,
            )
        return persist_mount_failure_fn(finalized, agent_name=agent_name, attempted_at=attempted_at, prior_health=prior_health, next_restart_count=next_restart_count, reason='runtime-missing-after-mount')

    refreshed = align_runtime_authority_fn(refreshed)
    refreshed_health = normalized_runtime_health_fn(refreshed) or refreshed.health
    if refreshed_health not in SUCCESS_RUNTIME_HEALTHS:
        finalized, applied = runtime_service.finalize_mount_attempt_failure(
            agent_name,
            attempt_id=attempt_id,
            attempted_at=attempted_at,
            state=AgentState.FAILED,
            health=refreshed_health or 'start-failed',
            reconcile_state='failed',
            restart_count=next_restart_count,
            reason=refreshed_health or 'mount-produced-unhealthy-runtime',
            lifecycle_state=refreshed.lifecycle_state,
        )
        if not applied:
            return persist_mount_superseded(
                stabilize_superseded_runtime(
                    align_runtime_authority_fn(finalized or refreshed),
                    attempted_at=attempted_at,
                    runtime_service=runtime_service,
                ),
                project_id=project_id,
                agent_name=agent_name,
                attempted_at=attempted_at,
                prior_health=prior_health,
                event_store=event_store,
                attempt_id=attempt_id,
            )
        return persist_mount_failure_fn(finalized, agent_name=agent_name, attempted_at=attempted_at, prior_health=prior_health, next_restart_count=next_restart_count, reason=refreshed_health or 'mount-produced-unhealthy-runtime')

    mounted, applied = runtime_service.finalize_mount_attempt_success(
        agent_name,
        attempt_id=attempt_id,
        attempted_at=attempted_at,
        restart_count=next_restart_count,
    )
    if not applied:
        return persist_mount_superseded(
            stabilize_superseded_runtime(
                align_runtime_authority_fn(mounted or refreshed),
                attempted_at=attempted_at,
                runtime_service=runtime_service,
            ),
            project_id=project_id,
            agent_name=agent_name,
            attempted_at=attempted_at,
            prior_health=prior_health,
            event_store=event_store,
            attempt_id=attempt_id,
        )
    mounted = persist_mount_success(mounted)
    record_mount_succeeded(
        event_store,
        project_id=project_id,
        agent_name=agent_name,
        attempted_at=attempted_at,
        prior_health=prior_health,
        runtime=mounted,
    )
    return mounted.health


def stabilize_superseded_runtime(runtime, *, attempted_at: str, runtime_service):
    if runtime is None:
        return None
    if runtime.state is AgentState.IDLE and runtime.reconcile_state == 'starting':
        return runtime_service.patch_runtime_state(
            runtime,
            reconcile_state='steady',
            last_reconcile_at=attempted_at,
            last_failure_reason=None,
            lifecycle_state='idle',
        )
    return runtime


__all__ = ["ensure_mounted"]
