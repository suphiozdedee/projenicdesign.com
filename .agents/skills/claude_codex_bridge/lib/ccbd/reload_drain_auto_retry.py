from __future__ import annotations

import os
from time import time
from typing import Callable

from agents.config_loader import load_project_config
from agents.models import AgentState
from ccbd.reload_apply_service import current_namespace_for_apply, run_additive_reload_apply
from ccbd.reload_drain import DrainRecord, plan_drain_transition, retire_record
from ccbd.reload_plan import build_reload_dry_run_plan


def tick_reload_drain_auto_retry(
    app,
    *,
    load_project_config_fn: Callable = load_project_config,
    run_apply_fn: Callable = run_additive_reload_apply,
) -> dict[str, object]:
    if not _auto_retry_enabled():
        return _payload('noop', reason='reload_drain_auto_retry_disabled')
    store = getattr(app, 'reload_drain_store', None)
    if store is None:
        return _payload('noop', reason='reload_drain_store_missing')
    queue = store.load()
    active = _active_unload_records(queue)
    if not active:
        return _payload('noop', reason='no_active_unload_drains')

    graph = app.current_service_graph()
    now_s = _now_s(app)
    queue, ready, changed = _transition_records(app, graph, queue, active, now_s=now_s)
    if changed:
        store.save(queue)
    if not ready:
        return _payload(
            'waiting',
            active_count=len(active),
            ready_agents=[],
            waiting_agents=[record.intent.agent_name for record in _active_unload_records(queue)],
        )

    loaded = load_project_config_fn(app.project_root)
    new_config = loaded.config if hasattr(loaded, 'config') else loaded
    namespace, namespace_diagnostics = current_namespace_for_apply(app, None)
    plan = build_reload_dry_run_plan(
        graph.config,
        new_config,
        current_config_identity=graph.config_identity,
        project_id=getattr(app, 'project_id', None),
        current_namespace=namespace,
    )
    remove_agents = _remove_agents(plan)
    retry_records = tuple(record for record in ready if record.intent.agent_name in remove_agents)
    stale_ready = tuple(record for record in ready if record.intent.agent_name not in remove_agents)
    if stale_ready:
        queue = _retire_records(queue, stale_ready, now_s=now_s)
        store.save(queue)
    if not retry_records:
        return _payload(
            'skipped',
            reason='no_ready_drain_matches_current_remove_plan',
            plan_class=plan.get('plan_class'),
            ready_agents=[record.intent.agent_name for record in ready],
            retired_stale_agents=[record.intent.agent_name for record in stale_ready],
            namespace_status=namespace_diagnostics.get('status') if isinstance(namespace_diagnostics, dict) else None,
        )

    result = run_apply_fn(app, new_config, current_namespace=namespace, lock_already_held=True)
    return _payload(
        'applied' if str(getattr(result, 'status', '') or '') == 'published' else 'blocked',
        reason=str(getattr(result, 'reason', '') or getattr(result, 'status', '') or ''),
        plan_class=str(getattr(result, 'plan_class', '') or ''),
        apply_status=str(getattr(result, 'status', '') or ''),
        retry_agents=[record.intent.agent_name for record in retry_records],
    )


def _active_unload_records(queue) -> tuple[DrainRecord, ...]:
    return tuple(
        record
        for record in tuple(getattr(queue, 'records', ()) or ())
        if not record.terminal and record.intent.intent_kind == 'unload'
    )


def _transition_records(app, graph, queue, records: tuple[DrainRecord, ...], *, now_s: float):
    ready: list[DrainRecord] = []
    changed = False
    for record in records:
        updated = plan_drain_transition(
            record,
            now_s=now_s,
            is_busy=lambda item: _agent_busy(app, graph, item.intent.agent_name),
        )
        if updated is not record:
            queue = queue.replace_record(updated)
            changed = True
        if updated.status == 'idle_ready':
            ready.append(updated)
    return queue, tuple(ready), changed


def _agent_busy(app, graph, agent_name: str) -> bool:
    dispatcher = getattr(app, 'dispatcher', None)
    has_outstanding = getattr(dispatcher, '_has_outstanding_work', None)
    if callable(has_outstanding):
        try:
            if has_outstanding(agent_name):
                return True
        except Exception:
            return True
    runtime = graph.registry.get(agent_name)
    return runtime is not None and runtime.state is AgentState.BUSY


def _remove_agents(plan: dict[str, object]) -> set[str]:
    return {
        str(item.get('agent') or '').strip()
        for item in tuple(plan.get('operations') or ())
        if isinstance(item, dict) and str(item.get('op') or '') == 'remove_agent' and str(item.get('agent') or '').strip()
    }


def _retire_records(queue, records: tuple[DrainRecord, ...], *, now_s: float):
    for record in records:
        retired = retire_record(record, now_s=now_s)
        queue = queue.replace_record(retired)
    return queue


def _now_s(app) -> float:
    clock_s = getattr(app, 'reload_drain_clock_s', None)
    if callable(clock_s):
        return float(clock_s())
    return time()


def _payload(status: str, **values) -> dict[str, object]:
    return {'reload_drain_auto_retry_status': status, **{key: value for key, value in values.items() if value is not None}}


def _auto_retry_enabled() -> bool:
    raw = str(os.environ.get('CCB_CCBD_RELOAD_DRAIN_AUTO_RETRY') or '').strip().lower()
    return raw not in {'0', 'false', 'no', 'off'}


__all__ = ['tick_reload_drain_auto_retry']
