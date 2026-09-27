from __future__ import annotations

_PANE_PLACEHOLDER_BODY = 'while :; do sleep 3600; done'


def pane_placeholder_cmd() -> str:
    return _PANE_PLACEHOLDER_BODY


def pane_placeholder_argv() -> tuple[str, ...]:
    return ('sh', '-lc', _PANE_PLACEHOLDER_BODY)


__all__ = ['pane_placeholder_argv', 'pane_placeholder_cmd']
