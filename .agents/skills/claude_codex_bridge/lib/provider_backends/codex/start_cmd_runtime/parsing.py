from __future__ import annotations

from pathlib import Path
import re
import shlex

MANAGED_RESUME_RE = re.compile(r'\bCCB_CODEX_RESUME_ID=(?P<session>\'[^\']*\'|"[^"]*"|[^;\s]*)')


def extract_resume_session_id(command: object) -> str | None:
    raw = _normalized_command(command)
    if not raw:
        return None
    if MANAGED_RESUME_RE.search(raw) is not None:
        return _managed_resume_session_id(raw)
    return _segmented_resume_session_id(raw)


def _managed_resume_session_id(raw: str) -> str | None:
    match = MANAGED_RESUME_RE.search(raw)
    if match is None:
        return None
    value = str(match.group('session') or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'\'', '"'}:
        value = value[1:-1]
    return _normalized_session_id(value)


def looks_like_bare_resume_cmd(command: str) -> bool:
    raw = _normalized_command(command)
    if not _is_bare_resume_candidate(raw):
        return False
    tokens = _tokenize_command(raw)
    if tokens is None:
        return False
    return _tokens_form_bare_resume(tokens)


def _normalized_command(command: object) -> str:
    return str(command or '').strip()


def _segmented_resume_session_id(raw: str) -> str | None:
    candidate: str | None = None
    for segment in raw.split(';'):
        tokens = _tokenize_command(segment)
        session_id = _token_resume_session_id(tokens)
        if session_id is not None:
            candidate = session_id
    return candidate


def _token_resume_session_id(tokens: list[str] | None) -> str | None:
    if not tokens:
        return None
    codex_index = find_codex_token_index(tokens)
    if codex_index is None:
        return None
    for index, token in enumerate(tokens[codex_index + 1 : -1], start=codex_index + 1):
        if token == 'resume':
            return _normalized_session_id(tokens[index + 1])
    return None


def _tokenize_command(raw: str) -> list[str] | None:
    try:
        return shlex.split(raw)
    except Exception:
        return None


def _is_bare_resume_candidate(raw: str) -> bool:
    if not raw:
        return False
    return ';' not in raw and 'CODEX_' not in raw and ' export ' not in f' {raw} '


def _tokens_form_bare_resume(tokens: list[str]) -> bool:
    if len(tokens) < 3:
        return False
    codex_index = find_codex_token_index(tokens)
    if codex_index is None:
        return False
    return codex_index + 2 == len(tokens) - 1 and tokens[codex_index + 1] == 'resume'


def _normalized_session_id(session_id: object) -> str | None:
    normalized = str(session_id or '').strip()
    return normalized or None


def find_codex_token_index(tokens: list[str]) -> int | None:
    for index, token in enumerate(tokens):
        try:
            if Path(token).name == 'codex':
                return index
        except Exception:
            if token == 'codex':
                return index
    return None


__all__ = ['extract_resume_session_id', 'find_codex_token_index', 'looks_like_bare_resume_cmd']
