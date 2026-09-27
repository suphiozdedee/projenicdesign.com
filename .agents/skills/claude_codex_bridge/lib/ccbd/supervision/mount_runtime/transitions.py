from __future__ import annotations

from .events import record_mount_failed, record_mount_superseded

SUCCESS_RUNTIME_HEALTHS = frozenset({'healthy', 'restored'})


def mount_actions_missing(*, mount_agent_fn, remount_project_fn) -> bool:
    return mount_agent_fn is None and remount_project_fn is None


def missing_mount_action_health(runtime) -> str:
    return 'unmounted' if runtime is None else runtime.health


def in_backoff_window(runtime, *, attempted_at: str, is_in_backoff_window_fn) -> bool:
    return runtime is not None and is_in_backoff_window_fn(runtime, now=attempted_at)


def start_mount_attempt(
    *,
    agent_name: str,
    runtime,
    attempted_at: str,
    build_starting_runtime_fn,
):
    starting = build_starting_runtime_fn(agent_name, runtime=runtime, attempted_at=attempted_at)
    prior_health = runtime.health if runtime is not None else 'unmounted'
    next_restart_count = starting.restart_count + 1
    return starting, prior_health, next_restart_count


def persist_mount_exception(
    finalized,
    *,
    project_id: str,
    agent_name: str,
    attempted_at: str,
    prior_health: str,
    event_store,
    reason: str,
) -> str:
    record_mount_failed(
        event_store,
        project_id=project_id,
        agent_name=agent_name,
        attempted_at=attempted_at,
        prior_health=prior_health,
        runtime=finalized,
        reason=reason,
    )
    return finalized.health


def persist_mount_transient(
    finalized,
    *,
    project_id: str,
    agent_name: str,
    attempted_at: str,
    prior_health: str,
    reason: str,
    event_store,
) -> str:
    record_mount_failed(
        event_store,
        project_id=project_id,
        agent_name=agent_name,
        attempted_at=attempted_at,
        prior_health=prior_health,
        runtime=finalized,
        reason=reason,
    )
    return finalized.health


def persist_mount_success(
    finalized,
):
    return finalized


def persist_mount_superseded(
    current,
    *,
    project_id: str,
    agent_name: str,
    attempted_at: str,
    prior_health: str,
    event_store,
    attempt_id: str,
) -> str:
    record_mount_superseded(
        event_store,
        project_id=project_id,
        agent_name=agent_name,
        attempted_at=attempted_at,
        prior_health=prior_health,
        runtime=current,
        attempt_id=attempt_id,
    )
    return current.health


def mount_or_reflow(agent_name: str, *, mount_agent_fn, remount_project_fn, should_reflow_project_mount_fn) -> None:
    if should_reflow_project_mount_fn(agent_name):
        remount_project_fn(f'mount_recovery:{agent_name}')
        return
    mount_agent_fn(agent_name)


__all__ = [
    'SUCCESS_RUNTIME_HEALTHS',
    'in_backoff_window',
    'missing_mount_action_health',
    'mount_actions_missing',
    'mount_or_reflow',
    'persist_mount_exception',
    'persist_mount_transient',
    'persist_mount_success',
    'persist_mount_superseded',
    'start_mount_attempt',
]
