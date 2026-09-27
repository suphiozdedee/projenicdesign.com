from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from completion.models import (
    CompletionConfidence,
    CompletionCursor,
    CompletionDecision,
    CompletionItemKind,
    CompletionStatus,
)
from provider_execution.base import ProviderPollResult, ProviderSubmission
from provider_execution.common import build_item, send_prompt_to_runtime_target

from ..comm import agy_pane_ready_for_input, agy_project_trust_dialog_visible
from ..native_log import AgyTranscriptObservation, observe_agy_transcript
from .helpers import hash_text, seconds_between, state_int, state_str


MAX_WAIT_SECS = 900.0
ANCHOR_WAIT_SECS = 300.0
READY_WAIT_SECS = 900.0
PANE_FALLBACK_STABLE_SECS = 10.0


def poll_submission(submission: ProviderSubmission, *, now: str) -> ProviderPollResult | None:
    state = dict(submission.runtime_state)

    send_error = state_str(state, 'send_error')
    if send_error:
        if _send_error_is_fatal(send_error):
            return _terminal(
                submission,
                state,
                now,
                status=CompletionStatus.FAILED,
                reason=f'send_failed:{send_error}',
                reply='',
                confidence=CompletionConfidence.DEGRADED,
            )
        state['delivery_ambiguous_send_error'] = send_error
        if not bool(state.get('prompt_sent')):
            state['prompt_sent'] = True
            state['prompt_deferred_until_ready'] = False

    pane_id = state_str(state, 'pane_id')
    req_id = state_str(state, 'request_anchor') or state_str(state, 'req_id')
    work_dir = state_str(state, 'work_dir')
    if not pane_id or not req_id or not work_dir:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.FAILED,
            reason='runtime_state_invalid',
            reply='',
            confidence=CompletionConfidence.DEGRADED,
        )

    backend = state.get('backend')
    if backend is None:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.FAILED,
            reason='runtime_handle_lost',
            reply='',
            confidence=CompletionConfidence.DEGRADED,
        )

    if not bool(state.get('prompt_sent')):
        return _poll_deferred_prompt(submission, state, now=now, backend=backend, pane_id=pane_id)

    started_at = state_str(state, 'started_at') or submission.accepted_at or now

    state['last_poll_at'] = now
    state['next_seq'] = state_int(state, 'next_seq', 1)
    total_secs = seconds_between(started_at, now)
    state['total_secs'] = total_secs

    observation = observe_agy_transcript(
        Path(work_dir),
        req_id,
        home_candidates=_home_candidates(state),
    )
    pane_observation = _observe_agy_pane_turn(backend, pane_id, req_id)
    if pane_observation is not None:
        pane_observation = _stabilize_pane_observation(state, pane_observation, now)
    if pane_observation is not None and (
        observation is None or (pane_observation.completed and not observation.completed)
    ):
        observation = pane_observation
        state['pane_fallback_observed'] = True

    if observation is None:
        pane_content = _pane_snapshot(backend, pane_id)
        pane_ready = agy_pane_ready_for_input(pane_content)
        if total_secs >= MAX_WAIT_SECS and not pane_ready:
            return _terminal(
                submission,
                state,
                now,
                status=CompletionStatus.INCOMPLETE,
                reason='agy_input_busy_timeout',
                reply='',
                confidence=CompletionConfidence.DEGRADED,
                diagnostics_extra={
                    'input_not_ready': True,
                    'diagnosis': 'AGY pane did not return to an input-ready state while waiting for transcript anchor.',
                },
            )
        if total_secs >= ANCHOR_WAIT_SECS and pane_ready:
            return _terminal(
                submission,
                state,
                now,
                status=CompletionStatus.INCOMPLETE,
                reason='agy_native_anchor_missing',
                reply='',
                confidence=CompletionConfidence.DEGRADED,
                diagnostics_extra={
                    'anchor_seen': False,
                    'diagnosis': 'AGY transcript did not record the submitted CCB_REQ_ID.',
                },
            )
        return ProviderPollResult(submission=replace(submission, runtime_state=state), items=(), decision=None)

    items = []
    transcript_path = str(observation.transcript_path or '')
    if transcript_path and transcript_path != state_str(state, 'session_path'):
        items.append(
            build_item(
                submission,
                kind=CompletionItemKind.SESSION_ROTATE,
                timestamp=now,
                seq=_next_seq(state),
                payload={
                    'session_path': transcript_path,
                    'provider_session_id': observation.conversation_id,
                },
                cursor_kwargs={'session_path': transcript_path},
            )
        )
        state['session_path'] = transcript_path
        state['anchor_emitted'] = False
        state['reply_buffer'] = ''
        state['last_reply_signature'] = ''
        state['turn_boundary_ref'] = ''

    if observation.request_seen and not bool(state.get('anchor_emitted')):
        items.append(
            build_item(
                submission,
                kind=CompletionItemKind.ANCHOR_SEEN,
                timestamp=now,
                seq=_next_seq(state),
                payload={
                    'turn_id': req_id,
                    'session_path': transcript_path or None,
                    'provider_session_id': observation.conversation_id,
                    'native_started_at': observation.native_started_at,
                },
                cursor_kwargs={'session_path': transcript_path or None},
            )
        )
        state['anchor_emitted'] = True

    coalesced_ids = tuple(getattr(observation, 'coalesced_request_ids', ()) or ())
    if coalesced_ids and not bool(getattr(observation, 'request_is_latest', True)):
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.INCOMPLETE,
            reason='agy_request_coalesced',
            reply='',
            confidence=CompletionConfidence.DEGRADED,
            diagnostics_extra={
                'request_coalesced': True,
                'coalesced_request_ids': list(coalesced_ids),
                'diagnosis': (
                    'AGY recorded this CCB_REQ_ID inside a USER_INPUT that also contained a later '
                    'CCB_REQ_ID, so no distinct reply can be safely attributed to this job.'
                ),
            },
        )

    reply = observation.reply or ''
    reply_signature = hash_text(reply)
    if reply and reply_signature != state_str(state, 'last_reply_signature'):
        state['reply_buffer'] = reply
        state['last_reply_signature'] = reply_signature
        items.append(
            build_item(
                submission,
                kind=CompletionItemKind.ASSISTANT_FINAL,
                timestamp=now,
                seq=_next_seq(state),
                payload={
                    'text': reply,
                    'reply': reply,
                    'final_answer': reply,
                    'turn_id': req_id,
                    'session_path': transcript_path or None,
                    'provider_session_id': observation.conversation_id,
                    'provider_turn_ref': observation.provider_turn_ref,
                    'native_completed': observation.completed,
                },
                cursor_kwargs={'session_path': transcript_path or None},
            )
        )

    boundary_ref = str(observation.provider_turn_ref or observation.conversation_id or transcript_path or req_id)
    if observation.completed and boundary_ref != state_str(state, 'turn_boundary_ref'):
        trust_evidence = _trust_evidence(state)
        items.append(
            build_item(
                submission,
                kind=CompletionItemKind.TURN_BOUNDARY,
                timestamp=now,
                seq=_next_seq(state),
                payload={
                    'reason': 'agy_transcript_response_done',
                    'last_agent_message': reply,
                    'turn_id': req_id,
                    'session_path': transcript_path or None,
                    'provider_session_id': observation.conversation_id,
                    'provider_turn_ref': observation.provider_turn_ref,
                    'native_completed_at': observation.native_completed_at,
                    'latest_status': observation.latest_status,
                    'request_coalesced': bool(coalesced_ids),
                    'coalesced_request_ids': list(coalesced_ids),
                    **trust_evidence,
                },
                cursor_kwargs={'session_path': transcript_path or None},
            )
        )
        state['turn_boundary_ref'] = boundary_ref

    if total_secs >= MAX_WAIT_SECS and not observation.completed:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.FAILED,
            reason='agy_native_turn_timeout',
            reply=str(state.get('reply_buffer') or ''),
            confidence=CompletionConfidence.DEGRADED,
        )

    progress = replace(submission, reply=str(state.get('reply_buffer') or ''), runtime_state=state)
    if items or progress != submission:
        return ProviderPollResult(submission=progress, items=tuple(items), decision=None)
    return None


def _poll_deferred_prompt(
    submission: ProviderSubmission,
    state: dict[str, object],
    *,
    now: str,
    backend: object,
    pane_id: str,
) -> ProviderPollResult:
    started_at = state_str(state, 'started_at') or submission.accepted_at or now
    ready_wait_secs = seconds_between(started_at, now)
    state['ready_wait_secs'] = ready_wait_secs
    content = _pane_snapshot(backend, pane_id)
    if agy_project_trust_dialog_visible(content):
        state['agy_project_trust_dialog_observed'] = True
        state.setdefault('agy_project_trust_dialog_observed_at', now)
        if (
            bool(state.get('agy_project_trust_confirmation_allowed'))
            and not bool(state.get('agy_project_trust_confirmation_attempted'))
        ):
            state['agy_project_trust_confirmation_attempted'] = True
            state['agy_project_trust_confirmation_count'] = 1
            confirm_error = _send_key(backend, pane_id, 'Enter')
            if confirm_error:
                state['agy_project_trust_confirmation_error'] = confirm_error
                with_diagnostics = _with_trust_diagnostics(submission, state)
                return _terminal(
                    with_diagnostics,
                    state,
                    now,
                    status=CompletionStatus.FAILED,
                    reason='agy_project_trust_confirmation_failed',
                    reply='',
                    confidence=CompletionConfidence.DEGRADED,
                    diagnostics_extra={
                        'agy_project_trust_confirmation_error': confirm_error,
                    },
                )
            state['agy_project_trust_confirmed'] = True
            state['agy_project_trust_confirmed_at'] = now
            state['last_poll_at'] = now
            state['next_seq'] = state_int(state, 'next_seq', 1)
            progress = _with_trust_diagnostics(submission, state)
            return ProviderPollResult(submission=progress, items=(), decision=None)

    if agy_pane_ready_for_input(content):
        pending_prompt = state_str(state, 'pending_prompt')
        if not pending_prompt:
            return _terminal(
                submission,
                state,
                now,
                status=CompletionStatus.FAILED,
                reason='runtime_state_invalid',
                reply='',
                confidence=CompletionConfidence.DEGRADED,
                diagnostics_extra={'missing_pending_prompt': True},
            )
        send_error = _send_prompt(backend, pane_id, pending_prompt)
        if send_error:
            state['send_error'] = send_error
            if _send_error_is_fatal(send_error):
                return _terminal(
                    submission,
                    state,
                    now,
                    status=CompletionStatus.FAILED,
                    reason=f'send_failed:{send_error}',
                    reply='',
                    confidence=CompletionConfidence.DEGRADED,
                )
            state['delivery_ambiguous_send_error'] = send_error
            state['prompt_sent'] = True
            state['prompt_sent_at'] = now
            state['prompt_deferred_until_ready'] = False
            state['started_at'] = now
            state['last_poll_at'] = now
            state['next_seq'] = state_int(state, 'next_seq', 1)
            return ProviderPollResult(submission=replace(submission, runtime_state=state), items=(), decision=None)
        state['prompt_sent'] = True
        state['prompt_sent_at'] = now
        state['prompt_deferred_until_ready'] = False
        state['started_at'] = now
        state['last_poll_at'] = now
        state['next_seq'] = state_int(state, 'next_seq', 1)
        return ProviderPollResult(submission=replace(submission, runtime_state=state), items=(), decision=None)

    if ready_wait_secs >= READY_WAIT_SECS:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.INCOMPLETE,
            reason='agy_input_not_ready',
            reply='',
            confidence=CompletionConfidence.DEGRADED,
            diagnostics_extra={
                'input_not_ready': True,
                'ready_wait_secs': ready_wait_secs,
                'diagnosis': 'AGY pane did not reach an input-ready state before prompt delivery.',
            },
        )
    state['last_poll_at'] = now
    state['next_seq'] = state_int(state, 'next_seq', 1)
    progress = _with_trust_diagnostics(submission, state)
    return ProviderPollResult(submission=progress, items=(), decision=None)


def _home_candidates(state: dict[str, object]) -> list[Path]:
    candidates: list[Path] = []
    for key in ('agy_home', 'home', 'runtime_home'):
        value = state_str(state, key)
        if value:
            candidates.append(Path(value))
    runtime_dir = state_str(state, 'runtime_dir')
    if runtime_dir:
        candidates.append(Path(runtime_dir) / 'home')
    return candidates


def _pane_snapshot(backend: object, pane_id: str) -> str:
    getter = getattr(backend, 'get_pane_content', None)
    if not callable(getter):
        getter = getattr(backend, 'get_text', None)
    if not callable(getter):
        return ''
    try:
        return str(getter(pane_id, lines=2000) or '')
    except Exception:
        return ''


def _observe_agy_pane_turn(backend: object, pane_id: str, req_id: str) -> AgyTranscriptObservation | None:
    content = _pane_snapshot(backend, pane_id)
    if not content or req_id not in content:
        return None
    completed = agy_pane_ready_for_input(content)
    reply = _extract_agy_pane_reply(content, req_id) if completed else ''
    return AgyTranscriptObservation(
        request_seen=True,
        completed=bool(completed and reply),
        reply=reply,
        conversation_id=pane_id,
        transcript_path=f'pane:{pane_id}',
        provider_turn_ref=f'pane:{pane_id}:{req_id}',
        line_count=len(content.splitlines()),
        native_started_at=None,
        native_completed_at=None,
        latest_status='PANE_FALLBACK' if reply else None,
    )


def _stabilize_pane_observation(
    state: dict[str, object],
    observation: AgyTranscriptObservation,
    now: str,
) -> AgyTranscriptObservation:
    reply = observation.reply or ''
    if not reply:
        state.pop('pane_fallback_candidate_signature', None)
        state.pop('pane_fallback_candidate_since', None)
        return observation

    signature = hash_text(reply)
    if signature != state_str(state, 'pane_fallback_candidate_signature'):
        state['pane_fallback_candidate_signature'] = signature
        state['pane_fallback_candidate_since'] = now
        return replace(observation, completed=False)

    stable_since = state_str(state, 'pane_fallback_candidate_since') or now
    stable_secs = seconds_between(stable_since, now)
    state['pane_fallback_stable_secs'] = stable_secs
    if stable_secs < PANE_FALLBACK_STABLE_SECS:
        return replace(observation, completed=False)
    return observation


def _extract_agy_pane_reply(content: str, req_id: str) -> str:
    normalized = str(content or '').replace('\r\n', '\n').replace('\r', '\n')
    anchor_index = normalized.rfind(req_id)
    if anchor_index < 0:
        return ''
    tail = normalized[anchor_index:].splitlines()
    start = _agy_answer_start(tail)
    if start is None:
        return ''
    lines: list[str] = []
    for raw in tail[start:]:
        stripped = raw.strip()
        if _agy_answer_end(stripped):
            break
        if _agy_drop_answer_line(stripped):
            continue
        lines.append(raw.rstrip())
    return _clean_agy_pane_reply('\n'.join(lines), req_id)


def _agy_answer_start(lines: list[str]) -> int | None:
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith('▸ Thought') or stripped.startswith('Thought for'):
            return index + 1
    return None


def _agy_answer_end(stripped: str) -> bool:
    if not stripped:
        return False
    if stripped == '>' or stripped.startswith('> '):
        return True
    if stripped.startswith('─') and len(stripped) >= 8:
        return True
    if stripped.startswith('? for shortcuts'):
        return True
    return False


def _agy_drop_answer_line(stripped: str) -> bool:
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith('ccb reply guidance:'):
        return True
    if lowered.startswith('- answer directly') or lowered.startswith('- include only relevant'):
        return True
    if lowered.startswith('- avoid raw logs'):
        return True
    if stripped.startswith('CCB_REQ_ID:'):
        return True
    if stripped.startswith('Eligibility Check'):
        return True
    if stripped.startswith('⎿  Eligibility check failed'):
        return True
    return False


def _clean_agy_pane_reply(text: str, req_id: str) -> str:
    del req_id
    lines = [line.rstrip() for line in str(text or '').splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return '\n'.join(lines).strip()


def _send_prompt(backend: object, pane_id: str, prompt: str) -> str | None:
    try:
        send_prompt_to_runtime_target(backend, pane_id, prompt)
    except Exception as exc:
        return f'send_text_failed:{exc!r}'
    return None


def _send_key(backend: object, pane_id: str, key: str) -> str | None:
    sender = getattr(backend, 'send_key', None)
    if not callable(sender):
        return 'terminal_backend_does_not_support_key_submission'
    try:
        sent = sender(pane_id, key)
    except Exception as exc:
        return f'send_key_failed:{exc!r}'
    if sent is False:
        return 'send_key_failed'
    return None


def _with_trust_diagnostics(
    submission: ProviderSubmission,
    state: dict[str, object],
) -> ProviderSubmission:
    diagnostics = dict(submission.diagnostics or {})
    for key in (
        'agy_project_trust_confirmation_allowed',
        'agy_project_trust_authority',
        'agy_project_trust_dialog_observed',
        'agy_project_trust_dialog_observed_at',
        'agy_project_trust_confirmation_attempted',
        'agy_project_trust_confirmation_count',
        'agy_project_trust_confirmed',
        'agy_project_trust_confirmed_at',
        'agy_project_trust_confirmation_error',
    ):
        if key in state:
            diagnostics[key] = state[key]
    state['diagnostics'] = diagnostics
    return replace(submission, diagnostics=diagnostics, runtime_state=state)


def _trust_evidence(state: dict[str, object]) -> dict[str, object]:
    evidence: dict[str, object] = {}
    for key in (
        'agy_project_trust_confirmation_allowed',
        'agy_project_trust_authority',
        'agy_project_trust_dialog_observed',
        'agy_project_trust_dialog_observed_at',
        'agy_project_trust_confirmation_attempted',
        'agy_project_trust_confirmation_count',
        'agy_project_trust_confirmed',
        'agy_project_trust_confirmed_at',
        'agy_project_trust_confirmation_error',
    ):
        if key in state:
            evidence[key] = state[key]
    return evidence


def _send_error_is_fatal(send_error: str) -> bool:
    lowered = str(send_error or '').lower()
    return 'does not support text submission' in lowered


def _terminal(
    submission: ProviderSubmission,
    state: dict[str, object],
    now: str,
    *,
    status: CompletionStatus,
    reason: str,
    reply: str,
    confidence: CompletionConfidence,
    diagnostics_extra: dict[str, object] | None = None,
) -> ProviderPollResult:
    cleaned_reply = reply or ''
    progress = replace(
        submission,
        runtime_state=state,
        status=status,
        reason=reason,
        reply=cleaned_reply,
        confidence=confidence,
    )
    cursor = CompletionCursor(
        source_kind=submission.source_kind,
        event_seq=state_int(state, 'next_seq', 1),
        updated_at=now,
    )
    diagnostics = {
        'mode': 'native_transcript_log',
        'total_secs': float(state.get('total_secs') or state.get('ready_wait_secs') or 0.0),
        'anchor_seen': bool(state.get('anchor_emitted')),
        'reply_chars': len(cleaned_reply),
    }
    if state.get('send_error'):
        diagnostics['send_error'] = str(state.get('send_error'))
    if state.get('delivery_ambiguous_send_error'):
        diagnostics['delivery_ambiguous_send_error'] = str(state.get('delivery_ambiguous_send_error'))
    diagnostics.update(diagnostics_extra or {})
    decision = CompletionDecision(
        terminal=True,
        status=status,
        reason=reason,
        confidence=confidence,
        reply=cleaned_reply,
        anchor_seen=bool(state.get('anchor_emitted')),
        reply_started=bool(cleaned_reply),
        reply_stable=bool(cleaned_reply) and status is CompletionStatus.COMPLETED,
        provider_turn_ref=state_str(state, 'req_id') or None,
        source_cursor=cursor,
        finished_at=now,
        diagnostics=diagnostics,
    )
    return ProviderPollResult(submission=progress, items=(), decision=decision)


def _next_seq(state: dict[str, object]) -> int:
    seq = state_int(state, 'next_seq', 1)
    state['next_seq'] = seq + 1
    return seq


__all__ = ['poll_submission']
