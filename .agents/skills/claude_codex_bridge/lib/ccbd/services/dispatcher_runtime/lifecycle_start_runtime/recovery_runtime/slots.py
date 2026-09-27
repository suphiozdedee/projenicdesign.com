from __future__ import annotations

from dataclasses import is_dataclass, replace
from types import SimpleNamespace

from agents.models import AgentState, AgentValidationError
from ccbd.api_models import TargetKind
from ccbd.services.runtime_recovery_policy import (
    HARD_BLOCKED_RUNTIME_HEALTHS,
    herdr_auto_restore_mode,
    herdr_recovery_capable,
    normalized_runtime_health,
    runtime_health_recoverable,
    should_record_recovery_capability_block,
)
from ccbd.supervision.recovery_context import build_recovery_context
from ccbd.supervision.recovery_transitions import mark_recovery_blocked
from ccbd.supervision.store import SupervisionEventStore

from ..models import QueuedTargetSlot
from .support import can_attempt_runtime_recovery

RUNNABLE_AGENT_STATES = frozenset({AgentState.IDLE, AgentState.STARTING, AgentState.DEGRADED})


def _degraded_runtime_action(dispatcher, runtime) -> str:
    health = normalized_runtime_health(runtime)
    if health in HARD_BLOCKED_RUNTIME_HEALTHS:
        return "blocked"
    if not herdr_recovery_capable(runtime):
        return "blocked"
    if not runtime_health_recoverable(runtime):
        return "keep"
    if not can_attempt_runtime_recovery(dispatcher, runtime):
        return "drop"
    return "refresh"


def _refresh_runtime(dispatcher, agent_name: str):
    try:
        return dispatcher._runtime_service.refresh_provider_binding(agent_name, recover=True)
    except Exception:
        return None


def _refreshed_slot(slot: QueuedTargetSlot, refreshed):
    if refreshed is None or refreshed.state not in RUNNABLE_AGENT_STATES:
        return None
    refreshed_health = normalized_runtime_health(refreshed)
    if refreshed_health in HARD_BLOCKED_RUNTIME_HEALTHS or runtime_health_recoverable(refreshed):
        return None
    return replace(slot, runtime=refreshed)


def _record_lifecycle_recovery_blocked(dispatcher, runtime) -> None:
    if not should_record_recovery_capability_block(runtime):
        return
    event_store = _supervision_event_store(dispatcher)
    if event_store is None:
        return
    occurred_at = _dispatcher_clock(dispatcher)()
    prior_health = normalized_runtime_health(runtime) or str(getattr(runtime, "health", "") or "")
    mode = herdr_auto_restore_mode(runtime)
    ctx = build_recovery_context(
        project_id=_dispatcher_project_id(dispatcher, runtime),
        agent_name=runtime.agent_name,
        runtime=runtime,
        registry=dispatcher._registry,
        runtime_service=dispatcher._runtime_service,
        remount_project_fn=None,
        clock=_dispatcher_clock(dispatcher),
        event_store=event_store,
        align_runtime_authority_fn=lambda value: value,
        upsert_if_changed_fn=lambda value, **updates: _upsert_runtime(dispatcher, value, **updates),
        is_in_backoff_window_fn=lambda *_args, **_kwargs: False,
        should_reflow_project_namespace_fn=lambda *_args, **_kwargs: False,
    )
    mark_recovery_blocked(
        ctx,
        runtime=runtime,
        occurred_at=occurred_at,
        prior_health=prior_health,
        reason=f"herdr-auto-restore-{mode}-not-recovery-capable",
    )


def _supervision_event_store(dispatcher):
    store = getattr(dispatcher, "_supervision_event_store", None)
    if store is not None:
        return store
    store = getattr(dispatcher, "_event_store", None)
    if store is not None and not store.__class__.__name__.startswith("Job"):
        return store
    layout = getattr(dispatcher, "_layout", None)
    if layout is None:
        return None
    return SupervisionEventStore(layout)


def _dispatcher_clock(dispatcher):
    clock = getattr(dispatcher, "_clock", None)
    return clock if callable(clock) else (lambda: "")


def _dispatcher_project_id(dispatcher, runtime) -> str:
    value = str(getattr(dispatcher, "_project_id", "") or "").strip()
    if value:
        return value
    value = str(getattr(runtime, "project_id", "") or "").strip()
    if value:
        return value
    layout = getattr(dispatcher, "_layout", None)
    return str(getattr(layout, "project_id", "") or "").strip()


def _upsert_runtime(dispatcher, runtime, **updates):
    if is_dataclass(runtime):
        updated = replace(runtime, **updates)
    else:
        values = dict(vars(runtime))
        values.update(updates)
        updated = SimpleNamespace(**values)
    upsert = getattr(dispatcher._registry, "upsert_authority", None)
    if callable(upsert):
        return upsert(updated)
    upsert = getattr(dispatcher._registry, "upsert", None)
    if callable(upsert):
        return upsert(updated)
    return updated


def refresh_slot_runtime_for_start(dispatcher, slot: QueuedTargetSlot) -> QueuedTargetSlot | None:
    runtime = slot.runtime
    if slot.target_kind is not TargetKind.AGENT:
        return slot
    if runtime is None or runtime.state in {AgentState.STOPPED, AgentState.FAILED}:
        if dispatcher._runtime_service is None:
            return None
        try:
            ensured = dispatcher._runtime_service.ensure_ready(slot.target_name)
        except AgentValidationError:
            return None
        if ensured is None or ensured.state not in RUNNABLE_AGENT_STATES:
            return None
        return replace(slot, runtime=ensured)
    if runtime.state is not AgentState.DEGRADED:
        return slot

    action = _degraded_runtime_action(dispatcher, runtime)
    if action == "blocked":
        _record_lifecycle_recovery_blocked(dispatcher, runtime)
        return None
    if action == "drop":
        return None
    if action == "keep":
        return slot
    return _refreshed_slot(slot, _refresh_runtime(dispatcher, runtime.agent_name))


def _iter_queued_runtimes(dispatcher):
    for agent_name in dispatcher._config.agents:
        if dispatcher._state.active_job(agent_name) is not None:
            continue
        if dispatcher._state.queue_depth(agent_name) == 0:
            continue
        runtime = dispatcher._registry.get(agent_name)
        if runtime is None or runtime.state in {AgentState.STOPPED, AgentState.FAILED}:
            yield agent_name, runtime
            continue
        if runtime.state not in RUNNABLE_AGENT_STATES:
            continue
        yield agent_name, runtime


def iter_runnable_agent_slots(dispatcher):
    for agent_name, runtime in _iter_queued_runtimes(dispatcher):
        if runtime is None:
            yield QueuedTargetSlot(
                target_kind=TargetKind.AGENT,
                target_name=agent_name,
                runtime=None,
            )
            continue
        if runtime.state is AgentState.DEGRADED:
            action = _degraded_runtime_action(dispatcher, runtime)
            if action == "blocked":
                _record_lifecycle_recovery_blocked(dispatcher, runtime)
                continue
            if action == "drop":
                continue
        yield QueuedTargetSlot(
            target_kind=TargetKind.AGENT,
            target_name=agent_name,
            runtime=runtime,
        )


__all__ = ["RUNNABLE_AGENT_STATES", "iter_runnable_agent_slots", "refresh_slot_runtime_for_start"]
