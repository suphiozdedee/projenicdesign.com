from __future__ import annotations

from dataclasses import replace
from typing import Any

from completion.models import (
    CompletionConfidence,
    CompletionDecision,
    CompletionSourceKind,
    CompletionStatus,
)
from provider_runtime.session_payload import completion_source_for_kind


_HERDR_AGENT_STATE_SOURCES = frozenset(
    {
        'herdr_agent_state',
        'herdr_agent_state_ref',
    }
)


def annotate_completion_authority(
    submission,
    decision: CompletionDecision | None,
    *,
    authority: str,
) -> CompletionDecision | None:
    if decision is None:
        return None
    diagnostics = dict(decision.diagnostics or {})
    source_kind = _source_kind_value(decision, submission)
    if source_kind:
        diagnostics.setdefault('completion_source_kind', source_kind)
        diagnostics.setdefault('completion_source', completion_source_for_kind(source_kind))
    if authority:
        diagnostics.setdefault('completion_authority', authority)
    if _has_herdr_agent_state_diagnostics(diagnostics):
        diagnostics.setdefault('herdr_agent_state_role', 'diagnostics_only')
    if decision.status is CompletionStatus.COMPLETED and _declares_herdr_agent_state_authority(diagnostics):
        return _incomplete_herdr_agent_state_decision(decision, diagnostics=diagnostics)
    if diagnostics == dict(decision.diagnostics or {}):
        return decision
    return replace(decision, diagnostics=diagnostics)


def _source_kind_value(decision: CompletionDecision, submission) -> str:
    cursor = getattr(decision, 'source_cursor', None)
    cursor_kind = getattr(cursor, 'source_kind', None)
    if cursor_kind is not None:
        return _completion_source_kind_value(cursor_kind)
    return _completion_source_kind_value(getattr(submission, 'source_kind', None))


def _completion_source_kind_value(value: object) -> str:
    if isinstance(value, CompletionSourceKind):
        return value.value
    raw = str(value or '').strip()
    if not raw:
        return ''
    try:
        return CompletionSourceKind(raw).value
    except ValueError:
        return ''


def _has_herdr_agent_state_diagnostics(diagnostics: dict[str, Any]) -> bool:
    return any(str(key) in _HERDR_AGENT_STATE_SOURCES for key in diagnostics)


def _declares_herdr_agent_state_authority(diagnostics: dict[str, Any]) -> bool:
    declared = {
        _normalized_source(diagnostics.get('completion_source')),
        _normalized_source(diagnostics.get('completion_source_kind')),
    }
    return bool(declared & _HERDR_AGENT_STATE_SOURCES)


def _normalized_source(value: object) -> str:
    return str(value or '').strip().lower().replace('-', '_')


def _incomplete_herdr_agent_state_decision(
    decision: CompletionDecision,
    *,
    diagnostics: dict[str, Any],
) -> CompletionDecision:
    merged = {
        **diagnostics,
        'completion_gate': 'provider_completion_authority',
        'original_status': decision.status.value,
        'original_reason': decision.reason or '',
        'suppress_completion_state_merge': True,
    }
    return replace(
        decision,
        status=CompletionStatus.INCOMPLETE,
        reason='herdr_agent_state_not_completion_authority',
        confidence=CompletionConfidence.DEGRADED,
        reply='',
        anchor_seen=False,
        reply_started=False,
        reply_stable=False,
        diagnostics=merged,
    )


__all__ = ['annotate_completion_authority']
