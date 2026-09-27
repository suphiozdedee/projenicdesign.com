from __future__ import annotations

from .records import get_job


def cleanup_stale_execution_states(dispatcher) -> tuple[str, ...]:
    state_store = _execution_state_store(dispatcher)
    if state_store is None:
        return ()
    try:
        states = state_store.list_all()
    except Exception:
        return ()

    removed: list[str] = []
    for state in states:
        job_id = state.job_id
        current = get_job(dispatcher, job_id)
        if current is not None and current.status not in dispatcher._terminal_event_by_status:
            continue
        _finish_execution(dispatcher, job_id)
        removed.append(job_id)
    return tuple(removed)


def finish_stale_execution_update(dispatcher, job_id: str) -> None:
    _finish_execution(dispatcher, job_id)


def _finish_execution(dispatcher, job_id: str) -> None:
    execution = dispatcher._execution_service
    finish = getattr(execution, 'finish', None)
    if callable(finish):
        finish(job_id)
        return
    state_store = _execution_state_store(dispatcher)
    if state_store is not None:
        state_store.remove(job_id)


def _execution_state_store(dispatcher):
    execution = dispatcher._execution_service
    if execution is None:
        return None
    return getattr(execution, '_state_store', None)


__all__ = ['cleanup_stale_execution_states', 'finish_stale_execution_update']
