from __future__ import annotations

from ccbd.reload_transaction_context import TransactionContext, pre_publish_blocker
from ccbd.reload_transaction_models import ReloadPublishTransactionResult
from ccbd.reload_transaction_results import blocked_result, failed_result
from ccbd.reload_transaction_signature import (
    assert_current_lease_signature_handoff,
    assert_mounted_lifecycle_signature_handoff,
    expected_generation,
    signature_error,
)


def initial_failure(
    app,
    context: TransactionContext,
    namespace_patch_result,
    runtime_mount_result,
) -> ReloadPublishTransactionResult | None:
    blocked = pre_publish_blocker(namespace_patch_result, runtime_mount_result)
    if blocked is not None:
        return blocked_result(*blocked, **context.result_kwargs())
    error = signature_error(
        context.old_config_signature,
        context.new_config_signature,
    )
    if error is not None:
        return failed_result(
            'config_signature_missing',
            error,
            **context.result_kwargs(),
        )
    return _handoff_failure(app, context)


def _handoff_failure(
    app,
    context: TransactionContext,
) -> ReloadPublishTransactionResult | None:
    generation = expected_generation(app)
    if generation is None:
        return failed_result(
            'lease_generation_missing',
            RuntimeError('current daemon lease generation is missing'),
            **context.result_kwargs(),
        )
    try:
        assert_current_lease_signature_handoff(app, expected_generation=generation)
        assert_mounted_lifecycle_signature_handoff(app, expected_generation=generation)
    except Exception as exc:
        return failed_result(
            'signature_handoff_failed',
            exc,
            lease_or_lifecycle_written=False,
            **context.result_kwargs(),
        )
    return None


__all__ = ['initial_failure']
