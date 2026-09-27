from __future__ import annotations

from ccbd.reload_transaction_context import TransactionContext
from ccbd.reload_transaction_models import ReloadPublishTransactionResult
from ccbd.reload_transaction_records import record
from ccbd.reload_transaction_results import failed_result, published_result
from ccbd.reload_transaction_signature_rollback import rollback_signatures


def publish_or_rollback(
    app,
    new_graph,
    context: TransactionContext,
    *,
    namespace_epoch: int | None,
    expected_generation: int,
    publish_graph_fn,
    lease,
    lifecycle,
) -> ReloadPublishTransactionResult:
    try:
        publisher = publish_graph_fn or (lambda graph: app.publish_service_graph(graph))
        publisher(new_graph)
    except Exception as exc:
        return _publish_failed(
            app,
            context,
            exc,
            namespace_epoch=namespace_epoch,
            expected_generation=expected_generation,
        )
    return published_result(
        new_graph=new_graph,
        lease=record(lease),
        lifecycle=record(lifecycle),
        **context.result_kwargs(),
    )


def _publish_failed(
    app,
    context: TransactionContext,
    error: Exception,
    *,
    namespace_epoch: int | None,
    expected_generation: int,
) -> ReloadPublishTransactionResult:
    rollback = rollback_signatures(
        app,
        context.old_config_signature,
        namespace_epoch=namespace_epoch,
        expected_generation=expected_generation,
        rollback_lease=True,
        rollback_lifecycle=True,
    )
    if rollback['lease'] is not None:
        app.lease = rollback['lease']
    return failed_result(
        'service_graph_publish_failed',
        error,
        lease=record(app.mount_manager.load_state()),
        lifecycle=record(app.lifecycle_store.load()),
        lease_or_lifecycle_written=not rollback['complete'],
        signature_rollback=rollback,
        **context.result_kwargs(),
    )


__all__ = ['publish_or_rollback']
