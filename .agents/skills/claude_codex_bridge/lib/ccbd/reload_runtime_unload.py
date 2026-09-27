from __future__ import annotations

from time import time

from agents.models import AgentState
from ccbd.reload_drain import DrainIntent, plan_drain_transition, retire_record
from ccbd.reload_runtime_mount_models import AdditiveRuntimeMountResult, blocked_mount_result, unloaded_result
from provider_runtime.helper_cleanup import terminate_helper_manifest_path


def run_removed_agent_unloads(
    app,
    graph,
    *,
    patch_result,
) -> AdditiveRuntimeMountResult:
    removed_agents = tuple(sorted((getattr(patch_result, 'removed_agents', {}) or {}).keys()))
    preserved_agents = tuple(sorted((getattr(patch_result, 'preserved_before', {}) or {}).keys()))
    if not removed_agents:
        return AdditiveRuntimeMountResult(
            status='noop',
            preserved_runtime_unchanged_agents=preserved_agents,
            diagnostics={
                'reason': 'no_removed_agent_panes',
                'graph_published': False,
                'lease_or_lifecycle_written': False,
                'config_watch_started': False,
                'cleanup_tmux_orphans': False,
                'unload_or_replace_executed': False,
            },
        )
    blocked = _unload_blocker(app, graph, removed_agents)
    if blocked is not None:
        return blocked_mount_result(*blocked, requested_agents=removed_agents)
    stopped: list[str] = []
    helpers: list[str] = []
    registry = graph.registry
    for agent_name in removed_agents:
        if terminate_helper_manifest_path(app.paths.agent_helper_path(agent_name)):
            helpers.append(agent_name)
        if registry.remove(agent_name) is not None:
            stopped.append(agent_name)
    _retire_unload_drain_records(app, removed_agents)
    return unloaded_result(
        requested_agents=removed_agents,
        unloaded_agents=removed_agents,
        stopped_agents=tuple(stopped),
        helper_terminated_agents=tuple(helpers),
        preserved_agents=preserved_agents,
    )


def pre_namespace_unload_blocker(app, graph, plan: dict[str, object]) -> tuple[object, ...] | None:
    agents = tuple(
        sorted(
            {
                str(item.get('agent') or '').strip()
                for item in tuple(plan.get('operations') or ())
                if isinstance(item, dict) and str(item.get('op') or '') == 'remove_agent'
            }
        )
    )
    if not agents:
        return None
    return _unload_blocker(app, graph, agents, plan=plan)


def _unload_blocker(app, graph, agents: tuple[str, ...], *, plan: dict[str, object] | None = None) -> tuple[object, ...] | None:
    dispatcher = getattr(app, 'dispatcher', None)
    has_outstanding = getattr(dispatcher, '_has_outstanding_work', None)
    for agent_name in agents:
        if callable(has_outstanding) and has_outstanding(agent_name):
            return _blocked_unload(
                app,
                plan=plan,
                agent_name=agent_name,
                reason='agent_has_outstanding_work',
                message=f'cannot unload agent with outstanding work: {agent_name}',
            )
        runtime = graph.registry.get(agent_name)
        if runtime is not None and runtime.state is AgentState.BUSY:
            return _blocked_unload(
                app,
                plan=plan,
                agent_name=agent_name,
                reason='agent_busy',
                message=f'cannot unload busy agent: {agent_name}',
            )
    return None


def _blocked_unload(app, *, plan: dict[str, object] | None, agent_name: str, reason: str, message: str) -> tuple[object, ...]:
    diagnostics = _record_unload_drain(app, plan=plan, agent_name=agent_name, reason=message)
    if diagnostics:
        return (reason, message, diagnostics)
    return (reason, message)


def _record_unload_drain(app, *, plan: dict[str, object] | None, agent_name: str, reason: str) -> dict[str, object]:
    store = getattr(app, 'reload_drain_store', None)
    if store is None:
        return {}
    now_s = _now_s(app)
    queue = store.load()
    existing = queue.active_records_for(agent_name)
    if existing:
        record = plan_drain_transition(existing[-1], now_s=now_s, is_busy=lambda _record: True)
        queue = queue.replace_record(record)
        store.save(queue)
        return {
            'drain_action': 'reused',
            'drain_accepted': True,
            'drain_record': record.to_record(),
            'drain_queue_pending_count': queue.pending_count,
        }
    intent = _unload_intent_from_plan(plan, agent_name=agent_name, now_s=now_s, reason=reason)
    result = queue.enqueue(intent, now_s=now_s)
    record = result.record
    queue = result.queue
    if result.accepted:
        record = plan_drain_transition(record, now_s=now_s, is_busy=lambda _record: True)
        queue = queue.replace_record(record)
        store.save(queue)
    return {
        'drain_action': 'enqueued' if result.accepted else 'rejected',
        'drain_accepted': bool(result.accepted),
        'drain_record': record.to_record(),
        'drain_queue_pending_count': queue.pending_count,
    }


def _unload_intent_from_plan(
    plan: dict[str, object] | None,
    *,
    agent_name: str,
    now_s: float,
    reason: str,
) -> DrainIntent:
    intent_id = ''
    for item in tuple((plan or {}).get('drain_intents') or ()):
        if not isinstance(item, dict):
            continue
        if str(item.get('intent_kind') or '') == 'unload' and str(item.get('agent') or '') == agent_name:
            intent_id = str(item.get('intent_id') or '').strip()
            break
    if not intent_id:
        intent_id = f'drain_unload_{agent_name}_{int(float(now_s) * 1000)}'
    return DrainIntent(
        intent_id=intent_id,
        intent_kind='unload',
        agent_name=agent_name,
        created_at_s=float(now_s),
        reason=reason,
        old_config_signature=str((plan or {}).get('old_config_signature') or '') or None,
        new_config_signature=str((plan or {}).get('new_config_signature') or '') or None,
    )


def _retire_unload_drain_records(app, agent_names: tuple[str, ...]) -> None:
    store = getattr(app, 'reload_drain_store', None)
    if store is None:
        return
    queue = store.load()
    now_s = _now_s(app)
    changed = False
    for agent_name in agent_names:
        for record in queue.active_records_for(agent_name):
            ready = plan_drain_transition(record, now_s=now_s, is_busy=lambda _record: False)
            retired = retire_record(ready, now_s=now_s)
            if retired is not record:
                queue = queue.replace_record(retired)
                changed = True
    if changed:
        store.save(queue)


def _now_s(app) -> float:
    clock_s = getattr(app, 'reload_drain_clock_s', None)
    if callable(clock_s):
        return float(clock_s())
    return time()


__all__ = ['pre_namespace_unload_blocker', 'run_removed_agent_unloads']
