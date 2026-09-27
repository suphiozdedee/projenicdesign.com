from __future__ import annotations

from dataclasses import replace
from time import time

from agents.models import AgentState
from ccbd.reload_drain import DrainIntent, plan_drain_transition, retire_record
from ccbd.reload_runtime_mount_models import (
    AdditiveRuntimeMountResult,
    blocked_mount_result,
    replaced_result,
)
from ccbd.reload_runtime_mount_service import run_additive_agent_mounts
from ccbd.reload_runtime_mount_state import summary_record
from ccbd.start_flow import run_start_flow
from provider_runtime.helper_cleanup import terminate_helper_manifest_path


def pre_namespace_replace_blocker(app, graph, plan: dict[str, object]) -> tuple[object, ...] | None:
    agents = tuple(
        sorted(
            {
                str(item.get('agent') or '').strip()
                for item in tuple(plan.get('operations') or ())
                if isinstance(item, dict) and str(item.get('op') or '') == 'replace_agent'
            }
        )
    )
    if not agents:
        return None
    dispatcher = getattr(app, 'dispatcher', None)
    has_outstanding = getattr(dispatcher, '_has_outstanding_work', None)
    for agent_name in agents:
        if callable(has_outstanding) and has_outstanding(agent_name):
            return _blocked_replace(
                app,
                plan=plan,
                agent_name=agent_name,
                reason='agent_has_outstanding_work',
                message=f'cannot replace agent with outstanding work: {agent_name}',
            )
        runtime = graph.registry.get(agent_name)
        if runtime is not None and runtime.state is AgentState.BUSY:
            return _blocked_replace(
                app,
                plan=plan,
                agent_name=agent_name,
                reason='agent_busy',
                message=f'cannot replace busy agent: {agent_name}',
            )
    return None


def run_replaced_agent_runtime_updates(
    app,
    graph,
    *,
    namespace,
    patch_result,
    run_start_flow_fn=run_start_flow,
) -> AdditiveRuntimeMountResult:
    replaced_panes = dict(getattr(patch_result, 'replaced_agents', {}) or {})
    replaced_agents = tuple(sorted(replaced_panes))
    if not replaced_agents:
        return AdditiveRuntimeMountResult(
            status='noop',
            diagnostics={
                'reason': 'no_replaced_agent_panes',
                'graph_published': False,
                'lease_or_lifecycle_written': False,
                'config_watch_started': False,
                'cleanup_tmux_orphans': False,
                'unload_or_replace_executed': False,
            },
        )

    registry = getattr(graph, 'registry', None)
    if registry is None:
        return blocked_mount_result(
            'runtime_registry_missing',
            'runtime replace updates require a target runtime registry',
            requested_agents=replaced_agents,
        )

    stopped: list[str] = []
    helpers: list[str] = []
    for agent_name in replaced_agents:
        if terminate_helper_manifest_path(app.paths.agent_helper_path(agent_name)):
            helpers.append(agent_name)
        if registry.remove(agent_name) is not None:
            stopped.append(agent_name)

    mount_patch = replace(
        patch_result,
        agent_panes=replaced_panes,
        preserved_before=_preserved_without_replaced(patch_result, replaced_agents),
        preserved_after=_preserved_without_replaced(patch_result, replaced_agents),
    )
    mount_result = run_additive_agent_mounts(
        app,
        graph,
        namespace=namespace,
        patch_result=mount_patch,
        run_start_flow_fn=run_start_flow_fn,
    )
    if str(getattr(mount_result, 'status', '') or '') != 'mounted':
        return _replace_failed_result(
            mount_result,
            requested_agents=replaced_agents,
            stopped_agents=tuple(stopped),
            helper_terminated_agents=tuple(helpers),
        )
    _retire_replace_drain_records(app, replaced_agents)
    return replaced_result(
        requested_agents=replaced_agents,
        replaced_agents=replaced_agents,
        mounted_agents=mount_result.mounted_agents,
        written_agents=mount_result.runtime_authority_written_agents,
        stopped_agents=tuple(stopped),
        helper_terminated_agents=tuple(helpers),
        preserved_agents=mount_result.preserved_runtime_unchanged_agents,
        summary={
            'replace': {
                'stopped_agents': list(stopped),
                'helper_terminated_agents': list(helpers),
                'reused_agent_panes': dict(replaced_panes),
            },
            'mount': summary_record(mount_result.summary),
        },
    )


def _blocked_replace(app, *, plan: dict[str, object], agent_name: str, reason: str, message: str) -> tuple[object, ...]:
    diagnostics = _record_replace_drain(app, plan=plan, agent_name=agent_name, reason=message)
    if diagnostics:
        return (reason, message, diagnostics)
    return (reason, message)


def _record_replace_drain(app, *, plan: dict[str, object], agent_name: str, reason: str) -> dict[str, object]:
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
    intent = _replace_intent_from_plan(plan, agent_name=agent_name, now_s=now_s, reason=reason)
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


def _replace_intent_from_plan(
    plan: dict[str, object],
    *,
    agent_name: str,
    now_s: float,
    reason: str,
) -> DrainIntent:
    intent_id = ''
    for item in tuple(plan.get('drain_intents') or ()):
        if not isinstance(item, dict):
            continue
        if str(item.get('intent_kind') or '') == 'replace' and str(item.get('agent') or '') == agent_name:
            intent_id = str(item.get('intent_id') or '').strip()
            break
    if not intent_id:
        intent_id = f'drain_replace_{agent_name}_{int(float(now_s) * 1000)}'
    return DrainIntent(
        intent_id=intent_id,
        intent_kind='replace',
        agent_name=agent_name,
        created_at_s=float(now_s),
        reason=reason,
        old_config_signature=str(plan.get('old_config_signature') or '') or None,
        new_config_signature=str(plan.get('new_config_signature') or '') or None,
    )


def _replace_failed_result(
    mount_result: AdditiveRuntimeMountResult,
    *,
    requested_agents: tuple[str, ...],
    stopped_agents: tuple[str, ...],
    helper_terminated_agents: tuple[str, ...],
) -> AdditiveRuntimeMountResult:
    diagnostics = dict(mount_result.diagnostics)
    diagnostics.update(
        {
            'runtime_authority_scope': 'replace_agents_only',
            'unload_or_replace_executed': bool(stopped_agents or helper_terminated_agents),
        }
    )
    return AdditiveRuntimeMountResult(
        status=mount_result.status,
        requested_agents=requested_agents,
        mounted_agents=mount_result.mounted_agents,
        runtime_authority_written_agents=mount_result.runtime_authority_written_agents,
        runtime_authority_stopped_agents=stopped_agents,
        helper_terminated_agents=helper_terminated_agents,
        preserved_runtime_unchanged_agents=mount_result.preserved_runtime_unchanged_agents,
        partial=True,
        summary={
            'replace': {
                'stopped_agents': list(stopped_agents),
                'helper_terminated_agents': list(helper_terminated_agents),
            },
            'mount': summary_record(mount_result.summary),
        },
        diagnostics=diagnostics,
    )


def _preserved_without_replaced(patch_result, replaced_agents: tuple[str, ...]) -> dict[str, str]:
    excluded = set(replaced_agents)
    return {
        str(agent): str(pane)
        for agent, pane in dict(getattr(patch_result, 'preserved_before', {}) or {}).items()
        if str(agent) not in excluded
    }


def _retire_replace_drain_records(app, agent_names: tuple[str, ...]) -> None:
    store = getattr(app, 'reload_drain_store', None)
    if store is None:
        return
    queue = store.load()
    now_s = _now_s(app)
    changed = False
    for agent_name in agent_names:
        for record in queue.active_records_for(agent_name):
            if record.intent.intent_kind != 'replace':
                continue
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


__all__ = ['pre_namespace_replace_blocker', 'run_replaced_agent_runtime_updates']
