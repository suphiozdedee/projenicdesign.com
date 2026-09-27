from __future__ import annotations

from dataclasses import dataclass

from ccbd.reload_transaction_records import graph_signature, record


@dataclass(frozen=True)
class TransactionContext:
    old_graph_version: int | None
    old_config_signature: str | None
    new_config_signature: str | None
    namespace_patch: dict[str, object] | None
    runtime_mount: dict[str, object] | None

    def result_kwargs(self) -> dict[str, object]:
        return {
            'old_graph_version': self.old_graph_version,
            'old_config_signature': self.old_config_signature,
            'new_config_signature': self.new_config_signature,
            'namespace_patch': self.namespace_patch,
            'runtime_mount': self.runtime_mount,
        }


def transaction_context(
    old_graph,
    new_graph,
    namespace_patch_result,
    runtime_mount_result,
) -> TransactionContext:
    return TransactionContext(
        old_graph_version=getattr(old_graph, 'version', None),
        old_config_signature=graph_signature(old_graph),
        new_config_signature=graph_signature(new_graph),
        namespace_patch=record(namespace_patch_result),
        runtime_mount=record(runtime_mount_result),
    )


def pre_publish_blocker(
    namespace_patch_result,
    runtime_mount_result,
) -> tuple[str, str] | None:
    namespace_status = str(getattr(namespace_patch_result, 'status', '') or '')
    if namespace_status != 'applied':
        return (
            'namespace_patch_not_applied',
            'namespace patch must be applied before publish, found '
            + (namespace_status or 'unknown'),
        )
    runtime_status = str(getattr(runtime_mount_result, 'status', '') or '')
    if runtime_status not in {'mounted', 'noop', 'unloaded', 'moved', 'replaced'}:
        return (
            'runtime_mount_not_ready',
            'runtime mounts must succeed before publish, found '
            + (runtime_status or 'unknown'),
        )
    return None


__all__ = ['TransactionContext', 'pre_publish_blocker', 'transaction_context']
