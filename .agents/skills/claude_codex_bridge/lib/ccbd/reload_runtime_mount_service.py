from __future__ import annotations

from ccbd.reload_runtime_mount_models import (
    AdditiveRuntimeMountResult,
    blocked_mount_result,
    failed_mount_result,
    mounted_result,
    noop_mount_result,
)
from ccbd.reload_runtime_mount_start import (
    call_start_flow_for_additive_mount,
    start_options,
)
from ccbd.reload_runtime_mount_state import (
    agent_names,
    agent_panes_from_record,
    changed_agents,
    runtime_snapshots,
    runtime_guard_agents,
    summary_record,
    summary_started,
)
from ccbd.reload_runtime_mount_validation import (
    blocked_mount_reason,
    existing_runtime_agents,
)
from ccbd.start_flow import run_start_flow


def run_additive_agent_mounts(
    app,
    graph,
    *,
    namespace,
    patch_result,
    run_start_flow_fn=run_start_flow,
) -> AdditiveRuntimeMountResult:
    prepared = _prepare_mount_context(graph, namespace, patch_result)
    if isinstance(prepared, AdditiveRuntimeMountResult):
        return prepared

    agent_panes, requested_agents, preserved_agents, supervisor = prepared
    registry = graph.registry
    before_new = runtime_snapshots(registry, requested_agents)
    existing = existing_runtime_agents(before_new, requested_agents)
    if existing:
        return blocked_mount_result(
            'runtime_authority_already_exists',
            'runtime mounts can only target agents without existing runtime authority: '
            + ','.join(existing),
            requested_agents=requested_agents,
        )
    guarded_agents = runtime_guard_agents(registry, requested_agents, preserved_agents)
    before_preserved = runtime_snapshots(registry, guarded_agents)
    return _run_start_flow_and_validate(
        app,
        supervisor,
        registry,
        namespace=namespace,
        agent_panes=agent_panes,
        requested_agents=requested_agents,
        preserved_agents=guarded_agents,
        before_preserved=before_preserved,
        before_new=before_new,
        run_start_flow_fn=run_start_flow_fn,
    )


def _prepare_mount_context(graph, namespace, patch_result):
    if str(getattr(patch_result, 'status', '') or '') != 'applied':
        return blocked_mount_result(
            'namespace_patch_not_applied',
            'runtime mounts require an applied namespace patch',
        )
    agent_panes = agent_panes_from_record(
        getattr(patch_result, 'agent_panes', {}) or {}
    )
    preserved_agents = tuple(
        agent_names(getattr(patch_result, 'preserved_before', {}) or {})
    )
    requested_agents = tuple(agent_panes)
    if not requested_agents:
        return noop_mount_result(preserved_agents)

    blocked = blocked_mount_reason(graph, namespace, agent_panes, preserved_agents)
    if blocked is not None:
        return blocked_mount_result(*blocked, requested_agents=requested_agents)
    supervisor = getattr(graph, 'runtime_supervisor', None)
    if supervisor is None:
        return blocked_mount_result(
            'runtime_supervisor_missing',
            'runtime mounts require a target runtime supervisor',
            requested_agents=requested_agents,
        )
    return agent_panes, requested_agents, preserved_agents, supervisor


def _run_start_flow_and_validate(
    app,
    supervisor,
    registry,
    *,
    namespace,
    agent_panes: dict[str, str],
    requested_agents: tuple[str, ...],
    preserved_agents: tuple[str, ...],
    before_preserved: dict[str, dict[str, object] | None],
    before_new: dict[str, dict[str, object] | None],
    run_start_flow_fn,
) -> AdditiveRuntimeMountResult:
    try:
        restore, auto_permission = start_options(supervisor, fallback_app=app)
        summary = call_start_flow_for_additive_mount(
            supervisor,
            namespace,
            agent_panes=agent_panes,
            requested_agents=requested_agents,
            restore=restore,
            auto_permission=auto_permission,
            run_start_flow_fn=run_start_flow_fn,
        )
    except Exception as exc:
        return _failed(
            'runtime_mount_failed',
            exc,
            registry,
            requested_agents,
            preserved_agents,
            before_preserved,
            before_new,
        )
    return _validate_mount_result(
        registry,
        requested_agents,
        preserved_agents,
        before_preserved,
        before_new,
        summary,
    )


def _validate_mount_result(
    registry,
    requested_agents: tuple[str, ...],
    preserved_agents: tuple[str, ...],
    before_preserved: dict[str, dict[str, object] | None],
    before_new: dict[str, dict[str, object] | None],
    summary,
) -> AdditiveRuntimeMountResult:
    after_preserved = runtime_snapshots(registry, preserved_agents)
    after_new = runtime_snapshots(registry, requested_agents)
    preserved_changed = changed_agents(before_preserved, after_preserved)
    if preserved_changed:
        error = RuntimeError(
            'preserved runtime authority changed: ' + ','.join(preserved_changed)
        )
        return _failed(
            'preserved_runtime_authority_changed',
            error,
            registry,
            requested_agents,
            preserved_agents,
            before_preserved,
            before_new,
            summary=summary,
        )
    missing = tuple(agent for agent in requested_agents if after_new.get(agent) is None)
    if missing:
        error = RuntimeError(
            'runtime authority missing after mount: ' + ','.join(missing)
        )
        return _failed(
            'runtime_authority_missing',
            error,
            registry,
            requested_agents,
            preserved_agents,
            before_preserved,
            before_new,
            summary=summary,
        )
    return mounted_result(
        requested_agents=requested_agents,
        mounted_agents=summary_started(summary, fallback=requested_agents),
        written_agents=changed_agents(before_new, after_new),
        preserved_agents=preserved_agents,
        summary=summary_record(summary),
    )


def _failed(
    reason: str,
    error: Exception,
    registry,
    requested_agents: tuple[str, ...],
    preserved_agents: tuple[str, ...],
    before_preserved: dict[str, dict[str, object] | None],
    before_new: dict[str, dict[str, object] | None],
    *,
    summary=None,
) -> AdditiveRuntimeMountResult:
    after_preserved = runtime_snapshots(registry, preserved_agents)
    after_new = runtime_snapshots(registry, requested_agents)
    preserved_changed = changed_agents(before_preserved, after_preserved)
    return failed_mount_result(
        reason=reason,
        error=error,
        requested_agents=requested_agents,
        mounted_agents=summary_started(summary, fallback=()),
        written_agents=changed_agents(before_new, after_new),
        preserved_unchanged_agents=tuple(
            agent for agent in preserved_agents if agent not in set(preserved_changed)
        ),
        preserved_changed_agents=preserved_changed,
        summary=summary_record(summary),
    )


__all__ = ['run_additive_agent_mounts']
