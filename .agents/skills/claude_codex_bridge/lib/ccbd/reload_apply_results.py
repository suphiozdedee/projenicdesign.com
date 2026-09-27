from __future__ import annotations

from copy import deepcopy

from ccbd.reload_apply_models import AdditiveReloadApplyResult
from ccbd.reload_transaction_records import graph_signature, record


def stage_result(
    status: str,
    stage: str,
    old_graph,
    target_graph,
    plan: dict[str, object],
    *,
    namespace_patch=None,
    runtime_mount=None,
    publish_transaction=None,
    diagnostics: dict[str, object],
) -> AdditiveReloadApplyResult:
    transaction_record = record(publish_transaction)
    return AdditiveReloadApplyResult(
        status=status,
        stage=stage,
        plan_class=str(plan.get('plan_class') or ''),
        old_graph_version=getattr(old_graph, 'version', None),
        target_graph_version=getattr(target_graph, 'version', None),
        published_graph_version=_published_graph_version(transaction_record),
        old_config_signature=graph_signature(old_graph),
        new_config_signature=graph_signature(target_graph),
        plan=deepcopy(plan),
        namespace_patch=record(namespace_patch),
        runtime_mount=record(runtime_mount),
        publish_transaction=transaction_record,
        diagnostics=diagnostics,
    )


def noop_result(old_graph, plan: dict[str, object]) -> AdditiveReloadApplyResult:
    return AdditiveReloadApplyResult(
        status='noop',
        stage='no_op',
        plan_class=str(plan.get('plan_class') or ''),
        old_graph_version=getattr(old_graph, 'version', None),
        old_config_signature=graph_signature(old_graph),
        new_config_signature=str(plan.get('new_config_signature') or '') or graph_signature(old_graph),
        plan=deepcopy(plan),
        diagnostics={
            'reason': 'no_change',
            'message': 'config identity and presentation fields are unchanged',
            **not_published_diagnostics(),
        },
    )


def namespace_residue(namespace_patch) -> dict[str, object]:
    patch_record = record(namespace_patch) or {}
    return {
        'partial': bool(patch_record.get('partial')),
        'created_windows': list(patch_record.get('created_windows') or ()),
        'created_panes': list(patch_record.get('created_panes') or ()),
        'agent_panes': dict(patch_record.get('agent_panes') or {}),
        'sidebar_panes': dict(patch_record.get('sidebar_panes') or {}),
        'removed_windows': list(patch_record.get('removed_windows') or ()),
        'removed_panes': list(patch_record.get('removed_panes') or ()),
        'removed_agents': dict(patch_record.get('removed_agents') or {}),
        'replaced_agents': dict(patch_record.get('replaced_agents') or {}),
        'moved_agents': dict(patch_record.get('moved_agents') or {}),
        'moved_agent_windows': dict(patch_record.get('moved_agent_windows') or {}),
        'reflowed_windows': list(patch_record.get('reflowed_windows') or ()),
        'reflow_errors': dict(patch_record.get('reflow_errors') or {}),
        'tool_panes': dict(patch_record.get('tool_panes') or {}),
        'rollback_actions': list(patch_record.get('rollback_actions') or ()),
    }


def runtime_residue(runtime_mount) -> dict[str, object]:
    mount_record = record(runtime_mount) or {}
    return {
        'partial': bool(mount_record.get('partial')),
        'requested_agents': list(mount_record.get('requested_agents') or ()),
        'mounted_agents': list(mount_record.get('mounted_agents') or ()),
        'runtime_authority_written_agents': list(
            mount_record.get('runtime_authority_written_agents') or ()
        ),
        'moved_agents': list(mount_record.get('moved_agents') or ()),
        'runtime_authority_moved_agents': list(
            mount_record.get('runtime_authority_moved_agents') or ()
        ),
        'unloaded_agents': list(mount_record.get('unloaded_agents') or ()),
        'replaced_agents': list(mount_record.get('replaced_agents') or ()),
        'runtime_authority_stopped_agents': list(
            mount_record.get('runtime_authority_stopped_agents') or ()
        ),
        'helper_terminated_agents': list(
            mount_record.get('helper_terminated_agents') or ()
        ),
    }


def status_of(value) -> str:
    if isinstance(value, dict):
        return str(value.get('status') or '').strip()
    return str(getattr(value, 'status', '') or '').strip()


def reason_of(value, *, fallback: str) -> str:
    value_record = record(value) or {}
    diagnostics = dict(value_record.get('diagnostics') or {})
    return str(diagnostics.get('reason') or fallback)


def message_of(value) -> str | None:
    value_record = record(value) or {}
    diagnostics = dict(value_record.get('diagnostics') or {})
    message = str(diagnostics.get('message') or '').strip()
    return message or None


def not_published_diagnostics(
    *,
    lease_or_lifecycle_written: bool = False,
    runtime_authority_written: bool | None = None,
) -> dict[str, object]:
    diagnostics = {
        'graph_published': False,
        'lease_or_lifecycle_written': bool(lease_or_lifecycle_written),
        'config_watch_started': False,
        'unload_or_replace_executed': False,
    }
    if runtime_authority_written is not None:
        diagnostics['runtime_authority_written'] = bool(runtime_authority_written)
    return diagnostics


def _published_graph_version(transaction_record: dict[str, object] | None):
    if transaction_record is None:
        return None
    return transaction_record.get('published_graph_version')


__all__ = [
    'message_of',
    'namespace_residue',
    'not_published_diagnostics',
    'noop_result',
    'reason_of',
    'runtime_residue',
    'stage_result',
    'status_of',
]
