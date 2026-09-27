from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Callable
from unittest import mock


TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT))

from codex_reconnect.cli import main
from codex_reconnect.network import ProbeResult
from codex_reconnect.tmux_watch import (
    CONTINUE_TEXT,
    EligibleError,
    Incident,
    PaneCursor,
    PaneIdentity,
    SessionEventTracker,
    SessionWatcher,
    TerminalLogError,
    TmuxClient,
    TmuxWatchError,
    WatchState,
    WatchStopped,
    classify_session_error,
    current_watch_context,
    disable_current,
    enable_current,
    initial_codex_log_cursor,
    load_watch_state,
    modify_watch_state,
    read_codex_terminal_logs,
    save_watch_state,
    watch_state_path,
)


THREAD_ID = "019f729b-d4d0-7610-98cd-62562042a0c0"


def _write_session(root: Path, *, thread_id: str = THREAD_ID) -> Path:
    session = root / "sessions" / "2026" / "07" / "20" / f"rollout-{thread_id}.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": thread_id, "type": "session_meta"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return session


def _append_event(session: Path, payload: dict[str, object]) -> None:
    with session.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": payload}) + "\n")
        handle.flush()


def _write_log_db(codex_home: Path) -> Path:
    path = codex_home / "logs_2.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target TEXT NOT NULL,
                feedback_log_body TEXT,
                thread_id TEXT
            )
            """
        )
    return path


def _append_log(
    path: Path,
    *,
    turn_id: str,
    message: str,
    thread_id: str = THREAD_ID,
    target: str = "codex_core::session::turn",
) -> int:
    body = (
        f"session_loop{{thread_id={thread_id}}}:"
        f"turn{{otel.name=session_task.turn turn.id={turn_id}}}:"
        "session_task.run:run_turn: "
        f"Turn error: {message}"
    )
    with sqlite3.connect(path) as connection:
        cursor = connection.execute(
            "INSERT INTO logs(target, feedback_log_body, thread_id) VALUES(?, ?, ?)",
            (target, body, thread_id),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


def _tmux_runner(
    command: list[str], **kwargs: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        command, 0, stdout=f"%7|4242|node|0\n", stderr=""
    )


def _environment(codex_home: Path) -> dict[str, str]:
    return {
        "HOME": str(codex_home.parent),
        "CODEX_HOME": str(codex_home),
        "CODEX_THREAD_ID": THREAD_ID,
        "TMUX": "/tmp/codex-reconnect-test.sock,123,0",
        "TMUX_PANE": "%7",
        "PATH": os.environ.get("PATH", ""),
    }


def _ccb_environment(codex_home: Path, root: Path) -> dict[str, str]:
    session_file = root / ".ccb-codex-session"
    session_file.write_text(
        json.dumps(
            {
                "active": True,
                "pane_id": "%7",
                "tmux_socket_path": "/tmp/codex-reconnect-ccb-test.sock",
                "codex_home": str(codex_home),
                "codex_session_id": THREAD_ID,
            }
        ),
        encoding="utf-8",
    )
    return {
        "HOME": str(codex_home.parent),
        "CODEX_HOME": str(codex_home),
        "CODEX_THREAD_ID": THREAD_ID,
        "CODEX_TMUX_SESSION": "%7",
        "CCB_SESSION_FILE": str(session_file),
        "PATH": os.environ.get("PATH", ""),
    }


def _state(root: Path, session: Path) -> tuple[Path, WatchState]:
    state_path = watch_state_path(root / "state", THREAD_ID)
    state = WatchState(
        schema_version=1,
        instance_id="instance-1",
        enabled=True,
        status="starting",
        thread_id=THREAD_ID,
        codex_home=str(root / "codex-home"),
        session_path=str(session),
        tmux_socket="/tmp/codex-reconnect-test.sock",
        pane_id="%7",
        pane_pid=4242,
        pane_command="node",
        session_offset=session.stat().st_size,
        watcher_pid=os.getpid(),
        openai_probe_url="https://openai.test/probe",
        public_probe_url="https://public.test/probe",
        probe_timeout=0.1,
        updated_at=time.time(),
        last_error=None,
    )
    save_watch_state(state_path, state)
    return state_path, state


class ContextTests(unittest.TestCase):
    def test_reconnect_on_outside_tmux_fails_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stderr = StringIO()
            environment = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "TMUX",
                    "TMUX_PANE",
                    "CCB_SESSION_FILE",
                    "CODEX_TMUX_SESSION",
                }
            }
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch(
                "sys.stderr", stderr
            ):
                result = main(["on", "--state-dir", temporary])
        self.assertEqual(result, 3)
        self.assertIn("not running inside tmux", stderr.getvalue())

    def test_context_binds_exact_thread_session_and_pane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary) / "codex-home"
            session = _write_session(codex_home)
            context = current_watch_context(
                _environment(codex_home), tmux_runner=_tmux_runner
            )
        self.assertEqual(context.thread_id, THREAD_ID)
        self.assertEqual(context.session_path, session)
        self.assertEqual(context.pane, PaneIdentity("%7", 4242, "node"))

    def test_context_uses_ccb_session_binding_when_tmux_env_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            session = _write_session(codex_home)
            context = current_watch_context(
                _ccb_environment(codex_home, root), tmux_runner=_tmux_runner
            )
        self.assertEqual(context.thread_id, THREAD_ID)
        self.assertEqual(context.session_path, session)
        self.assertEqual(
            context.tmux_socket, Path("/tmp/codex-reconnect-ccb-test.sock")
        )
        self.assertEqual(context.pane, PaneIdentity("%7", 4242, "node"))

    def test_ccb_session_binding_rejects_mismatched_pane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            _write_session(codex_home)
            environment = _ccb_environment(codex_home, root)
            environment["CODEX_TMUX_SESSION"] = "%8"
            with self.assertRaisesRegex(TmuxWatchError, "does not match"):
                current_watch_context(environment, tmux_runner=_tmux_runner)

    def test_session_metadata_must_match_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary) / "codex-home"
            session = _write_session(codex_home, thread_id="different-thread")
            renamed = session.with_name(f"rollout-{THREAD_ID}.jsonl")
            session.rename(renamed)
            with self.assertRaisesRegex(TmuxWatchError, "session was not found"):
                current_watch_context(
                    _environment(codex_home), tmux_runner=_tmux_runner
                )


class TmuxClientSubmissionTests(unittest.TestCase):
    def _runner(
        self,
        calls: list[tuple[list[str], dict[str, object]]],
        *,
        staged_line: str = "\n\x1b[1m›\x1b[0m continue\n\n  model · cwd\n",
        paste_allowed: bool = True,
        enter_allowed: bool = True,
    ) -> Callable[..., subprocess.CompletedProcess[str]]:
        def run(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            arguments = command[3:]
            calls.append((arguments, kwargs))
            stdout = ""
            if arguments[0] == "display-message":
                if "pane_current_command" in arguments[-1]:
                    stdout = "%7|4242|node|0\n"
                else:
                    stdout = "10|1|4\n"
            elif arguments[0] == "capture-pane":
                stdout = staged_line
            elif arguments[0] == "if-shell":
                true_command = arguments[-2]
                if "paste-buffer" in true_command:
                    stdout = (
                        "codex-reconnect-input-pasted\n"
                        if paste_allowed
                        else "codex-reconnect-input-skipped\n"
                    )
                else:
                    stdout = (
                        "codex-reconnect-enter-sent\n"
                        if enter_allowed
                        else "codex-reconnect-enter-skipped\n"
                    )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        return run

    def test_continue_uses_ccb_style_buffer_paste_delay_then_enter(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        delays: list[float] = []
        client = TmuxClient(
            Path("/tmp/codex-reconnect-test.sock"), runner=self._runner(calls)
        )
        submitted = client.send_continue_if_cursor(
            "%7", PaneCursor(2, 1, 4), 4242, wait=delays.append
        )
        operations = [arguments[0] for arguments, _ in calls]
        self.assertTrue(submitted)
        self.assertEqual(delays, [0.5])
        self.assertEqual(
            operations,
            [
                "load-buffer",
                "if-shell",
                "display-message",
                "capture-pane",
                "display-message",
                "if-shell",
                "delete-buffer",
            ],
        )
        self.assertEqual(calls[0][1]["input"], CONTINUE_TEXT)
        self.assertIn("paste-buffer -p", calls[1][0][-2])
        self.assertNotIn("send-keys", calls[1][0][-2])
        self.assertIn("send-keys -t %7 Enter", calls[-2][0][-2])

    def test_cursor_race_before_paste_skips_and_deletes_buffer(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        delays: list[float] = []
        client = TmuxClient(
            Path("/tmp/codex-reconnect-test.sock"),
            runner=self._runner(calls, paste_allowed=False),
        )
        submitted = client.send_continue_if_cursor(
            "%7", PaneCursor(2, 1, 4), 4242, wait=delays.append
        )
        self.assertFalse(submitted)
        self.assertEqual(delays, [])
        self.assertEqual(
            [arguments[0] for arguments, _ in calls],
            ["load-buffer", "if-shell", "delete-buffer"],
        )

    def test_changed_staged_text_refuses_enter_and_deletes_buffer(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        client = TmuxClient(
            Path("/tmp/codex-reconnect-test.sock"),
            runner=self._runner(calls, staged_line="\n› changed\n\n  model · cwd\n"),
        )
        with self.assertRaisesRegex(TmuxWatchError, "staged continue input changed"):
            client.send_continue_if_cursor(
                "%7", PaneCursor(2, 1, 4), 4242, wait=lambda seconds: None
            )
        self.assertEqual(calls[-1][0][0], "delete-buffer")
        self.assertFalse(
            any(
                arguments[0] == "if-shell" and "send-keys" in arguments[-2]
                for arguments, _ in calls
            )
        )

    def test_cursor_race_before_enter_fails_and_deletes_buffer(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        client = TmuxClient(
            Path("/tmp/codex-reconnect-test.sock"),
            runner=self._runner(calls, enter_allowed=False),
        )
        with self.assertRaisesRegex(TmuxWatchError, "cursor changed before Enter"):
            client.send_continue_if_cursor(
                "%7", PaneCursor(2, 1, 4), 4242, wait=lambda seconds: None
            )
        self.assertEqual(calls[-1][0][0], "delete-buffer")


class ClassificationTests(unittest.TestCase):
    def test_network_and_overload_errors_are_eligible(self) -> None:
        self.assertEqual(
            classify_session_error(
                {
                    "type": "error",
                    "codex_error_info": "other",
                    "message": "stream disconnected before completion: stream closed before response.completed",
                }
            ),
            EligibleError("network", "responseStreamDisconnected"),
        )
        self.assertEqual(
            classify_session_error(
                {
                    "type": "error",
                    "codex_error_info": {
                        "response_too_many_failed_attempts": {"http_status_code": 429}
                    },
                    "message": "exceeded retry limit, last status: 429 Too Many Requests",
                }
            ),
            EligibleError("overload", "responseTooManyFailedAttempts"),
        )
        self.assertEqual(
            classify_session_error(
                {
                    "type": "error",
                    "codex_error_info": "other",
                    "message": (
                        "Selected model is at capacity. "
                        "Please try a different model."
                    ),
                }
            ),
            EligibleError("overload", "serverOverloaded"),
        )

    def test_quota_auth_and_policy_errors_are_excluded(self) -> None:
        for message in (
            "usage limit reached",
            "unexpected status 403 Forbidden: 余额不足",
            "authentication failed",
            "context window exceeded",
        ):
            with self.subTest(message=message):
                self.assertIsNone(
                    classify_session_error(
                        {
                            "type": "error",
                            "codex_error_info": "other",
                            "message": message,
                        }
                    )
                )

    def test_internal_retry_error_is_not_terminal(self) -> None:
        self.assertIsNone(
            classify_session_error(
                {
                    "type": "error",
                    "willRetry": True,
                    "codex_error_info": "serverOverloaded",
                    "message": "server overloaded",
                }
            )
        )


class SqliteLogTests(unittest.TestCase):
    def test_cursor_accepts_current_user_owned_ccb_log_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared_home = root / "shared"
            target = _write_log_db(shared_home)
            row_id = _append_log(
                target,
                turn_id="old-turn",
                message="stream disconnected before completion",
            )
            codex_home = root / "managed-home"
            codex_home.mkdir()
            (codex_home / "logs_2.sqlite").symlink_to(target)

            source = initial_codex_log_cursor(codex_home, THREAD_ID)

        self.assertEqual(source, (target.resolve(), row_id))

    def test_cursor_excludes_history_and_reader_returns_real_terminal_shape(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary) / "codex-home"
            log_path = _write_log_db(codex_home)
            old_id = _append_log(
                log_path,
                turn_id="old-turn",
                message="stream disconnected before completion",
            )
            source = initial_codex_log_cursor(codex_home, THREAD_ID)
            self.assertEqual(source, (log_path.resolve(), old_id))
            _append_log(
                log_path,
                turn_id="internal-retry",
                message="tls handshake eof",
                target="codex_api::endpoint::responses_websocket",
            )
            terminal_id = _append_log(
                log_path,
                turn_id="failed-turn",
                message=(
                    "stream disconnected before completion: error sending request "
                    "for url (https://chatgpt.com/backend-api/codex/responses)"
                ),
            )
            cursor, records = read_codex_terminal_logs(log_path, THREAD_ID, old_id)
        self.assertEqual(cursor, terminal_id)
        self.assertEqual(
            records,
            [
                TerminalLogError(
                    terminal_id,
                    "failed-turn",
                    "stream disconnected before completion: error sending request "
                    "for url (https://chatgpt.com/backend-api/codex/responses)",
                )
            ],
        )

    def test_reader_ignores_other_threads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary) / "codex-home"
            log_path = _write_log_db(codex_home)
            _append_log(
                log_path,
                thread_id="other-thread",
                turn_id="other-turn",
                message="stream disconnected before completion",
            )
            cursor, records = read_codex_terminal_logs(log_path, THREAD_ID, 0)
        self.assertEqual(cursor, 0)
        self.assertEqual(records, [])


class TrackerTests(unittest.TestCase):
    def test_terminal_error_requires_task_complete(self) -> None:
        tracker = SessionEventTracker()
        self.assertEqual(
            tracker.observe(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-1"},
                }
            ),
            [],
        )
        self.assertEqual(
            tracker.observe(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "error",
                        "codex_error_info": "other",
                        "message": "stream disconnected before completion",
                    },
                }
            ),
            [],
        )
        actions = tracker.observe(
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1"},
            }
        )
        self.assertEqual([action.kind for action in actions], ["incident"])
        self.assertEqual(actions[0].incident.turn_id, "turn-1")

    def test_new_progress_cancels_incident_and_exact_off_stops(self) -> None:
        tracker = SessionEventTracker()
        tracker.incident = tracker_incident = Incident(
            "turn-1", "network", "responseStreamDisconnected"
        )
        actions = tracker.observe(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-2"},
            }
        )
        self.assertEqual(
            [action.kind for action in actions], ["cancelled_new_progress"]
        )
        self.assertIsNot(tracker.incident, tracker_incident)
        actions = tracker.observe(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "$reconnect off"},
            }
        )
        self.assertEqual([action.kind for action in actions], ["stop"])

    def test_any_recovery_error_opens_circuit(self) -> None:
        tracker = SessionEventTracker()
        tracker.mark_injected()
        tracker.observe(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "recovery-turn"},
            }
        )
        tracker.observe(
            {
                "type": "event_msg",
                "payload": {
                    "type": "error",
                    "codex_error_info": "other",
                    "message": "authentication failed",
                },
            }
        )
        actions = tracker.observe(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "recovery-turn",
                },
            }
        )
        self.assertEqual([action.kind for action in actions], ["circuit_open"])

    def test_terminal_sqlite_error_can_arrive_after_task_complete(self) -> None:
        tracker = SessionEventTracker()
        tracker.observe(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "failed-turn"},
            }
        )
        self.assertEqual(
            [
                action.kind
                for action in tracker.observe(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "failed-turn",
                            "last_agent_message": None,
                        },
                    }
                )
            ],
            ["turn_complete"],
        )
        actions = tracker.observe_terminal_log_error(
            "failed-turn",
            "stream disconnected before completion: error sending request for url",
        )
        self.assertEqual([action.kind for action in actions], ["incident"])

    def test_task_complete_nested_capacity_error_is_eligible(self) -> None:
        tracker = SessionEventTracker()
        tracker.observe(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "capacity-turn"},
            }
        )
        actions = tracker.observe(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "capacity-turn",
                    "last_agent_message": None,
                    "error": {
                        "message": (
                            "Selected model is at capacity. "
                            "Please try a different model."
                        ),
                        "codex_error_info": "server_overloaded",
                    },
                },
            }
        )
        self.assertEqual([action.kind for action in actions], ["incident"])
        self.assertEqual(
            actions[0].incident,
            Incident("capacity-turn", "overload", "serverOverloaded"),
        )
        self.assertEqual(
            tracker.observe_terminal_log_error(
                "capacity-turn",
                "Selected model is at capacity. Please try a different model.",
            ),
            [],
        )

    def test_task_complete_nested_retry_error_does_not_create_incident(self) -> None:
        tracker = SessionEventTracker()
        tracker.observe(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "retry-turn"},
            }
        )
        actions = tracker.observe(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "retry-turn",
                    "error": {
                        "message": "server overloaded",
                        "codex_error_info": "server_overloaded",
                        "willRetry": True,
                    },
                },
            }
        )
        self.assertEqual([action.kind for action in actions], ["turn_complete"])
        self.assertIsNone(tracker.incident)

    def test_terminal_capacity_sqlite_error_is_eligible(self) -> None:
        tracker = SessionEventTracker()
        tracker.observe(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "capacity-turn"},
            }
        )
        tracker.observe(
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "capacity-turn"},
            }
        )
        actions = tracker.observe_terminal_log_error(
            "capacity-turn",
            "Selected model is at capacity. " "Please try a different model.",
        )
        self.assertEqual([action.kind for action in actions], ["incident"])
        self.assertEqual(
            tracker.observe_terminal_log_error(
                "capacity-turn",
                "Selected model is at capacity. Please try a different model.",
            ),
            [],
        )

    def test_newer_turn_rejects_delayed_sqlite_error(self) -> None:
        tracker = SessionEventTracker()
        for turn_id in ("failed-turn", "newer-turn"):
            tracker.observe(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn_id},
                }
            )
            tracker.observe(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": turn_id},
                }
            )
        self.assertEqual(
            tracker.observe_terminal_log_error(
                "failed-turn", "stream disconnected before completion"
            ),
            [],
        )

    def test_any_terminal_sqlite_error_opens_recovery_circuit(self) -> None:
        tracker = SessionEventTracker()
        tracker.mark_injected()
        tracker.observe(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "recovery-turn"},
            }
        )
        tracker.observe(
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "recovery-turn"},
            }
        )
        actions = tracker.observe_terminal_log_error(
            "recovery-turn", "authentication failed"
        )
        self.assertEqual([action.kind for action in actions], ["circuit_open"])


class _FakeProcess:
    pid = 98765


class EnableDisableTests(unittest.TestCase):
    def test_enable_uses_active_provider_route_for_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            _write_session(codex_home)
            (codex_home / "config.toml").write_text(
                'model_provider = "custom"\n'
                '[model_providers.custom]\n'
                'base_url = "https://provider.example.test/v1"\n',
                encoding="utf-8",
            )
            state = enable_current(
                state_dir=root / "state",
                environment=_environment(codex_home),
                tmux_runner=_tmux_runner,
                process_factory=lambda *args, **kwargs: _FakeProcess(),
            )
            self.assertEqual(
                state.openai_probe_url, "https://provider.example.test/v1"
            )

    def test_enable_starts_one_bound_watcher_and_disable_is_thread_scoped(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def process_factory(command: list[str], **kwargs: object) -> _FakeProcess:
            calls.append((command, kwargs))
            return _FakeProcess()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            _write_session(codex_home)
            state = enable_current(
                state_dir=root / "state",
                environment=_environment(codex_home),
                tmux_runner=_tmux_runner,
                process_factory=process_factory,
            )
            self.assertTrue(state.enabled)
            self.assertEqual(state.pane_id, "%7")
            self.assertEqual(state.watcher_pid, _FakeProcess.pid)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("CODEX_THREAD_ID", calls[0][1]["env"])
            disabled = disable_current(
                state_dir=root / "state",
                environment=_environment(codex_home),
                tmux_runner=_tmux_runner,
            )
            self.assertIsNotNone(disabled)
            self.assertFalse(disabled.enabled)

    def test_enable_passes_current_sqlite_cursor_to_watcher(self) -> None:
        calls: list[list[str]] = []

        def process_factory(command: list[str], **kwargs: object) -> _FakeProcess:
            calls.append(command)
            return _FakeProcess()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            _write_session(codex_home)
            log_path = _write_log_db(codex_home)
            row_id = _append_log(
                log_path,
                turn_id="historical-turn",
                message="stream disconnected before completion",
            )
            enable_current(
                state_dir=root / "state",
                environment=_environment(codex_home),
                tmux_runner=_tmux_runner,
                process_factory=process_factory,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("--log-file") + 1], str(log_path))
        self.assertEqual(calls[0][calls[0].index("--log-cursor") + 1], str(row_id))

    def test_same_thread_supersedes_live_watcher_after_pane_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            codex_home = root / "codex-home"
            session = _write_session(codex_home)
            state_path = watch_state_path(state_dir, THREAD_ID)
            save_watch_state(
                state_path,
                WatchState(
                    schema_version=1,
                    instance_id="old-instance",
                    enabled=True,
                    status="armed",
                    thread_id=THREAD_ID,
                    codex_home=str(codex_home),
                    session_path=str(session),
                    tmux_socket="/tmp/codex-reconnect-test.sock",
                    pane_id="%7",
                    pane_pid=4100,
                    pane_command="node",
                    session_offset=session.stat().st_size,
                    watcher_pid=os.getpid(),
                    openai_probe_url="https://openai.test/probe",
                    public_probe_url=None,
                    probe_timeout=0.1,
                    updated_at=time.time(),
                    last_error=None,
                ),
            )

            state = enable_current(
                state_dir=state_dir,
                environment=_environment(codex_home),
                tmux_runner=_tmux_runner,
                process_factory=lambda *args, **kwargs: _FakeProcess(),
            )
            audit = state_path.with_suffix(".audit.jsonl").read_text(encoding="utf-8")

        self.assertNotEqual(state.instance_id, "old-instance")
        self.assertEqual(state.pane_pid, 4242)
        self.assertEqual(state.watcher_pid, _FakeProcess.pid)
        self.assertIn('"event":"watcher_superseded_pane_restart"', audit)
        self.assertIn('"oldPanePid":4100', audit)
        self.assertIn('"newPanePid":4242', audit)

    def test_same_thread_live_watcher_in_different_pane_remains_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            codex_home = root / "codex-home"
            session = _write_session(codex_home)
            state_path = watch_state_path(state_dir, THREAD_ID)
            save_watch_state(
                state_path,
                WatchState(
                    schema_version=1,
                    instance_id="other-pane-instance",
                    enabled=True,
                    status="armed",
                    thread_id=THREAD_ID,
                    codex_home=str(codex_home),
                    session_path=str(session),
                    tmux_socket="/tmp/codex-reconnect-test.sock",
                    pane_id="%8",
                    pane_pid=4100,
                    pane_command="node",
                    session_offset=session.stat().st_size,
                    watcher_pid=os.getpid(),
                    openai_probe_url="https://openai.test/probe",
                    public_probe_url=None,
                    probe_timeout=0.1,
                    updated_at=time.time(),
                    last_error=None,
                ),
            )

            with self.assertRaisesRegex(TmuxWatchError, "already armed in pane %8"):
                enable_current(
                    state_dir=state_dir,
                    environment=_environment(codex_home),
                    tmux_runner=_tmux_runner,
                    process_factory=lambda *args, **kwargs: _FakeProcess(),
                )
            self.assertEqual(
                load_watch_state(state_path).instance_id, "other-pane-instance"
            )

    def test_two_threads_create_independent_watcher_state(self) -> None:
        class Process:
            def __init__(self, pid: int):
                self.pid = pid

        next_pid = iter((91001, 91002))

        def process_factory(command: list[str], **kwargs: object) -> Process:
            return Process(next(next_pid))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            states = []
            for index, (thread_id, pane_id) in enumerate(
                (("thread-one", "%7"), ("thread-two", "%8")), start=1
            ):
                codex_home = root / f"codex-home-{index}"
                _write_session(codex_home, thread_id=thread_id)
                environment = _environment(codex_home)
                environment["CODEX_THREAD_ID"] = thread_id
                environment["TMUX_PANE"] = pane_id

                def runner(
                    command: list[str],
                    *,
                    expected_pane: str = pane_id,
                    **kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=f"{expected_pane}|4242|node|0\n",
                        stderr="",
                    )

                states.append(
                    enable_current(
                        state_dir=state_dir,
                        environment=environment,
                        tmux_runner=runner,
                        process_factory=process_factory,
                    )
                )
            self.assertNotEqual(states[0].thread_id, states[1].thread_id)
            self.assertNotEqual(states[0].pane_id, states[1].pane_id)
            self.assertTrue(watch_state_path(state_dir, "thread-one").is_file())
            self.assertTrue(watch_state_path(state_dir, "thread-two").is_file())

    def test_new_thread_supersedes_old_watcher_in_same_pane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            codex_home = root / "codex-home"
            _write_session(codex_home)
            old_path = watch_state_path(state_dir, "old-thread")
            save_watch_state(
                old_path,
                WatchState(
                    schema_version=1,
                    instance_id="old-instance",
                    enabled=True,
                    status="armed",
                    thread_id="old-thread",
                    codex_home=str(codex_home),
                    session_path=str(codex_home / "old.jsonl"),
                    tmux_socket="/tmp/codex-reconnect-test.sock",
                    pane_id="%7",
                    pane_pid=4242,
                    pane_command="node",
                    session_offset=0,
                    watcher_pid=os.getpid(),
                    openai_probe_url="https://openai.test/probe",
                    public_probe_url=None,
                    probe_timeout=0.1,
                    updated_at=time.time(),
                    last_error=None,
                ),
            )
            enable_current(
                state_dir=state_dir,
                environment=_environment(codex_home),
                tmux_runner=_tmux_runner,
                process_factory=lambda *args, **kwargs: _FakeProcess(),
            )
            old_state = load_watch_state(old_path)
            old_audit = old_path.with_suffix(".audit.jsonl").read_text(encoding="utf-8")
        self.assertFalse(old_state.enabled)
        self.assertEqual(old_state.status, "superseded")
        self.assertIn(
            '"event":"watcher_superseded_pane_reuse"',
            old_audit,
        )


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.lock = threading.Lock()

    def monotonic(self) -> float:
        with self.lock:
            return self.value

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.value += max(seconds, 0.01)
        time.sleep(0.001)


class _FakeTmux:
    def __init__(self) -> None:
        self.sent = threading.Event()
        self.send_count = 0
        self.settle_delays: list[float] = []
        self.prompt = (
            "\n\x1b[1m›\x1b[0m\x1b[48;2;61;64;64m "
            "\x1b[2mImplement {feature}\x1b[0m\n\n  model · cwd\n"
        )
        self.cursor = PaneCursor(2, 1, 4)

    def query_pane(self, pane_id: str) -> PaneIdentity:
        return PaneIdentity(pane_id, 4242, "node")

    def capture_pane_styled(self, pane_id: str) -> str:
        return self.prompt

    def query_cursor(self, pane_id: str) -> PaneCursor:
        return self.cursor

    def send_continue_if_cursor(
        self,
        pane_id: str,
        cursor: PaneCursor,
        pane_pid: int,
        *,
        wait: Callable[[float], None] = time.sleep,
        settle_seconds: float = 0.5,
    ) -> bool:
        if self.cursor != cursor or pane_pid != 4242:
            return False
        self.prompt = f"\n\x1b[1m›\x1b[0m {CONTINUE_TEXT}\n\n  model · cwd\n"
        self.cursor = PaneCursor(cursor.x + len(CONTINUE_TEXT), cursor.y, cursor.height)
        self.settle_delays.append(settle_seconds)
        wait(settle_seconds)
        self.send_count += 1
        self.sent.set()
        return True


class WatcherIntegrationTests(unittest.TestCase):
    def test_shutdown_signal_disables_current_watcher_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = _write_session(root / "codex-home")
            state_path, state = _state(root, session)
            watcher = SessionWatcher(
                state_path, state.instance_id, tmux_client=_FakeTmux()
            )

            with self.assertRaisesRegex(WatchStopped, "SIGTERM"):
                watcher._stop_for_signal(signal.SIGTERM)

            stopped = load_watch_state(state_path)
            audit = state_path.with_suffix(".audit.jsonl").read_text(encoding="utf-8")

        self.assertFalse(stopped.enabled)
        self.assertEqual(stopped.status, "off")
        self.assertIn('"event":"watcher_stopped_by_signal"', audit)
        self.assertIn('"signal":"SIGTERM"', audit)

    def test_startup_arming_retries_until_empty_prompt_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = _write_session(root / "codex-home")
            state_path, state = _state(root, session)
            fake_tmux = _FakeTmux()
            ready_prompt = fake_tmux.prompt
            ready_cursor = fake_tmux.cursor
            fake_tmux.prompt = "\nBooting Codex\n"
            fake_tmux.cursor = PaneCursor(0, 1, 2)
            clock = _FakeClock()
            watcher = SessionWatcher(
                state_path,
                state.instance_id,
                tmux_client=fake_tmux,
                poll_interval=0.01,
                monotonic=clock.monotonic,
                wait=clock.sleep,
            )
            thread = threading.Thread(target=watcher.run)
            thread.start()
            time.sleep(0.03)
            self.assertEqual(load_watch_state(state_path).status, "arming")

            fake_tmux.prompt = ready_prompt
            fake_tmux.cursor = ready_cursor
            deadline = time.time() + 3
            while time.time() < deadline:
                if load_watch_state(state_path).status == "armed":
                    break
                time.sleep(0.01)
            self.assertEqual(load_watch_state(state_path).status, "armed")

            modify_watch_state(
                state_path,
                state.instance_id,
                lambda current: replace(
                    current, enabled=False, status="off", updated_at=time.time()
                ),
            )
            thread.join(3)
            self.assertFalse(thread.is_alive())
            audit = state_path.with_suffix(".audit.jsonl").read_text(encoding="utf-8")
            self.assertEqual(audit.count('"event":"empty_prompt_learned"'), 1)

    def test_lazily_created_sqlite_log_is_discovered_after_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            session = _write_session(codex_home)
            state_path, state = _state(root, session)
            watcher = SessionWatcher(
                state_path,
                state.instance_id,
                tmux_client=_FakeTmux(),
                poll_interval=0,
            )
            watcher.tracker.observe(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "failed-turn"},
                }
            )
            watcher.tracker.observe(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "failed-turn"},
                }
            )
            self.assertFalse(watcher._consume_log_errors())

            log_path = _write_log_db(codex_home)
            _append_log(
                log_path,
                turn_id="failed-turn",
                message="stream disconnected before completion",
            )

            self.assertTrue(watcher._consume_log_errors())
            self.assertIsNotNone(watcher.tracker.incident)
            self.assertEqual(watcher.tracker.incident.turn_id, "failed-turn")

    def test_terminal_disconnect_waits_for_two_successes_then_injects_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = _write_session(root / "codex-home")
            state_path, state = _state(root, session)
            fake_tmux = _FakeTmux()
            clock = _FakeClock()
            results = iter(
                [
                    ProbeResult("https://openai.test", False, None, 0.01, "down"),
                    ProbeResult("https://openai.test", True, 405, 0.01),
                    ProbeResult("https://openai.test", True, 405, 0.01),
                ]
            )
            watcher = SessionWatcher(
                state_path,
                state.instance_id,
                tmux_client=fake_tmux,
                primary_probe=lambda url, timeout: next(results),
                public_probe=lambda url, timeout: ProbeResult(url, True, 204, 0.01),
                poll_interval=0.01,
                monotonic=clock.monotonic,
                wait=clock.sleep,
                random_value=lambda: 0.0,
            )
            thread = threading.Thread(target=watcher.run)
            thread.start()
            _append_event(
                session, {"type": "task_complete", "turn_id": "activation-turn"}
            )
            time.sleep(0.03)
            self.assertEqual(load_watch_state(state_path).status, "armed")
            fake_tmux.prompt = (
                "\n\x1b[1m›\x1b[0m\x1b[48;2;61;64;64m "
                "\x1b[2mFind and fix a bug in @filename\x1b[0m\n"
                "\n  model · cwd\n"
            )
            _append_event(session, {"type": "task_started", "turn_id": "failed-turn"})
            _append_event(
                session,
                {
                    "type": "error",
                    "codex_error_info": "other",
                    "message": "stream disconnected before completion: stream closed before response.completed",
                },
            )
            _append_event(session, {"type": "task_complete", "turn_id": "failed-turn"})
            self.assertTrue(
                fake_tmux.sent.wait(3),
                msg=(
                    load_watch_state(state_path),
                    state_path.with_suffix(".audit.jsonl").read_text(encoding="utf-8"),
                    state_path.with_suffix(".watcher.log").read_text(encoding="utf-8")
                    if state_path.with_suffix(".watcher.log").exists()
                    else "",
                ),
            )
            self.assertEqual(fake_tmux.send_count, 1)
            self.assertEqual(fake_tmux.settle_delays, [0.5])
            modify_watch_state(
                state_path,
                state.instance_id,
                lambda current: replace(
                    current,
                    enabled=False,
                    status="off",
                    updated_at=time.time(),
                ),
            )
            thread.join(3)
            self.assertFalse(thread.is_alive())
            audit = state_path.with_suffix(".audit.jsonl").read_text(encoding="utf-8")
            self.assertEqual(audit.count('"event":"network_probe"'), 3)
            self.assertEqual(audit.count('"event":"continue_submitted"'), 1)

    def test_real_sqlite_turn_error_plus_jsonl_completion_injects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            session = _write_session(codex_home)
            log_path = _write_log_db(codex_home)
            state_path, state = _state(root, session)
            fake_tmux = _FakeTmux()
            clock = _FakeClock()
            watcher = SessionWatcher(
                state_path,
                state.instance_id,
                tmux_client=fake_tmux,
                primary_probe=lambda url, timeout: ProbeResult(url, True, 405, 0.01),
                poll_interval=0.01,
                monotonic=clock.monotonic,
                wait=clock.sleep,
                random_value=lambda: 0.0,
                log_path=log_path,
                log_cursor=0,
            )
            thread = threading.Thread(target=watcher.run)
            thread.start()
            _append_event(session, {"type": "task_started", "turn_id": "failed-turn"})
            _append_event(
                session,
                {
                    "type": "task_complete",
                    "turn_id": "failed-turn",
                    "last_agent_message": None,
                },
            )
            time.sleep(0.03)
            _append_log(
                log_path,
                turn_id="failed-turn",
                message=(
                    "stream disconnected before completion: error sending request "
                    "for url (https://chatgpt.com/backend-api/codex/responses)"
                ),
            )
            self.assertTrue(fake_tmux.sent.wait(3))
            self.assertEqual(fake_tmux.send_count, 1)
            self.assertEqual(fake_tmux.settle_delays, [0.5])
            modify_watch_state(
                state_path,
                state.instance_id,
                lambda current: replace(
                    current, enabled=False, status="off", updated_at=time.time()
                ),
            )
            thread.join(3)
            self.assertFalse(thread.is_alive())
            audit = state_path.with_suffix(".audit.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event":"terminal_log_error_observed"', audit)
            self.assertIn('"event":"continue_submitted"', audit)

    def test_nested_capacity_completion_waits_for_two_probes_then_injects_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            session = _write_session(codex_home)
            state_path, state = _state(root, session)
            fake_tmux = _FakeTmux()
            clock = _FakeClock()
            watcher = SessionWatcher(
                state_path,
                state.instance_id,
                tmux_client=fake_tmux,
                primary_probe=lambda url, timeout: ProbeResult(url, True, 405, 0.01),
                poll_interval=0.01,
                monotonic=clock.monotonic,
                wait=clock.sleep,
                random_value=lambda: 0.0,
            )
            thread = threading.Thread(target=watcher.run)
            thread.start()
            _append_event(session, {"type": "task_started", "turn_id": "capacity-turn"})
            _append_event(
                session,
                {
                    "type": "task_complete",
                    "turn_id": "capacity-turn",
                    "last_agent_message": None,
                    "error": {
                        "message": (
                            "Selected model is at capacity. "
                            "Please try a different model."
                        ),
                        "codex_error_info": "server_overloaded",
                    },
                },
            )
            self.assertTrue(fake_tmux.sent.wait(3))
            self.assertEqual(fake_tmux.send_count, 1)
            self.assertEqual(fake_tmux.settle_delays, [0.5])
            modify_watch_state(
                state_path,
                state.instance_id,
                lambda current: replace(
                    current, enabled=False, status="off", updated_at=time.time()
                ),
            )
            thread.join(3)
            self.assertFalse(thread.is_alive())
            audit = state_path.with_suffix(".audit.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event":"terminal_failure_observed"', audit)
            self.assertIn('"failureKind":"overload"', audit)
            self.assertEqual(audit.count('"event":"network_probe"'), 2)
            self.assertEqual(audit.count('"event":"continue_submitted"'), 1)

    def test_changed_prompt_refuses_input_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = _write_session(root / "codex-home")
            state_path, state = _state(root, session)
            fake_tmux = _FakeTmux()
            watcher = SessionWatcher(
                state_path,
                state.instance_id,
                tmux_client=fake_tmux,
                primary_probe=lambda url, timeout: ProbeResult(url, True, 405, 0.01),
                poll_interval=0,
                stable_successes=1,
                random_value=lambda: 0.0,
            )
            watcher.empty_prompt_observed = True
            fake_tmux.prompt = "\n\x1b[1m›\x1b[0m partially typed user input\n\n"
            fake_tmux.cursor = PaneCursor(2, 1, 3)
            tracker = watcher.tracker
            tracker.incident = Incident(
                "failed-turn", "network", "responseStreamDisconnected"
            )
            watcher.next_probe_at = 0
            with session.open("rb") as handle:
                handle.seek(state.session_offset)
                watcher._maybe_probe_and_inject(handle)
            self.assertEqual(fake_tmux.send_count, 0)
            self.assertIsNone(tracker.incident)

    def test_cursor_change_between_prompt_check_and_send_refuses_injection(
        self,
    ) -> None:
        class CursorRaceTmux(_FakeTmux):
            def send_continue_if_cursor(
                self,
                pane_id: str,
                cursor: PaneCursor,
                pane_pid: int,
                **kwargs: object,
            ) -> bool:
                self.cursor = PaneCursor(3, cursor.y, cursor.height)
                return super().send_continue_if_cursor(
                    pane_id, cursor, pane_pid, **kwargs
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = _write_session(root / "codex-home")
            state_path, state = _state(root, session)
            fake_tmux = CursorRaceTmux()
            watcher = SessionWatcher(
                state_path,
                state.instance_id,
                tmux_client=fake_tmux,
                primary_probe=lambda url, timeout: ProbeResult(url, True, 405, 0.01),
                stable_successes=1,
                random_value=lambda: 0.0,
            )
            watcher.empty_prompt_observed = True
            watcher.tracker.incident = Incident(
                "failed-turn", "network", "responseStreamDisconnected"
            )
            with session.open("rb") as handle:
                handle.seek(state.session_offset)
                watcher._maybe_probe_and_inject(handle)
            audit = state_path.with_suffix(".audit.jsonl").read_text(encoding="utf-8")
        self.assertEqual(fake_tmux.send_count, 0)
        self.assertIn('"event":"recovery_skipped_input_race"', audit)

    def test_exact_reconnect_off_during_network_wait_stops_without_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = _write_session(root / "codex-home")
            state_path, state = _state(root, session)
            fake_tmux = _FakeTmux()
            clock = _FakeClock()
            watcher = SessionWatcher(
                state_path,
                state.instance_id,
                tmux_client=fake_tmux,
                primary_probe=lambda url, timeout: ProbeResult(
                    url, False, None, 0.01, "down"
                ),
                public_probe=lambda url, timeout: ProbeResult(
                    url, False, None, 0.01, "down"
                ),
                poll_interval=0.01,
                monotonic=clock.monotonic,
                wait=clock.sleep,
                random_value=lambda: 0.0,
            )
            thread = threading.Thread(target=watcher.run)
            thread.start()
            _append_event(session, {"type": "task_started", "turn_id": "failed-turn"})
            _append_event(
                session,
                {
                    "type": "error",
                    "codex_error_info": "other",
                    "message": "stream disconnected before completion",
                },
            )
            _append_event(session, {"type": "task_complete", "turn_id": "failed-turn"})
            time.sleep(0.03)
            _append_event(
                session, {"type": "user_message", "message": "$reconnect off"}
            )
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(load_watch_state(state_path).enabled)
            self.assertEqual(fake_tmux.send_count, 0)

    def test_changed_pane_identity_fails_closed(self) -> None:
        class ChangedPaneTmux(_FakeTmux):
            def query_pane(self, pane_id: str) -> PaneIdentity:
                return PaneIdentity(pane_id, 9999, "node")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = _write_session(root / "codex-home")
            state_path, state = _state(root, session)
            fake_tmux = ChangedPaneTmux()
            watcher = SessionWatcher(
                state_path,
                state.instance_id,
                tmux_client=fake_tmux,
                primary_probe=lambda url, timeout: ProbeResult(url, True, 405, 0.01),
                stable_successes=1,
                random_value=lambda: 0.0,
            )
            watcher.empty_prompt_observed = True
            watcher.tracker.incident = Incident(
                "failed-turn", "network", "responseStreamDisconnected"
            )
            with session.open("rb") as handle:
                handle.seek(state.session_offset)
                with self.assertRaisesRegex(TmuxWatchError, "pane identity changed"):
                    watcher._maybe_probe_and_inject(handle)
            self.assertEqual(fake_tmux.send_count, 0)


if __name__ == "__main__":
    unittest.main()
