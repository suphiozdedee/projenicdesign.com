from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from ccbd.reload_apply_models import AdditiveReloadApplyResult
from ccbd.reload_apply_results import not_published_diagnostics
from ccbd.reload_transaction_records import graph_signature


_ALLOWED_PLAN_CLASSES = frozenset({
    'no_change',
    'view_only_change',
    'maintenance_change',
    'add_agent',
    'add_window',
    'remove_agent',
    'replace_agent',
    'move_agent',
    'add_tool_window',
    'remove_tool_window',
})
_ALLOWED_OPERATIONS = frozenset({
    'view_only_change',
    'maintenance_change',
    'add_agent',
    'add_window',
    'remove_agent',
    'replace_agent',
    'move_agent',
    'add_tool_window',
    'remove_tool_window',
    'layout_change',
})


def plan_blocker(plan: dict[str, object]) -> tuple[object, ...] | None:
    if str(plan.get('status') or '') != 'ok':
        return ('plan_not_ok', 'reload apply requires a valid dry-run plan')
    plan_class = str(plan.get('plan_class') or '')
    if plan_class not in _ALLOWED_PLAN_CLASSES:
        return (
            'unsupported_plan_class',
            'reload apply currently accepts view_only_change, maintenance_change, no_change, '
            'add_agent, add_window, idle remove_agent, idle replace_agent, guarded move_agent, '
            'add_tool_window, and remove_tool_window',
        )
    operation_blocker = _operation_blocker(plan)
    if operation_blocker is not None:
        return operation_blocker
    if not bool(plan.get('future_safe_to_apply')):
        return (
            'plan_not_future_safe',
            'dry-run plan is not future-safe for additive apply',
        )
    if plan_class in {
        'add_agent',
        'add_window',
        'remove_agent',
        'replace_agent',
        'move_agent',
        'add_tool_window',
        'remove_tool_window',
    }:
        return _namespace_patch_blocker(plan)
    return None


def plan_blocked_result(
    old_graph,
    plan: dict[str, object],
    blocker: tuple[object, ...],
    *,
    namespace_diagnostics: dict[str, object],
) -> AdditiveReloadApplyResult:
    reason, message, extra_diagnostics = _blocker_parts(blocker)
    return AdditiveReloadApplyResult(
        status='blocked',
        stage='plan',
        plan_class=str(plan.get('plan_class') or ''),
        old_graph_version=getattr(old_graph, 'version', None),
        old_config_signature=graph_signature(old_graph),
        new_config_signature=str(plan.get('new_config_signature') or '') or None,
        plan=deepcopy(plan),
        diagnostics={
            'reason': reason,
            'message': message,
            **extra_diagnostics,
            'namespace': namespace_diagnostics,
            **not_published_diagnostics(),
        },
    )


def _blocker_parts(blocker: tuple[object, ...]) -> tuple[str, str, dict[str, object]]:
    reason = str(blocker[0] if len(blocker) > 0 else 'blocked')
    message = str(blocker[1] if len(blocker) > 1 else 'reload apply blocked')
    extra = blocker[2] if len(blocker) > 2 else {}
    if isinstance(extra, Mapping):
        return reason, message, dict(extra)
    return reason, message, {}


def _operation_blocker(plan: dict[str, object]) -> tuple[str, str] | None:
    unsupported = unsupported_operations(plan)
    if unsupported:
        return (
            'unsupported_operations',
            'additive reload apply rejects operations: ' + ','.join(unsupported),
        )
    return None


def unsupported_operations(plan: dict[str, object]) -> tuple[str, ...]:
    operations = tuple(dict(item) for item in tuple(plan.get('operations') or ()))
    unsupported: set[str] = set()
    for item in operations:
        name = _operation_name(item)
        if name == 'layout_change' and str(item.get('change') or '') == 'remove_window':
            continue
        if name not in _ALLOWED_OPERATIONS:
            unsupported.add(name)
    return tuple(sorted(unsupported))


def _operation_name(item: dict[str, object]) -> str:
    return str(item.get('op') or '').strip() or 'unknown'


def _namespace_patch_blocker(plan: dict[str, object]) -> tuple[str, str] | None:
    patch_plan = dict(plan.get('namespace_patch_plan') or {})
    if str(patch_plan.get('status') or '') != 'planned':
        return (
            'namespace_patch_plan_not_planned',
            'additive reload apply requires an unblocked namespace patch plan',
        )
    if tuple(patch_plan.get('blocked_operations') or ()):
        return (
            'namespace_patch_plan_blocked',
            'additive reload apply requires zero blocked namespace operations',
        )
    scope = dict(patch_plan.get('scope') or {})
    if not bool(scope.get('verified')):
        return (
            'namespace_scope_unverified',
            'additive reload apply requires verified project namespace scope',
        )
    return None


__all__ = ['plan_blocked_result', 'plan_blocker']
