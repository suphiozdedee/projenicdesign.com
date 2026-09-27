from __future__ import annotations

from enum import Enum


class FocusErrorCode(str, Enum):
    STALE_VIEW = 'stale_view'
    UNKNOWN_WINDOW = 'unknown_window'
    UNKNOWN_AGENT = 'unknown_agent'
    TARGET_MISSING = 'target_missing'
    TMUX_FOCUS_FAILED = 'tmux_focus_failed'
    INVALID_REQUEST = 'invalid_request'


class ProjectFocusError(RuntimeError):
    def __init__(self, code: FocusErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f'{code.value}: {message}')


def focus_success(*, kind: str, window: str, agent: str | None, namespace_epoch: int) -> dict[str, object]:
    return {
        'focused': True,
        'kind': kind,
        'window': window,
        'agent': agent,
        'namespace_epoch': namespace_epoch,
    }


__all__ = ['FocusErrorCode', 'ProjectFocusError', 'focus_success']
