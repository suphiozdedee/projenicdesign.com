from __future__ import annotations

from ccbd.reload_apply_results import not_published_diagnostics
from ccbd.services.project_namespace_runtime import (
    NamespacePatchApplyResult,
    build_namespace_topology_plan,
)


def current_namespace(app, provided_namespace):
    if provided_namespace is not None:
        return provided_namespace, {'status': 'provided'}
    namespace_controller = getattr(app, 'project_namespace', None)
    load = getattr(namespace_controller, 'load', None)
    if not callable(load):
        return None, {'status': 'missing_controller'}
    try:
        namespace = load()
    except Exception as exc:
        return None, {
            'status': 'load_failed',
            'error_type': type(exc).__name__,
            'error': str(exc),
        }
    if namespace is None:
        return None, {'status': 'missing'}
    return namespace, {'status': 'loaded'}


def topology_for(app, config):
    return build_namespace_topology_plan(
        config,
        ccbd_socket_path=str(app.paths.ccbd_socket_path),
        project_root=str(app.project_root),
    )


def apply_namespace_patch(
    app,
    *,
    plan: dict[str, object],
    old_topology,
    new_topology,
    apply_namespace_patch_fn,
):
    if str(plan.get('plan_class') or '') in {'view_only_change', 'maintenance_change'}:
        return config_only_namespace_patch_result(plan)
    patch_plan = dict(plan.get('namespace_patch_plan') or {})
    if apply_namespace_patch_fn is not None:
        return _custom_namespace_patch(
            patch_plan,
            old_topology,
            new_topology,
            apply_namespace_patch_fn,
        )
    return _controller_namespace_patch(app, patch_plan, old_topology, new_topology)


def _custom_namespace_patch(
    patch_plan: dict[str, object],
    old_topology,
    new_topology,
    apply_namespace_patch_fn,
):
    try:
        return apply_namespace_patch_fn(
            patch_plan=patch_plan,
            old_topology=old_topology,
            new_topology=new_topology,
        )
    except Exception as exc:
        return exception_namespace_patch_result(exc)


def _controller_namespace_patch(app, patch_plan, old_topology, new_topology):
    try:
        return app.project_namespace.apply_reload_patch(
            patch_plan=patch_plan,
            old_topology=old_topology,
            new_topology=new_topology,
        )
    except Exception as exc:
        return exception_namespace_patch_result(exc)


def config_only_namespace_patch_result(
    plan: dict[str, object],
) -> NamespacePatchApplyResult:
    steps = tuple((plan.get('namespace_patch_plan') or {}).get('steps') or ())
    plan_class = str(plan.get('plan_class') or 'config_only_change')
    return NamespacePatchApplyResult(
        status='applied',
        diagnostics={
            'reason': plan_class,
            'supported_operations': ['view_only_change', 'maintenance_change'],
            'namespace_state_written': False,
            'graph_published': False,
            'runtime_authority_written': False,
            'lease_or_lifecycle_written': False,
            'steps': [dict(step) for step in steps if isinstance(step, dict)],
        },
    )


def replace_agent_namespace_patch_result(
    old_graph,
    plan: dict[str, object],
) -> NamespacePatchApplyResult:
    agents = _replace_agents_from_plan(plan)
    replaced: dict[str, str] = {}
    missing: list[str] = []
    registry = getattr(old_graph, 'registry', None)
    for agent_name in agents:
        runtime = registry.get(agent_name) if registry is not None else None
        pane_id = _runtime_pane_id(runtime)
        if pane_id is None:
            missing.append(agent_name)
            continue
        replaced[agent_name] = pane_id
    if missing:
        return NamespacePatchApplyResult(
            status='blocked',
            diagnostics={
                'reason': 'replace_pane_evidence_missing',
                'message': 'replace_agent requires existing managed pane evidence: ' + ','.join(missing),
                **not_published_diagnostics(runtime_authority_written=False),
            },
        )
    preserved = _preserved_runtime_panes(old_graph, exclude=tuple(replaced))
    return NamespacePatchApplyResult(
        status='applied',
        replaced_agents=replaced,
        preserved_before=preserved,
        preserved_after=preserved,
        diagnostics={
            'reason': 'replace_agent_same_slot',
            'namespace_state_written': False,
            'graph_published': False,
            'runtime_authority_written': False,
            'lease_or_lifecycle_written': False,
            'unload_or_replace_executed': False,
        },
    )


def exception_namespace_patch_result(exc: Exception) -> NamespacePatchApplyResult:
    return NamespacePatchApplyResult(
        status='failed',
        diagnostics={
            'reason': 'namespace_patch_failed',
            'error_type': type(exc).__name__,
            'error': str(exc),
            **not_published_diagnostics(runtime_authority_written=False),
        },
    )


def _replace_agents_from_plan(plan: dict[str, object]) -> tuple[str, ...]:
    agents: list[str] = []
    for item in tuple(plan.get('operations') or ()):
        if not isinstance(item, dict):
            continue
        if str(item.get('op') or '') != 'replace_agent':
            continue
        agent_name = str(item.get('agent') or '').strip()
        if agent_name and agent_name not in agents:
            agents.append(agent_name)
    return tuple(agents)


def _runtime_pane_id(runtime) -> str | None:
    pane_id = str(
        getattr(runtime, 'active_pane_id', None)
        or getattr(runtime, 'pane_id', None)
        or ''
    ).strip()
    if pane_id.startswith('%'):
        return pane_id
    return None


def _preserved_runtime_panes(old_graph, *, exclude: tuple[str, ...]) -> dict[str, str]:
    excluded = set(exclude)
    registry = getattr(old_graph, 'registry', None)
    if registry is None:
        return {}
    preserved: dict[str, str] = {}
    for runtime in tuple(registry.list_all() or ()):
        agent_name = str(getattr(runtime, 'agent_name', '') or '').strip()
        if not agent_name or agent_name in excluded:
            continue
        pane_id = _runtime_pane_id(runtime)
        if pane_id is not None:
            preserved[agent_name] = pane_id
    return preserved


__all__ = [
    'apply_namespace_patch',
    'config_only_namespace_patch_result',
    'current_namespace',
    'exception_namespace_patch_result',
    'replace_agent_namespace_patch_result',
    'topology_for',
]
