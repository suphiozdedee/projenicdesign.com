from __future__ import annotations


def published_diagnostics(app, diagnostics: dict[str, object]) -> dict[str, object]:
    updated = dict(diagnostics)
    updated['project_view_cache_invalidated'] = invalidate_project_view_cache(app)
    updated['sidebar_refresh_signal_sent'] = False
    return updated


def invalidate_project_view_cache(app) -> bool:
    service = getattr(app, 'project_view_service', None)
    invalidate = getattr(service, 'invalidate_cache', None)
    if not callable(invalidate):
        return False
    try:
        invalidate()
    except Exception:
        return False
    return True


__all__ = ['invalidate_project_view_cache', 'published_diagnostics']
