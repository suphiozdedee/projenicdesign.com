from __future__ import annotations

from agents.models import AgentState
from ccbd.services.runtime_recovery_policy import (
    PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH,
    PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTHS,
    RUNTIME_RECOVERY_CIRCUIT_OPEN_HEALTH,
    RUNTIME_RECOVERY_PROBING_HEALTH,
    normalized_runtime_health,
    recovery_circuit_threshold,
)

from .recovery_context import RecoveryContext
from .recovery_events import append_recovery_event

SUCCESS_RUNTIME_HEALTHS = frozenset({'healthy', 'restored'})
MAX_CONSECUTIVE_RECOVERY_ATTEMPTS = 6
RECOVERY_STABILITY_WINDOW_S = 90


def start_recovery(
    ctx: RecoveryContext,
    *,
    attempted_at: str,
    prior_health: str,
):
    recovering = ctx.upsert_if_changed_fn(
        ctx.runtime,
        reconcile_state='recovering',
        last_reconcile_at=attempted_at,
        lifecycle_state='recovering',
    )
    append_recovery_event(
        ctx,
        event_kind='recover_started',
        occurred_at=attempted_at,
        runtime=recovering,
        prior_health=prior_health,
        result_health=prior_health,
    )
    return recovering


def attempt_recovery_action(ctx: RecoveryContext, *, recovering):
    if ctx.should_reflow_project_namespace_fn(recovering):
        ctx.remount_project_fn(f'pane_recovery:{ctx.agent_name}')
        return ctx.registry.get(ctx.agent_name), None
    refreshed = ctx.runtime_service.refresh_provider_binding(ctx.agent_name, recover=True)
    if refreshed is None:
        return None, None
    if normalized_runtime_health(refreshed) in PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTHS:
        return refreshed, str(
            getattr(refreshed, 'last_failure_reason', None)
            or normalized_runtime_health(refreshed)
        )
    if ctx.should_reflow_project_namespace_fn(recovering, recovered=refreshed):
        ctx.remount_project_fn(f'pane_recovery:{ctx.agent_name}')
        return ctx.registry.get(ctx.agent_name), None
    return refreshed, None


def mark_recovery_missing(
    ctx: RecoveryContext,
    *,
    recovering,
    attempted_at: str,
    restart_count: int,
    recovery_failure_count: int,
    prior_health: str,
) -> str:
    if recovery_failure_count >= recovery_circuit_threshold(
        recovering,
        default=MAX_CONSECUTIVE_RECOVERY_ATTEMPTS,
    ):
        return mark_recovery_circuit_open(
            ctx,
            runtime=recovering,
            occurred_at=attempted_at,
            restart_count=restart_count,
            recovery_failure_count=recovery_failure_count,
            prior_health=prior_health,
            reason='runtime-missing-after-recover',
        )
    failed = ctx.upsert_if_changed_fn(
        recovering,
        state=AgentState.DEGRADED,
        reconcile_state='degraded',
        restart_count=restart_count,
        recovery_failure_count=recovery_failure_count,
        last_reconcile_at=attempted_at,
        last_failure_reason='runtime-missing-after-recover',
        lifecycle_state='degraded',
    )
    append_recovery_event(
        ctx,
        event_kind='recover_failed',
        occurred_at=attempted_at,
        runtime=failed,
        prior_health=prior_health,
        result_health='unmounted',
        details={'reason': 'runtime-missing-after-recover'},
    )
    return 'unmounted'


def mark_recovery_blocked(
    ctx: RecoveryContext,
    *,
    runtime,
    occurred_at: str,
    prior_health: str,
    reason: str,
) -> str:
    blocked = ctx.upsert_if_changed_fn(
        runtime,
        state=AgentState.DEGRADED,
        health=PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH,
        reconcile_state='blocked',
        last_reconcile_at=occurred_at,
        last_failure_reason=reason,
        lifecycle_state='degraded',
    )
    append_recovery_event(
        ctx,
        event_kind='recover_blocked',
        occurred_at=occurred_at,
        runtime=blocked,
        prior_health=prior_health,
        result_health=PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH,
        details={'action': 'blocked', 'reason': reason},
    )
    return blocked.health


def mark_recovery_succeeded(
    ctx: RecoveryContext,
    *,
    refreshed,
    attempted_at: str,
    restart_count: int,
    prior_health: str,
    next_health: str,
) -> str:
    next_state = AgentState.IDLE if refreshed.state is AgentState.DEGRADED else refreshed.state
    stabilized = ctx.upsert_if_changed_fn(
        refreshed,
        state=next_state,
        health=next_health,
        reconcile_state='steady',
        restart_count=restart_count,
        recovery_failure_count=0,
        last_reconcile_at=attempted_at,
        last_failure_reason=None,
        lifecycle_state=next_state.value,
    )
    append_recovery_event(
        ctx,
        event_kind='recover_succeeded',
        occurred_at=attempted_at,
        runtime=stabilized,
        prior_health=prior_health,
        result_health=next_health,
        details={
            'restart_count': stabilized.restart_count,
            'stable_after_s': RECOVERY_STABILITY_WINDOW_S,
        },
    )
    return stabilized.health


def mark_recovery_probing(
    ctx: RecoveryContext,
    *,
    refreshed,
    attempted_at: str,
    restart_count: int,
    recovery_failure_count: int,
    prior_health: str,
    failure_reason: str | None,
) -> str:
    probing = ctx.upsert_if_changed_fn(
        refreshed,
        state=AgentState.DEGRADED,
        health=RUNTIME_RECOVERY_PROBING_HEALTH,
        reconcile_state='probing',
        restart_count=restart_count,
        recovery_failure_count=recovery_failure_count,
        last_reconcile_at=attempted_at,
        last_failure_reason=failure_reason or prior_health or 'pane-recovery-probing',
        lifecycle_state='recovering',
    )
    append_recovery_event(
        ctx,
        event_kind='recover_probing',
        occurred_at=attempted_at,
        runtime=probing,
        prior_health=prior_health,
        result_health=RUNTIME_RECOVERY_PROBING_HEALTH,
        details={
            'restart_count': probing.restart_count,
            'recovery_failure_count': probing.recovery_failure_count,
            'stability_window_s': RECOVERY_STABILITY_WINDOW_S,
        },
    )
    return probing.health


def mark_recovery_failed(
    ctx: RecoveryContext,
    *,
    refreshed,
    attempted_at: str,
    restart_count: int,
    recovery_failure_count: int,
    prior_health: str,
    next_health: str,
    failure_reason: str | None,
) -> str:
    next_health = normalized_runtime_health(refreshed) or next_health
    recovery_blocked = next_health in PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTHS
    if not recovery_blocked and recovery_failure_count >= recovery_circuit_threshold(
        refreshed,
        default=MAX_CONSECUTIVE_RECOVERY_ATTEMPTS,
    ):
        return mark_recovery_circuit_open(
            ctx,
            runtime=refreshed,
            occurred_at=attempted_at,
            restart_count=restart_count,
            recovery_failure_count=recovery_failure_count,
            prior_health=prior_health,
            reason=failure_reason or next_health or 'recover-failed',
        )
    failure_runtime = ctx.upsert_if_changed_fn(
        refreshed,
        state=AgentState.DEGRADED,
        reconcile_state='blocked' if recovery_blocked else 'degraded',
        restart_count=restart_count,
        recovery_failure_count=recovery_failure_count,
        last_reconcile_at=attempted_at,
        last_failure_reason=failure_reason or next_health or prior_health or 'recover-failed',
        lifecycle_state='degraded',
    )
    append_recovery_event(
        ctx,
        event_kind='recover_failed',
        occurred_at=attempted_at,
        runtime=failure_runtime,
        prior_health=prior_health,
        result_health=next_health,
        details={'reason': failure_runtime.last_failure_reason or 'recover-failed'},
    )
    return failure_runtime.health


def mark_recovery_circuit_open(
    ctx: RecoveryContext,
    *,
    runtime,
    occurred_at: str,
    restart_count: int,
    recovery_failure_count: int,
    prior_health: str,
    reason: str,
) -> str:
    detail = (
        f'Automatic pane recovery stopped after {recovery_failure_count} consecutive '
        f'unstable attempts ({reason}). Repair the provider/session state, then run '
        f'`ccb restart {ctx.agent_name}` or remount the project.'
    )
    blocked = ctx.upsert_if_changed_fn(
        runtime,
        state=AgentState.DEGRADED,
        health=RUNTIME_RECOVERY_CIRCUIT_OPEN_HEALTH,
        reconcile_state='blocked',
        restart_count=restart_count,
        recovery_failure_count=recovery_failure_count,
        last_reconcile_at=occurred_at,
        last_failure_reason=detail,
        lifecycle_state='degraded',
    )
    append_recovery_event(
        ctx,
        event_kind='recover_blocked',
        occurred_at=occurred_at,
        runtime=blocked,
        prior_health=prior_health,
        result_health=RUNTIME_RECOVERY_CIRCUIT_OPEN_HEALTH,
        details={
            'reason': reason,
            'recovery_failure_count': recovery_failure_count,
            'max_attempts': recovery_circuit_threshold(
                runtime,
                default=MAX_CONSECUTIVE_RECOVERY_ATTEMPTS,
            ),
        },
    )
    return blocked.health

__all__ = [
    'SUCCESS_RUNTIME_HEALTHS',
    'MAX_CONSECUTIVE_RECOVERY_ATTEMPTS',
    'RECOVERY_STABILITY_WINDOW_S',
    'attempt_recovery_action',
    'mark_recovery_blocked',
    'mark_recovery_circuit_open',
    'mark_recovery_failed',
    'mark_recovery_missing',
    'mark_recovery_probing',
    'mark_recovery_succeeded',
    'start_recovery',
]
