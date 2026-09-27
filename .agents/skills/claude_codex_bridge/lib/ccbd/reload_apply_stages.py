from __future__ import annotations

from ccbd.reload_apply_publish import publish_transaction
from ccbd.reload_apply_results import (
    message_of,
    namespace_residue,
    not_published_diagnostics,
    reason_of,
    runtime_residue,
    stage_result,
    status_of,
)
from ccbd.reload_sensitive_diagnostics import redact_sensitive_diagnostics
from ccbd.reload_transaction_records import record


def namespace_patch_failed(old_graph, target_graph, plan, namespace_patch):
    patch_record = record(namespace_patch) or {}
    patch_diagnostics = redact_sensitive_diagnostics(dict(patch_record.get('diagnostics') or {}))
    message = message_of(namespace_patch)
    if not message:
        error = str(patch_diagnostics.get('error') or '').strip()
        if error:
            message = f'{patch_diagnostics.get("error_type", "error")}: {error}'
    return stage_result(
        'blocked' if status_of(namespace_patch) == 'blocked' else 'failed',
        'namespace_patch',
        old_graph,
        target_graph,
        plan,
        namespace_patch=namespace_patch,
        diagnostics={
            'reason': reason_of(namespace_patch, fallback='namespace_patch_failed'),
            'message': message,
            **{
                key: value
                for key, value in patch_diagnostics.items()
                if str(key).startswith('error_')
            },
            'namespace_residue': namespace_residue(namespace_patch),
            **not_published_diagnostics(),
        },
    )


def runtime_mount_failed(
    old_graph,
    target_graph,
    plan,
    namespace_patch,
    runtime_mount,
):
    return stage_result(
        'blocked' if status_of(runtime_mount) == 'blocked' else 'failed',
        'runtime_mount',
        old_graph,
        target_graph,
        plan,
        namespace_patch=namespace_patch,
        runtime_mount=runtime_mount,
        diagnostics={
            'reason': reason_of(runtime_mount, fallback='runtime_mount_failed'),
            'message': message_of(runtime_mount),
            'namespace_residue': namespace_residue(namespace_patch),
            'runtime_residue': runtime_residue(runtime_mount),
            **not_published_diagnostics(),
        },
    )


def publish_stage(
    app,
    old_graph,
    target_graph,
    plan,
    *,
    namespace,
    namespace_patch,
    runtime_mount,
    publish_transaction_fn,
    publish_graph_fn,
    update_lease_config_signature_fn,
    update_lifecycle_config_signature_fn,
):
    transaction = publish_transaction(
        app,
        target_graph,
        namespace=namespace,
        namespace_patch=namespace_patch,
        runtime_mount=runtime_mount,
        publish_transaction_fn=publish_transaction_fn,
        publish_graph_fn=publish_graph_fn,
        update_lease_config_signature_fn=update_lease_config_signature_fn,
        update_lifecycle_config_signature_fn=update_lifecycle_config_signature_fn,
    )
    if status_of(transaction) != 'published':
        return publish_failed(
            old_graph,
            target_graph,
            plan,
            namespace_patch,
            runtime_mount,
            transaction,
        )
    transaction_record = record(transaction)
    return stage_result(
        'published',
        'publish_transaction',
        old_graph,
        target_graph,
        plan,
        namespace_patch=namespace_patch,
        runtime_mount=runtime_mount,
        publish_transaction=transaction,
        diagnostics=dict((transaction_record or {}).get('diagnostics') or {}),
    )


def publish_failed(
    old_graph,
    target_graph,
    plan,
    namespace_patch,
    runtime_mount,
    transaction,
):
    transaction_record = record(transaction)
    diagnostics = dict((transaction_record or {}).get('diagnostics') or {})
    return stage_result(
        'blocked' if status_of(transaction) == 'blocked' else 'failed',
        'publish_transaction',
        old_graph,
        target_graph,
        plan,
        namespace_patch=namespace_patch,
        runtime_mount=runtime_mount,
        publish_transaction=transaction,
        diagnostics={
            'reason': reason_of(transaction, fallback='publish_transaction_failed'),
            'message': message_of(transaction),
            'namespace_residue': namespace_residue(namespace_patch),
            'runtime_residue': runtime_residue(runtime_mount),
            'publish_transaction_diagnostics': diagnostics,
            **not_published_diagnostics(
                lease_or_lifecycle_written=bool(
                    diagnostics.get('lease_or_lifecycle_written', False)
                )
            ),
        },
    )


__all__ = [
    'namespace_patch_failed',
    'publish_failed',
    'publish_stage',
    'runtime_mount_failed',
]
