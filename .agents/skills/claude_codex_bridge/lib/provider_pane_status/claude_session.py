from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from provider_backends.claude.comm_runtime.parsing_runtime.structured import structured_event
from provider_backends.claude.execution_runtime.event_reading_runtime.api_errors import (
    api_error_event,
    terminal_api_error_payload,
)
from .control_messages import is_provider_local_control_message
from .claude_pane import ACTIVE_STATES as PANE_ACTIVE_STATES, ClaudePaneStatus


CLAUDE_ACTIVE_ACTIVITY_MAX_AGE_S = 180.0


RUNTIME_STATUS_CATALOG: dict[str, str] = {
    "free": "Claude is explicitly available for a new turn from hook or session evidence.",
    "start": "A prompt was submitted and Claude has not yet emitted a first explicit hook/session signal for that turn.",
    "working": "Claude hook or session evidence shows an active turn.",
    "tool_running": "Claude hook or session evidence shows tool execution.",
    "waiting_for_user": "Claude is waiting for user approval, input, or permission.",
    "api_error": "Claude reports provider/API/model/rate-limit/server failure.",
    "failed": "Claude reports a generic provider/runtime failure.",
    "pane_dead": "The tmux pane or server is gone.",
    "unknown": "No explicit Claude hook or session evidence is available.",
}


@dataclass(frozen=True)
class ClaudeActivityStatus:
    state: str
    reason: str
    source: str = "activity"
    activity_state: str | None = None
    activity_reason: str | None = None
    event_name: str | None = None
    updated_at: str | None = None
    notes: tuple[str, ...] = ()

    def to_record(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "source": self.source,
            "activity_state": self.activity_state,
            "activity_reason": self.activity_reason,
            "event_name": self.event_name,
            "updated_at": self.updated_at,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ClaudeSessionStatus:
    state: str
    reason: str
    session_path: str | None = None
    latest_session_mtime_s: float | None = None
    matched_patterns: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "state": self.state,
            "reason": self.reason,
            "session_path": self.session_path,
            "matched_patterns": list(self.matched_patterns),
            "notes": list(self.notes),
        }
        if self.latest_session_mtime_s is not None:
            record["latest_session_mtime_s"] = round(self.latest_session_mtime_s, 3)
        return record


@dataclass(frozen=True)
class ClaudeRuntimeStatus:
    state: str
    reason: str
    source: str
    activity_state: str | None = None
    activity_reason: str | None = None
    session_state: str | None = None
    session_reason: str | None = None
    pane_state: str | None = None
    pane_reason: str | None = None
    notes: tuple[str, ...] = ()

    def to_record(self) -> dict[str, object]:
        return {
            "provider": "claude",
            "state": self.state,
            "reason": self.reason,
            "source": self.source,
            "activity_state": self.activity_state,
            "activity_reason": self.activity_reason,
            "session_state": self.session_state,
            "session_reason": self.session_reason,
            "pane_state": self.pane_state,
            "pane_reason": self.pane_reason,
            "notes": list(self.notes),
        }


def claude_activity_status(
    activity: object | None,
    *,
    now: str | None = None,
    active_max_age_s: float = CLAUDE_ACTIVE_ACTIVITY_MAX_AGE_S,
) -> ClaudeActivityStatus | None:
    if activity is None:
        return None
    activity_state = _clean(getattr(activity, "state", None))
    reason = str(getattr(activity, "reason", "") or "").strip()
    event_name = str(getattr(activity, "event_name", "") or "").strip()
    updated_at = str(getattr(activity, "updated_at", "") or "").strip() or None
    diagnostics = getattr(activity, "diagnostics", None)
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    if activity_state in {"active", "pending"}:
        age_s = _age_seconds(now, updated_at)
        if age_s is not None and age_s > max(0.0, float(active_max_age_s)):
            return None
    base = {
        "activity_state": activity_state or None,
        "activity_reason": reason or None,
        "event_name": event_name or None,
        "updated_at": updated_at,
    }

    if activity_state == "active":
        if _event_is_tool(event_name) or diagnostics.get("tool_name"):
            return ClaudeActivityStatus("tool_running", "claude_activity_tool_running", **base)
        return ClaudeActivityStatus("working", "claude_activity_active", **base)
    if activity_state == "pending":
        return ClaudeActivityStatus("waiting_for_user", "claude_activity_waiting_for_user", **base)
    if activity_state == "idle":
        return ClaudeActivityStatus("free", "claude_activity_idle", **base)
    if activity_state == "failed":
        if _diagnostics_indicate_api_error(diagnostics, reason=reason):
            return ClaudeActivityStatus("api_error", "claude_activity_api_error", **base)
        return ClaudeActivityStatus("failed", "claude_activity_failed", **base)
    return None


def read_claude_session_status(
    session_path: Path | str | None,
    *,
    min_mtime_s: float | None = None,
    max_tail_lines: int = 200,
) -> ClaudeSessionStatus:
    if session_path is None:
        return ClaudeSessionStatus("unknown", "missing_session_path")

    path = Path(session_path).expanduser()
    path_text = str(path)
    if not path.exists():
        return ClaudeSessionStatus("unknown", "session_path_missing", session_path=path_text)
    if not path.is_file():
        return ClaudeSessionStatus("unknown", "session_path_not_file", session_path=path_text)
    mtime_s = _safe_mtime(path)
    if min_mtime_s is not None and mtime_s < min_mtime_s:
        return ClaudeSessionStatus(
            "unknown",
            "session_before_current_job",
            session_path=path_text,
            latest_session_mtime_s=mtime_s,
        )

    entries = _read_tail_entries(path, max_lines=max_tail_lines)
    if not entries:
        return ClaudeSessionStatus(
            "unknown",
            "empty_or_unparsed_session",
            session_path=path_text,
            latest_session_mtime_s=mtime_s,
        )

    latest = _latest_claude_session_signal(entries)
    if latest is None:
        return ClaudeSessionStatus(
            "unknown",
            "no_known_session_signal",
            session_path=path_text,
            latest_session_mtime_s=mtime_s,
        )
    state, reason, pattern, notes = latest
    return ClaudeSessionStatus(
        state,
        reason,
        session_path=path_text,
        latest_session_mtime_s=mtime_s,
        matched_patterns=(pattern,),
        notes=notes,
    )


def compose_claude_runtime_status(
    activity_status: ClaudeActivityStatus | None,
    session_status: ClaudeSessionStatus | None,
    *,
    job_running: bool,
    pane_status: ClaudePaneStatus | None = None,
) -> ClaudeRuntimeStatus:
    if pane_status is not None and pane_status.state in {
        *PANE_ACTIVE_STATES,
        "waiting_for_user",
        "api_error",
        "failed",
        "pane_dead",
    }:
        return ClaudeRuntimeStatus(
            pane_status.state,
            pane_status.reason,
            "pane",
            activity_status.activity_state if activity_status is not None else None,
            activity_status.activity_reason if activity_status is not None else None,
            session_status.state if session_status is not None else None,
            session_status.reason if session_status is not None else None,
            pane_status.state,
            pane_status.reason,
            notes=pane_status.notes,
        )

    if activity_status is not None and activity_status.state in {"api_error", "failed"}:
        return _runtime_from_activity(activity_status, session_status, pane_status)

    if pane_status is not None and pane_status.state == "free":
        return ClaudeRuntimeStatus(
            "free",
            pane_status.reason,
            "pane",
            activity_status.activity_state if activity_status is not None else None,
            activity_status.activity_reason if activity_status is not None else None,
            session_status.state if session_status is not None else None,
            session_status.reason if session_status is not None else None,
            pane_status.state,
            pane_status.reason,
            notes=pane_status.notes,
        )

    if activity_status is not None and activity_status.state == "waiting_for_user":
        return _runtime_from_activity(activity_status, session_status, pane_status)

    if (
        activity_status is not None
        and session_status is not None
        and session_status.state != "unknown"
        and _session_is_newer_than_activity(session_status, activity_status)
    ):
        return _runtime_from_session(
            session_status,
            activity_status=activity_status,
            pane_status=pane_status,
        )

    if activity_status is not None:
        return _runtime_from_activity(activity_status, session_status, pane_status)

    if session_status is not None and session_status.state != "unknown":
        return _runtime_from_session(
            session_status,
            activity_status=None,
            pane_status=pane_status,
        )

    if job_running:
        return ClaudeRuntimeStatus(
            "start",
            "prompt_submitted_waiting_for_first_signal",
            "stabilizer",
            None,
            None,
            session_status.state if session_status is not None else None,
            session_status.reason if session_status is not None else None,
            pane_status.state if pane_status is not None else None,
            pane_status.reason if pane_status is not None else None,
            notes=_raw_session_notes(session_status),
        )

    if session_status is None or session_status.reason == "missing_session_path":
        return ClaudeRuntimeStatus(
            "free",
            "no_claude_session_no_active_turn",
            "runtime",
            None,
            None,
            session_status.state if session_status is not None else None,
            session_status.reason if session_status is not None else None,
            pane_status.state if pane_status is not None else None,
            pane_status.reason if pane_status is not None else None,
            notes=_raw_session_notes(session_status),
        )

    return ClaudeRuntimeStatus(
        "unknown",
        session_status.reason,
        "session",
        None,
        None,
        session_status.state,
        session_status.reason,
        pane_status.state if pane_status is not None else None,
        pane_status.reason if pane_status is not None else None,
        notes=session_status.notes,
    )


def _runtime_from_activity(
    activity_status: ClaudeActivityStatus,
    session_status: ClaudeSessionStatus | None,
    pane_status: ClaudePaneStatus | None,
) -> ClaudeRuntimeStatus:
    return ClaudeRuntimeStatus(
        activity_status.state,
        activity_status.reason,
        "activity",
        activity_status.activity_state,
        activity_status.activity_reason,
        session_status.state if session_status is not None else None,
        session_status.reason if session_status is not None else None,
        pane_status.state if pane_status is not None else None,
        pane_status.reason if pane_status is not None else None,
        notes=activity_status.notes,
    )


def _runtime_from_session(
    session_status: ClaudeSessionStatus,
    *,
    activity_status: ClaudeActivityStatus | None,
    pane_status: ClaudePaneStatus | None,
) -> ClaudeRuntimeStatus:
    return ClaudeRuntimeStatus(
        session_status.state,
        session_status.reason,
        "session",
        activity_status.activity_state if activity_status is not None else None,
        activity_status.activity_reason if activity_status is not None else None,
        session_status.state,
        session_status.reason,
        pane_status.state if pane_status is not None else None,
        pane_status.reason if pane_status is not None else None,
        notes=session_status.notes,
    )


def _latest_claude_session_signal(entries: list[dict[str, Any]]) -> tuple[str, str, str, tuple[str, ...]] | None:
    latest: tuple[str, str, str, tuple[str, ...]] | None = None
    for entry in entries:
        event = structured_event(entry)
        if event is None:
            if _entry_has_assistant_tool_use(entry):
                latest = ("tool_running", "claude_session_tool_use", "tool_use", ())
            continue

        role = str(event.get("role") or "")
        if role == "system":
            api_error = terminal_api_error_payload(event)
            if api_error is not None:
                latest = ("api_error", "claude_session_api_error", "api_error", _api_error_notes(api_error))
            elif api_error_event(event):
                latest = ("working", "claude_session_api_retry", "api_retry", ())
            continue

        if role == "user":
            if is_provider_local_control_message(event.get("text")):
                latest = ("free", "claude_session_local_control", "local_control", ())
                continue
            latest = ("working", "claude_session_user_turn", "user", ())
            continue

        if role == "assistant":
            stop_reason = str(event.get("stop_reason") or "").strip().lower()
            if _entry_has_assistant_tool_use(entry):
                latest = ("tool_running", "claude_session_tool_use", "tool_use", ())
            elif stop_reason == "end_turn":
                latest = ("free", "claude_session_assistant_end_turn", "assistant_end_turn", ())
            else:
                latest = ("working", "claude_session_assistant_message", "assistant", ())
    return latest


def _read_tail_entries(path: Path, *, max_lines: int) -> list[dict[str, Any]]:
    lines = _read_tail_lines(path, max_bytes=128 * 1024, max_lines=max_lines)
    entries: list[dict[str, Any]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _read_tail_lines(path: Path, *, max_bytes: int, max_lines: int) -> list[str]:
    if max_bytes <= 0 or max_lines <= 0:
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read(max_bytes)
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines[-max_lines:]


def _entry_has_assistant_tool_use(entry: dict[str, Any]) -> bool:
    message = entry.get("message")
    if not isinstance(message, dict):
        payload = entry.get("payload")
        message = payload if isinstance(payload, dict) else {}
    role = str(message.get("role") or entry.get("type") or "").strip().lower()
    if role != "assistant":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    for item in content:
        if isinstance(item, dict) and str(item.get("type") or "").strip().lower() == "tool_use":
            return True
    return False


def _diagnostics_indicate_api_error(diagnostics: dict[str, object], *, reason: str) -> bool:
    tokens = {
        str(diagnostics.get("reason") or "").strip().lower(),
        str(diagnostics.get("error_type") or "").strip().lower(),
        str(reason or "").strip().lower(),
    }
    if any("api" in token for token in tokens if token):
        return True
    return bool(str(diagnostics.get("error_code") or "").strip())


def _event_is_tool(event_name: str | None) -> bool:
    return str(event_name or "").strip().lower().replace("_", "").replace("-", "") == "pretooluse"


def _api_error_notes(api_error: dict[str, object]) -> tuple[str, ...]:
    notes: list[str] = []
    for key in ("error_code", "error_path", "retry_attempt", "max_retries"):
        value = api_error.get(key)
        if value is not None:
            notes.append(f"{key}={value}")
    return tuple(notes)


def _raw_session_notes(session_status: ClaudeSessionStatus | None) -> tuple[str, ...]:
    if session_status is None:
        return ()
    return (
        f"raw_session_state={session_status.state}",
        f"raw_session_reason={session_status.reason}",
    )


def _session_is_newer_than_activity(
    session_status: ClaudeSessionStatus,
    activity_status: ClaudeActivityStatus,
) -> bool:
    session_mtime_s = session_status.latest_session_mtime_s
    activity_time = _parse_timestamp(activity_status.updated_at)
    if session_mtime_s is None or activity_time is None:
        return False
    return session_mtime_s > activity_time.timestamp()


def _age_seconds(now: str | None, updated_at: str | None) -> float | None:
    current = _parse_timestamp(now)
    observed = _parse_timestamp(updated_at)
    if current is None or observed is None:
        return None
    return max(0.0, (current - observed).total_seconds())


def _parse_timestamp(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _clean(value: object) -> str:
    return str(value or "").strip().lower()


__all__ = [
    "CLAUDE_ACTIVE_ACTIVITY_MAX_AGE_S",
    "ClaudeActivityStatus",
    "ClaudeRuntimeStatus",
    "ClaudeSessionStatus",
    "RUNTIME_STATUS_CATALOG",
    "claude_activity_status",
    "compose_claude_runtime_status",
    "read_claude_session_status",
]
