from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
from typing import Any

from ccbd.api_models import JobRecord
from completion.models import (
    CompletionConfidence,
    CompletionCursor,
    CompletionDecision,
    CompletionItemKind,
    CompletionSourceKind,
    CompletionStatus,
)
from provider_core.instance_resolution import named_agent_instance
from provider_core.protocol import request_anchor_for_job
from provider_core.runtime_shared import provider_start_parts
from provider_execution.active_runtime.polling_runtime.result import runtime_error_result
from provider_execution.base import ProviderPollResult, ProviderRuntimeContext, ProviderSubmission
from provider_execution.common import build_item, error_submission, no_wrap_requested

from .protocol import wrap_mimo_prompt
from .session import load_project_session


_RUN_PROCS: dict[str, subprocess.Popen] = {}
_MAX_STDERR_CHARS = 4000


class MimoProviderAdapter:
    provider = "mimo"

    def restore_diagnostics(self) -> dict[str, object]:
        return {
            "resume_supported": False,
            "restore_mode": "resubmit_required",
            "restore_reason": "provider_resume_unsupported",
            "restore_detail": (
                "mimo jobs run through native `mimo run --format json`; "
                "completed stdout artifacts can be inspected after restart, "
                "but interrupted in-flight jobs should be resubmitted"
            ),
        }

    def start(self, job: JobRecord, *, context: ProviderRuntimeContext | None, now: str) -> ProviderSubmission:
        return _start_submission(job, context=context, now=now, provider=self.provider)

    def poll(self, submission: ProviderSubmission, *, now: str) -> ProviderPollResult | None:
        return _poll_submission(submission, now=now)

    def cancel(self, submission: ProviderSubmission) -> None:
        _terminate_process(submission.runtime_state, grace=False)

    def export_runtime_state(self, submission: ProviderSubmission) -> dict[str, object]:
        return _serializable_state(submission.runtime_state)

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
            source_kind=CompletionSourceKind.STRUCTURED_RESULT_STREAM,
            reason="runtime_unavailable",
            error="work_dir_missing",
        )

    session = _load_session_for_job(work_dir, job)
    if session is None:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.STRUCTURED_RESULT_STREAM,
            reason="runtime_unavailable",
            error="mimo_session_file_missing",
        )

    runtime_dir = _path_from_session(session.data, "runtime_dir")
    completion_dir = _path_from_session(session.data, "completion_artifact_dir")
    if completion_dir is None:
        completion_dir = (runtime_dir or (work_dir / ".ccb" / "runtime" / "mimo")) / "completion"
    completion_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = completion_dir / f"{job.job_id}.mimo-run.jsonl"
    stderr_path = completion_dir / f"{job.job_id}.mimo-run.stderr.log"
    request_anchor = request_anchor_for_job(job.job_id)
    no_wrap = no_wrap_requested(job)
    prompt = job.request.body if no_wrap else wrap_mimo_prompt(job.request.body or "", request_anchor)
    cmd = [*provider_start_parts(provider), "run", "--pure", "--format", "json", "--dir", str(work_dir), prompt]
    env = _mimo_run_env(session.data)

    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(
                cmd,
                cwd=str(work_dir),
                env=env,
                stdout=stdout,
                stderr=stderr,
                text=True,
                start_new_session=True,
            )
    except Exception as exc:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.STRUCTURED_RESULT_STREAM,
            reason="mimo_run_start_failed",
            error=f"{type(exc).__name__}: {exc}",
        )

    _RUN_PROCS[job.job_id] = proc
    state = {
        "mode": "mimo_run",
        "provider": provider,
        "job_id": job.job_id,
        "request_anchor": request_anchor,
        "work_dir": str(work_dir),
        "started_at": now,
        "last_poll_at": now,
        "next_seq": 1,
        "anchor_emitted": bool(no_wrap),
        "no_wrap": bool(no_wrap),
        "pure_mode": True,
        "reply_buffer": "",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "pid": proc.pid,
        "returncode": None,
        "mimo_home": str(env.get("MIMOCODE_HOME") or ""),
        "mimo_config_path": str(env.get("MIMOCODE_CONFIG") or ""),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest(),
    }
    return ProviderSubmission(
        job_id=job.job_id,
        agent_name=job.agent_name,
        provider=provider,
        accepted_at=now,
        ready_at=now,
        source_kind=CompletionSourceKind.STRUCTURED_RESULT_STREAM,
        reply="",
        diagnostics={
            "provider": provider,
            "mode": "mimo_run",
            "workspace_path": str(work_dir),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "pid": proc.pid,
        },
        runtime_state=state,
    )


def _poll_submission(submission: ProviderSubmission, *, now: str) -> ProviderPollResult | None:
    mode = str(submission.runtime_state.get("mode") or "")
    if mode in {"passive", "error"}:
        return runtime_error_result(
            submission,
            now=now,
            reason=str(submission.runtime_state.get("reason") or "runtime_unavailable"),
            error=str(submission.runtime_state.get("error") or ""),
        )
    if mode != "mimo_run":
        return runtime_error_result(submission, now=now, reason="runtime_state_corrupt")

    state = dict(submission.runtime_state)
    state["last_poll_at"] = now
    state["next_seq"] = _state_int(state, "next_seq", 1)

    proc = _RUN_PROCS.get(submission.job_id)
    if proc is not None:
        state["returncode"] = proc.poll()
        if state["returncode"] is not None:
            _RUN_PROCS.pop(submission.job_id, None)

    observation = _read_mimo_run_output(Path(str(state.get("stdout_path") or "")))
    items = []
    if not bool(state.get("anchor_emitted")):
        items.append(
            build_item(
                submission,
                kind=CompletionItemKind.ANCHOR_SEEN,
                timestamp=now,
                seq=_next_seq(state),
                payload={
                    "turn_id": str(state.get("request_anchor") or submission.job_id),
                    "source": "mimo_run_prompt_submitted",
                },
            )
        )
        state["anchor_emitted"] = True

    reply = observation.text.strip()
    if reply and reply != str(state.get("reply_buffer") or ""):
        state["reply_buffer"] = reply
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
                    "turn_id": str(state.get("request_anchor") or submission.job_id),
                    "provider_turn_ref": observation.turn_ref,
                    "completed_at": observation.completed_at,
                    "finish_reason": observation.finish_reason,
                },
            )
        )

    terminal = _terminal_result_if_ready(
        submission,
        state,
        observation=observation,
        returncode=_coerce_returncode(state.get("returncode")),
        items=items,
        now=now,
    )
    if terminal is not None:
        return terminal

    updated = replace(
        submission,
        reply=str(state.get("reply_buffer") or ""),
        runtime_state=state,
    )
    if items or updated != submission:
        return ProviderPollResult(submission=updated, items=tuple(items), decision=None)
    return None


def _terminal_result_if_ready(
    submission: ProviderSubmission,
    state: dict[str, object],
    *,
    observation: "_MimoRunObservation",
    returncode: int | None,
    items: list,
    now: str,
) -> ProviderPollResult | None:
    if observation.error:
        return _terminal(
            submission,
            state,
            items,
            now,
            status=CompletionStatus.FAILED,
            reason="mimo_run_error",
            reply=str(state.get("reply_buffer") or ""),
            confidence=CompletionConfidence.DEGRADED,
            diagnostics_extra={"error": observation.error},
        )

    reply = str(state.get("reply_buffer") or observation.text or "").strip()
    if observation.finished:
        if observation.finish_reason and observation.finish_reason not in {"stop", "end_turn", "completed"}:
            status = CompletionStatus.INCOMPLETE
            reason = f"mimo_run_finished:{observation.finish_reason}"
            confidence = CompletionConfidence.OBSERVED
        elif not reply:
            status = CompletionStatus.INCOMPLETE
            reason = "mimo_run_empty_reply"
            confidence = CompletionConfidence.DEGRADED
        else:
            status = CompletionStatus.COMPLETED
            reason = "mimo_run_stop"
            confidence = CompletionConfidence.OBSERVED
        if observation.finished and not bool(state.get("turn_boundary_emitted")):
            items.append(
                build_item(
                    submission,
                    kind=CompletionItemKind.TURN_BOUNDARY,
                    timestamp=now,
                    seq=_next_seq(state),
                    payload={
                        "reason": reason,
                        "last_agent_message": reply,
                        "turn_id": str(state.get("request_anchor") or submission.job_id),
                        "provider_turn_ref": observation.turn_ref,
                        "finish_reason": observation.finish_reason,
                        "completed_at": observation.completed_at,
                    },
                )
            )
            state["turn_boundary_emitted"] = True
        return _terminal(
            submission,
            state,
            items,
            now,
            status=status,
            reason=reason,
            reply=reply,
            confidence=confidence,
            diagnostics_extra={
                "finish_reason": observation.finish_reason,
                "stdout_path": str(state.get("stdout_path") or ""),
                "stderr_path": str(state.get("stderr_path") or ""),
                "returncode": returncode,
            },
        )

    if returncode is not None and returncode != 0:
        return _terminal(
            submission,
            state,
            items,
            now,
            status=CompletionStatus.FAILED,
            reason="mimo_run_failed",
            reply=reply,
            confidence=CompletionConfidence.DEGRADED,
            diagnostics_extra={
                "returncode": returncode,
                "stderr_tail": _stderr_tail(Path(str(state.get("stderr_path") or ""))),
            },
        )
    if returncode == 0 and not observation.finished:
        if observation.finish_reason:
            return _terminal(
                submission,
                state,
                items,
                now,
                status=CompletionStatus.INCOMPLETE,
                reason=f"mimo_run_finished:{observation.finish_reason}",
                reply=reply,
                confidence=CompletionConfidence.DEGRADED,
                diagnostics_extra={
                    "finish_reason": observation.finish_reason,
                    "returncode": returncode,
                },
            )
        return _terminal(
            submission,
            state,
            items,
            now,
            status=CompletionStatus.INCOMPLETE,
            reason="mimo_run_finished_without_step_finish",
            reply=reply,
            confidence=CompletionConfidence.DEGRADED,
            diagnostics_extra={"returncode": returncode},
        )
    return None


def _terminal(
    submission: ProviderSubmission,
    state: dict[str, object],
    items: list,
    now: str,
    *,
    status: CompletionStatus,
    reason: str,
    reply: str,
    confidence: CompletionConfidence,
    diagnostics_extra: dict[str, object] | None = None,
) -> ProviderPollResult:
    state["returncode"] = _coerce_returncode(state.get("returncode"))
    updated = replace(
        submission,
        runtime_state=state,
        status=status,
        reason=reason,
        reply=reply,
        confidence=confidence,
    )
    cursor = _last_cursor(items) or CompletionCursor(
        source_kind=submission.source_kind,
        event_seq=_state_int(state, "next_seq", 1),
        updated_at=now,
    )
    diagnostics = {
        "mode": "mimo_run",
        "anchor_seen": bool(state.get("anchor_emitted")),
        "reply_chars": len(reply or ""),
    }
    diagnostics.update(diagnostics_extra or {})
    decision = CompletionDecision(
        terminal=True,
        status=status,
        reason=reason,
        confidence=confidence,
        reply=reply or "",
        anchor_seen=bool(state.get("anchor_emitted")),
        reply_started=bool(reply),
        reply_stable=bool(reply) and status is CompletionStatus.COMPLETED,
        provider_turn_ref=str(state.get("request_anchor") or submission.job_id),
        source_cursor=cursor,
        finished_at=now,
        diagnostics=diagnostics,
    )
    _terminate_process(state, grace=True)
    return ProviderPollResult(submission=updated, items=tuple(items), decision=decision)


class _MimoRunObservation:
    def __init__(
        self,
        *,
        text: str = "",
        finished: bool = False,
        finish_reason: str = "",
        turn_ref: str | None = None,
        completed_at: object | None = None,
        error: str = "",
    ) -> None:
        self.text = text
        self.finished = finished
        self.finish_reason = finish_reason
        self.turn_ref = turn_ref
        self.completed_at = completed_at
        self.error = error


def _read_mimo_run_output(path: Path) -> _MimoRunObservation:
    if not path or not path.is_file():
        return _MimoRunObservation()
    chunks: list[str] = []
    finished = False
    finish_reason = ""
    turn_ref: str | None = None
    completed_at: object | None = None
    error = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return _MimoRunObservation(error=f"read_stdout_failed:{exc}")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = _event_type(event)
        nested_type = _event_type(event.get("part")) if isinstance(event.get("part"), dict) else ""
        effective_type = event_type or nested_type
        if effective_type == "text":
            text = _event_text(event)
            if text:
                chunks.append(text)
            turn_ref = turn_ref or _event_ref(event)
            completed_at = completed_at or event.get("time") or event.get("timestamp")
            continue
        if effective_type in {"step_finish", "turn_finish", "finish", "done"}:
            reason = _event_reason(event) or finish_reason or "stop"
            if _is_intermediate_finish_reason(reason):
                finish_reason = reason
                turn_ref = turn_ref or _event_ref(event)
                completed_at = completed_at or event.get("time") or event.get("timestamp")
                continue
            finished = True
            finish_reason = reason
            turn_ref = turn_ref or _event_ref(event)
            completed_at = completed_at or event.get("time") or event.get("timestamp")
            continue
        if effective_type in {"error", "failed"}:
            error = _event_text(event) or _event_reason(event) or "mimo_run_error"
    return _MimoRunObservation(
        text="".join(chunks),
        finished=finished,
        finish_reason=finish_reason,
        turn_ref=turn_ref,
        completed_at=completed_at,
        error=error,
    )


def _is_intermediate_finish_reason(reason: str) -> bool:
    normalized = reason.strip().lower().replace("-", "_")
    return normalized in {"tool_calls", "tool_call"}


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("type") or event.get("event") or event.get("kind") or "").strip().lower().replace("-", "_")


def _event_text(event: dict[str, Any]) -> str:
    for key in ("text", "content", "message"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    payload = event.get("payload")
    if isinstance(payload, dict):
        text = _event_text(payload)
        if text:
            return text
    part = event.get("part")
    if isinstance(part, dict):
        text = _event_text(part)
        if text:
            return text
    return ""


def _event_reason(event: dict[str, Any]) -> str:
    for key in ("reason", "finish_reason", "stop_reason", "status"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value.strip()
    for key in ("payload", "properties", "part"):
        nested = event.get(key)
        if isinstance(nested, dict):
            reason = _event_reason(nested)
            if reason:
                return reason
    return ""


def _event_ref(event: dict[str, Any]) -> str | None:
    for key in ("id", "message_id", "messageID", "session_id", "sessionID"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    payload = event.get("payload")
    if isinstance(payload, dict):
        ref = _event_ref(payload)
        if ref:
            return ref
    part = event.get("part")
    if isinstance(part, dict):
        ref = _event_ref(part)
        if ref:
            return ref
    return None


def _resolve_work_dir(job: JobRecord, context: ProviderRuntimeContext | None) -> Path | None:
    value = (context.workspace_path if context else None) or job.workspace_path
    if not value:
        return None
    try:
        return Path(str(value)).expanduser()
    except Exception:
        return None


def _load_session_for_job(work_dir: Path, job: JobRecord):
    agent_name = str(job.provider_instance or job.agent_name or "mimo")
    instance = named_agent_instance(agent_name, primary_agent="mimo")
    if instance is not None:
        session = load_project_session(work_dir, instance)
        if session is not None:
            return session
        return None
    return load_project_session(work_dir, instance=agent_name) or load_project_session(work_dir)


def _mimo_run_env(session_data: dict[str, object]) -> dict[str, str]:
    env = dict(os.environ)
    mimo_home = str(session_data.get("mimo_home") or "").strip()
    if mimo_home:
        Path(mimo_home).mkdir(parents=True, exist_ok=True)
        env["HOME"] = mimo_home
        env["MIMOCODE_HOME"] = mimo_home
        if "WSL_DISTRO_NAME" in os.environ:
            env["USERPROFILE"] = mimo_home
    config_path = str(session_data.get("mimo_config_path") or "").strip()
    if config_path and Path(config_path).is_file():
        env["MIMOCODE_CONFIG"] = config_path
    env["MIMOCODE_DISABLE_AUTOUPDATE"] = "true"
    env["MIMOCODE_ENABLE_ANALYSIS"] = "false"
    return env


def _path_from_session(session_data: dict[str, object], key: str) -> Path | None:
    value = str(session_data.get(key) or "").strip()
    if not value:
        return None
    try:
        return Path(value).expanduser()
    except Exception:
        return None


def _next_seq(state: dict[str, object]) -> int:
    seq = _state_int(state, "next_seq", 1)
    state["next_seq"] = seq + 1
    return seq


def _state_int(state: dict[str, object], key: str, default: int) -> int:
    try:
        return int(state.get(key, default))
    except (TypeError, ValueError):
        return default


def _coerce_returncode(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _last_cursor(items: list) -> CompletionCursor | None:
    if not items:
        return None
    return getattr(items[-1], "cursor", None)


def _stderr_tail(path: Path) -> str:
    if not path or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-_MAX_STDERR_CHARS:]


def _terminate_process(state: dict[str, object], *, grace: bool) -> None:
    if _coerce_returncode(state.get("returncode")) is not None:
        return
    pid = _coerce_returncode(state.get("pid"))
    if pid is None:
        return
    proc = _RUN_PROCS.pop(str(state.get("job_id") or ""), None)
    if proc is None:
        for job_id, candidate in list(_RUN_PROCS.items()):
            if getattr(candidate, "pid", None) == pid:
                proc = _RUN_PROCS.pop(job_id, None)
                break
    if proc is not None and proc.poll() is not None:
        return
    signum = signal.SIGTERM if grace else signal.SIGKILL
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        return
    except Exception:
        try:
            os.kill(pid, signum)
        except Exception:
            return


def _serializable_state(state: dict[str, object]) -> dict[str, object]:
    allowed = {
        "mode",
        "provider",
        "job_id",
        "request_anchor",
        "work_dir",
        "started_at",
        "last_poll_at",
        "next_seq",
        "anchor_emitted",
        "no_wrap",
        "reply_buffer",
        "stdout_path",
        "stderr_path",
        "pid",
        "returncode",
        "mimo_home",
        "mimo_config_path",
        "prompt_sha256",
        "turn_boundary_emitted",
    }
    return {key: value for key, value in dict(state).items() if key in allowed}


def build_execution_adapter() -> MimoProviderAdapter:
    return MimoProviderAdapter()


__all__ = ["MimoProviderAdapter", "build_execution_adapter"]
