from __future__ import annotations

import errno

from .records import KeeperState

KEEPER_START_FAILURE_SUPPRESS_AFTER = 20
KEEPER_RESTART_SUPPRESSED_PREFIX = 'keeper_restart_suppressed'

_RESOURCE_EXHAUSTION_ERRNOS = {
    errno.EAGAIN,
    errno.EMFILE,
    errno.ENFILE,
    errno.ENOMEM,
}

_RESOURCE_EXHAUSTION_MARKERS = (
    'resource temporarily unavailable',
    'cannot allocate memory',
    'too many open files',
)


def keeper_start_failure_suppression_reason(state: KeeperState, exc: BaseException) -> str | None:
    reason = exception_summary(exc)
    if resource_exhaustion_error(exc):
        return f'{KEEPER_RESTART_SUPPRESSED_PREFIX}:resource_exhausted:{reason}'
    if int(state.restart_count) >= KEEPER_START_FAILURE_SUPPRESS_AFTER:
        return (
            f'{KEEPER_RESTART_SUPPRESSED_PREFIX}:'
            f'max_start_failures:{state.restart_count}:{reason}'
        )
    return None


def resource_exhaustion_error(exc: BaseException) -> bool:
    for current in _exception_chain(exc):
        err_no = getattr(current, 'errno', None)
        try:
            if err_no is not None and int(err_no) in _RESOURCE_EXHAUSTION_ERRNOS:
                return True
        except Exception:
            pass
        text = exception_summary(current).lower()
        if any(marker in text for marker in _RESOURCE_EXHAUSTION_MARKERS):
            return True
    return False


def exception_summary(exc: BaseException) -> str:
    text = str(exc or '').strip()
    return text or type(exc).__name__


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


__all__ = [
    'KEEPER_RESTART_SUPPRESSED_PREFIX',
    'KEEPER_START_FAILURE_SUPPRESS_AFTER',
    'exception_summary',
    'keeper_start_failure_suppression_reason',
    'resource_exhaustion_error',
]
