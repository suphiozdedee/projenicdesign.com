from __future__ import annotations

import base64
import json
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Callable


TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT))

from codex_reconnect.control import (
    ControlError,
    SessionControl,
    load_control,
    save_control,
    set_session_control,
)
from codex_reconnect.managed import JsonlAuditLog, TransparentAppServerBridge
from codex_reconnect.network import ProbeResult
from codex_reconnect.recovery import DisconnectRecoverySupervisor, RECOVERY_PROMPT


FAKE_BRIDGE_SERVER = Path(__file__).with_name("fake_bridge_app_server.py")


def _receive_matching(
    client: _WebSocketClient,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    max_messages: int = 100,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    interleaved: list[dict[str, Any]] = []
    for _ in range(max_messages):
        message = client.receive()
        if predicate(message):
            return message, interleaved
        interleaved.append(message)
    raise AssertionError(f"matching message not received; interleaved={interleaved!r}")


def _receive_response(
    client: _WebSocketClient, request_id: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return _receive_matching(client, lambda message: message.get("id") == request_id)


def _open_overload_bridge(
    root: Path,
) -> tuple[TransparentAppServerBridge, _WebSocketClient, Path, Path]:
    skill_root = root / "skills"
    (skill_root / "reconnect").mkdir(parents=True)
    (skill_root / "reconnect" / "SKILL.md").write_text("test", encoding="utf-8")
    control_path = root / "control.json"
    audit_path = root / "audit.jsonl"
    save_control(control_path, SessionControl.disabled("overload-instance"))
    bridge = TransparentAppServerBridge(
        app_server_command=[
            sys.executable,
            str(FAKE_BRIDGE_SERVER),
            "server-overloaded",
        ],
        socket_path=root / "bridge.sock",
        skill_root=skill_root,
        control_path=control_path,
        instance_id="overload-instance",
        audit_log=JsonlAuditLog(audit_path),
        app_server_stderr_path=root / "stderr.log",
        openai_probe_url="https://openai.test",
        public_probe_url=None,
    )
    bridge.start()
    client = _WebSocketClient(root / "bridge.sock")
    client.connect()
    assert client.socket is not None
    client.socket.settimeout(5)
    client.send({"id": 1, "method": "initialize", "params": {}})
    initialized, _ = _receive_response(client, 1)
    if "error" in initialized:
        raise AssertionError(initialized)
    client.send({"method": "initialized", "params": {}})
    client.send(
        {
            "id": 2,
            "method": "thread/start",
            "params": {"model": "gpt-overload-test", "cwd": str(root)},
        }
    )
    started, _ = _receive_response(client, 2)
    if "error" in started:
        raise AssertionError(started)
    client.send(
        {
            "id": 3,
            "method": "turn/start",
            "params": {
                "threadId": "thread-1",
                "model": "gpt-overload-test",
                "input": [{"type": "text", "text": "$reconnect on"}],
            },
        }
    )
    armed, _ = _receive_response(client, 3)
    if "error" in armed or not load_control(control_path).enabled:
        raise AssertionError(armed)
    return bridge, client, control_path, audit_path


def _read_audit(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


class ControlTests(unittest.TestCase):
    def test_current_session_toggle_is_atomic_and_instance_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "control.json"
            save_control(path, SessionControl.disabled("instance-1"))
            enabled = set_session_control(
                path,
                instance_id="instance-1",
                session_id="thread-1",
                enabled=True,
            )
            self.assertTrue(enabled.enabled)
            self.assertEqual(load_control(path).session_id, "thread-1")
            with self.assertRaises(ControlError):
                load_control(path, expected_instance_id="another-instance")
            disabled = set_session_control(
                path,
                instance_id="instance-1",
                session_id="thread-1",
                enabled=False,
            )
            self.assertFalse(disabled.enabled)


class RecoveryTests(unittest.TestCase):
    def _supervisor(
        self,
        temporary: str,
        *,
        read_turns: list[dict[str, Any]],
        primary_results: list[ProbeResult] | None = None,
    ) -> tuple[
        DisconnectRecoverySupervisor,
        list[tuple[str, dict[str, Any]]],
        list[str],
        Path,
    ]:
        control_path = Path(temporary) / "control.json"
        save_control(control_path, SessionControl.disabled("instance-1"))
        set_session_control(
            control_path,
            instance_id="instance-1",
            session_id="thread-1",
            enabled=True,
        )
        calls: list[tuple[str, dict[str, Any]]] = []
        notices: list[str] = []

        def rpc(method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
            calls.append((method, params))
            if method == "thread/read":
                return {"thread": {"id": "thread-1", "turns": read_turns}}
            if method == "turn/start":
                return {"turn": {"id": "recovery-turn", "status": "inProgress"}}
            return {}

        results = list(
            primary_results
            or [
                ProbeResult("https://openai.test", True, 405, 0.01),
                ProbeResult("https://openai.test", True, 405, 0.01),
            ]
        )

        def primary_probe(url: str, timeout: float) -> ProbeResult:
            return results.pop(0)

        supervisor = DisconnectRecoverySupervisor(
            rpc=rpc,
            notify=lambda thread_id, message: notices.append(message),
            log=lambda event, fields: None,
            model_for_thread=lambda thread_id: "gpt-test",
            control_path=control_path,
            instance_id="instance-1",
            stop_event=threading.Event(),
            openai_probe_url="https://openai.test",
            public_probe_url=None,
            primary_probe=primary_probe,
            wait=lambda seconds: None,
        )
        return supervisor, calls, notices, control_path

    def test_network_failure_waits_for_two_successes_then_reconciles_once(self) -> None:
        failed = {
            "id": "failed-turn",
            "status": "failed",
            "startedAt": 1,
            "error": {"codexErrorInfo": {"responseStreamDisconnected": {}}},
        }
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, calls, notices, _ = self._supervisor(
                temporary,
                read_turns=[failed],
                primary_results=[
                    ProbeResult("https://openai.test", False, None, 0.01, "offline"),
                    ProbeResult("https://openai.test", True, 405, 0.01),
                    ProbeResult("https://openai.test", True, 405, 0.01),
                ],
            )
            supervisor.observe(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": failed},
                }
            )
            self.assertTrue(supervisor.wait_for_idle())
            self.assertEqual(
                [method for method, _ in calls], ["thread/read", "turn/start"]
            )
            turn_params = calls[-1][1]
            self.assertEqual(turn_params["threadId"], "thread-1")
            self.assertEqual(turn_params["model"], "gpt-test")
            self.assertEqual(turn_params["input"][0]["text"], RECOVERY_PROMPT)
            self.assertTrue(any("gate passed" in notice for notice in notices))

    def test_normal_completion_and_usage_limit_never_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, calls, _, _ = self._supervisor(temporary, read_turns=[])
            supervisor.observe(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "ok", "status": "completed", "error": None},
                    },
                }
            )
            supervisor.observe(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {
                            "id": "quota",
                            "status": "failed",
                            "error": {"codexErrorInfo": "usageLimitExceeded"},
                        },
                    },
                }
            )
            self.assertTrue(supervisor.wait_for_idle())
            self.assertEqual(calls, [])

    def test_newer_user_turn_wins_reconciliation_race(self) -> None:
        failed = {
            "id": "failed-turn",
            "status": "failed",
            "startedAt": 1,
            "error": {"codexErrorInfo": {"httpConnectionFailed": {}}},
        }
        newer = {"id": "manual-turn", "status": "completed", "startedAt": 2}
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, calls, _, _ = self._supervisor(
                temporary, read_turns=[failed, newer]
            )
            supervisor.observe(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": failed},
                }
            )
            self.assertTrue(supervisor.wait_for_idle())
            self.assertEqual([method for method, _ in calls], ["thread/read"])

    def test_new_client_turn_cancels_recovery_before_reconciliation(self) -> None:
        failed = {
            "id": "failed-turn",
            "status": "failed",
            "startedAt": 1,
            "error": {"codexErrorInfo": {"httpConnectionFailed": {}}},
        }
        with tempfile.TemporaryDirectory() as temporary:
            control_path = Path(temporary) / "control.json"
            save_control(control_path, SessionControl.disabled("instance-1"))
            set_session_control(
                control_path,
                instance_id="instance-1",
                session_id="thread-1",
                enabled=True,
            )
            probe_entered = threading.Event()
            release_probe = threading.Event()
            calls: list[str] = []

            def primary_probe(url: str, timeout: float) -> ProbeResult:
                probe_entered.set()
                if not release_probe.wait(2):
                    raise AssertionError("test did not release network probe")
                return ProbeResult(url, True, 405, 0.01)

            supervisor = DisconnectRecoverySupervisor(
                rpc=lambda method, params, timeout: calls.append(method) or {},
                notify=lambda thread_id, message: None,
                log=lambda event, fields: None,
                model_for_thread=lambda thread_id: "gpt-test",
                control_path=control_path,
                instance_id="instance-1",
                stop_event=threading.Event(),
                openai_probe_url="https://openai.test",
                public_probe_url=None,
                primary_probe=primary_probe,
                wait=lambda seconds: None,
            )
            supervisor.observe(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": failed},
                }
            )
            self.assertTrue(probe_entered.wait(2))
            supervisor.note_client_turn("thread-1")
            release_probe.set()
            self.assertTrue(supervisor.wait_for_idle())
            self.assertEqual(calls, [])

    def test_recovery_turn_failure_opens_circuit(self) -> None:
        failed = {
            "id": "failed-turn",
            "status": "failed",
            "startedAt": 1,
            "error": {"codexErrorInfo": "serverOverloaded"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            supervisor, calls, notices, _ = self._supervisor(
                temporary, read_turns=[failed]
            )
            supervisor.observe(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": failed},
                }
            )
            self.assertTrue(supervisor.wait_for_idle())
            recovery_failed = {
                "id": "recovery-turn",
                "status": "failed",
                "error": {"codexErrorInfo": "unauthorized"},
            }
            supervisor.observe(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": recovery_failed},
                }
            )
            self.assertTrue(supervisor.wait_for_idle())
            self.assertEqual(
                [method for method, _ in calls], ["thread/read", "turn/start"]
            )
            self.assertTrue(
                any("automatic continuation stopped" in notice for notice in notices)
            )


class BridgeTests(unittest.TestCase):
    def test_server_overloaded_end_to_end_retries_once_same_model_and_opens_circuit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge, client, _, audit_path = _open_overload_bridge(root)
            probe_results = [
                ProbeResult("https://openai.test", False, None, 0.01, "offline"),
                ProbeResult("https://openai.test", True, 405, 0.01),
                ProbeResult("https://openai.test", True, 405, 0.01),
            ]
            probe_calls: list[ProbeResult] = []
            wait_calls: list[float] = []
            waited_before_first_probe: list[float] = []

            def primary_probe(url: str, timeout: float) -> ProbeResult:
                if not probe_calls:
                    waited_before_first_probe.append(sum(wait_calls))
                result = probe_results.pop(0)
                probe_calls.append(result)
                return result

            bridge.recovery.primary_probe = primary_probe
            bridge.recovery.wait = wait_calls.append
            try:
                self.assertEqual(
                    bridge.model_for_thread("thread-1"), "gpt-overload-test"
                )
                client.send(
                    {
                        "id": 4,
                        "method": "turn/start",
                        "params": {
                            "threadId": "thread-1",
                            "model": "gpt-overload-test",
                            "input": [{"type": "text", "text": "trigger overload"}],
                        },
                    }
                )
                triggered, _ = _receive_response(client, 4)
                self.assertEqual(triggered["result"]["turn"]["id"], "turn-overload")
                self.assertTrue(bridge.recovery.wait_for_idle())

                client.send({"id": 5, "method": "test/state", "params": {}})
                before_terminal, retry_events = _receive_response(client, 5)
                retry_errors = [
                    event for event in retry_events if event.get("method") == "error"
                ]
                self.assertEqual(len(retry_errors), 1)
                self.assertTrue(retry_errors[0]["params"]["willRetry"])
                self.assertEqual(before_terminal["result"]["threadReads"], 0)
                self.assertEqual(before_terminal["result"]["recoveryStarts"], [])
                self.assertEqual(probe_calls, [])

                client.send({"id": 6, "method": "test/completeOverload", "params": {}})
                completed_request, _ = _receive_response(client, 6)
                self.assertNotIn("error", completed_request)
                recovery_started, terminal_events = _receive_matching(
                    client,
                    lambda message: message.get("method") == "test/recoveryTurnStarted",
                )
                self.assertTrue(bridge.recovery.wait_for_idle())

                final_errors = [
                    event
                    for event in terminal_events
                    if event.get("method") == "error"
                    and event.get("params", {}).get("willRetry") is False
                ]
                terminal_turns = [
                    event
                    for event in terminal_events
                    if event.get("method") == "turn/completed"
                ]
                self.assertEqual(len(final_errors), 1)
                self.assertEqual(len(terminal_turns), 1)
                self.assertEqual(
                    terminal_turns[0]["params"]["turn"]["error"]["codexErrorInfo"],
                    "serverOverloaded",
                )
                recovery_request = recovery_started["params"]["request"]
                self.assertEqual(recovery_started["params"]["count"], 1)
                self.assertEqual(recovery_request["threadId"], "thread-1")
                self.assertEqual(recovery_request["model"], "gpt-overload-test")
                self.assertEqual(
                    recovery_request["input"],
                    [{"type": "text", "text": RECOVERY_PROMPT}],
                )
                self.assertTrue(
                    recovery_request["clientUserMessageId"].startswith("reconnect-")
                )
                self.assertEqual(
                    [result.reachable for result in probe_calls], [False, True, True]
                )
                self.assertEqual(probe_results, [])
                self.assertGreaterEqual(waited_before_first_probe[0], 1.0)

                client.send({"id": 7, "method": "test/state", "params": {}})
                recovered_state, _ = _receive_response(client, 7)
                self.assertEqual(recovered_state["result"]["threadReads"], 1)
                self.assertEqual(len(recovered_state["result"]["recoveryStarts"]), 1)

                client.send({"id": 8, "method": "test/rerouteRecovery", "params": {}})
                _receive_response(client, 8)
                interrupted, reroute_events = _receive_matching(
                    client,
                    lambda message: message.get("method") == "test/recoveryInterrupted",
                )
                self.assertEqual(interrupted["params"]["count"], 1)
                self.assertEqual(interrupted["params"]["threadId"], "thread-1")
                self.assertEqual(interrupted["params"]["turnId"], "turn-recovery")
                self.assertTrue(
                    any(
                        event.get("method") == "warning"
                        and "refused a model reroute"
                        in event.get("params", {}).get("message", "")
                        for event in reroute_events
                    )
                )

                client.send({"id": 9, "method": "test/failRecovery", "params": {}})
                _receive_response(client, 9)
                circuit_warning, _ = _receive_matching(
                    client,
                    lambda message: message.get("method") == "warning"
                    and "automatic continuation stopped"
                    in message.get("params", {}).get("message", ""),
                )
                self.assertEqual(circuit_warning["params"]["threadId"], "thread-1")
                self.assertTrue(bridge.recovery.wait_for_idle())

                client.send({"id": 10, "method": "test/state", "params": {}})
                final_state, _ = _receive_response(client, 10)
                self.assertEqual(final_state["result"]["threadReads"], 1)
                self.assertEqual(len(final_state["result"]["recoveryStarts"]), 1)
                self.assertEqual(len(final_state["result"]["interrupts"]), 1)

                audit = _read_audit(audit_path)
                network_probes = [
                    event for event in audit if event["event"] == "network_probe"
                ]
                self.assertEqual(
                    [event["readiness"] for event in network_probes],
                    ["primary_unavailable", "ready", "ready"],
                )
                recovery_events = [
                    event
                    for event in audit
                    if event["event"] == "recovery_turn_started"
                ]
                self.assertEqual(len(recovery_events), 1)
                self.assertEqual(recovery_events[0]["failureKind"], "overload")
                self.assertEqual(recovery_events[0]["errorClass"], "serverOverloaded")
                self.assertEqual(recovery_events[0]["model"], "gpt-overload-test")
                self.assertEqual(
                    sum(event["event"] == "model_reroute_refused" for event in audit),
                    1,
                )
                self.assertEqual(
                    sum(event["event"] == "recovery_circuit_open" for event in audit),
                    1,
                )
            finally:
                bridge.close()
                client.close()

    def test_server_overloaded_wait_is_cancelled_by_reconnect_off(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge, client, control_path, audit_path = _open_overload_bridge(root)
            wait_entered = threading.Event()
            release_wait = threading.Event()
            probe_calls: list[str] = []

            def blocking_wait(seconds: float) -> None:
                wait_entered.set()
                if not release_wait.wait(3):
                    raise AssertionError("test did not release overload backoff")

            bridge.recovery.wait = blocking_wait
            bridge.recovery.primary_probe = lambda url, timeout: (
                probe_calls.append(url) or ProbeResult(url, True, 405, 0.01)
            )
            try:
                client.send(
                    {
                        "id": 4,
                        "method": "turn/start",
                        "params": {
                            "threadId": "thread-1",
                            "model": "gpt-overload-test",
                            "input": [{"type": "text", "text": "trigger overload"}],
                        },
                    }
                )
                _receive_response(client, 4)
                client.send({"id": 5, "method": "test/completeOverload", "params": {}})
                _receive_response(client, 5)
                self.assertTrue(wait_entered.wait(2))

                client.send(
                    {
                        "id": 6,
                        "method": "turn/start",
                        "params": {
                            "threadId": "thread-1",
                            "model": "gpt-overload-test",
                            "input": [{"type": "text", "text": "$reconnect off"}],
                        },
                    }
                )
                _receive_response(client, 6)
                self.assertFalse(load_control(control_path).enabled)
                release_wait.set()
                self.assertTrue(bridge.recovery.wait_for_idle())

                client.send({"id": 7, "method": "test/state", "params": {}})
                state, _ = _receive_response(client, 7)
                self.assertEqual(state["result"]["threadReads"], 0)
                self.assertEqual(state["result"]["recoveryStarts"], [])
                self.assertEqual(probe_calls, [])
                audit = _read_audit(audit_path)
                self.assertEqual(
                    sum(event["event"] == "recovery_turn_started" for event in audit),
                    0,
                )
            finally:
                release_wait.set()
                bridge.close()
                client.close()

    def test_transparent_bridge_projects_skill_tracks_model_and_intercepts_toggle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_root = root / "skills"
            (skill_root / "reconnect").mkdir(parents=True)
            (skill_root / "reconnect" / "SKILL.md").write_text("test", encoding="utf-8")
            control_path = root / "control.json"
            save_control(control_path, SessionControl.disabled("instance-1"))
            bridge = TransparentAppServerBridge(
                app_server_command=[sys.executable, str(FAKE_BRIDGE_SERVER)],
                socket_path=root / "bridge.sock",
                skill_root=skill_root,
                control_path=control_path,
                instance_id="instance-1",
                audit_log=JsonlAuditLog(root / "audit.jsonl"),
                app_server_stderr_path=root / "stderr.log",
                openai_probe_url="https://openai.test",
                public_probe_url=None,
            )
            bridge.start()
            client = _WebSocketClient(root / "bridge.sock")
            try:
                client.connect()
                client.send({"id": 1, "method": "initialize", "params": {}})
                self.assertEqual(client.receive()["id"], 1)
                client.send({"method": "initialized", "params": {}})
                client.send(
                    {
                        "id": 2,
                        "method": "thread/start",
                        "params": {"model": "gpt-test", "cwd": str(root)},
                    }
                )
                self.assertEqual(client.receive()["id"], 2)
                self.assertEqual(bridge.model_for_thread("thread-1"), "gpt-test")
                client.send(
                    {
                        "id": 3,
                        "method": "turn/start",
                        "params": {
                            "threadId": "thread-1",
                            "model": "gpt-test",
                            "input": [
                                {"type": "text", "text": "$reconnect on"},
                                {"type": "skill", "name": "reconnect", "path": "test"},
                            ],
                        },
                    }
                )
                self.assertEqual(client.receive()["id"], 3)
                self.assertTrue(load_control(control_path).enabled)
                client.send(
                    {
                        "id": 4,
                        "method": "turn/start",
                        "params": {
                            "threadId": "thread-1",
                            "model": "gpt-test",
                            "input": [{"type": "text", "text": "$continuity off"}],
                        },
                    }
                )
                self.assertEqual(client.receive()["id"], 4)
                self.assertTrue(load_control(control_path).enabled)
                client.send(
                    {
                        "id": 5,
                        "method": "turn/start",
                        "params": {
                            "threadId": "thread-1",
                            "model": "gpt-test",
                            "input": [{"type": "text", "text": "$reconnect off"}],
                        },
                    }
                )
                self.assertEqual(client.receive()["id"], 5)
                self.assertFalse(load_control(control_path).enabled)
            finally:
                bridge.close()
                client.close()


class _WebSocketClient:
    def __init__(self, path: Path):
        self.path = path
        self.socket: socket.socket | None = None
        self.buffer = bytearray()

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(self.path))
        self.socket = connection
        key = base64.b64encode(b"0123456789abcdef").decode("ascii")
        connection.sendall(
            (
                "GET / HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        response = self._read_until(b"\r\n\r\n")
        if not response.startswith(b"HTTP/1.1 101"):
            raise AssertionError(response)

    def send(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        mask = b"\x01\x02\x03\x04"
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(encoded))
        if len(encoded) < 126:
            header = bytes((0x81, 0x80 | len(encoded)))
        else:
            header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", len(encoded))
        assert self.socket is not None
        self.socket.sendall(header + mask + masked)

    def receive(self) -> dict[str, Any]:
        first, second = self._read_exact(2)
        if first != 0x81 or second & 0x80:
            raise AssertionError((first, second))
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        return json.loads(self._read_exact(length))

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def _read_until(self, marker: bytes) -> bytes:
        assert self.socket is not None
        while True:
            index = self.buffer.find(marker)
            if index >= 0:
                end = index + len(marker)
                result = bytes(self.buffer[:end])
                del self.buffer[:end]
                return result
            chunk = self.socket.recv(4096)
            if not chunk:
                raise AssertionError("websocket closed during handshake")
            self.buffer.extend(chunk)

    def _read_exact(self, size: int) -> bytes:
        assert self.socket is not None
        while len(self.buffer) < size:
            chunk = self.socket.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise AssertionError("websocket closed")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result
