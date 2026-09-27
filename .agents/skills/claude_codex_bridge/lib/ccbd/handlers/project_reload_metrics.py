from __future__ import annotations


def metrics_fields(payload: dict[str, object], *, fallback_plan_class: str) -> tuple[str, str | None]:
    plan_class = str(payload.get('plan_class') or fallback_plan_class)
    error_text = _error_text(payload)
    if error_text:
        return plan_class, error_text
    if str(payload.get('status') or '') not in {'ok', 'published', 'noop'}:
        return plan_class, _diagnostic_text(payload)
    return plan_class, None


def _error_text(payload: dict[str, object]) -> str | None:
    errors = [str(item) for item in (payload.get('errors') or ()) if str(item)]
    return '; '.join(errors) if errors else None


def _diagnostic_text(payload: dict[str, object]) -> str | None:
    diagnostics = dict(payload.get('diagnostics') or {})
    reason = str(diagnostics.get('reason') or '').strip()
    message = str(diagnostics.get('message') or '').strip()
    return ': '.join(item for item in (reason, message) if item) or None


__all__ = ['metrics_fields']
