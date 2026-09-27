from __future__ import annotations

from dataclasses import dataclass

from ccbd.reload_transaction_context import (
    TransactionContext,
    transaction_context,
)
from ccbd.reload_transaction_models import ReloadPublishTransactionResult
from ccbd.reload_transaction_preflight import initial_failure
from ccbd.reload_transaction_publish import publish_or_rollback
from ccbd.reload_transaction_records import record
from ccbd.reload_transaction_results import failed_result
from ccbd.reload_transaction_signature import (
    expected_generation,
    update_current_lease_config_signature,
    update_mounted_lifecycle_config_signature,
)
from ccbd.reload_transaction_signature_rollback import rollback_signatures


@dataclass(frozen=True)
class _SignatureWriteResult:
    lease: object | None = None
    lifecycle: object | None = None
    failure: ReloadPublishTransactionResult | None = None


def publish_additive_reload_transaction(
    app,
    new_graph,
    *,
    namespace,
    namespace_patch_result,
    runtime_mount_result,
    update_lease_config_signature_fn=None,
    update_lifecycle_config_signature_fn=None,
    publish_graph_fn=None,
) -> ReloadPublishTransactionResult:
    old_graph = app.current_service_graph()
    context = transaction_context(
        old_graph,
        new_graph,
        namespace_patch_result,
        runtime_mount_result,
    )
    failure = initial_failure(
        app,
        context,
        namespace_patch_result,
        runtime_mount_result,
    )
    if failure is not None:
        return failure

    generation = expected_generation(app)
    assert generation is not None
    namespace_epoch = getattr(namespace, 'namespace_epoch', None)
    signatures = _write_signatures(
        app,
        context,
        namespace_epoch=namespace_epoch,
        expected_generation=generation,
        update_lease_config_signature_fn=update_lease_config_signature_fn,
        update_lifecycle_config_signature_fn=update_lifecycle_config_signature_fn,
    )
    if signatures.failure is not None:
        return signatures.failure
    return publish_or_rollback(
        app,
        new_graph,
        context,
        namespace_epoch=namespace_epoch,
        expected_generation=generation,
        publish_graph_fn=publish_graph_fn,
        lease=signatures.lease,
        lifecycle=signatures.lifecycle,
    )


def _write_signatures(
    app,
    context: TransactionContext,
    *,
    namespace_epoch: int | None,
    expected_generation: int,
    update_lease_config_signature_fn,
    update_lifecycle_config_signature_fn,
) -> _SignatureWriteResult:
    lease = None
    lifecycle = None
    try:
        lease = (update_lease_config_signature_fn or update_current_lease_config_signature)(
            app,
            context.new_config_signature,
            expected_generation=expected_generation,
        )
        lifecycle = _update_lifecycle_signature(
            app,
            context,
            namespace_epoch=namespace_epoch,
            expected_generation=expected_generation,
            update_lifecycle_config_signature_fn=update_lifecycle_config_signature_fn,
        )
    except Exception as exc:
        return _signature_write_failed(
            app,
            context,
            exc,
            namespace_epoch=namespace_epoch,
            expected_generation=expected_generation,
            rollback_lease=lease is not None,
            rollback_lifecycle=lifecycle is not None,
        )
    app.lease = lease
    return _SignatureWriteResult(lease=lease, lifecycle=lifecycle)


def _update_lifecycle_signature(
    app,
    context: TransactionContext,
    *,
    namespace_epoch: int | None,
    expected_generation: int,
    update_lifecycle_config_signature_fn,
):
    updater = update_lifecycle_config_signature_fn or update_mounted_lifecycle_config_signature
    return updater(
        app,
        context.new_config_signature,
        namespace_epoch=namespace_epoch,
        expected_generation=expected_generation,
    )


def _signature_write_failed(
    app,
    context: TransactionContext,
    error: Exception,
    *,
    namespace_epoch: int | None,
    expected_generation: int,
    rollback_lease: bool,
    rollback_lifecycle: bool,
) -> _SignatureWriteResult:
    rollback = rollback_signatures(
        app,
        context.old_config_signature,
        namespace_epoch=namespace_epoch,
        expected_generation=expected_generation,
        rollback_lease=rollback_lease,
        rollback_lifecycle=rollback_lifecycle,
    )
    failure = failed_result(
        'signature_handoff_failed',
        error,
        lease=record(app.mount_manager.load_state()),
        lifecycle=record(app.lifecycle_store.load()),
        lease_or_lifecycle_written=not rollback['complete'],
        signature_rollback=rollback,
        **context.result_kwargs(),
    )
    return _SignatureWriteResult(failure=failure)


__all__ = ['publish_additive_reload_transaction']
