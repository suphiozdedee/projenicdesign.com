from __future__ import annotations

import re
from collections.abc import Mapping


_AFFIRMATIVE_DETAIL_READY_PATTERNS = (
    ('expected_stop', r'\bexpected\s+stop\s*(?::|is)\s*`?detail_ready`?\b'),
    ('terminal_expectation', r'\b(?:preserve\s+)?terminal\s+expectation\s*(?:is\s+)?`?detail_ready`?\b'),
    ('terminal_status', r'\b(?:continue\s+)?with\s+terminal\s+status\s*(?:is\s+)?`?detail_ready`?\b'),
    ('normative_stop', r'\b(?:must|shall)\b[^.!?\n]{0,120}\bstops?\s+(?:at|as|on)\s+`?detail_ready`?\b'),
    ('stop_at_detail_ready', r'\b(?:the\s+)?task\s+stops?\s+(?:at|as|on)\s+`?detail_ready`?\b'),
    (
        'controller_visible_outcome',
        r'\bcontroller-visible\s+(?:task\s+)?outcome\s+remains\s+`?detail_ready`?\b',
    ),
    (
        'expected_controller_visible_outcome',
        r'\bexpected\s+controller-visible\s+(?:task\s+)?outcome\s+is\s+`?detail_ready`?\b',
    ),
)
_NEGATION_PATTERN = re.compile(r'\b(?:do\s+not|must\s+not|is\s+not|no\s+longer)\b', re.IGNORECASE)
_WEAK_MODAL_PATTERN = re.compile(r'\b(?:may|might|could|would|should|can)\b', re.IGNORECASE)
_CONDITIONAL_PATTERN = re.compile(r'\b(?:if|when|unless)\b', re.IGNORECASE)
_EXAMPLE_PATTERN = re.compile(
    r'(?:\b(?:example|for\s+example|sample|hypothetical)\b|\be\.g\.)',
    re.IGNORECASE,
)
_CONFLICTING_STATUS_PATTERN = re.compile(r'\b(?:replan_required|blocked|done|cancelled)\b', re.IGNORECASE)
_SCHEMA_PATTERN = re.compile(r'\b(?:schema|token(?:s)?|enum|allowed\s+statuses)\b', re.IGNORECASE)
_JSON_PATTERN = re.compile(r'(^|\n)\s*[\[{]|"[^"\n]+"\s*:', re.IGNORECASE)
_FENCE_PATTERN = re.compile(r'(^|\n)\s*(?:```|~~~)', re.IGNORECASE)
_QUOTE_PATTERN = re.compile(r'(^|\n)\s*(?:(?:[-*+]|\d+[.)])\s+)*>\s*', re.IGNORECASE)
_TASK_REFERENCE_PATTERN = re.compile(
    r'\b(?:for\s+task|task\s+id)\s*[:=]?\s*`?([A-Za-z0-9][A-Za-z0-9_-]*)`?',
    re.IGNORECASE,
)
_TASK_LABEL_PATTERN = re.compile(r'\btask\s+`?([A-Za-z0-9][A-Za-z0-9_-]*)`?\s*:', re.IGNORECASE)
_TASK_SCOPE_LABEL_PATTERN = re.compile(
    r'^\s*(?:#{1,6}\s+)?(?:'
    r'(?:\*{1,3}|_{1,3})(?:task|task[_-]id)\s*:\s*(?:\*{1,3}|_{1,3})'
    r'|(?:\*{1,3}|_{1,3})?(?:task|task[_-]id)(?:\*{1,3}|_{1,3})?\s*:'
    r')\s*(?P<value>[^\r\n]*)$',
    re.IGNORECASE | re.MULTILINE,
)
_OTHER_TASK_PATTERN = re.compile(r'\b(?:other|another)\s+task\b', re.IGNORECASE)
_TASK_ID_VALUE_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


def match_detail_ready_stop_contract(
    corpus: object,
    *,
    task_id: object | None = None,
) -> dict[str, object] | None:
    """Accept only an explicit, unambiguous detail_ready stop contract corpus."""
    entries = _corpus_entries(corpus)
    if not entries:
        return None
    scope = str(task_id or '').strip()
    statements: list[tuple[str, str]] = []
    for kind, text in entries:
        if _unsafe_corpus(text, task_id=scope):
            return None
        statements.extend((kind, statement) for statement in _statements(text))
    matches: list[dict[str, str]] = []
    for kind, statement in statements:
        for name, pattern in _AFFIRMATIVE_DETAIL_READY_PATTERNS:
            if re.search(pattern, statement, flags=re.IGNORECASE):
                matches.append({'kind': kind, 'match': name, 'statement': statement})
                break
    if not matches:
        return None
    return {'status': 'detail_ready', 'evidence': matches}


def _corpus_entries(corpus: object) -> tuple[tuple[str, str], ...]:
    if isinstance(corpus, Mapping):
        return tuple((str(kind), str(text or '')) for kind, text in sorted(corpus.items()))
    text = str(corpus or '')
    return (('text', text),) if text else ()


def _unsafe_corpus(text: str, *, task_id: str) -> bool:
    if (
        _FENCE_PATTERN.search(text)
        or _QUOTE_PATTERN.search(text)
        or _JSON_PATTERN.search(text)
        or _NEGATION_PATTERN.search(text)
        or _WEAK_MODAL_PATTERN.search(text)
        or _CONDITIONAL_PATTERN.search(text)
        or _EXAMPLE_PATTERN.search(text)
        or _CONFLICTING_STATUS_PATTERN.search(text)
        or _SCHEMA_PATTERN.search(text)
        or _OTHER_TASK_PATTERN.search(text)
        or '?' in text
    ):
        return True
    for reference in (*_TASK_REFERENCE_PATTERN.findall(text), *_TASK_LABEL_PATTERN.findall(text)):
        if not task_id or reference != task_id:
            return True
    for match in _TASK_SCOPE_LABEL_PATTERN.finditer(text):
        reference = _canonical_task_scope_value(match.group('value'))
        if reference is None or not task_id or reference != task_id:
            return True
    return False


def _canonical_task_scope_value(value: str) -> str | None:
    text = value.strip()
    for pattern in (
        r'`([A-Za-z0-9][A-Za-z0-9_-]*)`',
        r'<([A-Za-z0-9][A-Za-z0-9_-]*)>',
        r'\*{1,3}([A-Za-z0-9][A-Za-z0-9_-]*)\*{1,3}',
        r'_{1,3}([A-Za-z0-9][A-Za-z0-9_-]*)_{1,3}',
        r'\[([A-Za-z0-9][A-Za-z0-9_-]*)\]\([^\s()]+\)',
    ):
        match = re.fullmatch(pattern, text)
        if match:
            return match.group(1)
    return text if _TASK_ID_VALUE_PATTERN.fullmatch(text) else None


def _statements(text: str) -> tuple[str, ...]:
    protected = re.sub(r'\be\.g\.', 'e_g_', text, flags=re.IGNORECASE)
    return tuple(
        part.replace('e_g_', 'e.g.').strip()
        for part in re.split(r'(?<=[.!?])\s+', protected)
        if part.strip()
    )


__all__ = ['match_detail_ready_stop_contract']
