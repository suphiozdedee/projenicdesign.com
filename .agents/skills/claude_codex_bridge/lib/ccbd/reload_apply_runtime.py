from __future__ import annotations

from ccbd.reload_apply_results import not_published_diagnostics
from ccbd.reload_runtime_mount_models import AdditiveRuntimeMountResult
from ccbd.reload_runtime_mount_models import deferred_mount_result
from ccbd.reload_runtime_mount import run_additive_agent_mounts
from ccbd.reload_runtime_move import run_moved_agent_runtime_updates
from ccbd.reload_runtime_replace import run_replaced_agent_runtime_updates
from ccbd.reload_runtime_unload import run_removed_agent_unloads


PUBLISH_READY_RUNTIME_STATUSES = frozenset({'mounted', 'noop', 'unloaded', 'moved', 'replaced'})


def run_runtime_mount(
    app,
    target_graph,
    *,
    old_graph=None,
    namespace,
    namespace_patch,
    run_runtime_mount_fn,
    run_start_flow_fn,
):
    if _is_herdr_namespace(namespace):
        return deferred_mount_result(
            tuple(str(agent) for agent in tuple(getattr(namespace_patch, 'agent_panes', {}) or {})),
            tuple(str(agent) for agent in tuple(getattr(namespace_patch, 'preserved_before', {}) or {})),
        )
    if run_runtime_mount_fn is not None:
        try:
            return run_runtime_mount_fn(
                app,
                target_graph,
                namespace=namespace,
                patch_result=namespace_patch,
            )
        except Exception as exc:
            return exception_runtime_mount_result(exc)
    kwargs = {
        'namespace': namespace,
        'patch_result': namespace_patch,
    }
    if run_start_flow_fn is not None:
        kwargs['run_start_flow_fn'] = run_start_flow_fn
    try:
        if getattr(namespace_patch, 'removed_agents', None):
            return run_removed_agent_unloads(
                app,
                old_graph or app.current_service_graph(),
                patch_result=namespace_patch,
            )
        if getattr(namespace_patch, 'replaced_agents', None):
            return run_replaced_agent_runtime_updates(
                app,
                target_graph,
                **kwargs,
            )
        if getattr(namespace_patch, 'moved_agents', None) and getattr(namespace_patch, 'agent_panes', None):
            return _run_moved_and_added_runtime_updates(
                app,
                target_graph,
                namespace=namespace,
                namespace_patch=namespace_patch,
                run_start_flow_fn=run_start_flow_fn,
            )
        if getattr(namespace_patch, 'moved_agents', None):
            return run_moved_agent_runtime_updates(
                app,
                target_graph,
                patch_result=namespace_patch,
            )
        return run_additive_agent_mounts(app, target_graph, **kwargs)
    except Exception as exc:
        return exception_runtime_mount_result(exc)


def _is_herdr_namespace(namespace) -> bool:
    return (
        str(getattr(namespace, 'backend_impl', '') or '').strip() == 'herdr'
        or str(getattr(namespace, 'namespace_backend_family', '') or '').strip() == 'herdr-native'
    )


def exception_runtime_mount_result(exc: Exception) -> AdditiveRuntimeMountResult:
    return AdditiveRuntimeMountResult(
        status='failed',
        diagnostics={
            'reason': 'runtime_mount_failed',
            'error_type': type(exc).__name__,
            'error': str(exc),
            **not_published_diagnostics(),
        },
    )


def _run_moved_and_added_runtime_updates(
    app,
    target_graph,
    *,
    namespace,
    namespace_patch,
    run_start_flow_fn,
) -> AdditiveRuntimeMountResult:
    kwargs = {
        'namespace': namespace,
        'patch_result': namespace_patch,
    }
    if run_start_flow_fn is not None:
        kwargs['run_start_flow_fn'] = run_start_flow_fn
    mount_result = run_additive_agent_mounts(app, target_graph, **kwargs)
    if str(getattr(mount_result, 'status', '') or '') not in PUBLISH_READY_RUNTIME_STATUSES:
        return mount_result
    move_result = run_moved_agent_runtime_updates(app, target_graph, patch_result=namespace_patch)
    if str(getattr(move_result, 'status', '') or '') not in PUBLISH_READY_RUNTIME_STATUSES:
        return move_result
    return _combined_moved_and_mounted_result(move_result, mount_result)


def _combined_moved_and_mounted_result(
    move_result: AdditiveRuntimeMountResult,
    mount_result: AdditiveRuntimeMountResult,
) -> AdditiveRuntimeMountResult:
    return AdditiveRuntimeMountResult(
        status='mounted' if mount_result.mounted_agents else 'moved',
        requested_agents=_unique((*move_result.requested_agents, *mount_result.requested_agents)),
        mounted_agents=mount_result.mounted_agents,
        runtime_authority_written_agents=mount_result.runtime_authority_written_agents,
        moved_agents=move_result.moved_agents,
        runtime_authority_moved_agents=move_result.runtime_authority_moved_agents,
        preserved_runtime_unchanged_agents=_unique(
            (
                *move_result.preserved_runtime_unchanged_agents,
                *mount_result.preserved_runtime_unchanged_agents,
            )
        ),
        partial=False,
        summary={
            'move': move_result.to_record(),
            'mount': mount_result.to_record(),
        },
        diagnostics={
            'reason': None,
            'runtime_authority_scope': 'moved_and_new_agents',
            **not_published_diagnostics(),
        },
    )


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


__all__ = [
    'PUBLISH_READY_RUNTIME_STATUSES',
    'exception_runtime_mount_result',
    'run_runtime_mount',
]
