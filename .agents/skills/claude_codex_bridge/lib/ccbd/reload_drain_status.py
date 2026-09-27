from __future__ import annotations

from time import time

from ccbd.reload_drain import DrainQueueStore


def reload_drain_status_payload(source) -> dict[str, object]:
    store = _reload_drain_store(source)
    if store is None:
        return {
            'available': False,
            'active_count': 0,
            'active_records': [],
        }
    try:
        queue = store.load()
    except Exception as exc:
        return {
            'available': False,
            'active_count': 0,
            'active_records': [],
            'error_type': type(exc).__name__,
            'error': str(exc),
        }
    now_s = _now_s(source)
    active_records = [
        _record_payload(record, now_s=now_s)
        for record in queue.records
        if not record.terminal
    ]
    return {
        'available': True,
        'active_count': len(active_records),
        'active_records': active_records,
        'retry_command': 'ccb reload' if active_records else None,
    }


def reload_drain_revision(source) -> tuple[int, int] | None:
    path = _reload_drain_path(source)
    if path is None:
        return None
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _record_payload(record, *, now_s: float) -> dict[str, object]:
    return {
        'intent_id': record.intent.intent_id,
        'intent_kind': record.intent.intent_kind,
        'agent': record.intent.agent_name,
        'phase': record.phase,
        'status': record.status,
        'busy': record.busy,
        'reason': record.reason,
        'age_s': max(0.0, float(now_s) - float(record.created_at_s)),
        'deadline_in_s': max(0.0, float(record.deadline_at_s) - float(now_s)),
        'max_age_deadline_in_s': max(
            0.0,
            float(record.max_age_deadline_at_s) - float(now_s),
        ),
        'transition_count': int(record.transition_count),
    }


def _reload_drain_store(source):
    store = getattr(source, 'reload_drain_store', None)
    if store is not None:
        return store
    paths = getattr(source, 'paths', None)
    if paths is None:
        return None
    return DrainQueueStore(paths)


def _reload_drain_path(source):
    paths = getattr(source, 'paths', None)
    path = getattr(paths, 'ccbd_reload_drain_path', None)
    return path


def _now_s(source) -> float:
    clock_s = getattr(source, 'reload_drain_clock_s', None)
    if callable(clock_s):
        return float(clock_s())
    return time()


__all__ = ['reload_drain_revision', 'reload_drain_status_payload']
