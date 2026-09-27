from __future__ import annotations

from .project_reload_cache import published_diagnostics


def apply_reload_payload(result, *, app) -> dict[str, object]:
    payload = result.to_record()
    plan = dict(payload.get('plan') or {})
    diagnostics = dict(payload.get('diagnostics') or {})
    if str(payload.get('status') or '') == 'published':
        diagnostics = published_diagnostics(app, diagnostics)
    payload['diagnostics'] = diagnostics
    payload['dry_run'] = False
    published = str(payload.get('status') or '') == 'published'
    payload['mutation_enabled'] = published
    payload['safe_to_apply'] = published
    payload['future_safe_to_apply'] = bool(plan.get('future_safe_to_apply'))
    payload['operations'] = list(plan.get('operations') or ())
    payload['drain_intents'] = list(plan.get('drain_intents') or ())
    payload['namespace_patch_plan'] = plan.get('namespace_patch_plan')
    payload['reasons'] = list(plan.get('reasons') or ())
    payload['warnings'] = list(plan.get('warnings') or ())
    payload['errors'] = apply_errors(payload)
    return payload


def non_dry_run_invalid_config_payload(plan: dict[str, object]) -> dict[str, object]:
    payload = dict(plan)
    payload['dry_run'] = False
    payload['mutation_enabled'] = False
    payload['safe_to_apply'] = False
    payload['diagnostics'] = {
        'reason': 'invalid_config',
        'message': '; '.join(str(item) for item in tuple(plan.get('errors') or ()) if str(item)),
        'graph_published': False,
        'lease_or_lifecycle_written': False,
        'config_watch_started': False,
        'unload_or_replace_executed': False,
    }
    return payload


def apply_errors(payload: dict[str, object]) -> list[str]:
    if str(payload.get('status') or '') in {'published', 'noop'}:
        return []
    diagnostics = dict(payload.get('diagnostics') or {})
    reason = str(diagnostics.get('reason') or payload.get('status') or '').strip()
    message = str(diagnostics.get('message') or '').strip()
    if reason and message:
        return [f'{reason}: {message}']
    if reason:
        return [reason]
    return []


__all__ = [
    'apply_errors',
    'apply_reload_payload',
    'non_dry_run_invalid_config_payload',
]
