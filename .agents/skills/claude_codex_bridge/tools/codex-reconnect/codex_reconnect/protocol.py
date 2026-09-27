from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Sequence
from typing import Any


class ProtocolError(RuntimeError):
    pass


class TransportClosedError(ProtocolError):
    pass


class RequestTimeoutError(ProtocolError):
    pass


class RpcError(ProtocolError):
    def __init__(self, method: str, payload: Any):
        super().__init__(f"app-server request failed: {method}: {payload}")
        self.method = method
        self.payload = payload


class JsonlAppServer:
    def __init__(self, command: Sequence[str]):
        if not command:
            raise ValueError("app-server command must not be empty")
        self.command = tuple(command)
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._responses: dict[int, dict[str, Any]] = {}
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._fatal: ProtocolError | None = None
        self._next_id = 1
        self._closing = False
        self._initialized = False

    def start(self) -> None:
        if self._process is not None:
            raise ProtocolError("app-server process has already been started")
        self._process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._reader = threading.Thread(
            target=self._read_loop, name="codex-app-server-jsonl", daemon=True
        )
        self._reader.start()

    def initialize(
        self,
        *,
        client_name: str,
        client_version: str,
        timeout: float = 10.0,
        experimental_api: bool = True,
    ) -> dict[str, Any]:
        if self._initialized:
            raise ProtocolError("initialize may be sent only once per connection")
        result = self.request(
            "initialize",
            {
                "clientInfo": {"name": client_name, "version": client_version},
                "capabilities": {"experimentalApi": experimental_api},
            },
            timeout=timeout,
        )
        self.notify("initialized", {})
        self._initialized = True
        return result

    def request(
        self, method: str, params: dict[str, Any], *, timeout: float = 10.0
    ) -> dict[str, Any]:
        if timeout <= 0:
            raise ValueError("request timeout must be positive")
        with self._condition:
            request_id = self._next_id
            self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self._condition:
            while request_id not in self._responses:
                if self._fatal is not None:
                    raise self._fatal
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RequestTimeoutError(
                        f"timed out waiting for app-server method {method}"
                    )
                self._condition.wait(remaining)
            response = self._responses.pop(request_id)
        if "error" in response:
            raise RpcError(method, response["error"])
        result = response.get("result")
        if not isinstance(result, dict):
            raise ProtocolError(f"app-server result for {method} must be an object")
        return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def next_event(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            if self._fatal is not None:
                raise self._fatal
            return None

    def is_alive(self) -> bool:
        return (
            self._process is not None
            and self._process.poll() is None
            and self._fatal is None
        )

    def close(self, *, timeout: float = 2.0) -> None:
        process = self._process
        if process is None:
            return
        self._closing = True
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout)
        if self._reader is not None:
            self._reader.join(timeout=timeout)
        if process.stdout is not None:
            process.stdout.close()

    def __enter__(self) -> "JsonlAppServer":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise TransportClosedError("app-server process is not started")
        if process.poll() is not None:
            raise TransportClosedError(
                f"app-server exited with status {process.returncode}"
            )
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        try:
            with self._write_lock:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TransportClosedError(
                f"failed to write app-server JSONL: {exc}"
            ) from exc

    def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            for raw_line in process.stdout:
                try:
                    message = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    self._set_fatal(
                        ProtocolError(f"malformed app-server JSONL frame: {exc}")
                    )
                    return
                if not isinstance(message, dict):
                    self._set_fatal(
                        ProtocolError("app-server JSONL frame must be an object")
                    )
                    return
                response_id = message.get("id")
                if isinstance(response_id, int) and (
                    "result" in message or "error" in message
                ):
                    with self._condition:
                        self._responses[response_id] = message
                        self._condition.notify_all()
                else:
                    self._events.put(message)
        finally:
            if not self._closing and self._fatal is None:
                return_code = process.poll()
                suffix = "" if return_code is None else f" with status {return_code}"
                self._set_fatal(
                    TransportClosedError(f"app-server stdout closed{suffix}")
                )

    def _set_fatal(self, error: ProtocolError) -> None:
        with self._condition:
            self._fatal = error
            self._condition.notify_all()
