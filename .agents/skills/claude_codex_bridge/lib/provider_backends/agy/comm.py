from __future__ import annotations

import re
from dataclasses import dataclass


_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
_PROMPT_LINE_RE = re.compile(r'^\s*>\s*$')
_BUSY_MARKERS = ('▸ Thought', '● ', 'Running…', 'Running...', 'ctrl+o to expand')
_TRUST_ACCESS = 'Accessing workspace:'
_TRUST_QUESTION = 'Do you trust the contents of this project?'
_TRUST_PERMISSION = 'Antigravity CLI requires permission to read, edit, and execute file'
_TRUST_YES = '> Yes, I trust this folder'
_TRUST_NO = 'No, exit'
_TRUST_NAVIGATION = '↑/↓ Navigate · enter Confirm'


@dataclass
class AgyPaneReader:
    """Thin wrapper around a terminal backend that snapshots an agy tmux pane.

    The underlying TmuxBackend.get_pane_content already strips ANSI, but other
    backend types may not, so we strip defensively and tolerate failures by
    returning an empty string instead of raising.
    """

    backend: object
    pane_id: str
    lines: int = 200

    def snapshot(self) -> str:
        getter = getattr(self.backend, 'get_pane_content', None)
        if not callable(getter):
            getter = getattr(self.backend, 'get_text', None)
        if not callable(getter):
            return ''
        try:
            content = getter(self.pane_id, lines=self.lines)
        except Exception:
            return ''
        if not content:
            return ''
        return _ANSI_RE.sub('', content)


def agy_pane_ready_for_input(content: str) -> bool:
    """Return true when the Antigravity TUI is at an empty input prompt."""
    text = _ANSI_RE.sub('', str(content or ''))
    if not text.strip():
        return False
    lines = text.replace('\r\n', '\n').replace('\r', '\n').splitlines()
    tail = lines[-80:]
    lowered_tail = '\n'.join(tail).lower()
    if '? for shortcuts' not in lowered_tail and 'gemini' not in lowered_tail:
        return False

    for index in range(len(tail) - 1, -1, -1):
        if not _PROMPT_LINE_RE.match(tail[index]):
            continue
        if _has_busy_activity(tail[index + 1 :]):
            continue
        after = '\n'.join(tail[index:]).lower()
        if '? for shortcuts' in after or 'gemini' in after:
            return True
    return False


def agy_project_trust_dialog_visible(content: str) -> bool:
    """Return true only for AGY's complete default-Yes project trust dialog."""
    text = _ANSI_RE.sub('', str(content or ''))
    lines = [line.strip() for line in text.replace('\r\n', '\n').replace('\r', '\n').splitlines()]
    tail = [line for line in lines if line][-40:]
    try:
        question_index = len(tail) - 1 - tail[::-1].index(_TRUST_QUESTION)
        yes_index = tail.index(_TRUST_YES, question_index + 1)
        no_index = tail.index(_TRUST_NO, yes_index + 1)
        navigation_index = tail.index(_TRUST_NAVIGATION, no_index + 1)
    except ValueError:
        return False
    if _TRUST_ACCESS not in tail[:question_index]:
        return False
    permission_text = ' '.join(tail[question_index + 1 : yes_index])
    if _TRUST_PERMISSION not in permission_text:
        return False
    if no_index != yes_index + 1 or navigation_index != no_index + 1:
        return False
    return len(tail) - navigation_index - 1 <= 1


def _has_busy_activity(lines: list[str]) -> bool:
    for line in lines:
        stripped = line.strip()
        if any(marker in stripped for marker in _BUSY_MARKERS):
            return True
    return False


__all__ = ['AgyPaneReader', 'agy_pane_ready_for_input', 'agy_project_trust_dialog_visible']
