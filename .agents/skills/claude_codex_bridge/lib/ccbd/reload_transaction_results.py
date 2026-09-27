from __future__ import annotations

from ccbd.reload_transaction_models import ReloadPublishTransactionResult
from ccbd.reload_transaction_records import rollback_record


def blocked_result(
    reason: str,
    message: str,
    *,
    old_graph_version,
    old_config_signature,
    new_config_signature,
    namespace_patch,
    runtime_mount,
) -> ReloadPublishTransactionResult:
    return ReloadPublishTransactionResult(
        status='blocked',
        old_graph_version=old_graph_version,
        old_config_signature=old_config_signature,
        new_config_signature=new_config_signature,
        namespace_patch=namespace_patch,
        runtime_mount=runtime_mount,
        diagnostics={
            'reason': reason,
            'message': message,
            **_not_published_diagnostics(),
        },
    )


def failed_result(
    reason: str,
    error: Exception,
    *,
    old_graph_version,
    old_config_signature,
    new_config_signature,
    namespace_patch,
    runtime_mount,
    lease=None,
    lifecycle=None,
    lease_or_lifecycle_written: bool = False,
    signature_rollback: dict[str, object] | None = None,
) -> ReloadPublishTransactionResult:
    return ReloadPublishTransactionResult(
        status='failed',
        old_graph_version=old_graph_version,
        old_config_signature=old_config_signature,
        new_config_signature=new_config_signature,
        namespace_patch=namespace_patch,
        runtime_mount=runtime_mount,
        lease=lease,
        lifecycle=lifecycle,
        diagnostics={
            'reason': reason,
            'error_type': type(error).__name__,
            'error': str(error),
            'signature_rollback': rollback_record(signature_rollback),
            **_not_published_diagnostics(
                lease_or_lifecycle_written=lease_or_lifecycle_written
            ),
        },
    )


def published_result(
    *,
    new_graph,
    old_graph_version,
    old_config_signature,
    new_config_signature,
    namespace_patch,
    runtime_mount,
    lease,
    lifecycle,
) -> ReloadPublishTransactionResult:
    runtime_diagnostics = dict((runtime_mount or {}).get('diagnostics') or {})
    return ReloadPublishTransactionResult(
        status='published',
        published_graph_version=getattr(new_graph, 'version', None),
        old_graph_version=old_graph_version,
        old_config_signature=old_config_signature,
        new_config_signature=new_config_signature,
        namespace_patch=namespace_patch,
        runtime_mount=runtime_mount,
        lease=lease,
        lifecycle=lifecycle,
        diagnostics={
            'reason': None,
            'graph_published': True,
            'lease_or_lifecycle_written': True,
            'config_watch_started': False,
            'unload_or_replace_executed': bool(runtime_diagnostics.get('unload_or_replace_executed', False)),
        },
    )


def _not_published_diagnostics(
    *,
    lease_or_lifecycle_written: bool = False,
) -> dict[str, object]:
    return {
        'graph_published': False,
        'lease_or_lifecycle_written': bool(lease_or_lifecycle_written),
        'config_watch_started': False,
        'unload_or_replace_executed': False,
    }


__all__ = ['blocked_result', 'failed_result', 'published_result']
