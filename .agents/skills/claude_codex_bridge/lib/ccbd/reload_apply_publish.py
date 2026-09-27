from __future__ import annotations

from ccbd.reload_transaction import publish_additive_reload_transaction
from ccbd.reload_transaction_records import graph_signature, record


def publish_transaction(
    app,
    target_graph,
    *,
    namespace,
    namespace_patch,
    runtime_mount,
    publish_transaction_fn,
    publish_graph_fn,
    update_lease_config_signature_fn,
    update_lifecycle_config_signature_fn,
):
    kwargs = {
        'namespace': namespace,
        'namespace_patch_result': namespace_patch,
        'runtime_mount_result': runtime_mount,
        'publish_graph_fn': publish_graph_fn,
        'update_lease_config_signature_fn': update_lease_config_signature_fn,
        'update_lifecycle_config_signature_fn': update_lifecycle_config_signature_fn,
    }
    try:
        if publish_transaction_fn is not None:
            return publish_transaction_fn(app, target_graph, **kwargs)
        return publish_additive_reload_transaction(app, target_graph, **kwargs)
    except Exception as exc:
        return exception_publish_transaction_result(
            app,
            target_graph,
            namespace_patch,
            runtime_mount,
            exc,
        )


def exception_publish_transaction_result(
    app,
    target_graph,
    namespace_patch,
    runtime_mount,
    exc: Exception,
):
    from ccbd.reload_transaction_results import failed_result

    old_graph = app.current_service_graph()
    return failed_result(
        'publish_transaction_failed',
        exc,
        old_graph_version=getattr(old_graph, 'version', None),
        old_config_signature=graph_signature(old_graph),
        new_config_signature=graph_signature(target_graph),
        namespace_patch=record(namespace_patch),
        runtime_mount=record(runtime_mount),
        lease_or_lifecycle_written=False,
    )


__all__ = ['exception_publish_transaction_result', 'publish_transaction']
