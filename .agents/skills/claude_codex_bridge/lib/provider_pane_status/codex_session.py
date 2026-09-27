from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .codex_pane import ACTIVE_STATES, PaneStatus


_SESSION_FREE_EVENTS: dict[str, str] = {
    "task_complete": "codex_session_task_complete",
}
_SESSION_INTERRUPTED_EVENTS: dict[str, str] = {
    "turn_aborted": "codex_session_turn_aborted",
    "thread_rolled_back": "codex_session_thread_rolled_back",
}
_SESSION_TERMINAL_EVENTS = {**_SESSION_FREE_EVENTS, **_SESSION_INTERRUPTED_EVENTS}

RUNTIME_STATUS_CATALOG: dict[str, str] = {
    "free": "Codex is explicitly available for a new turn from session evidence or pane completion evidence.",
    "start": "A prompt was submitted and Codex has not yet emitted a first explicit pane/session signal for that turn.",
    "working": "Codex is visibly or session-log actively working.",
    "tool_running": "A foreground or background tool/terminal is visibly running.",
    "interrupted": "The latest Codex turn was interrupted or rolled back and needs user attention.",
    "waiting_for_user": "Codex is waiting for user confirmation, approval, trust, or menu input.",
    "auth_required": "Codex is not logged in or is waiting for sign-in/API-key setup.",
    "auth_failed": "Codex reports authentication or API-key rejection.",
    "api_error": "Codex reports provider/API/model/rate-limit/server failure text.",
    "config_error": "Codex reports invalid provider/configuration text.",
    "reconnecting": "Codex reports stream recovery or retrying connection.",
    "failed": "Generic visible provider/runtime failure.",
    "pane_dead": "The tmux pane or server is gone.",
    "unknown": "No explicit pane or session evidence is available.",
}


@dataclass(frozen=True)
class CodexSessionStatus:
    state: str
    reason: str
    session_root: str | None = None
    latest_session_path: str | None = None
    session_count: int = 0
    scanned_session_count: int = 0
    latest_session_mtime_s: float | None = None
    matched_patterns: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "state": self.state,
            "reason": self.reason,
            "session_root": self.session_root,
            "latest_session_path": self.latest_session_path,
            "session_count": self.session_count,
            "scanned_session_count": self.scanned_session_count,
            "matched_patterns": list(self.matched_patterns),
            "notes": list(self.notes),
        }
        if self.latest_session_mtime_s is not None:
            record["latest_session_mtime_s"] = round(self.latest_session_mtime_s, 3)
        return record


@dataclass(frozen=True)
class CodexRuntimeStatus:
    state: str
    reason: str
    source: str
    pane_state: str
    pane_reason: str
    session_state: str | None = None
    session_reason: str | None = None
    notes: tuple[str, ...] = ()

    def to_record(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "source": self.source,
            "pane_state": self.pane_state,
            "pane_reason": self.pane_reason,
            "session_state": self.session_state,
            "session_reason": self.session_reason,
            "notes": list(self.notes),
        }


def read_codex_session_status(
    session_root: Path | str | None,
    *,
    work_dir: Path | str | None = None,
    min_mtime_s: float | None = None,
    max_scan_files: int = 16,
    max_tail_lines: int = 200,
) -> CodexSessionStatus:
    if session_root is None:
        return CodexSessionStatus("unknown", "missing_session_root")

    root = Path(session_root).expanduser()
    root_text = str(root)
    if not root.exists():
        return CodexSessionStatus("unknown", "session_root_missing", session_root=root_text)
    if not root.is_dir():
        return CodexSessionStatus("unknown", "session_root_not_directory", session_root=root_text)

    files = sorted(root.rglob("*.jsonl"), key=lambda path: _safe_mtime(path), reverse=True)
    if not files:
        if min_mtime_s is not None:
            return CodexSessionStatus("unknown", "no_recent_codex_session_files", session_root=root_text)
        return CodexSessionStatus("free", "no_codex_session_files", session_root=root_text)

    expected_cwd = _normalize_path(work_dir)
    scanned = 0
    for path in files[: max(1, max_scan_files)]:
        scanned += 1
        mtime_s = _safe_mtime(path)
        if min_mtime_s is not None and mtime_s < min_mtime_s:
            continue
        if expected_cwd is not None and not _session_matches_work_dir(path, expected_cwd):
            continue
        return _classify_session_file(
            path,
            session_root=root_text,
            session_count=len(files),
            scanned_session_count=scanned,
            latest_session_mtime_s=mtime_s,
            max_tail_lines=max_tail_lines,
        )

    reason = "no_recent_matching_codex_session_files" if min_mtime_s is not None else "no_matching_codex_session_files"
    state = "unknown" if min_mtime_s is not None else "free"
    return CodexSessionStatus(
        state,
        reason,
        session_root=root_text,
        session_count=len(files),
        scanned_session_count=scanned,
    )


def compose_codex_runtime_status(
    pane_status: PaneStatus,
    session_status: CodexSessionStatus | None,
) -> CodexRuntimeStatus:
    if pane_status.state in {
        *ACTIVE_STATES,
        "waiting_for_user",
        "auth_required",
        "auth_failed",
        "api_error",
        "config_error",
        "failed",
        "pane_dead",
    }:
        return _runtime_from_pane(pane_status)

    if pane_status.state == "completed":
        return CodexRuntimeStatus(
            "free",
            "codex_pane_completed",
            "pane",
            pane_status.state,
            pane_status.reason,
            session_status.state if session_status is not None else None,
            session_status.reason if session_status is not None else None,
        )

    if session_status is not None:
        if session_status.state == "working":
            return _runtime_from_session(pane_status, session_status)
        if session_status.state == "interrupted":
            return _runtime_from_session(pane_status, session_status)
        if session_status.state == "free" and pane_status.reason != "empty_capture":
            return _runtime_from_session(pane_status, session_status)

    return CodexRuntimeStatus(
        "unknown",
        pane_status.reason,
        "pane",
        pane_status.state,
        pane_status.reason,
        session_status.state if session_status is not None else None,
        session_status.reason if session_status is not None else None,
    )


def _classify_session_file(
    path: Path,
    *,
    session_root: str,
    session_count: int,
    scanned_session_count: int,
    latest_session_mtime_s: float,
    max_tail_lines: int,
) -> CodexSessionStatus:
    latest_task_event: str | None = None
    saw_assistant_response = False
    notes: list[str] = []
    for entry in _read_tail_entries(path, max_lines=max_tail_lines):
        payload = _payload(entry)
        entry_type = str(entry.get("type") or "")
        payload_type = str(payload.get("type") or "")
        if entry_type == "event_msg" and payload_type == "task_started":
            latest_task_event = "task_started"
        elif entry_type == "event_msg" and payload_type in _SESSION_TERMINAL_EVENTS:
            latest_task_event = payload_type
        elif entry_type == "response_item" and payload_type == "message" and str(payload.get("role") or "") == "assistant":
            saw_assistant_response = True

    base = {
        "session_root": session_root,
        "latest_session_path": str(path),
        "session_count": session_count,
        "scanned_session_count": scanned_session_count,
        "latest_session_mtime_s": latest_session_mtime_s,
    }
    if latest_task_event in _SESSION_FREE_EVENTS:
        return CodexSessionStatus(
            "free",
            _SESSION_FREE_EVENTS[latest_task_event],
            matched_patterns=(latest_task_event,),
            **base,
        )
    if latest_task_event in _SESSION_INTERRUPTED_EVENTS:
        return CodexSessionStatus(
            "interrupted",
            _SESSION_INTERRUPTED_EVENTS[latest_task_event],
            matched_patterns=(latest_task_event,),
            **base,
        )
    if latest_task_event == "task_started":
        return CodexSessionStatus(
            "working",
            "codex_session_task_started",
            matched_patterns=("task_started",),
            **base,
        )
    if saw_assistant_response:
        notes.append("assistant_response_without_task_complete")
    return CodexSessionStatus(
        "unknown",
        "no_session_task_boundary",
        notes=tuple(notes),
        **base,
    )


def _runtime_from_pane(pane_status: PaneStatus) -> CodexRuntimeStatus:
    return CodexRuntimeStatus(
        pane_status.state,
        pane_status.reason,
        "pane",
        pane_status.state,
        pane_status.reason,
    )


def _runtime_from_session(pane_status: PaneStatus, session_status: CodexSessionStatus) -> CodexRuntimeStatus:
    return CodexRuntimeStatus(
        session_status.state,
        session_status.reason,
        "session",
        pane_status.state,
        pane_status.reason,
        session_status.state,
        session_status.reason,
    )


def _read_tail_entries(path: Path, *, max_lines: int) -> list[dict[str, Any]]:
    lines = _read_tail_lines(path, max_bytes=128 * 1024, max_lines=max_lines)
    return _decode_json_lines(lines)


def _read_head_entries(path: Path, *, max_lines: int) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = []
            for _ in range(max(1, max_lines)):
                line = fh.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
    except OSError:
        return []
    return _decode_json_lines(lines)


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


def _decode_json_lines(lines: list[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _session_matches_work_dir(path: Path, expected_cwd: str) -> bool:
    for entry in _read_head_entries(path, max_lines=40):
        payload = _payload(entry)
        cwd = payload.get("cwd")
        if cwd is not None and _normalize_path(cwd) == expected_cwd:
            return True
    return False


def _payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload = entry.get("payload")
    return payload if isinstance(payload, dict) else {}


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _normalize_path(value: Path | str | None) -> str | None:
    if value is None:
        return None
    return str(Path(value).expanduser().resolve(strict=False))


__all__ = [
    "CodexRuntimeStatus",
    "CodexSessionStatus",
    "RUNTIME_STATUS_CATALOG",
    "compose_codex_runtime_status",
    "read_codex_session_status",
]
