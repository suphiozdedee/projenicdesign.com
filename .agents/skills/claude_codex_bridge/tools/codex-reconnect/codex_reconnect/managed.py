from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .control import ControlError, SessionControl, save_control, set_session_control
from .network import (
    DEFAULT_OPENAI_PROBE_URL,
    DEFAULT_PUBLIC_PROBE_URL,
    resolve_primary_probe_url,
)
from .paths import default_state_dir
from .recovery import DisconnectRecoverySupervisor
from .websocket import UnixWebSocketServer, WebSocketError


INTERNAL_REQUEST_PREFIX = "reconnect:"


class ManagedSessionError(RuntimeError):
    pass


@dataclass(slots=True)
class _PendingRequest:
    event: threading.Event
    method: str
    response: dict[str, Any] | None = None


class JsonlAuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.parent.exists() and self.path.parent.is_symlink():
            raise ManagedSessionError(
                f"audit directory must not be a symlink: {self.path.parent}"
            )
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = self.path.parent.stat()
        if hasattr(os, "getuid") and parent_stat.st_uid != os.getuid():
            raise ManagedSessionError(
                f"audit directory is not owned by the current user: {self.path.parent}"
            )
        os.chmod(self.path.parent, 0o700)
        self._lock = threading.Lock()

    def write(self, event: str, fields: dict[str, Any]) -> None:
        payload = {"timestamp": time.time(), "event": event, **fields}
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        with self._lock:
            descriptor = os.open(
                self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")


class TransparentAppServerBridge:
    """Translate one local UDS WebSocket TUI connection to app-server JSONL."""

    def __init__(
        self,
        *,
        app_server_command: Sequence[str],
        socket_path: Path,
        skill_root: Path,
        control_path: Path,
        instance_id: str,
        audit_log: JsonlAuditLog,
        app_server_stderr_path: Path,
        openai_probe_url: str = DEFAULT_OPENAI_PROBE_URL,
        public_probe_url: str | None = DEFAULT_PUBLIC_PROBE_URL,
        probe_timeout: float = 5.0,
    ):
        if not app_server_command:
            raise ValueError("app-server command must not be empty")
        self.app_server_command = tuple(app_server_command)
        self.socket_path = Path(socket_path)
        self.skill_root = Path(skill_root).resolve()
        self.control_path = Path(control_path)
        self.instance_id = instance_id
        self.audit_log = audit_log
        self.app_server_stderr_path = Path(app_server_stderr_path)
        self.stop_event = threading.Event()
        self.websocket = UnixWebSocketServer(self.socket_path)
        self._process: subprocess.Popen[str] | None = None
        self._stderr_handle: Any = None
        self._reader: threading.Thread | None = None
        self._ingress: threading.Thread | None = None
        self._app_write_lock = threading.Lock()
        self._client_request_lock = threading.Lock()
        self._client_requests: dict[tuple[str, object], tuple[str, dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._pending: dict[str, _PendingRequest] = {}
        self._model_lock = threading.Lock()
        self._thread_models: dict[str, str] = {}
        self._fatal_lock = threading.Lock()
        self._fatal: BaseException | None = None
        self.recovery = DisconnectRecoverySupervisor(
            rpc=self.request,
            notify=self.notify_tui,
            log=self.audit_log.write,
            model_for_thread=self.model_for_thread,
            control_path=self.control_path,
            instance_id=self.instance_id,
            stop_event=self.stop_event,
            openai_probe_url=openai_probe_url,
            public_probe_url=public_probe_url,
            probe_timeout=probe_timeout,
        )

    def start(self) -> None:
        self.websocket.start()
        self.app_server_stderr_path.parent.mkdir(
            mode=0o700, parents=True, exist_ok=True
        )
        self._stderr_handle = self.app_server_stderr_path.open("a", encoding="utf-8")
        os.chmod(self.app_server_stderr_path, 0o600)
        self._process = subprocess.Popen(
            self.app_server_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_handle,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._reader = threading.Thread(
            target=self._read_app_server,
            name="codex-reconnect-app-server-out",
            daemon=True,
        )
        self._ingress = threading.Thread(
            target=self._read_tui,
            name="codex-reconnect-tui-in",
            daemon=True,
        )
        self._reader.start()
        self._ingress.start()
        self.audit_log.write(
            "bridge_started",
            {
                "socketPath": str(self.socket_path),
                "command": list(self.app_server_command),
            },
        )

    def request(
        self, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        if timeout <= 0:
            raise ValueError("request timeout must be positive")
        self._raise_if_fatal()
        request_id = INTERNAL_REQUEST_PREFIX + str(uuid.uuid4())
        pending = _PendingRequest(threading.Event(), method)
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            self._send_app_server(
                {"id": request_id, "method": method, "params": params}
            )
            if not pending.event.wait(timeout):
                raise ManagedSessionError(
                    f"timed out waiting for app-server method {method}"
                )
            self._raise_if_fatal()
            response = pending.response
            if not isinstance(response, dict):
                raise ManagedSessionError(
                    f"app-server returned no response for {method}"
                )
            if "error" in response:
                raise ManagedSessionError(
                    f"app-server method {method} failed: {response['error']}"
                )
            result = response.get("result")
            if not isinstance(result, dict):
                raise ManagedSessionError(
                    f"app-server result for {method} is not an object"
                )
            return result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify_tui(self, thread_id: str | None, message: str) -> None:
        params: dict[str, Any] = {"message": message}
        if thread_id is not None:
            params["threadId"] = thread_id
        self._send_tui({"method": "warning", "params": params}, fail_closed=False)

    def model_for_thread(self, thread_id: str) -> str | None:
        with self._model_lock:
            return self._thread_models.get(thread_id)

    def close(self) -> None:
        self.stop_event.set()
        self.websocket.close()
        process = self._process
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3.0)
            if process.stdout is not None:
                process.stdout.close()
        for thread in (self._ingress, self._reader):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None
        self.audit_log.write("bridge_stopped", {})

    def _read_tui(self) -> None:
        try:
            self.websocket.accept()
            while not self.stop_event.is_set():
                raw = self.websocket.recv_text()
                if raw is None:
                    return
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ManagedSessionError(
                        f"TUI sent malformed JSON: {exc}"
                    ) from exc
                if not isinstance(message, dict):
                    raise ManagedSessionError("TUI websocket message is not an object")
                self._observe_client_message(message)
                self._send_app_server(message)
                if message.get("method") == "initialized" and "id" not in message:
                    self.request(
                        "skills/extraRoots/set",
                        {"extraRoots": [str(self.skill_root)]},
                        10.0,
                    )
                    self.audit_log.write(
                        "reconnect_skill_projected",
                        {"skillRoot": str(self.skill_root)},
                    )
        except (ManagedSessionError, WebSocketError, OSError) as exc:
            if not self.stop_event.is_set():
                self._set_fatal(exc)

    def _read_app_server(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            for raw_line in process.stdout:
                if self.stop_event.is_set():
                    return
                try:
                    message = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise ManagedSessionError(
                        f"app-server emitted malformed JSONL: {exc}"
                    ) from exc
                if not isinstance(message, dict):
                    raise ManagedSessionError(
                        "app-server JSONL message is not an object"
                    )
                response_id = message.get("id")
                if (
                    isinstance(response_id, str)
                    and response_id.startswith(INTERNAL_REQUEST_PREFIX)
                    and ("result" in message or "error" in message)
                ):
                    with self._pending_lock:
                        pending = self._pending.get(response_id)
                    if pending is not None:
                        pending.response = message
                        pending.event.set()
                    continue
                self._observe_server_message(message)
                self._send_tui(message)
            if not self.stop_event.is_set():
                return_code = process.poll()
                raise ManagedSessionError(
                    f"app-server output closed with status {return_code}"
                )
        except (ManagedSessionError, WebSocketError, OSError) as exc:
            if not self.stop_event.is_set():
                self._set_fatal(exc)

    def _observe_client_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        request_id = message.get("id")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        if request_id is not None:
            with self._client_request_lock:
                self._client_requests[_request_key(request_id)] = (method, params)
        if method == "turn/start":
            thread_id = params.get("threadId")
            model = params.get("model")
            if isinstance(thread_id, str):
                self.recovery.note_client_turn(thread_id)
            if isinstance(thread_id, str) and isinstance(model, str) and model:
                self._record_model(thread_id, model)
            self._apply_reconnect_command(thread_id, params.get("input"))

    def _apply_reconnect_command(self, thread_id: object, turn_input: object) -> None:
        if not isinstance(thread_id, str) or not isinstance(turn_input, list):
            return
        text_parts = [
            item.get("text", "")
            for item in turn_input
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        command = (
            " ".join(part for part in text_parts if isinstance(part, str))
            .strip()
            .lower()
        )
        if command not in {"$reconnect on", "$reconnect off"}:
            return
        enabled = command.endswith(" on")
        try:
            state = set_session_control(
                self.control_path,
                instance_id=self.instance_id,
                session_id=thread_id,
                enabled=enabled,
            )
        except ControlError as exc:
            raise ManagedSessionError(f"failed to apply {command}: {exc}") from exc
        self.audit_log.write(
            "reconnect_armed" if state.enabled else "reconnect_disarmed",
            {"threadId": thread_id},
        )

    def _observe_server_message(self, message: dict[str, Any]) -> None:
        response_id = message.get("id")
        if response_id is not None and ("result" in message or "error" in message):
            with self._client_request_lock:
                request = self._client_requests.pop(_request_key(response_id), None)
            if request is not None and "result" in message:
                method, params = request
                result = message.get("result")
                if method in {
                    "thread/start",
                    "thread/resume",
                    "thread/fork",
                } and isinstance(result, dict):
                    thread = result.get("thread")
                    thread_id = thread.get("id") if isinstance(thread, dict) else None
                    model = result.get("model") or params.get("model")
                    if isinstance(thread_id, str) and isinstance(model, str) and model:
                        self._record_model(thread_id, model)
        method = message.get("method")
        params = message.get("params")
        if method == "thread/settings/updated" and isinstance(params, dict):
            thread_id = params.get("threadId")
            settings = params.get("threadSettings")
            model = settings.get("model") if isinstance(settings, dict) else None
            if isinstance(thread_id, str) and isinstance(model, str) and model:
                self._record_model(thread_id, model)
        self.recovery.observe(message)

    def _record_model(self, thread_id: str, model: str) -> None:
        with self._model_lock:
            previous = self._thread_models.get(thread_id)
            self._thread_models[thread_id] = model
        if previous != model:
            self.audit_log.write(
                "thread_model_observed", {"threadId": thread_id, "model": model}
            )

    def _send_app_server(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise ManagedSessionError("app-server is not running")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._app_write_lock:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ManagedSessionError(
                f"failed to write app-server JSONL: {exc}"
            ) from exc

    def _send_tui(self, message: dict[str, Any], *, fail_closed: bool = True) -> None:
        if self.stop_event.is_set():
            return
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            self.websocket.send_text(encoded)
        except WebSocketError:
            if fail_closed:
                raise

    def _set_fatal(self, error: BaseException) -> None:
        with self._fatal_lock:
            if self._fatal is None:
                self._fatal = error
                self.audit_log.write("bridge_fatal", {"error": str(error)})
        self.stop_event.set()
        with self._pending_lock:
            for pending in self._pending.values():
                pending.event.set()
        self.websocket.close()

    def _raise_if_fatal(self) -> None:
        with self._fatal_lock:
            fatal = self._fatal
        if fatal is not None:
            raise ManagedSessionError(f"reconnect bridge failed: {fatal}")
        if self.stop_event.is_set():
            raise ManagedSessionError("reconnect bridge is stopped")


class ManagedCodexSession:
    def __init__(
        self,
        *,
        codex_command: str,
        codex_args: Sequence[str],
        state_dir: Path | None = None,
        openai_probe_url: str | None = None,
        public_probe_url: str | None = DEFAULT_PUBLIC_PROBE_URL,
        probe_timeout: float = 5.0,
    ):
        self.codex_command = codex_command
        self.codex_args = tuple(codex_args)
        self.state_dir = Path(state_dir or default_state_dir())
        self.openai_probe_url = openai_probe_url
        self.public_probe_url = public_probe_url
        self.probe_timeout = probe_timeout

    def run(self) -> int:
        instance_id = uuid.uuid4().hex
        log_dir = self.state_dir / "managed"
        audit_path = log_dir / f"{instance_id}.jsonl"
        stderr_path = log_dir / f"{instance_id}.app-server.stderr.log"
        audit_log = JsonlAuditLog(audit_path)
        runtime_parent = _secure_runtime_parent()
        with tempfile.TemporaryDirectory(
            prefix="run-", dir=runtime_parent
        ) as temporary:
            runtime_dir = Path(temporary)
            os.chmod(runtime_dir, 0o700)
            socket_path = runtime_dir / "app.sock"
            control_path = runtime_dir / "control.json"
            save_control(control_path, SessionControl.disabled(instance_id))
            tool_root = Path(__file__).resolve().parents[1]
            skill_root = tool_root / "skills"
            if not (skill_root / "reconnect" / "SKILL.md").is_file():
                raise ManagedSessionError(f"reconnect skill is missing: {skill_root}")
            environment = os.environ.copy()
            resolved_probe_url = resolve_primary_probe_url(
                environment.get("CODEX_HOME"),
                configured_url=self.openai_probe_url,
                environment=environment,
            )
            bridge = TransparentAppServerBridge(
                app_server_command=[self.codex_command, "app-server", "--stdio"],
                socket_path=socket_path,
                skill_root=skill_root,
                control_path=control_path,
                instance_id=instance_id,
                audit_log=audit_log,
                app_server_stderr_path=stderr_path,
                openai_probe_url=resolved_probe_url,
                public_probe_url=self.public_probe_url,
                probe_timeout=self.probe_timeout,
            )
            bridge.start()
            environment.update(
                {
                    "CODEX_RECONNECT_MANAGED": "1",
                    "CODEX_RECONNECT_INSTANCE_ID": instance_id,
                    "CODEX_RECONNECT_CONTROL_FILE": str(control_path),
                }
            )
            command = [
                self.codex_command,
                "--remote",
                f"unix://{socket_path}",
                *self.codex_args,
            ]
            print(
                f"codex-reconnect managed Codex starting; audit log: {audit_path}",
                file=sys.stderr,
                flush=True,
            )
            try:
                completed = subprocess.run(command, env=environment, check=False)
                return_code = completed.returncode
            except KeyboardInterrupt:
                return_code = 130
            finally:
                bridge.close()
            print(
                f"codex-reconnect managed Codex stopped; audit log: {audit_path}",
                file=sys.stderr,
            )
            return return_code


def _request_key(request_id: object) -> tuple[str, object]:
    if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
        return (type(request_id).__name__, repr(request_id))
    return (type(request_id).__name__, request_id)


def _secure_runtime_parent() -> str:
    uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
    parent = Path(tempfile.gettempdir()) / f"codex-reconnect-{uid}"
    if parent.exists() and parent.is_symlink():
        raise ManagedSessionError(f"runtime parent must not be a symlink: {parent}")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_stat = parent.stat()
    if hasattr(os, "getuid") and parent_stat.st_uid != os.getuid():
        raise ManagedSessionError(
            f"runtime parent is not owned by the current user: {parent}"
        )
    os.chmod(parent, 0o700)
    return str(parent)
