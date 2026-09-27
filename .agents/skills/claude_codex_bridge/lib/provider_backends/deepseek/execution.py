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
    CompletionItemKind,
    CompletionSourceKind,
    CompletionStatus,
)
from provider_backends.native_cli_support import wrap_native_prompt
from provider_core.protocol import request_anchor_for_job
from provider_execution.base import ProviderPollResult, ProviderRuntimeContext, ProviderSubmission
from provider_execution.common import build_item, error_submission, send_prompt_to_runtime_target

from .native_log import (
    INTERRUPTED_STATUSES,
    PERMISSION_DENIED_STATUSES,
    TERMINAL_FAILURE_STATUSES,
    WAITING_USER_STATUSES,
    observe_deepseek_session,
)
from .session import load_project_session


MAX_WAIT_SECS = 300.0
ANCHOR_WAIT_SECS = 120.0


class DeepSeekProviderAdapter:
    provider = "deepseek"

    def restore_diagnostics(self) -> dict[str, object]:
        return {
            "resume_supported": False,
            "restore_mode": "resubmit_required",
            "restore_reason": "provider_resume_unsupported",
            "restore_detail": "deepseek/deepcode native session polling cannot resume an interrupted in-flight job; resubmit after restart",
        }

    def start(
        self,
        job: JobRecord,
        *,
        context: ProviderRuntimeContext | None,
        now: str,
    ) -> ProviderSubmission:
        return _start_submission(job, context=context, now=now, provider=self.provider)

    def poll(self, submission: ProviderSubmission, *, now: str) -> ProviderPollResult | None:
        return _poll_submission(submission, now=now)

    def resume(
        self,
        job: JobRecord,
        submission: ProviderSubmission,
        *,
        context: ProviderRuntimeContext | None,
        persisted_state,
        now: str,
    ) -> ProviderSubmission | None:
        del job, submission, context, persisted_state, now
        return None


def _start_submission(
    job: JobRecord,
    *,
    context: ProviderRuntimeContext | None,
    now: str,
    provider: str,
) -> ProviderSubmission:
    work_dir = _resolve_work_dir(job, context)
    if work_dir is None:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.SESSION_SNAPSHOT,
            reason="runtime_unavailable",
            error="work_dir_missing",
        )

    session = None
    load_error: str | None = None
    instance = (job.agent_name or "").strip().lower() or None
    try:
        if instance is not None:
            session = load_project_session(work_dir, instance=instance)
        if session is None:
            session = load_project_session(work_dir)
    except Exception as exc:
        load_error = f"load_session_failed:{exc!r}"

    if session is None:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.SESSION_SNAPSHOT,
            reason="runtime_unavailable",
            error=load_error or "deepseek_session_file_missing",
        )

    pane_id = str(getattr(session, "pane_id", "") or "").strip()
    if not pane_id:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.SESSION_SNAPSHOT,
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
            source_kind=CompletionSourceKind.SESSION_SNAPSHOT,
            reason="backend_unavailable",
            error=backend_error or "terminal_backend_unavailable",
        )

    req_id = request_anchor_for_job(job.job_id)
    prompt = wrap_native_prompt(job.request.body or "", req_id)

    send_error: str | None = None
    try:
        send_prompt_to_runtime_target(backend, pane_id, prompt)
    except Exception as exc:
        send_error = f"send_text_failed:{exc!r}"

    diagnostics: dict[str, object] = {
        "provider": provider,
        "mode": "native_session_snapshot",
        "pane_id": pane_id,
        "req_id": req_id,
        "task_id": job.request.task_id,
        "workspace_path": str(work_dir),
    }
    if send_error:
        diagnostics["send_error"] = send_error

    return ProviderSubmission(
        job_id=job.job_id,
        agent_name=job.agent_name,
        provider=provider,
        accepted_at=now,
        ready_at=now,
        source_kind=CompletionSourceKind.SESSION_SNAPSHOT,
        reply="",
        diagnostics=diagnostics,
        runtime_state={
            "mode": "native_session_snapshot",
            "provider": provider,
            "backend": backend,
            "pane_id": pane_id,
            "request_anchor": req_id,
            "req_id": req_id,
            "work_dir": str(work_dir),
            "deepseek_home": str(getattr(session, "deepseek_home", "") or ""),
            "started_at": now,
            "last_poll_at": now,
            "prompt_sent": send_error is None,
            "send_error": send_error,
            "next_seq": 1,
            "anchor_emitted": False,
            "reply_buffer": "",
            "last_reply_signature": "",
            "turn_boundary_ref": "",
            "session_path": "",
        },
    )


def _poll_submission(submission: ProviderSubmission, *, now: str) -> ProviderPollResult | None:
    state = dict(submission.runtime_state)
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
    req_id = _state_str(state, "request_anchor") or _state_str(state, "req_id") or submission.job_id
    work_dir = _state_str(state, "work_dir")
    if not pane_id or not req_id or not work_dir:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.FAILED,
            reason="runtime_state_invalid",
            reply="",
            confidence=CompletionConfidence.DEGRADED,
        )

    if state.get("backend") is None:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.FAILED,
            reason="runtime_handle_lost",
            reply="",
            confidence=CompletionConfidence.DEGRADED,
        )

    state["last_poll_at"] = now
    state["next_seq"] = _state_int(state, "next_seq", 1)
    started_at = _state_str(state, "started_at") or submission.accepted_at or now
    total_secs = _seconds_between(started_at, now)
    state["total_secs"] = total_secs

    deepseek_home = _state_str(state, "deepseek_home")
    observation = observe_deepseek_session(
        Path(work_dir),
        req_id,
        home_candidates=(Path(deepseek_home),) if deepseek_home else None,
    )
    if observation is None:
        if total_secs >= ANCHOR_WAIT_SECS:
            return _terminal(
                submission,
                state,
                now,
                status=CompletionStatus.INCOMPLETE,
                reason="deepseek_native_anchor_missing",
                reply="",
                confidence=CompletionConfidence.DEGRADED,
                diagnostics_extra={
                    "anchor_seen": False,
                    "diagnosis": "DeepCode native session store did not record the submitted CCB_REQ_ID.",
                },
            )
        return None

    items = []
    session_path = str(observation.session_path or "")
    if session_path and session_path != _state_str(state, "session_path"):
        items.append(
            build_item(
                submission,
                kind=CompletionItemKind.SESSION_ROTATE,
                timestamp=now,
                seq=_next_seq(state),
                payload={
                    "session_path": session_path,
                    "provider_session_id": observation.session_id,
                },
                cursor_kwargs={"session_path": session_path},
            )
        )
        state["session_path"] = session_path
        state["anchor_emitted"] = False
        state["reply_buffer"] = ""
        state["last_reply_signature"] = ""
        state["turn_boundary_ref"] = ""

    if observation.request_seen and not bool(state.get("anchor_emitted")):
        items.append(
            build_item(
                submission,
                kind=CompletionItemKind.ANCHOR_SEEN,
                timestamp=now,
                seq=_next_seq(state),
                payload={
                    "turn_id": req_id,
                    "session_path": session_path or None,
                    "provider_session_id": observation.session_id,
                },
                cursor_kwargs={"session_path": session_path or None},
            )
        )
        state["anchor_emitted"] = True

    status = (observation.status or "").strip().lower()
    reply = observation.reply or ""

    if status in TERMINAL_FAILURE_STATUSES:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.FAILED,
            reason="deepseek_native_failed",
            reply=reply,
            confidence=CompletionConfidence.OBSERVED,
            diagnostics_extra={
                "native_status": status,
                "fail_reason": observation.fail_reason or "",
            },
        )
    if status in INTERRUPTED_STATUSES:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.CANCELLED,
            reason="deepseek_native_interrupted",
            reply=reply,
            confidence=CompletionConfidence.OBSERVED,
            diagnostics_extra={"native_status": status},
        )
    if status in WAITING_USER_STATUSES:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.INCOMPLETE,
            reason="deepseek_native_waiting_for_user",
            reply=reply,
            confidence=CompletionConfidence.OBSERVED,
            diagnostics_extra={
                "native_status": status,
                "diagnosis": "DeepCode session is waiting for user permission or input.",
            },
        )
    if status in PERMISSION_DENIED_STATUSES:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.INCOMPLETE,
            reason="deepseek_native_permission_denied",
            reply=reply,
            confidence=CompletionConfidence.OBSERVED,
            diagnostics_extra={
                "native_status": status,
                "fail_reason": observation.fail_reason or "",
                "diagnosis": "DeepCode recorded a user permission denial for this session.",
            },
        )
    if observation.completed and not reply:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.INCOMPLETE,
            reason="deepseek_native_empty_reply",
            reply="",
            confidence=CompletionConfidence.OBSERVED,
            diagnostics_extra={
                "native_status": status,
                "empty_reply": True,
                "error_type": "empty_provider_reply",
                "diagnosis": "DeepCode marked the session completed but no assistant reply was found after the CCB_REQ_ID.",
            },
        )

    reply_signature = _hash_text(reply)
    if reply and reply_signature != _state_str(state, "last_reply_signature"):
        state["reply_buffer"] = reply
        state["last_reply_signature"] = reply_signature
        items.append(
            build_item(
                submission,
                kind=CompletionItemKind.ASSISTANT_FINAL,
                timestamp=now,
                seq=_next_seq(state),
                payload={
                    "text": reply,
                    "reply": reply,
                    "final_answer": reply,
                    "turn_id": req_id,
                    "session_path": session_path or None,
                    "provider_session_id": observation.session_id,
                    "provider_turn_ref": observation.provider_turn_ref,
                    "native_status": status,
                    "native_completed": observation.completed,
                },
                cursor_kwargs={"session_path": session_path or None},
            )
        )

    boundary_ref = str(observation.provider_turn_ref or observation.session_id or session_path or req_id)
    if observation.completed and boundary_ref != _state_str(state, "turn_boundary_ref"):
        items.append(
            build_item(
                submission,
                kind=CompletionItemKind.TURN_BOUNDARY,
                timestamp=now,
                seq=_next_seq(state),
                payload={
                    "reason": "deepseek_session_completed",
                    "last_agent_message": reply,
                    "turn_id": req_id,
                    "session_path": session_path or None,
                    "provider_session_id": observation.session_id,
                    "provider_turn_ref": observation.provider_turn_ref,
                    "native_status": status,
                    "native_updated_at": observation.updated_at,
                },
                cursor_kwargs={"session_path": session_path or None},
            )
        )
        state["turn_boundary_ref"] = boundary_ref

    if total_secs >= MAX_WAIT_SECS and not observation.completed:
        return _terminal(
            submission,
            state,
            now,
            status=CompletionStatus.FAILED,
            reason="deepseek_native_turn_timeout",
            reply=str(state.get("reply_buffer") or ""),
            confidence=CompletionConfidence.DEGRADED,
            diagnostics_extra={"native_status": status},
        )

    updated = replace(submission, reply=str(state.get("reply_buffer") or ""), runtime_state=state)
    if items or updated != submission:
        return ProviderPollResult(submission=updated, items=tuple(items))
    return None


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
        "mode": "native_session_snapshot",
        "total_secs": float(state.get("total_secs") or 0.0),
        "anchor_seen": bool(state.get("anchor_emitted")),
        "reply_chars": len(cleaned_reply),
    }
    diagnostics.update(diagnostics_extra or {})
    decision = CompletionDecision(
        terminal=True,
        status=status,
        reason=reason,
        confidence=confidence,
        reply=cleaned_reply,
        anchor_seen=bool(state.get("anchor_emitted")),
        reply_started=bool(cleaned_reply),
        reply_stable=bool(cleaned_reply) and status is CompletionStatus.COMPLETED,
        provider_turn_ref=_state_str(state, "request_anchor") or submission.job_id,
        source_cursor=cursor,
        finished_at=now,
        diagnostics=diagnostics,
    )
    return ProviderPollResult(submission=progress, items=(), decision=decision)


def _resolve_work_dir(job: JobRecord, context: ProviderRuntimeContext | None) -> Path | None:
    candidate = (context.workspace_path if context else None) or job.workspace_path
    if not candidate:
        return None
    try:
        return Path(candidate).expanduser()
    except Exception:
        return None


def _hash_text(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


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


def _next_seq(state: dict[str, object]) -> int:
    seq = _state_int(state, "next_seq", 1)
    state["next_seq"] = seq + 1
    return seq


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


def build_execution_adapter() -> DeepSeekProviderAdapter:
    return DeepSeekProviderAdapter()


__all__ = ["DeepSeekProviderAdapter", "build_execution_adapter"]
