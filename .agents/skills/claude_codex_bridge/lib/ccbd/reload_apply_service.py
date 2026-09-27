from __future__ import annotations

from agents.config_identity import project_config_identity_payload
from ccbd.reload_apply_graph import build_reload_service_graph
from ccbd.reload_apply_namespace import (
    apply_namespace_patch,
    current_namespace,
    replace_agent_namespace_patch_result,
    topology_for,
)
from ccbd.reload_apply_plan import plan_blocked_result, plan_blocker
from ccbd.reload_apply_results import noop_result, status_of
from ccbd.reload_apply_runtime import (
    PUBLISH_READY_RUNTIME_STATUSES,
    run_runtime_mount,
)
from ccbd.reload_apply_stages import (
    namespace_patch_failed,
    publish_stage,
    runtime_mount_failed,
)
from ccbd.reload_handoff import begin_reload_handoff, clear_reload_handoff
from ccbd.reload_plan import build_reload_dry_run_plan
from ccbd.reload_runtime_unload import pre_namespace_unload_blocker
from ccbd.reload_runtime_replace import pre_namespace_replace_blocker


def run_additive_reload_apply(
    app,
    new_config,
    *,
    lock_already_held: bool = False,
    current_namespace=None,
    apply_namespace_patch_fn=None,
    run_runtime_mount_fn=None,
    run_start_flow_fn=None,
    publish_transaction_fn=None,
    publish_graph_fn=None,
    update_lease_config_signature_fn=None,
    update_lifecycle_config_signature_fn=None,
):
    if lock_already_held:
        return _run_locked(
            app,
            new_config,
            current_namespace=current_namespace,
            apply_namespace_patch_fn=apply_namespace_patch_fn,
            run_runtime_mount_fn=run_runtime_mount_fn,
            run_start_flow_fn=run_start_flow_fn,
            publish_transaction_fn=publish_transaction_fn,
            publish_graph_fn=publish_graph_fn,
            update_lease_config_signature_fn=update_lease_config_signature_fn,
            update_lifecycle_config_signature_fn=update_lifecycle_config_signature_fn,
        )
    lock = getattr(app, 'start_maintenance_lock', None)
    if lock is None:
        return _run_locked(
            app,
            new_config,
            current_namespace=current_namespace,
            apply_namespace_patch_fn=apply_namespace_patch_fn,
            run_runtime_mount_fn=run_runtime_mount_fn,
            run_start_flow_fn=run_start_flow_fn,
            publish_transaction_fn=publish_transaction_fn,
            publish_graph_fn=publish_graph_fn,
            update_lease_config_signature_fn=update_lease_config_signature_fn,
            update_lifecycle_config_signature_fn=update_lifecycle_config_signature_fn,
        )
    with lock:
        return _run_locked(
            app,
            new_config,
            current_namespace=current_namespace,
            apply_namespace_patch_fn=apply_namespace_patch_fn,
            run_runtime_mount_fn=run_runtime_mount_fn,
            run_start_flow_fn=run_start_flow_fn,
            publish_transaction_fn=publish_transaction_fn,
            publish_graph_fn=publish_graph_fn,
            update_lease_config_signature_fn=update_lease_config_signature_fn,
            update_lifecycle_config_signature_fn=update_lifecycle_config_signature_fn,
        )


def _run_locked(
    app,
    new_config,
    *,
    current_namespace,
    apply_namespace_patch_fn,
    run_runtime_mount_fn,
    run_start_flow_fn,
    publish_transaction_fn,
    publish_graph_fn,
    update_lease_config_signature_fn,
    update_lifecycle_config_signature_fn,
):
    old_graph = app.current_service_graph()
    namespace, namespace_diagnostics = current_namespace_for_apply(app, current_namespace)
    plan = _dry_run_plan(app, old_graph, new_config, namespace)
    blocker = plan_blocker(plan)
    if blocker is None:
        blocker = pre_namespace_unload_blocker(app, old_graph, plan)
    if blocker is None:
        blocker = pre_namespace_replace_blocker(app, old_graph, plan)
    if blocker is not None:
        return plan_blocked_result(
            old_graph,
            plan,
            blocker,
            namespace_diagnostics=namespace_diagnostics,
        )
    if str(plan.get('plan_class') or '') == 'no_change':
        return noop_result(old_graph, plan)

    handoff = begin_reload_handoff(app, target_config_identity=project_config_identity_payload(new_config))
    try:
        target_graph = build_reload_service_graph(app, new_config)
        namespace_patch = _namespace_patch_stage(
            app,
            old_graph,
            new_config,
            plan,
            apply_namespace_patch_fn,
        )
        if status_of(namespace_patch) != 'applied':
            return namespace_patch_failed(old_graph, target_graph, plan, namespace_patch)

        runtime_mount = run_runtime_mount(
            app,
            target_graph,
            old_graph=old_graph,
            namespace=namespace,
            namespace_patch=namespace_patch,
            run_runtime_mount_fn=run_runtime_mount_fn,
            run_start_flow_fn=run_start_flow_fn,
        )
        if status_of(runtime_mount) not in PUBLISH_READY_RUNTIME_STATUSES:
            return runtime_mount_failed(
                old_graph,
                target_graph,
                plan,
                namespace_patch,
                runtime_mount,
            )

        return publish_stage(
            app,
            old_graph,
            target_graph,
            plan,
            namespace=namespace,
            namespace_patch=namespace_patch,
            runtime_mount=runtime_mount,
            publish_transaction_fn=publish_transaction_fn,
            publish_graph_fn=publish_graph_fn,
            update_lease_config_signature_fn=update_lease_config_signature_fn,
            update_lifecycle_config_signature_fn=update_lifecycle_config_signature_fn,
        )
    finally:
        if handoff is not None:
            clear_reload_handoff(app)


def current_namespace_for_apply(app, provided_namespace):
    return current_namespace(app, provided_namespace)


def _dry_run_plan(app, old_graph, new_config, namespace):
    return build_reload_dry_run_plan(
        old_graph.config,
        new_config,
        current_config_identity=old_graph.config_identity,
        project_id=getattr(app, 'project_id', None),
        current_namespace=namespace,
    )


def _namespace_patch_stage(app, old_graph, new_config, plan, apply_namespace_patch_fn):
    if str(plan.get('plan_class') or '') == 'replace_agent':
        return replace_agent_namespace_patch_result(old_graph, plan)
    return apply_namespace_patch(
        app,
        plan=plan,
        old_topology=topology_for(app, old_graph.config),
        new_topology=topology_for(app, new_config),
        apply_namespace_patch_fn=apply_namespace_patch_fn,
    )


__all__ = ['current_namespace_for_apply', 'run_additive_reload_apply']
