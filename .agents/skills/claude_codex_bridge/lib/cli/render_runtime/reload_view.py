from __future__ import annotations

from collections.abc import Mapping


def render_reload(payload: Mapping[str, object]) -> tuple[str, ...]:
    status = str(payload.get('status') or 'unknown')
    lines = [
        f'reload_status: {status}',
        f'dry_run: {str(bool(payload.get("dry_run"))).lower()}',
        f'mutation_enabled: {str(bool(payload.get("mutation_enabled"))).lower()}',
        f'plan_class: {payload.get("plan_class")}',
        f'safe_to_apply: {str(bool(payload.get("safe_to_apply"))).lower()}',
        f'future_safe_to_apply: {str(bool(payload.get("future_safe_to_apply"))).lower()}',
        f'old_config_signature: {payload.get("old_config_signature")}',
        f'new_config_signature: {payload.get("new_config_signature")}',
    ]
    lines.extend(_reload_apply_lines(payload))
    lines.extend(_reload_operation_lines(payload))
    lines.extend(_reload_drain_intent_lines(payload))
    lines.extend(_reload_active_drain_lines(payload))
    patch_plan = payload.get('namespace_patch_plan')
    if isinstance(patch_plan, Mapping):
        lines.extend(_namespace_patch_lines(patch_plan))
    lines.extend(_prefixed_values('reload_reason', payload.get('reasons')))
    lines.extend(_prefixed_values('reload_warning', payload.get('warnings')))
    lines.extend(_prefixed_values('reload_error', payload.get('errors')))
    return tuple(lines)


def _reload_apply_lines(payload: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    if payload.get('stage') is not None:
        lines.append(f'reload_stage: {payload.get("stage")}')
    for key, label in (
        ('old_graph_version', 'reload_old_graph_version'),
        ('target_graph_version', 'reload_target_graph_version'),
        ('published_graph_version', 'reload_published_graph_version'),
    ):
        if payload.get(key) is not None:
            lines.append(f'{label}: {payload.get(key)}')
    diagnostics = payload.get('diagnostics')
    if isinstance(diagnostics, Mapping):
        lines.extend(_reload_diagnostic_lines(diagnostics))
    return lines


def _reload_operation_lines(payload: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    for operation in tuple(payload.get('operations') or ()):
        if isinstance(operation, Mapping):
            lines.append(f'reload_operation: {_operation_line(operation)}')
        else:
            lines.append(f'reload_operation: {operation}')
    return lines


def _reload_drain_intent_lines(payload: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    for intent in tuple(payload.get('drain_intents') or ()):
        if isinstance(intent, Mapping):
            lines.append(f'reload_drain_intent: {_drain_intent_line(intent)}')
        else:
            lines.append(f'reload_drain_intent: {intent}')
    return lines


def _reload_active_drain_lines(payload: Mapping[str, object]) -> list[str]:
    drains = payload.get('reload_drains')
    if not isinstance(drains, Mapping):
        return []
    lines = [f'reload_drain_active_count: {int(drains.get("active_count") or 0)}']
    for record in tuple(drains.get('active_records') or ()):
        if isinstance(record, Mapping):
            lines.append(f'reload_drain_active: {_active_drain_line(record)}')
    retry_command = drains.get('retry_command')
    if retry_command:
        lines.append(f'reload_drain_retry: {retry_command}')
    return lines


def _reload_diagnostic_lines(diagnostics: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    lines.extend(
        _optional_key_values(
            'reload_diagnostic',
            diagnostics,
            (
                'reason',
                'message',
                'error_operation',
                'error_category',
                'error_backend_impl',
                'error_ipc_ref',
                'error_evidence',
            ),
        )
    )
    replace_agents = diagnostics.get('replace_agents')
    if isinstance(replace_agents, (list, tuple)):
        lines.append(f'reload_diagnostic: replace_agents={_render_value(replace_agents)}')
    if diagnostics.get('next_supported_action'):
        lines.append(f'reload_diagnostic: next_supported_action={diagnostics.get("next_supported_action")}')
    for key in (
        'graph_published',
        'lease_or_lifecycle_written',
        'config_watch_started',
        'unload_or_replace_executed',
        'project_view_cache_invalidated',
        'sidebar_refresh_signal_sent',
    ):
        if key in diagnostics:
            lines.append(f'reload_diagnostic: {key}={str(bool(diagnostics.get(key))).lower()}')
    for key, label in (
        ('namespace_residue', 'reload_namespace_residue'),
        ('runtime_residue', 'reload_runtime_residue'),
    ):
        residue = diagnostics.get(key)
        if isinstance(residue, Mapping):
            lines.append(f'{label}: {_reload_residue_line(residue)}')
    return lines


def _reload_residue_line(residue: Mapping[str, object]) -> str:
    fields = []
    for key in (
        'partial',
        'created_windows',
        'created_panes',
        'agent_panes',
        'sidebar_panes',
        'tool_panes',
        'removed_windows',
        'removed_panes',
        'removed_agents',
        'replaced_agents',
        'reflowed_windows',
        'reflow_errors',
        'rollback_actions',
        'requested_agents',
        'mounted_agents',
        'runtime_authority_written_agents',
        'unloaded_agents',
        'replaced_agents',
        'runtime_authority_stopped_agents',
        'helper_terminated_agents',
    ):
        if key in residue:
            fields.append(f'{key}={_render_value(residue.get(key))}')
    return ' '.join(fields)


def _operation_line(operation: Mapping[str, object]) -> str:
    fields = [f'op={operation.get("op")}']
    fields.extend(_present_fields(operation, ('agent', 'window', 'from_window', 'to_window', 'field', 'change')))
    fields.extend(_list_fields(operation, ('agents', 'fields')))
    reason = operation.get('reason')
    if reason:
        fields.append(f'reason={reason}')
    return ' '.join(fields)


def _drain_intent_line(intent: Mapping[str, object]) -> str:
    fields = [f'intent_kind={intent.get("intent_kind")}']
    fields.extend(_present_fields(intent, ('agent', 'initial_phase')))
    if intent.get('dry_run_only') is not None:
        fields.append(f'dry_run_only={str(bool(intent.get("dry_run_only"))).lower()}')
    reason = intent.get('reason')
    if reason:
        fields.append(f'reason={reason}')
    return ' '.join(fields)


def _active_drain_line(record: Mapping[str, object]) -> str:
    fields = []
    fields.extend(_present_fields(record, ('agent', 'intent_kind', 'phase', 'status')))
    if record.get('busy') is not None:
        fields.append(f'busy={str(bool(record.get("busy"))).lower()}')
    fields.extend(_present_fields(record, ('age_s', 'deadline_in_s', 'reason')))
    return ' '.join(fields)


def _namespace_patch_lines(plan: Mapping[str, object]) -> list[str]:
    lines = [
        f'reload_namespace_patch_status: {plan.get("status")}',
        f'reload_namespace_patch_apply_deferred: {str(bool(plan.get("apply_deferred"))).lower()}',
    ]
    lines.extend(
        f'reload_namespace_patch_step: {_namespace_patch_step_line(step)}'
        for step in tuple(plan.get('steps') or ())
        if isinstance(step, Mapping)
    )
    lines.extend(
        f'reload_namespace_patch_blocked: {_namespace_patch_blocked_line(blocked)}'
        for blocked in tuple(plan.get('blocked_operations') or ())
        if isinstance(blocked, Mapping)
    )
    return lines


def _namespace_patch_step_line(step: Mapping[str, object]) -> str:
    fields = [f'action={step.get("action")}']
    fields.extend(_present_fields(step, ('window', 'agent', 'role', 'slot_key', 'managed_by', 'anchor_agent')))
    reason = step.get('reason')
    if reason:
        fields.append(f'reason={reason}')
    return ' '.join(fields)


def _namespace_patch_blocked_line(blocked: Mapping[str, object]) -> str:
    fields = [f'op={blocked.get("op")}']
    fields.extend(_present_fields(blocked, ('agent', 'window')))
    reason = blocked.get('reason')
    if reason:
        fields.append(f'reason={reason}')
    return ' '.join(fields)


def _present_fields(record: Mapping[str, object], keys: tuple[str, ...]) -> list[str]:
    fields = []
    for key in keys:
        value = record.get(key)
        if value not in (None, ''):
            fields.append(f'{key}={value}')
    return fields


def _list_fields(record: Mapping[str, object], keys: tuple[str, ...]) -> list[str]:
    fields = []
    for key in keys:
        value = tuple(record.get(key) or ())
        if value:
            fields.append(f'{key}={",".join(str(item) for item in value)}')
    return fields


def _optional_key_values(prefix: str, record: Mapping[str, object], keys: tuple[str, ...]) -> list[str]:
    return [
        f'{prefix}: {key}={record.get(key)}'
        for key in keys
        if record.get(key) not in (None, '')
    ]


def _prefixed_values(prefix: str, values) -> list[str]:
    return [f'{prefix}: {value}' for value in tuple(values or ())]


def _render_value(value: object) -> str:
    if isinstance(value, Mapping):
        return ','.join(f'{item_key}:{item_value}' for item_key, item_value in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return ','.join(str(item) for item in value)
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


__all__ = ['render_reload']
