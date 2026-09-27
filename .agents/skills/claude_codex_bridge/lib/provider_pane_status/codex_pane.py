from __future__ import annotations

import re
from dataclasses import dataclass

from .models import PaneCompletionEvidence


ACTIVE_STATES = frozenset({"working", "tool_running", "reconnecting"})
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
STATUS_MARKER_RE = r"[•◦]"
WORKED_FOR_RE = re.compile(
    r"^[•✔✓]\s*worked\s+for\s+(?:\d+\s*h\s*)?(?:\d+\s*m\s*)?\d+\s*s\b",
    re.IGNORECASE,
)
CODEX_STATUS_LINE_RE = re.compile(rf"^{STATUS_MARKER_RE}")
CODEX_RECONNECT_LINE_RE = re.compile(rf"^{STATUS_MARKER_RE}\s*reconnecting\b", re.IGNORECASE)
CODEX_WORKING_LINE_RE = re.compile(
    rf"^{STATUS_MARKER_RE}\s*(?:working|running|booting mcp server:?)\b.*\((?:\d+\s*h\s*)?(?:\d+\s*m\s*)?\d+\s*s\b[^)]*(?:esc to interrupt|interrupt)[^)]*\)",
    re.IGNORECASE,
)
CODEX_TOOL_LINE_RE = re.compile(
    rf"^{STATUS_MARKER_RE}\s*(?:working|running)\b.*(?:\d+\s+background terminals? running|background terminals? running|/ps to view|shells? still running|running scheduled task)",
    re.IGNORECASE,
)

ERROR_MARKERS = (
    "stream disconnected before completion",
    "error sending request for url",
    "failed to connect",
    "could not connect",
    "connection refused",
    "connection reset",
    "connection closed",
    "connection timed out",
    "request timed out",
    "model unavailable",
    "model_not_found",
    "rate limit",
    "rate_limit",
    "too many requests",
    "overloaded",
    "api error",
    "internal server error",
)
AUTH_REQUIRED_MARKERS = (
    "sign in with chatgpt",
    "sign in with device code",
    "provide your own api key",
    "connect an api key",
    "usage-based billing",
    "not logged in",
    "login required",
    "please sign in",
    "run codex login",
)
AUTH_FAILED_MARKERS = (
    "invalid api key",
    "incorrect api key",
    "authentication failed",
    "unauthorized",
    "401 unauthorized",
    "no api key provided",
)
API_ERROR_MARKERS = (
    "error sending request for url",
    "failed to connect",
    "could not connect",
    "connection refused",
    "connection reset",
    "connection closed",
    "connection timed out",
    "request timed out",
    "model unavailable",
    "model_not_found",
    "rate limit",
    "rate_limit",
    "too many requests",
    "overloaded",
    "api error",
    "internal server error",
    "bad request",
    "request failed",
)
CONFIG_ERROR_MARKERS = (
    "failed to parse config",
    "invalid config",
    "invalid configuration",
    "unknown model provider",
    "model provider",
    "config.toml",
)
WAITING_MARKERS = (
    "do you trust the contents of this directory?",
    "press enter to continue",
    "approval required",
    "requires approval",
    "allow this command",
)
STATUS_CATALOG: dict[str, str] = {
    "completed": "Codex shows an explicit terminal summary such as Worked for.",
    "working": "Codex reports model/runtime work, usually with a Working/Running timer.",
    "tool_running": "A foreground or background tool/terminal is visibly running.",
    "waiting_for_user": "Codex is waiting for user confirmation, approval, trust, or menu input; auth prompts win when markers overlap.",
    "auth_required": "Codex is not logged in or is waiting for sign-in/API-key setup; this is checked before generic waiting text.",
    "auth_failed": "Codex reports authentication or API-key rejection.",
    "api_error": "Codex reports provider/API/model/rate-limit/server failure text.",
    "config_error": "Codex reports invalid provider/configuration text.",
    "reconnecting": "Codex reports stream recovery or retrying connection; recoverable active state.",
    "failed": "Generic visible provider/runtime failure not classified above.",
    "pane_dead": "The tmux pane or server is gone.",
    "unknown": "Pane evidence is empty, contradictory, or not yet classified.",
}


@dataclass(frozen=True)
class PaneStatus:
    state: str
    reason: str
    matched_patterns: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    terminal_outcome: str | None = None
    completion_evidence: PaneCompletionEvidence | None = None

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "state": self.state,
            "reason": self.reason,
            "matched_patterns": list(self.matched_patterns),
            "notes": list(self.notes),
        }
        if self.terminal_outcome is not None:
            record["terminal_outcome"] = self.terminal_outcome
        if self.completion_evidence is not None:
            record["completion_evidence"] = self.completion_evidence.to_record()
        return record


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def normalize_screen(text: str) -> str:
    cleaned = strip_ansi(text).replace("\r", "\n").replace("\xa0", " ")
    lines = [line.rstrip() for line in cleaned.splitlines()]
    return "\n".join(lines)


def parse_codex_pane_status(
    pane_text: str | None,
    *,
    pane_dead: bool = False,
) -> PaneStatus:
    """Classify visible Codex pane text using explicit tail-most evidence only.

    Active states must come from Codex status-line shaped rows such as
    "• Working (... esc to interrupt)"; body text containing the same words is
    ignored. Terminal completion is only an explicit terminal summary that
    appears no earlier than the last active status line. Everything else stays
    unknown instead of being inferred from quiet output or prompt visibility.
    """
    if pane_dead:
        return PaneStatus(
            "pane_dead",
            "pane_dead",
            ("pane_dead",),
            terminal_outcome="pane_dead",
            completion_evidence=PaneCompletionEvidence(
                "pane_dead",
                "codex_pane",
                "pane_dead",
            ),
        )

    normalized = normalize_screen(pane_text or "")
    recent_lines = [line.rstrip() for line in normalized.splitlines() if line.strip()]
    recent = " ".join(line.strip() for line in recent_lines[-20:]).lower()
    worked_for_index = _last_worked_for_index(recent_lines)
    active_status = _last_active_status(recent_lines)
    active_index = active_status[0]
    completed = worked_for_index >= 0 and worked_for_index >= active_index

    if not recent:
        return PaneStatus("unknown", "empty_capture")

    if completed:
        return PaneStatus(
            "completed",
            "codex_worked_for_terminal_summary",
            ("worked_for",),
            terminal_outcome="completed",
            completion_evidence=PaneCompletionEvidence(
                "completed",
                "codex_pane",
                "codex_worked_for_terminal_summary",
            ),
        )

    matches = _matched(AUTH_REQUIRED_MARKERS, recent)
    if matches:
        return PaneStatus(
            "auth_required",
            "codex_auth_required",
            matches,
        )

    matches = _matched(AUTH_FAILED_MARKERS, recent)
    if matches:
        return PaneStatus(
            "auth_failed",
            "provider_auth_failed",
            matches,
            terminal_outcome="failed",
            completion_evidence=PaneCompletionEvidence(
                "failed",
                "codex_pane",
                "provider_auth_failed",
            ),
        )

    matches = _matched(CONFIG_ERROR_MARKERS, recent)
    if matches:
        return PaneStatus(
            "config_error",
            "provider_config_error",
            matches,
            terminal_outcome="failed",
            completion_evidence=PaneCompletionEvidence(
                "failed",
                "codex_pane",
                "provider_config_error",
            ),
        )

    if active_status[1] == "reconnecting":
        return PaneStatus(
            "reconnecting",
            "provider_reconnecting",
            ("reconnecting_status_line",),
            ("recoverable_stream_retry_visible",),
        )

    matches = _matched(API_ERROR_MARKERS, recent)
    if matches:
        return PaneStatus(
            "api_error",
            "provider_api_error",
            matches,
            terminal_outcome="failed",
            completion_evidence=PaneCompletionEvidence(
                "failed",
                "codex_pane",
                "provider_api_error",
            ),
        )

    matches = _matched(ERROR_MARKERS, recent)
    if matches:
        return PaneStatus(
            "failed",
            "provider_error_text",
            matches,
            terminal_outcome="failed",
            completion_evidence=PaneCompletionEvidence(
                "failed",
                "codex_pane",
                "provider_error_text",
            ),
        )

    if active_status[1] == "tool_running":
        return PaneStatus(
            "tool_running",
            "provider_tool_running",
            ("tool_status_line",),
        )

    if active_status[1] == "working":
        return PaneStatus(
            "working",
            "codex_working_status_line",
            ("running_status_time", "working_status_line"),
        )

    matches = _matched(WAITING_MARKERS, recent)
    if matches and "press esc to interrupt" not in recent and _waiting_prompt_near_tail(recent_lines):
        return PaneStatus("waiting_for_user", "provider_waiting_for_user", matches)

    return PaneStatus("unknown", "no_known_status_pattern")


def _matched(markers: tuple[str, ...], text: str) -> tuple[str, ...]:
    return tuple(marker for marker in markers if marker in text)


def _last_worked_for_index(lines: list[str]) -> int:
    for index in range(len(lines) - 1, max(-1, len(lines) - 13), -1):
        line = lines[index]
        if WORKED_FOR_RE.search(line):
            return index
    return -1


def _last_active_status(lines: list[str]) -> tuple[int, str | None, str]:
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if not CODEX_STATUS_LINE_RE.search(line):
            continue
        if CODEX_RECONNECT_LINE_RE.search(line):
            return index, "reconnecting", line
        if CODEX_TOOL_LINE_RE.search(line):
            return index, "tool_running", line
        if CODEX_WORKING_LINE_RE.search(line):
            return index, "working", line
    return -1, None, ""


def _waiting_prompt_near_tail(lines: list[str]) -> bool:
    tail = " ".join(lines[-12:]).lower()
    return bool(_matched(WAITING_MARKERS, tail))


__all__ = [
    "ACTIVE_STATES",
    "PaneCompletionEvidence",
    "PaneStatus",
    "STATUS_CATALOG",
    "normalize_screen",
    "parse_codex_pane_status",
    "strip_ansi",
]
