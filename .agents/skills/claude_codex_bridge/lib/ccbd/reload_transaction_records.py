from __future__ import annotations


def graph_signature(graph) -> str | None:
    identity = getattr(graph, 'config_identity', None)
    if not isinstance(identity, dict):
        return None
    return str(identity.get('config_signature') or '').strip() or None


def record(value) -> dict[str, object] | None:
    if value is None:
        return None
    to_record = getattr(value, 'to_record', None)
    if callable(to_record):
        return dict(to_record())
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, '__dict__', {}) or {})


def rollback_record(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        'attempted': bool(value.get('attempted')),
        'complete': bool(value.get('complete')),
        'lease': record(value.get('lease')),
        'lifecycle': record(value.get('lifecycle')),
        'errors': list(value.get('errors') or ()),
    }


__all__ = ['graph_signature', 'record', 'rollback_record']
