from __future__ import annotations

import fcntl
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .network import (
    DEFAULT_OPENAI_PROBE_URL,
    DEFAULT_PUBLIC_PROBE_URL,
    ProbeResult,
    classify_readiness,
    probe_https,
    resolve_primary_probe_url,
)
from .paths import default_state_dir
from .policy import full_jitter_delay


WATCH_SCHEMA_VERSION = 1
MAX_STATE_BYTES = 64 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
PANE_ID_RE = re.compile(r"^%[0-9]+$")
TURN_ID_IN_LOG_RE = re.compile(
    r"\bturn(?:\.id|_id)=\"?([A-Za-z0-9][A-Za-z0-9_.:-]{0,255})"
)
TERMINAL_LOG_TARGET = "codex_core::session::turn"
TERMINAL_LOG_MARKER = "Turn error:"
MAX_LOG_ROWS_PER_POLL = 2048
ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
EMPTY_PROMPT_CURSOR_X = 2
CONTINUE_TEXT = "continue"
INPUT_SETTLE_SECONDS = 0.5


class TmuxWatchError(RuntimeError):
    pass


class WatchStopped(TmuxWatchError):
    pass


@dataclass(frozen=True, slots=True)
class PaneIdentity:
    pane_id: str
    pane_pid: int
    pane_command: str


@dataclass(frozen=True, slots=True)
class PaneCursor:
    x: int
    y: int
    height: int


@dataclass(frozen=True, slots=True)
class WatchContext:
    thread_id: str
    codex_home: Path
    session_path: Path
    tmux_socket: Path
    pane: PaneIdentity


@dataclass(frozen=True, slots=True)
class WatchState:
    schema_version: int
    instance_id: str
    enabled: bool
    status: str
    thread_id: str
    codex_home: str
    session_path: str
    tmux_socket: str
    pane_id: str
    pane_pid: int
    pane_command: str
    session_offset: int
    watcher_pid: int | None
    openai_probe_url: str
    public_probe_url: str | None
    probe_timeout: float
    updated_at: float
    last_error: str | None

    @classmethod
    def from_dict(cls, payload: object) -> "WatchState":
        if not isinstance(payload, dict):
            raise TmuxWatchError("watch state must be a JSON object")
        expected = {
            "schema_version",
            "instance_id",
            "enabled",
            "status",
            "thread_id",
            "codex_home",
            "session_path",
            "tmux_socket",
            "pane_id",
            "pane_pid",
            "pane_command",
            "session_offset",
            "watcher_pid",
            "openai_probe_url",
            "public_probe_url",
            "probe_timeout",
            "updated_at",
            "last_error",
        }
        if set(payload) != expected:
            raise TmuxWatchError("watch state schema mismatch")
        state = cls(**payload)
        state.validate()
        return state

    def validate(self) -> None:
        if self.schema_version != WATCH_SCHEMA_VERSION:
            raise TmuxWatchError(
                f"unsupported watch state schema: {self.schema_version!r}"
            )
        _validate_identifier(self.instance_id, "instance id")
        _validate_identifier(self.thread_id, "thread id")
        if not isinstance(self.enabled, bool):
            raise TmuxWatchError("watch state enabled must be boolean")
        if not isinstance(self.status, str) or not self.status:
            raise TmuxWatchError("watch state status is invalid")
        for label, raw in (
            ("Codex home", self.codex_home),
            ("session path", self.session_path),
            ("tmux socket", self.tmux_socket),
        ):
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                raise TmuxWatchError(f"watch state {label} must be absolute")
        if not isinstance(self.pane_id, str) or not PANE_ID_RE.fullmatch(self.pane_id):
            raise TmuxWatchError("watch state pane id is invalid")
        if not isinstance(self.pane_pid, int) or self.pane_pid <= 0:
            raise TmuxWatchError("watch state pane pid is invalid")
        if not isinstance(self.pane_command, str) or not self.pane_command:
            raise TmuxWatchError("watch state pane command is invalid")
        if not isinstance(self.session_offset, int) or self.session_offset < 0:
            raise TmuxWatchError("watch state session offset is invalid")
        if self.watcher_pid is not None and (
            not isinstance(self.watcher_pid, int) or self.watcher_pid <= 0
        ):
            raise TmuxWatchError("watch state watcher pid is invalid")
        if not isinstance(self.openai_probe_url, str):
            raise TmuxWatchError("watch state OpenAI probe URL is invalid")
        if self.public_probe_url is not None and not isinstance(
            self.public_probe_url, str
        ):
            raise TmuxWatchError("watch state public probe URL is invalid")
        if not isinstance(self.probe_timeout, (int, float)) or self.probe_timeout <= 0:
            raise TmuxWatchError("watch state probe timeout is invalid")
        if not isinstance(self.updated_at, (int, float)):
            raise TmuxWatchError("watch state update timestamp is invalid")
        if self.last_error is not None and not isinstance(self.last_error, str):
            raise TmuxWatchError("watch state last error is invalid")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EligibleError:
    failure_kind: str
    error_class: str


@dataclass(frozen=True, slots=True)
class Incident:
    turn_id: str
    failure_kind: str
    error_class: str


@dataclass(frozen=True, slots=True)
class TrackerAction:
    kind: str
    incident: Incident | None = None


@dataclass(frozen=True, slots=True)
class TerminalLogError:
    row_id: int
    turn_id: str
    message: str


class TmuxClient:
    def __init__(
        self,
        socket_path: Path,
        *,
        tmux_command: str = "tmux",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.socket_path = Path(socket_path)
        self.tmux_command = tmux_command
        self.runner = runner

    def query_pane(self, pane_id: str) -> PaneIdentity:
        completed = self._run(
            [
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{pane_id}|#{pane_pid}|#{pane_current_command}|#{pane_dead}",
            ]
        )
        parts = completed.stdout.rstrip("\n").split("|")
        if len(parts) != 4:
            raise TmuxWatchError("tmux returned malformed pane identity")
        actual_id, raw_pid, command, dead = parts
        if actual_id != pane_id or not PANE_ID_RE.fullmatch(actual_id):
            raise TmuxWatchError("tmux returned a different pane")
        try:
            pane_pid = int(raw_pid)
        except ValueError as exc:
            raise TmuxWatchError("tmux returned an invalid pane pid") from exc
        if dead != "0" or pane_pid <= 0 or not command:
            raise TmuxWatchError(f"tmux pane {pane_id} is not live")
        return PaneIdentity(actual_id, pane_pid, command)

    def capture_pane_styled(self, pane_id: str) -> str:
        return self._run(["capture-pane", "-p", "-e", "-t", pane_id]).stdout

    def query_cursor(self, pane_id: str) -> PaneCursor:
        completed = self._run(
            [
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{cursor_x}|#{cursor_y}|#{pane_height}",
            ]
        )
        parts = completed.stdout.rstrip("\n").split("|")
        if len(parts) != 3:
            raise TmuxWatchError("tmux returned malformed cursor state")
        try:
            cursor = PaneCursor(*(int(part) for part in parts))
        except ValueError as exc:
            raise TmuxWatchError("tmux returned invalid cursor state") from exc
        if (
            cursor.x < 0
            or cursor.y < 0
            or cursor.height <= 0
            or cursor.y >= cursor.height
        ):
            raise TmuxWatchError("tmux returned out-of-range cursor state")
        return cursor

    def send_continue_if_cursor(
        self,
        pane_id: str,
        cursor: PaneCursor,
        pane_pid: int,
        *,
        wait: Callable[[float], None] = time.sleep,
        settle_seconds: float = INPUT_SETTLE_SECONDS,
    ) -> bool:
        if settle_seconds < 0:
            raise ValueError("input settle delay must be non-negative")
        buffer_name = f"codex-reconnect-{os.getpid()}-{uuid.uuid4().hex}"
        self._run(["load-buffer", "-b", buffer_name, "-"], input_text=CONTINUE_TEXT)
        try:
            pasted = self._run_cursor_condition(
                pane_id,
                cursor,
                pane_pid,
                true_command=(
                    f"paste-buffer -p -t {pane_id} -b {buffer_name} ; "
                    "display-message -p -- codex-reconnect-input-pasted"
                ),
                success_marker="codex-reconnect-input-pasted",
                skipped_marker="codex-reconnect-input-skipped",
            )
            if not pasted:
                return False
            if settle_seconds:
                wait(settle_seconds)
            staged_cursor = self._staged_continue_cursor(pane_id, cursor, pane_pid)
            if staged_cursor is None:
                raise TmuxWatchError(
                    "staged continue input changed before submission; refusing Enter"
                )
            submitted = self._run_cursor_condition(
                pane_id,
                staged_cursor,
                pane_pid,
                true_command=(
                    f"send-keys -t {pane_id} Enter ; "
                    "display-message -p -- codex-reconnect-enter-sent"
                ),
                success_marker="codex-reconnect-enter-sent",
                skipped_marker="codex-reconnect-enter-skipped",
            )
            if not submitted:
                raise TmuxWatchError(
                    "tmux cursor changed before Enter; reconnect was not submitted"
                )
            return True
        finally:
            self._run(["delete-buffer", "-b", buffer_name], check=False)

    def _staged_continue_cursor(
        self, pane_id: str, origin: PaneCursor, pane_pid: int
    ) -> PaneCursor | None:
        actual = self.query_pane(pane_id)
        if actual.pane_pid != pane_pid:
            raise TmuxWatchError(
                "tmux pane identity changed after paste; refusing Enter"
            )
        captured = self.capture_pane_styled(pane_id)
        cursor = self.query_cursor(pane_id)
        expected = PaneCursor(origin.x + len(CONTINUE_TEXT), origin.y, origin.height)
        lines = captured.splitlines()
        if cursor != expected or cursor.y >= len(lines):
            return None
        visible = ANSI_CSI_RE.sub("", lines[cursor.y]).rstrip()
        if visible != f"› {CONTINUE_TEXT}":
            return None
        return cursor

    def _run_cursor_condition(
        self,
        pane_id: str,
        cursor: PaneCursor,
        pane_pid: int,
        *,
        true_command: str,
        success_marker: str,
        skipped_marker: str,
    ) -> bool:
        condition = (
            "#{&&:"
            f"#{{&&:#{{==:#{{cursor_x}},{cursor.x}}},"
            f"#{{==:#{{cursor_y}},{cursor.y}}}}},"
            f"#{{==:#{{pane_pid}},{pane_pid}}}}}"
        )
        completed = self._run(
            [
                "if-shell",
                "-F",
                "-t",
                pane_id,
                condition,
                true_command,
                f"display-message -p -- {skipped_marker}",
            ]
        )
        result = completed.stdout.strip()
        if result == success_marker:
            return True
        if result == skipped_marker:
            return False
        raise TmuxWatchError("tmux returned an unknown input-injection result")

    def _run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.tmux_command, "-S", str(self.socket_path), *arguments]
        try:
            return self.runner(
                command,
                check=check,
                capture_output=True,
                text=True,
                input=input_text,
                timeout=5.0,
            )
        except FileNotFoundError as exc:
            raise TmuxWatchError("tmux executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise TmuxWatchError("tmux command timed out") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "tmux command failed").strip()
            raise TmuxWatchError(detail) from exc


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        _secure_directory(self.path.parent)

    def write(self, event: str, **fields: object) -> None:
        encoded = json.dumps(
            {"timestamp": time.time(), "event": event, **fields},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8", closefd=True) as handle:
                descriptor = -1
                handle.write(encoded + "\n")
        finally:
            if descriptor >= 0:
                os.close(descriptor)


class SessionEventTracker:
    def __init__(self) -> None:
        self.active_turn_id: str | None = None
        self.pending_error: EligibleError | None = None
        self.pending_error_turn_id: str | None = None
        self.saw_any_error = False
        self.any_error_turn_id: str | None = None
        self.incident: Incident | None = None
        self.recovery_expected = False
        self.recovery_turn_id: str | None = None
        self.last_recovery_completed_turn_id: str | None = None
        self.last_completed_turn_id: str | None = None
        self.terminal_error_turn_ids: set[str] = set()

    def observe(self, event: object) -> list[TrackerAction]:
        if not isinstance(event, dict) or event.get("type") != "event_msg":
            return []
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return []
        event_type = payload.get("type")
        actions: list[TrackerAction] = []
        if event_type == "user_message":
            message = payload.get("message")
            if isinstance(message, str) and message.strip() == "$reconnect off":
                return [TrackerAction("stop")]
            if self.incident is not None:
                self.incident = None
                actions.append(TrackerAction("cancelled_new_progress"))
            if self.recovery_expected and not (
                isinstance(message, str) and message.strip() == "continue"
            ):
                self.recovery_expected = False
                self.recovery_turn_id = None
            return actions
        if event_type == "task_started":
            turn_id = payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                return []
            if self.incident is not None:
                self.incident = None
                actions.append(TrackerAction("cancelled_new_progress"))
            self.last_completed_turn_id = None
            self.active_turn_id = turn_id
            if self.recovery_expected and self.recovery_turn_id is None:
                self.recovery_turn_id = turn_id
            return actions
        if event_type == "error":
            self.saw_any_error = True
            self.any_error_turn_id = self.active_turn_id
            eligible = classify_session_error(payload)
            if eligible is not None:
                self.pending_error = eligible
                self.pending_error_turn_id = self.active_turn_id
            return []
        if event_type != "task_complete":
            return []
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return [TrackerAction("turn_complete")]
        # Recent Codex versions attach terminal failures to task_complete
        # instead of emitting a separate event_msg:error record. Feed that
        # nested error through the same state used by the legacy event shape.
        nested_error = payload.get("error")
        if isinstance(nested_error, dict):
            self.saw_any_error = True
            self.any_error_turn_id = turn_id
            error_payload = dict(nested_error)
            error_payload["type"] = "error"
            eligible = classify_session_error(error_payload)
            if eligible is not None:
                self.pending_error = eligible
                self.pending_error_turn_id = turn_id
        self.last_completed_turn_id = turn_id
        pending_matches = self.pending_error is not None and (
            self.pending_error_turn_id is None or self.pending_error_turn_id == turn_id
        )
        if pending_matches:
            assert self.pending_error is not None
            incident = Incident(
                turn_id,
                self.pending_error.failure_kind,
                self.pending_error.error_class,
            )
            self.pending_error = None
            self.pending_error_turn_id = None
            self.saw_any_error = False
            self.any_error_turn_id = None
            self.active_turn_id = None
            if self.recovery_expected and (
                self.recovery_turn_id is None or self.recovery_turn_id == turn_id
            ):
                self.recovery_expected = False
                self.recovery_turn_id = None
                self.last_recovery_completed_turn_id = None
                self.incident = None
                return [TrackerAction("circuit_open", incident)]
            self.incident = incident
            return [TrackerAction("incident", incident)]
        any_error_matches = self.saw_any_error and (
            self.any_error_turn_id is None or self.any_error_turn_id == turn_id
        )
        self.pending_error = None
        self.pending_error_turn_id = None
        self.saw_any_error = False
        self.any_error_turn_id = None
        self.active_turn_id = None
        if self.recovery_expected and any_error_matches:
            self.recovery_expected = False
            self.recovery_turn_id = None
            self.last_recovery_completed_turn_id = None
            self.incident = None
            return [
                TrackerAction(
                    "circuit_open", Incident(turn_id, "out_of_scope", "other")
                )
            ]
        if self.recovery_expected and self.recovery_turn_id == turn_id:
            self.recovery_expected = False
            self.recovery_turn_id = None
            self.last_recovery_completed_turn_id = turn_id
            actions.append(TrackerAction("recovery_succeeded"))
        actions.append(TrackerAction("turn_complete"))
        return actions

    def observe_terminal_log_error(
        self, turn_id: str, message: str
    ) -> list[TrackerAction]:
        if self.incident is not None and self.incident.turn_id == turn_id:
            return []
        if turn_id in self.terminal_error_turn_ids:
            return []
        matches_active = self.active_turn_id == turn_id
        matches_completed = (
            self.active_turn_id is None and self.last_completed_turn_id == turn_id
        )
        if not matches_active and not matches_completed:
            return []
        self.terminal_error_turn_ids.add(turn_id)
        eligible = classify_session_error(
            {
                "type": "error",
                "codex_error_info": "other",
                "message": message,
            }
        )
        if matches_active:
            self.saw_any_error = True
            self.any_error_turn_id = turn_id
            if eligible is not None:
                self.pending_error = eligible
                self.pending_error_turn_id = turn_id
            return []
        late_recovery_error = self.last_recovery_completed_turn_id == turn_id
        if late_recovery_error or (
            self.recovery_expected
            and (self.recovery_turn_id is None or self.recovery_turn_id == turn_id)
        ):
            self.recovery_expected = False
            self.recovery_turn_id = None
            self.last_recovery_completed_turn_id = None
            self.incident = None
            incident = Incident(
                turn_id,
                eligible.failure_kind if eligible is not None else "out_of_scope",
                eligible.error_class if eligible is not None else "other",
            )
            return [TrackerAction("circuit_open", incident)]
        if eligible is None:
            return []
        incident = Incident(turn_id, eligible.failure_kind, eligible.error_class)
        self.incident = incident
        return [TrackerAction("incident", incident)]

    def mark_injected(self) -> None:
        self.incident = None
        self.recovery_expected = True
        self.recovery_turn_id = None
        self.last_recovery_completed_turn_id = None


class SessionWatcher:
    def __init__(
        self,
        state_path: Path,
        instance_id: str,
        *,
        tmux_client: TmuxClient | None = None,
        primary_probe: Callable[[str, float], ProbeResult] | None = None,
        public_probe: Callable[[str, float], ProbeResult] | None = None,
        poll_interval: float = 0.25,
        stable_successes: int = 2,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        log_path: Path | None = None,
        log_cursor: int = 0,
        log_reader: Callable[[Path, str, int], tuple[int, list[TerminalLogError]]]
        | None = None,
    ):
        if poll_interval < 0:
            raise ValueError("poll interval must be non-negative")
        if stable_successes < 1:
            raise ValueError("stable successes must be positive")
        if log_cursor < 0:
            raise ValueError("log cursor must be non-negative")
        self.state_path = Path(state_path)
        self.instance_id = instance_id
        self.state = load_watch_state(self.state_path)
        if self.state.instance_id != self.instance_id:
            raise WatchStopped("watcher instance was superseded")
        self.tmux = tmux_client or TmuxClient(Path(self.state.tmux_socket))
        self.primary_probe = primary_probe or (
            lambda url, timeout: probe_https(url, timeout=timeout)
        )
        self.public_probe = public_probe or (
            lambda url, timeout: probe_https(url, timeout=timeout)
        )
        self.poll_interval = poll_interval
        self.stable_successes = stable_successes
        self.monotonic = monotonic
        self.wait = wait
        self.random_value = random_value
        self.log_path = Path(log_path) if log_path is not None else None
        self.log_cursor = log_cursor
        self.log_reader = log_reader or read_codex_terminal_logs
        self.log_source_error: str | None = None
        self.tracker = SessionEventTracker()
        self.audit = AuditLog(_audit_path(self.state_path))
        self.empty_prompt_observed = False
        self.next_probe_at = 0.0
        self.probe_attempt = 0
        self.consecutive_successes = 0
        self.prompt_learn_after: float | None = None

    def run(self) -> int:
        session_path = Path(self.state.session_path)
        try:
            with session_path.open("rb") as handle:
                handle.seek(self.state.session_offset)
                self._set_status("arming")
                self.empty_prompt_observed = self._empty_input_cursor() is not None
                if self.empty_prompt_observed:
                    self._set_status("armed")
                    self.audit.write(
                        "empty_prompt_learned",
                        threadId=self.state.thread_id,
                        paneId=self.state.pane_id,
                    )
                else:
                    self.prompt_learn_after = self.monotonic() + max(
                        0.25, self.poll_interval
                    )
                self.audit.write(
                    "watcher_started",
                    threadId=self.state.thread_id,
                    paneId=self.state.pane_id,
                    sessionOffset=self.state.session_offset,
                )
                while True:
                    self._assert_enabled()
                    saw_event = self._consume_available(handle)
                    saw_event = self._consume_log_errors() or saw_event
                    self._maybe_learn_empty_prompt()
                    self._maybe_probe_and_inject(handle)
                    if not saw_event:
                        self.wait(self.poll_interval)
        except WatchStopped:
            return 0
        except (OSError, ValueError, TmuxWatchError) as exc:
            self._fail(str(exc))
            return 3

    def _consume_available(self, handle: Any) -> bool:
        saw_event = False
        while True:
            position = handle.tell()
            raw = handle.readline(MAX_EVENT_BYTES + 1)
            if not raw:
                return saw_event
            if len(raw) > MAX_EVENT_BYTES:
                raise TmuxWatchError("Codex session event exceeds safety limit")
            if not raw.endswith(b"\n"):
                handle.seek(position)
                return saw_event
            saw_event = True
            try:
                event = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise TmuxWatchError(f"Codex session contains malformed JSON: {exc}")
            actions = self.tracker.observe(event)
            for action in actions:
                self._handle_action(action)

    def _handle_action(self, action: TrackerAction) -> None:
        if action.kind == "stop":
            self.audit.write("reconnect_off_observed", threadId=self.state.thread_id)
            self._disable("off", None)
            raise WatchStopped("reconnect was turned off")
        if action.kind == "incident":
            assert action.incident is not None
            self.probe_attempt = 0
            self.consecutive_successes = 0
            delay = 0.0
            if action.incident.failure_kind == "overload":
                delay = max(
                    1.0,
                    full_jitter_delay(
                        2, cap_seconds=8.0, random_value=self.random_value
                    ),
                )
            self.next_probe_at = self.monotonic() + delay
            self._set_status("waiting_network")
            self.audit.write(
                "terminal_failure_observed",
                threadId=self.state.thread_id,
                turnId=action.incident.turn_id,
                failureKind=action.incident.failure_kind,
                errorClass=action.incident.error_class,
                initialDelaySeconds=delay,
            )
            return
        if action.kind == "cancelled_new_progress":
            self.consecutive_successes = 0
            self._set_status("armed")
            self.audit.write(
                "recovery_cancelled_new_progress", threadId=self.state.thread_id
            )
            return
        if action.kind == "circuit_open":
            incident = action.incident
            self.audit.write(
                "recovery_circuit_open",
                threadId=self.state.thread_id,
                turnId=incident.turn_id if incident else None,
            )
            self._disable(
                "circuit_open",
                "automatic continuation also failed; reconnect was turned off",
            )
            raise WatchStopped("recovery circuit opened")
        if action.kind == "recovery_succeeded":
            self._set_status("armed")
            self.audit.write("recovery_succeeded", threadId=self.state.thread_id)
            return
        if action.kind == "turn_complete" and self.tracker.incident is None:
            if not self.empty_prompt_observed:
                self.prompt_learn_after = self.monotonic() + max(
                    0.25, self.poll_interval
                )
                self._set_status("arming")

    def _consume_log_errors(self) -> bool:
        if self.log_path is None:
            candidate = Path(self.state.codex_home) / "logs_2.sqlite"
            if not candidate.exists():
                return False
            try:
                _validate_sqlite_log_path(candidate)
                self.log_path = candidate.resolve(strict=True)
            except (OSError, TmuxWatchError):
                return False
            self.log_cursor = 0
            self.audit.write(
                "sqlite_log_source_discovered",
                threadId=self.state.thread_id,
            )
        try:
            cursor, records = self.log_reader(
                self.log_path, self.state.thread_id, self.log_cursor
            )
        except (OSError, sqlite3.Error, TmuxWatchError) as exc:
            error = str(exc)
            if error != self.log_source_error:
                self.audit.write(
                    "sqlite_log_source_unavailable",
                    threadId=self.state.thread_id,
                    error=error,
                )
                self.log_source_error = error
            return False
        if self.log_source_error is not None:
            self.audit.write(
                "sqlite_log_source_restored", threadId=self.state.thread_id
            )
            self.log_source_error = None
        self.log_cursor = cursor
        for record in records:
            self.audit.write(
                "terminal_log_error_observed",
                threadId=self.state.thread_id,
                turnId=record.turn_id,
                logRowId=record.row_id,
            )
            for action in self.tracker.observe_terminal_log_error(
                record.turn_id, record.message
            ):
                self._handle_action(action)
        return bool(records)

    def _maybe_probe_and_inject(self, handle: Any) -> None:
        incident = self.tracker.incident
        if incident is None or self.monotonic() < self.next_probe_at:
            return
        primary = self.primary_probe(
            self.state.openai_probe_url, self.state.probe_timeout
        )
        public: ProbeResult | None = None
        if not primary.reachable and self.state.public_probe_url is not None:
            public = self.public_probe(
                self.state.public_probe_url, self.state.probe_timeout
            )
        readiness = classify_readiness(primary, public)
        self.audit.write(
            "network_probe",
            threadId=self.state.thread_id,
            readiness=readiness.value,
            primaryReachable=primary.reachable,
            primaryStatus=primary.status,
            publicReachable=public.reachable if public is not None else None,
        )
        if not primary.reachable:
            self.consecutive_successes = 0
            self.probe_attempt += 1
            self.next_probe_at = self.monotonic() + max(
                1.0,
                full_jitter_delay(
                    self.probe_attempt,
                    cap_seconds=30.0,
                    random_value=self.random_value,
                ),
            )
            return
        self.consecutive_successes += 1
        if self.consecutive_successes < self.stable_successes:
            self.next_probe_at = self.monotonic() + max(0.25, self.poll_interval)
            return
        self._consume_available(handle)
        self._consume_log_errors()
        if self.tracker.incident != incident:
            return
        self._assert_enabled()
        actual = self.tmux.query_pane(self.state.pane_id)
        expected = PaneIdentity(
            self.state.pane_id, self.state.pane_pid, self.state.pane_command
        )
        if actual != expected:
            raise TmuxWatchError("tmux pane identity changed; refusing input injection")
        empty_cursor = self._empty_input_cursor()
        if not self.empty_prompt_observed or empty_cursor is None:
            self.audit.write(
                "recovery_skipped_input_state",
                threadId=self.state.thread_id,
                turnId=incident.turn_id,
            )
            self.tracker.incident = None
            self._set_status("armed")
            return
        if not self.tmux.send_continue_if_cursor(
            self.state.pane_id,
            empty_cursor,
            self.state.pane_pid,
            wait=self.wait,
        ):
            self.audit.write(
                "recovery_skipped_input_race",
                threadId=self.state.thread_id,
                turnId=incident.turn_id,
            )
            self.tracker.incident = None
            self._set_status("armed")
            return
        self.tracker.mark_injected()
        self.consecutive_successes = 0
        self.audit.write(
            "continue_submitted",
            threadId=self.state.thread_id,
            paneId=self.state.pane_id,
            failedTurnId=incident.turn_id,
            errorClass=incident.error_class,
        )
        self._set_status("recovery_sent")

    def _maybe_learn_empty_prompt(self) -> None:
        if (
            self.empty_prompt_observed
            or self.prompt_learn_after is None
            or self.monotonic() < self.prompt_learn_after
            or self.tracker.incident is not None
            or self.tracker.recovery_expected
        ):
            return
        if self._empty_input_cursor() is None:
            self.prompt_learn_after = self.monotonic() + max(0.25, self.poll_interval)
            return
        self.empty_prompt_observed = True
        self.prompt_learn_after = None
        self._set_status("armed")
        self.audit.write(
            "empty_prompt_learned",
            threadId=self.state.thread_id,
            paneId=self.state.pane_id,
        )

    def _empty_input_cursor(self) -> PaneCursor | None:
        captured = self.tmux.capture_pane_styled(self.state.pane_id)
        cursor = self.tmux.query_cursor(self.state.pane_id)
        lines = captured.splitlines()
        if cursor.y >= len(lines):
            return None
        if cursor.x != EMPTY_PROMPT_CURSOR_X:
            return None
        if not _styled_prompt_line_is_empty(lines[cursor.y]):
            return None
        return cursor

    def _assert_enabled(self) -> None:
        current = load_watch_state(self.state_path)
        if current.instance_id != self.instance_id or not current.enabled:
            raise WatchStopped("watcher was disabled or superseded")
        self.state = current

    def _stop_for_signal(self, signum: int) -> None:
        signal_name = signal.Signals(signum).name
        try:
            self._disable("off", None)
            try:
                self.audit.write(
                    "watcher_stopped_by_signal",
                    threadId=self.state.thread_id,
                    signal=signal_name,
                )
            except OSError:
                pass
        except (OSError, TmuxWatchError):
            pass
        raise WatchStopped(f"watcher received {signal_name}")

    def _set_status(self, status: str) -> None:
        def update(current: WatchState) -> WatchState:
            if not current.enabled:
                raise WatchStopped("watcher was disabled")
            return replace(
                current, status=status, updated_at=time.time(), last_error=None
            )

        self.state = modify_watch_state(self.state_path, self.instance_id, update)

    def _disable(self, status: str, error: str | None) -> None:
        def update(current: WatchState) -> WatchState:
            return replace(
                current,
                enabled=False,
                status=status,
                updated_at=time.time(),
                last_error=error,
            )

        self.state = modify_watch_state(self.state_path, self.instance_id, update)

    def _fail(self, error: str) -> None:
        try:
            self._disable("error", error)
        except (OSError, TmuxWatchError):
            pass
        self.audit.write("watcher_error", threadId=self.state.thread_id, error=error)


def _styled_prompt_line_is_empty(line: str) -> bool:
    _, marker, suffix = line.partition("›")
    if not marker:
        return False
    visible = ANSI_CSI_RE.sub("", line)
    if not visible.startswith("› "):
        return False

    dim = False
    consumed_input_origin = False
    index = 0
    while index < len(suffix):
        escape = ANSI_CSI_RE.match(suffix, index)
        if escape is not None:
            sequence = escape.group(0)
            if sequence.endswith("m"):
                parameters = sequence[2:-1]
                try:
                    values = [
                        int(value) if value else 0 for value in parameters.split(";")
                    ]
                except ValueError:
                    return False
                if values and values[0] not in {38, 48, 58}:
                    for value in values:
                        if value in {0, 22}:
                            dim = False
                        elif value == 2:
                            dim = True
            index = escape.end()
            continue
        character = suffix[index]
        index += 1
        if not consumed_input_origin:
            if character != " ":
                return False
            consumed_input_origin = True
            continue
        return dim
    return consumed_input_origin


def classify_session_error(payload: object) -> EligibleError | None:
    if not isinstance(payload, dict) or payload.get("type") != "error":
        return None
    if payload.get("willRetry") is True or payload.get("will_retry") is True:
        return None
    message = payload.get("message")
    lowered = message.lower() if isinstance(message, str) else ""
    if any(
        marker in lowered
        for marker in (
            "quota",
            "billing",
            "usage limit",
            "usage_limit",
            "insufficient",
            "unauthorized",
            "forbidden",
            "authentication",
            "余额不足",
            "额度不足",
            "剩余额度",
            "预扣费",
            "套餐不可访问",
            "context window",
            "safety policy",
        )
    ):
        return None
    info = payload.get("codex_error_info")
    info_key = ""
    info_value: object = None
    if isinstance(info, str):
        info_key = info
    elif isinstance(info, dict) and len(info) == 1:
        info_key, info_value = next(iter(info.items()))
    normalized = re.sub(r"[^a-z0-9]", "", info_key.lower())
    if normalized == "serveroverloaded":
        return EligibleError("overload", "serverOverloaded")
    if normalized in {
        "httpconnectionfailed",
        "responsestreamconnectionfailed",
        "responsestreamdisconnected",
    }:
        return EligibleError("network", info_key)
    if normalized == "responsetoomanyfailedattempts":
        status = (
            info_value.get("http_status_code") if isinstance(info_value, dict) else None
        )
        return EligibleError(
            "overload" if status == 429 else "network",
            "responseTooManyFailedAttempts",
        )
    if any(
        marker in lowered
        for marker in (
            "server overloaded",
            "server is overloaded",
            "selected model is at capacity",
            "model is at capacity",
            "model at capacity",
            "too many requests",
            "last status: 429",
        )
    ):
        return EligibleError("overload", "serverOverloaded")
    if any(
        marker in lowered
        for marker in (
            "stream disconnected before completion",
            "stream closed before response.completed",
            "error sending request",
            "connection failed",
            "failed to connect",
            "network is unreachable",
            "dns error",
            "request timed out",
            "connection timed out",
        )
    ):
        return EligibleError("network", "responseStreamDisconnected")
    return None


def initial_codex_log_cursor(
    codex_home: Path, thread_id: str
) -> tuple[Path, int] | None:
    _validate_identifier(thread_id, "thread id")
    path = Path(codex_home) / "logs_2.sqlite"
    if not path.exists():
        return None
    _validate_sqlite_log_path(path)
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        ) as connection:
            _validate_sqlite_schema(connection)
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM logs WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise TmuxWatchError(f"failed to read Codex SQLite log cursor: {exc}") from exc
    cursor = row[0] if row is not None else 0
    if not isinstance(cursor, int) or cursor < 0:
        raise TmuxWatchError("Codex SQLite log cursor is invalid")
    return path.resolve(), cursor


def read_codex_terminal_logs(
    path: Path, thread_id: str, after_id: int
) -> tuple[int, list[TerminalLogError]]:
    _validate_identifier(thread_id, "thread id")
    if not isinstance(after_id, int) or after_id < 0:
        raise TmuxWatchError("Codex SQLite log cursor is invalid")
    path = Path(path)
    _validate_sqlite_log_path(path)
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        ) as connection:
            _validate_sqlite_schema(connection)
            rows = connection.execute(
                """
                SELECT id, target, feedback_log_body
                FROM logs
                WHERE thread_id = ? AND id > ?
                ORDER BY id
                LIMIT ?
                """,
                (thread_id, after_id, MAX_LOG_ROWS_PER_POLL),
            ).fetchall()
    except sqlite3.Error as exc:
        raise TmuxWatchError(f"failed to read Codex SQLite logs: {exc}") from exc
    cursor = after_id
    records: list[TerminalLogError] = []
    for row_id, target, body in rows:
        if not isinstance(row_id, int) or row_id <= cursor:
            raise TmuxWatchError("Codex SQLite log row id is invalid")
        cursor = row_id
        if target != TERMINAL_LOG_TARGET or not isinstance(body, str):
            continue
        marker_at = body.rfind(TERMINAL_LOG_MARKER)
        if marker_at < 0:
            continue
        turn_match = TURN_ID_IN_LOG_RE.search(body)
        if turn_match is None:
            continue
        turn_id = turn_match.group(1)
        message = body[marker_at + len(TERMINAL_LOG_MARKER) :].strip()
        if message:
            records.append(TerminalLogError(row_id, turn_id, message))
    return cursor, records


def _supersede_pane_watchers(
    state_dir: Path, context: WatchContext, new_thread_id: str
) -> None:
    watchers_dir = Path(state_dir) / "watchers"
    if not watchers_dir.is_dir():
        return
    for path in watchers_dir.glob("*.json"):
        try:
            state = load_watch_state(path)
        except (FileNotFoundError, OSError, TmuxWatchError):
            continue
        if (
            not state.enabled
            or state.thread_id == new_thread_id
            or state.tmux_socket != str(context.tmux_socket)
            or state.pane_id != context.pane.pane_id
            or state.pane_pid != context.pane.pane_pid
        ):
            continue

        def supersede(current: WatchState) -> WatchState:
            return replace(
                current,
                enabled=False,
                status="superseded",
                updated_at=time.time(),
                last_error=None,
            )

        try:
            modify_watch_state(path, state.instance_id, supersede)
        except (FileNotFoundError, OSError, TmuxWatchError):
            continue
        AuditLog(_audit_path(path)).write(
            "watcher_superseded_pane_reuse",
            threadId=state.thread_id,
            newThreadId=new_thread_id,
            paneId=state.pane_id,
        )


def _supersede_restarted_pane_watcher(
    state_path: Path, state: WatchState, context: WatchContext
) -> bool:
    if (
        state.tmux_socket != str(context.tmux_socket)
        or state.pane_id != context.pane.pane_id
        or state.pane_pid == context.pane.pane_pid
    ):
        return False

    def supersede(current: WatchState) -> WatchState:
        return replace(
            current,
            enabled=False,
            status="superseded",
            updated_at=time.time(),
            last_error=None,
        )

    modify_watch_state(state_path, state.instance_id, supersede)
    AuditLog(_audit_path(state_path)).write(
        "watcher_superseded_pane_restart",
        threadId=state.thread_id,
        paneId=state.pane_id,
        oldPanePid=state.pane_pid,
        newPanePid=context.pane.pane_pid,
    )
    return True


def _validate_sqlite_log_path(path: Path) -> None:
    try:
        path_stat = path.lstat()
        resolved = path.resolve(strict=True)
        target_stat = resolved.stat()
    except OSError as exc:
        raise TmuxWatchError(f"Codex SQLite log is not a regular file: {path}") from exc
    if path.is_symlink() and hasattr(os, "getuid") and path_stat.st_uid != os.getuid():
        raise TmuxWatchError(
            "Codex SQLite log symlink is not owned by the current user"
        )
    if not stat.S_ISREG(target_stat.st_mode):
        raise TmuxWatchError(f"Codex SQLite log is not a regular file: {path}")
    if hasattr(os, "getuid") and target_stat.st_uid != os.getuid():
        raise TmuxWatchError("Codex SQLite log is not owned by the current user")


def _validate_sqlite_schema(connection: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(logs)").fetchall()
        if len(row) > 1 and isinstance(row[1], str)
    }
    required = {"id", "target", "feedback_log_body", "thread_id"}
    if not required.issubset(columns):
        raise TmuxWatchError("Codex SQLite log schema is unsupported")


def enable_current(
    *,
    state_dir: Path | None = None,
    openai_probe_url: str | None = None,
    public_probe_url: str | None = DEFAULT_PUBLIC_PROBE_URL,
    probe_timeout: float = 5.0,
    environment: Mapping[str, str] | None = None,
    tmux_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    process_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> WatchState:
    env = os.environ if environment is None else environment
    context = current_watch_context(env, tmux_runner=tmux_runner)
    resolved_probe_url = resolve_primary_probe_url(
        context.codex_home,
        configured_url=openai_probe_url,
        environment=dict(env),
    )
    root = Path(state_dir or default_state_dir())
    state_path = watch_state_path(root, context.thread_id)
    _secure_directory(state_path.parent)
    try:
        current = load_watch_state(state_path)
    except FileNotFoundError:
        current = None
    except TmuxWatchError:
        current = None
    if current is not None and current.enabled and _process_alive(current.watcher_pid):
        if _state_matches_context(current, context):
            return current
        if not _supersede_restarted_pane_watcher(state_path, current, context):
            raise TmuxWatchError(
                f"thread {context.thread_id} is already armed in pane {current.pane_id}"
            )
    _supersede_pane_watchers(root, context, context.thread_id)
    log_source = initial_codex_log_cursor(context.codex_home, context.thread_id)
    instance_id = uuid.uuid4().hex
    state = WatchState(
        schema_version=WATCH_SCHEMA_VERSION,
        instance_id=instance_id,
        enabled=True,
        status="starting",
        thread_id=context.thread_id,
        codex_home=str(context.codex_home),
        session_path=str(context.session_path),
        tmux_socket=str(context.tmux_socket),
        pane_id=context.pane.pane_id,
        pane_pid=context.pane.pane_pid,
        pane_command=context.pane.pane_command,
        session_offset=context.session_path.stat().st_size,
        watcher_pid=None,
        openai_probe_url=resolved_probe_url,
        public_probe_url=public_probe_url,
        probe_timeout=probe_timeout,
        updated_at=time.time(),
        last_error=None,
    )
    with state_lock(state_path):
        save_watch_state(state_path, state)
    launcher = Path(__file__).resolve().parents[1] / "codex-reconnect"
    if not launcher.is_file():
        raise TmuxWatchError(f"watcher launcher is missing: {launcher}")
    log_path = _watcher_log_path(state_path)
    descriptor = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    child_env = _watcher_environment(env)
    watcher_command = [
        str(launcher),
        "_watch",
        "--state-file",
        str(state_path),
        "--instance-id",
        instance_id,
    ]
    if log_source is not None:
        log_path, log_cursor = log_source
        watcher_command.extend(
            ["--log-file", str(log_path), "--log-cursor", str(log_cursor)]
        )
    try:
        process = process_factory(
            watcher_command,
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=descriptor,
            start_new_session=True,
            close_fds=True,
            env=child_env,
        )
    except Exception:
        os.close(descriptor)
        _disable_if_current(state_path, instance_id, "error", "watcher failed to start")
        raise
    os.close(descriptor)

    def record_pid(current_state: WatchState) -> WatchState:
        return replace(
            current_state,
            watcher_pid=process.pid,
            status="arming",
            updated_at=time.time(),
        )

    return modify_watch_state(state_path, instance_id, record_pid)


def disable_current(
    *,
    state_dir: Path | None = None,
    environment: Mapping[str, str] | None = None,
    tmux_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> WatchState | None:
    env = os.environ if environment is None else environment
    context = current_watch_context(env, tmux_runner=tmux_runner)
    state_path = watch_state_path(
        Path(state_dir or default_state_dir()), context.thread_id
    )
    try:
        current = load_watch_state(state_path)
    except FileNotFoundError:
        return None
    if not _state_matches_context(current, context):
        raise TmuxWatchError("reconnect watcher belongs to a different tmux pane")

    def update(state: WatchState) -> WatchState:
        return replace(
            state,
            enabled=False,
            status="off",
            updated_at=time.time(),
            last_error=None,
        )

    return modify_watch_state(state_path, current.instance_id, update)


def status_current(
    *,
    state_dir: Path | None = None,
    environment: Mapping[str, str] | None = None,
    tmux_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> WatchState | None:
    env = os.environ if environment is None else environment
    context = current_watch_context(env, tmux_runner=tmux_runner)
    state_path = watch_state_path(
        Path(state_dir or default_state_dir()), context.thread_id
    )
    try:
        state = load_watch_state(state_path)
    except FileNotFoundError:
        return None
    if not _state_matches_context(state, context):
        raise TmuxWatchError("reconnect watcher belongs to a different tmux pane")
    return state


def current_watch_context(
    environment: Mapping[str, str],
    *,
    tmux_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> WatchContext:
    raw_tmux = environment.get("TMUX", "")
    pane_id = environment.get("TMUX_PANE", "")
    if raw_tmux and pane_id:
        if not PANE_ID_RE.fullmatch(pane_id):
            raise TmuxWatchError("TMUX_PANE is invalid")
        fields = raw_tmux.rsplit(",", 2)
        if len(fields) != 3 or not fields[0]:
            raise TmuxWatchError("TMUX is malformed")
        tmux_socket = Path(fields[0]).expanduser()
    elif raw_tmux or pane_id:
        raise TmuxWatchError("tmux binding is incomplete (TMUX/TMUX_PANE mismatch)")
    else:
        tmux_socket, pane_id = _ccb_tmux_binding(environment)
    if not tmux_socket.is_absolute():
        raise TmuxWatchError("tmux socket path must be absolute")
    thread_id = environment.get("CODEX_THREAD_ID", "")
    _validate_identifier(thread_id, "CODEX_THREAD_ID")
    raw_home = environment.get("HOME")
    codex_home = Path(
        environment.get("CODEX_HOME")
        or (Path(raw_home) if raw_home else Path.home()) / ".codex"
    ).expanduser()
    if not codex_home.is_absolute():
        codex_home = codex_home.resolve()
    session_path = find_session_file(codex_home, thread_id)
    tmux_command = shutil.which("tmux", path=environment.get("PATH")) or "tmux"
    pane = TmuxClient(
        tmux_socket, tmux_command=tmux_command, runner=tmux_runner
    ).query_pane(pane_id)
    return WatchContext(
        thread_id,
        codex_home.resolve(),
        session_path.resolve(),
        tmux_socket,
        pane,
    )


def _ccb_tmux_binding(environment: Mapping[str, str]) -> tuple[Path, str]:
    raw_session_file = str(environment.get("CCB_SESSION_FILE") or "").strip()
    if not raw_session_file:
        raise TmuxWatchError(
            "current Codex CLI is not running inside tmux "
            "(TMUX/TMUX_PANE or CCB_SESSION_FILE missing)"
        )
    session_file = Path(raw_session_file).expanduser()
    if not session_file.is_absolute():
        raise TmuxWatchError("CCB_SESSION_FILE must be absolute")
    try:
        file_stat = session_file.lstat()
    except OSError as exc:
        raise TmuxWatchError("CCB session binding file was not found") from exc
    if session_file.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
        raise TmuxWatchError("CCB session binding is not a regular file")
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise TmuxWatchError("CCB session binding is not owned by the current user")
    if file_stat.st_size > MAX_STATE_BYTES:
        raise TmuxWatchError("CCB session binding is too large")
    try:
        payload = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TmuxWatchError("CCB session binding is invalid") from exc
    if not isinstance(payload, dict) or payload.get("active") is not True:
        raise TmuxWatchError("CCB session binding is not active")

    pane_id = str(payload.get("pane_id") or "").strip()
    if not PANE_ID_RE.fullmatch(pane_id):
        raise TmuxWatchError("CCB session pane_id is invalid")
    declared_pane = str(environment.get("CODEX_TMUX_SESSION") or "").strip()
    if declared_pane and declared_pane != pane_id:
        raise TmuxWatchError("CCB session pane does not match CODEX_TMUX_SESSION")

    raw_socket = str(payload.get("tmux_socket_path") or "").strip()
    tmux_socket = Path(raw_socket).expanduser()
    if not raw_socket or not tmux_socket.is_absolute():
        raise TmuxWatchError("CCB session tmux_socket_path is invalid")

    thread_id = str(environment.get("CODEX_THREAD_ID") or "").strip()
    session_thread_id = str(payload.get("codex_session_id") or "").strip()
    if session_thread_id and session_thread_id != thread_id:
        raise TmuxWatchError("CCB session thread does not match CODEX_THREAD_ID")

    raw_codex_home = str(environment.get("CODEX_HOME") or "").strip()
    session_codex_home = str(payload.get("codex_home") or "").strip()
    if raw_codex_home and session_codex_home:
        if (
            Path(raw_codex_home).expanduser().resolve()
            != Path(session_codex_home).expanduser().resolve()
        ):
            raise TmuxWatchError("CCB session Codex home does not match CODEX_HOME")
    return tmux_socket, pane_id


def find_session_file(codex_home: Path, thread_id: str) -> Path:
    _validate_identifier(thread_id, "thread id")
    root = Path(codex_home) / "sessions"
    if not root.is_dir():
        raise TmuxWatchError(f"Codex session directory was not found: {root}")
    matches = sorted(root.rglob(f"*{thread_id}*.jsonl"))
    verified: list[Path] = []
    for path in matches:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                first = handle.readline(MAX_EVENT_BYTES + 1)
            record = json.loads(first)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(record, dict)
            and record.get("type") == "session_meta"
            and isinstance(record.get("payload"), dict)
            and record["payload"].get("id") == thread_id
        ):
            verified.append(path)
    if not verified:
        raise TmuxWatchError(
            f"matching Codex session was not found for thread {thread_id}"
        )
    if len(verified) > 1:
        raise TmuxWatchError(
            f"multiple Codex sessions matched thread {thread_id}; refusing to guess"
        )
    path = verified[0]
    file_stat = path.stat()
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise TmuxWatchError("Codex session is not owned by the current user")
    return path


def watch_state_path(state_dir: Path, thread_id: str) -> Path:
    _validate_identifier(thread_id, "thread id")
    return Path(state_dir) / "watchers" / f"{thread_id}.json"


def load_watch_state(path: Path) -> WatchState:
    path = Path(path)
    if path.is_symlink():
        raise TmuxWatchError(f"watch state must not be a symlink: {path}")
    try:
        file_stat = path.stat()
    except FileNotFoundError:
        raise
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise TmuxWatchError("watch state is not owned by the current user")
    if file_stat.st_size > MAX_STATE_BYTES:
        raise TmuxWatchError("watch state exceeds safety limit")
    try:
        return WatchState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TmuxWatchError(f"failed to read watch state: {exc}") from exc


def save_watch_state(path: Path, state: WatchState) -> None:
    state.validate()
    path = Path(path)
    _secure_directory(path.parent)
    if path.exists() and path.is_symlink():
        raise TmuxWatchError("watch state must not be a symlink")
    encoded = (
        json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".watch.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def modify_watch_state(
    path: Path,
    expected_instance_id: str,
    update: Callable[[WatchState], WatchState],
) -> WatchState:
    with state_lock(path):
        current = load_watch_state(path)
        if current.instance_id != expected_instance_id:
            raise WatchStopped("watcher instance was superseded")
        changed = update(current)
        save_watch_state(path, changed)
        return changed


class state_lock:
    def __init__(self, state_path: Path):
        self.path = Path(f"{state_path}.lock")
        self.descriptor = -1

    def __enter__(self) -> "state_lock":
        _secure_directory(self.path.parent)
        if self.path.is_symlink():
            raise TmuxWatchError("watch state lock must not be a symlink")
        self.descriptor = os.open(
            self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600
        )
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.descriptor >= 0:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = -1


def watcher_main(
    state_file: Path,
    instance_id: str,
    *,
    log_path: Path | None = None,
    log_cursor: int = 0,
) -> int:
    try:
        watcher = SessionWatcher(
            state_file,
            instance_id,
            log_path=log_path,
            log_cursor=log_cursor,
        )
        previous_handlers: dict[signal.Signals, Any] = {}

        def stop(signum: int, _frame: Any) -> None:
            watcher._stop_for_signal(signum)

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, stop)
        try:
            return watcher.run()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    except (OSError, ValueError, TmuxWatchError) as exc:
        print(f"codex-reconnect watcher failed: {exc}", file=sys.stderr)
        return 3


def _state_matches_context(state: WatchState, context: WatchContext) -> bool:
    return (
        state.thread_id == context.thread_id
        and state.codex_home == str(context.codex_home)
        and state.session_path == str(context.session_path)
        and state.tmux_socket == str(context.tmux_socket)
        and state.pane_id == context.pane.pane_id
        and state.pane_pid == context.pane.pane_pid
        and state.pane_command == context.pane.pane_command
    )


def _watcher_environment(environment: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    result = {key: value for key, value in environment.items() if key in allowed}
    result["PYTHONUNBUFFERED"] = "1"
    return result


def _process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _disable_if_current(
    state_path: Path, instance_id: str, status: str, error: str
) -> None:
    try:
        modify_watch_state(
            state_path,
            instance_id,
            lambda state: replace(
                state,
                enabled=False,
                status=status,
                last_error=error,
                updated_at=time.time(),
            ),
        )
    except (OSError, TmuxWatchError):
        pass


def _secure_directory(path: Path) -> None:
    path = Path(path)
    if path.exists() and path.is_symlink():
        raise TmuxWatchError(f"directory must not be a symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    file_stat = path.stat()
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise TmuxWatchError(f"directory is not owned by the current user: {path}")
    os.chmod(path, 0o700)


def _validate_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not THREAD_ID_RE.fullmatch(value):
        raise TmuxWatchError(f"{label} is missing or invalid")
    return value


def _watcher_log_path(state_path: Path) -> Path:
    return state_path.with_suffix(".watcher.log")


def _audit_path(state_path: Path) -> Path:
    return state_path.with_suffix(".audit.jsonl")
