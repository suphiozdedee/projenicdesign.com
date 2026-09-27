from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass


_UNUSABLE_LINE_PATTERNS = (
    re.compile(r'^(?:error:\s*)?(?:pane is dead|pane dead)\b[.!:;\-\s]*$', re.IGNORECASE),
    re.compile(r'^(?:codex\s+)?shutting down(?:\.\.\.)?[.!:;\-\s]*$', re.IGNORECASE),
)
_IDLE_PROMPT_RE = re.compile(r'^\s*›\s+\S.*$', re.MULTILINE)
_ACTIVE_STATUS_RE = re.compile(r'^\s*•\s+(?:Working|Thinking|Running)\b', re.MULTILINE | re.IGNORECASE)


def looks_unusable(text: str) -> bool:
    for line in str(text or '').splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if any(pattern.match(candidate) for pattern in _UNUSABLE_LINE_PATTERNS):
            return True
    return False


def looks_ready(text: str) -> bool:
    normalized = _latest_codex_region(str(text or ''))
    lowered = normalized.lower()
    if looks_unusable(normalized):
        return False
    if 'openai codex' not in lowered:
        tail = '\n'.join(normalized.splitlines()[-24:])
        return bool(_IDLE_PROMPT_RE.search(tail)) and not bool(_ACTIVE_STATUS_RE.search(tail))
    if 'model:' in lowered and 'loading' in lowered:
        return False
    return '›' in normalized or '>_' in normalized or '/model to change' in lowered


def wait_for_runtime_ready(backend: object, pane_id: str, *, timeout_s: float = 8.0) -> bool:
    get_pane_content = getattr(backend, 'get_pane_content', None)
    if not callable(get_pane_content):
        return True
    deadline = time.time() + resolved_timeout(timeout_s)
    state = ReadinessState()
    while time.time() < deadline:
        try:
            text = str(get_pane_content(pane_id, lines=120) or '')
        except Exception:
            return True
        if text.strip():
            state.saw_content = True
        if looks_unusable(text):
            time.sleep(0.2)
            continue
        if stable_ready_seen(state, text):
            time.sleep(0.2)
            return True
        time.sleep(0.2)
    return not state.saw_content


def resolved_timeout(timeout_s: float) -> float:
    try:
        return max(0.0, float(os.environ.get('CCB_CODEX_READY_TIMEOUT_S', timeout_s)))
    except Exception:
        return max(0.0, timeout_s)


@dataclass
class ReadinessState:
    stable_text: str = ''
    stable_since: float | None = None
    saw_content: bool = False


def stable_ready_seen(state: ReadinessState, text: str) -> bool:
    if not looks_ready(text):
        state.stable_text = ''
        state.stable_since = None
        return False
    fingerprint = _latest_codex_region(text).strip()
    if fingerprint != state.stable_text:
        state.stable_text = fingerprint
        state.stable_since = time.time()
        return False
    if state.stable_since is None:
        state.stable_since = time.time()
        return False
    return time.time() - state.stable_since >= 0.5


def _latest_codex_region(text: str) -> str:
    normalized = str(text or '')
    lowered = normalized.lower()
    index = lowered.rfind('openai codex')
    if index < 0:
        return normalized
    return normalized[index:]


__all__ = ['looks_ready', 'looks_unusable', 'wait_for_runtime_ready']
