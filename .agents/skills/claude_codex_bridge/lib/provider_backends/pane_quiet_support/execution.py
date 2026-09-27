from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ccbd.api_models import JobRecord
from completion.models import (
    CompletionConfidence,
    CompletionCursor,
    CompletionDecision,
    CompletionSourceKind,
    CompletionStatus,
)
from provider_core.protocol import request_anchor_for_job
from provider_execution.base import ProviderPollResult, ProviderRuntimeContext, ProviderSubmission
from provider_execution.common import error_submission, send_prompt_to_runtime_target

from .protocol import extract_reply_for_req, pane_contains_req_anchor, wrap_pane_quiet_prompt
from .reader import PaneSnapshotReader


PANE_LINES_DEFAULT = 2000
QUIET_SECS = 4.0
MAX_WAIT_SECS = 300.0
MIN_OBSERVED_SECS = 2.0
ANCHOR_WAIT_SECS = 120.0
READY_WAIT_SECS = 60.0


def start_submission(
    job: JobRecord,
    *,
    context: ProviderRuntimeContext | None,
    now: str,
    provider: str,
    load_project_session_fn,
) -> ProviderSubmission:
    work_dir = _resolve_work_dir(job, context)
    if work_dir is None:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.TERMINAL_TEXT,
            reason="runtime_unavailable",
            error="work_dir_missing",
        )

    session = None
    load_error: str | None = None
    instance = (job.agent_name or "").strip().lower() or None
    try:
        if instance is not None:
            session = load_project_session_fn(work_dir, instance=instance)
        if session is None:
            session = load_project_session_fn(work_dir)
    except Exception as exc:
        load_error = f"load_session_failed:{exc!r}"

    if session is None:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.TERMINAL_TEXT,
            reason="runtime_unavailable",
            error=load_error or f"{provider}_session_file_missing",
        )

    pane_id = str(getattr(session, "pane_id", "") or "").strip()
    if not pane_id:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.TERMINAL_TEXT,
            reason="pane_unavailable",
            error="pane_id_missing_in_session",
        )

    try:
        backend = session.backend()
    except Exception as exc:
        backend = None
        backend_error = f"backend_resolve_failed:{exc!r}"
    else:
        backend_error = None

    if backend is None:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.TERMINAL_TEXT,
            reason="backend_unavailable",
            error=backend_error or "terminal_backend_unavailable",
        )

    req_id = request_anchor_for_job(job.job_id)
    prompt = wrap_pane_quiet_prompt(job.request.body or "", req_id)
    reader = PaneSnapshotReader(backend=backend, pane_id=pane_id, lines=PANE_LINES_DEFAULT)

    send_error: str | None = None
    prompt_sent = False
    prompt_deferred_until_ready = False
    initial_content = reader.snapshot() if _requires_ready_before_send(provider) else ""
    if _requires_ready_before_send(provider) and not _pane_ready_for_input(initial_content, provider):
        prompt_deferred_until_ready = True
    else:
        send_error = _send_prompt(backend, pane_id, prompt)
        prompt_sent = send_error is None

    diagnostics: dict[str, object] = {
        "provider": provider,
        "mode": "pane_quiet",
        "pane_id": pane_id,
        "req_id": req_id,
        "task_id": job.request.task_id,
        "workspace_path": str(work_dir),
    }
    if send_error:
        diagnostics["send_error"] = send_error
    if prompt_deferred_until_ready:
        diagnostics["prompt_deferred_until_ready"] = True

    return ProviderSubmission(
        job_id=job.job_id,
        agent_name=job.agent_name,
        provider=provider,
        accepted_at=now,
        ready_at=now,
        source_kind=CompletionSourceKind.TERMINAL_TEXT,
        reply="",
        diagnostics=diagnostics,
        runtime_state={
            "mode": "pane_quiet",
            "provider": provider,
            "reader": reader,
            "backend": backend,
            "pane_id": pane_id,
            "req_id": req_id,
            "pane_lines": PANE_LINES_DEFAULT,
            "started_at": now,
            "last_hash": None,
            "last_change_at": now,
            "last_poll_at": now,
            "prompt_sent": prompt_sent,
            "pending_prompt": prompt,
            "prompt_deferred_until_ready": prompt_deferred_until_ready,
            "send_error": send_error,
            "snapshot_errors": 0,
            "next_seq": 1,
        },
    )


def poll_submission(submission: ProviderSubmission, *, now: str) -> ProviderPollResult | None:
    state = dict(submission.runtime_state)
    provider = _state_str(state, "provider") or submission.provider

    send_error = state.get("send_error")
    if send_error:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.FAILED,
            reason=f"send_failed:{send_error}",
            reply="",
            confidence=CompletionConfidence.DEGRADED,
        )

    pane_id = _state_str(state, "pane_id")
    req_id = _state_str(state, "req_id")
    if not pane_id or not req_id:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.FAILED,
            reason="runtime_state_invalid",
            reply="",
            confidence=CompletionConfidence.DEGRADED,
        )

    reader = _ensure_reader(state)
    if reader is None:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.FAILED,
            reason="runtime_handle_lost",
            reply="",
            confidence=CompletionConfidence.DEGRADED,
        )

    content = reader.snapshot()
    if not content:
        state["snapshot_errors"] = _state_int(state, "snapshot_errors", 0) + 1

    if not bool(state.get("prompt_sent")):
        started_at = _state_str(state, "started_at") or submission.accepted_at or now
        ready_wait_secs = _seconds_between(started_at, now)
        state["ready_wait_secs"] = ready_wait_secs
        if _pane_ready_for_input(content, provider):
            pending_prompt = _state_str(state, "pending_prompt")
            if not pending_prompt:
                return _terminal(
                    submission,
                    state,
                    now,
                    status=CompletionStatus.FAILED,
                    reason="runtime_state_invalid",
                    reply="",
                    confidence=CompletionConfidence.DEGRADED,
                    diagnostics_extra={"missing_pending_prompt": True},
                )
            send_error = _send_prompt(state.get("backend"), pane_id, pending_prompt)
            if send_error:
                state["send_error"] = send_error
                return _terminal(
                    submission,
                    state,
                    now,
                    status=CompletionStatus.FAILED,
                    reason=f"send_failed:{send_error}",
                    reply="",
                    confidence=CompletionConfidence.DEGRADED,
                )
            state["prompt_sent"] = True
            state["prompt_sent_at"] = now
            state["prompt_deferred_until_ready"] = False
            state["started_at"] = now
            state["last_change_at"] = now
            state["last_hash"] = _hash_text(content) if content else None
            state["last_poll_at"] = now
            state["next_seq"] = _state_int(state, "next_seq", 1) + 1
            progress = replace(submission, runtime_state=state)
            return ProviderPollResult(submission=progress, items=(), decision=None)
        if ready_wait_secs >= READY_WAIT_SECS:
            return _terminal(
                submission,
                state,
                now,
                status=CompletionStatus.INCOMPLETE,
                reason=f"{provider}_input_not_ready",
                reply="",
                confidence=CompletionConfidence.DEGRADED,
                diagnostics_extra={
                    "input_not_ready": True,
                    "ready_wait_secs": ready_wait_secs,
                    "diagnosis": f"{provider} pane did not reach an input-ready state before prompt delivery.",
                },
            )
        state["last_poll_at"] = now
        state["next_seq"] = _state_int(state, "next_seq", 1) + 1
        progress = replace(submission, runtime_state=state)
        return ProviderPollResult(submission=progress, items=(), decision=None)

    current_hash = _hash_text(content) if content else _state_str(state, "last_hash")
    last_hash = state.get("last_hash")
    started_at = _state_str(state, "started_at") or submission.accepted_at or now
    last_change_at = _state_str(state, "last_change_at") or started_at

    if content and current_hash != last_hash:
        state["last_hash"] = current_hash
        state["last_change_at"] = now
        last_change_at = now

    state["last_poll_at"] = now
    state["next_seq"] = _state_int(state, "next_seq", 1) + 1

    quiet_secs = _seconds_between(last_change_at, now)
    total_secs = _seconds_between(started_at, now)
    state["quiet_secs"] = quiet_secs
    state["total_secs"] = total_secs

    reply, done_seen = extract_reply_for_req(content, req_id)
    state["done_seen"] = done_seen
    state["reply_chars"] = len(reply)

    anchor_present = bool(content) and pane_contains_req_anchor(content, req_id)
    state["anchor_present"] = anchor_present

    if done_seen and reply:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.COMPLETED,
            reason="pane_done_marker",
            reply=reply,
            confidence=CompletionConfidence.OBSERVED,
        )

    if done_seen and not reply:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.INCOMPLETE,
            reason="pane_done_empty_reply",
            reply="",
            confidence=CompletionConfidence.OBSERVED,
            diagnostics_extra=_empty_reply_diagnostics(provider),
        )

    if total_secs >= MAX_WAIT_SECS:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.FAILED,
            reason="pane_quiet_timeout",
            reply=reply,
            confidence=CompletionConfidence.DEGRADED,
        )

    if reply and total_secs >= MIN_OBSERVED_SECS and quiet_secs >= QUIET_SECS:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.COMPLETED,
            reason="pane_text_quiet",
            reply=reply,
            confidence=CompletionConfidence.DEGRADED,
        )

    if not anchor_present and total_secs >= ANCHOR_WAIT_SECS:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.INCOMPLETE,
            reason=f"{provider}_input_unresponsive",
            reply="",
            confidence=CompletionConfidence.DEGRADED,
        )

    progress = replace(submission, runtime_state=state)
    return ProviderPollResult(submission=progress, items=(), decision=None)


def _ensure_reader(state: dict[str, object]) -> PaneSnapshotReader | None:
    reader = state.get("reader")
    if isinstance(reader, PaneSnapshotReader):
        return reader
    backend = state.get("backend")
    pane_id = _state_str(state, "pane_id")
    lines = _state_int(state, "pane_lines", 200)
    if backend is None or not pane_id:
        return None
    rebuilt = PaneSnapshotReader(backend=backend, pane_id=pane_id, lines=lines)
    state["reader"] = rebuilt
    return rebuilt


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
    cleaned_reply = reply or ""
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
        event_seq=_state_int(state, "next_seq", 1),
        updated_at=now,
    )
    diagnostics = {
        "mode": "pane_quiet",
        "quiet_secs": float(state.get("quiet_secs") or 0.0),
        "total_secs": float(state.get("total_secs") or 0.0),
        "done_seen": bool(state.get("done_seen")),
        "anchor_present": bool(state.get("anchor_present")),
        "snapshot_errors": _state_int(state, "snapshot_errors", 0),
        "reply_chars": _state_int(state, "reply_chars", 0),
    }
    diagnostics.update(diagnostics_extra or {})
    decision = CompletionDecision(
        terminal=True,
        status=status,
        reason=reason,
        confidence=confidence,
        reply=cleaned_reply,
        anchor_seen=bool(state.get("anchor_present")) or bool(state.get("done_seen")) or bool(cleaned_reply),
        reply_started=bool(cleaned_reply),
        reply_stable=bool(cleaned_reply) and status is CompletionStatus.COMPLETED,
        provider_turn_ref=_state_str(state, "req_id") or None,
        source_cursor=cursor,
        finished_at=now,
        diagnostics=diagnostics,
    )
    return ProviderPollResult(submission=progress, items=(), decision=decision)


def _empty_reply_diagnostics(provider: str) -> dict[str, object]:
    diagnosis = (
        f"{provider} pane showed the requested done marker without assistant "
        "reply text; inspect the pane transcript and provider auth/API output."
    )
    return {
        "empty_reply": True,
        "error_type": "empty_provider_reply",
        "message": diagnosis,
        "diagnosis": diagnosis,
    }


def _requires_ready_before_send(provider: str) -> bool:
    return provider == "kimi"


def _pane_ready_for_input(content: str, provider: str) -> bool:
    if provider != "kimi":
        return True
    text = content or ""
    legacy_ready = "── input" in text and "agent (" in text
    prompt_ready = "│ >" in text and "context:" in text
    return legacy_ready or prompt_ready


def _send_prompt(backend: object, pane_id: str, prompt: str) -> str | None:
    try:
        send_prompt_to_runtime_target(backend, pane_id, prompt)
    except Exception as exc:
        return f"send_text_failed:{exc!r}"
    return None


def _resolve_work_dir(job: JobRecord, context: ProviderRuntimeContext | None) -> Path | None:
    candidate = (context.workspace_path if context else None) or job.workspace_path
    if not candidate:
        return None
    try:
        return Path(candidate).expanduser()
    except Exception:
        return None


def _hash_text(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()


def _parse_now(now: str) -> datetime | None:
    if not now:
        return None
    try:
        return datetime.fromisoformat(now.replace("Z", "+00:00"))
    except Exception:
        return None


def _seconds_between(start: str, end: str) -> float:
    start_dt = _parse_now(start)
    end_dt = _parse_now(end)
    if start_dt is None or end_dt is None:
        return 0.0
    return max(0.0, (end_dt - start_dt).total_seconds())


def _state_int(state: dict[str, object], key: str, default: int) -> int:
    value = state.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _state_str(state: dict[str, object], key: str, default: str = "") -> str:
    value = state.get(key)
    if value is None:
        return default
    return str(value)


__all__ = ["poll_submission", "start_submission"]
