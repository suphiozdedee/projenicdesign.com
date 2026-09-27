from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .control import ControlError, SessionControl, load_control
from .network import (
    DEFAULT_OPENAI_PROBE_URL,
    DEFAULT_PUBLIC_PROBE_URL,
    ProbeResult,
    classify_readiness,
    probe_https,
)
from .policy import NETWORK_ERRORS, codex_error_class, full_jitter_delay


RECOVERY_PROMPT = "上一轮因网络中断或模型服务高负载而失败。先检查本会话现有进度和工作区状态，" "只继续尚未完成的部分，不重复已经完成的操作。"


class RecoveryError(RuntimeError):
    pass


RpcCall = Callable[[str, dict[str, Any], float], dict[str, Any]]
Notifier = Callable[[str | None, str], None]
Logger = Callable[[str, dict[str, Any]], None]
ModelLookup = Callable[[str], str | None]
Probe = Callable[[str, float], ProbeResult]


class DisconnectRecoverySupervisor:
    """Observe terminal failures and inject at most one reconciled recovery turn."""

    def __init__(
        self,
        *,
        rpc: RpcCall,
        notify: Notifier,
        log: Logger,
        model_for_thread: ModelLookup,
        control_path: Path,
        instance_id: str,
        stop_event: threading.Event,
        openai_probe_url: str = DEFAULT_OPENAI_PROBE_URL,
        public_probe_url: str | None = DEFAULT_PUBLIC_PROBE_URL,
        probe_timeout: float = 5.0,
        stable_probe_successes: int = 2,
        request_timeout: float = 20.0,
        primary_probe: Probe | None = None,
        public_probe: Probe | None = None,
        wait: Callable[[float], None] = time.sleep,
    ):
        if stable_probe_successes < 1:
            raise ValueError("stable_probe_successes must be positive")
        self.rpc = rpc
        self.notify = notify
        self.log = log
        self.model_for_thread = model_for_thread
        self.control_path = Path(control_path)
        self.instance_id = instance_id
        self.stop_event = stop_event
        self.openai_probe_url = openai_probe_url
        self.public_probe_url = public_probe_url
        self.probe_timeout = probe_timeout
        self.stable_probe_successes = stable_probe_successes
        self.request_timeout = request_timeout
        self.primary_probe = primary_probe or (
            lambda url, timeout: probe_https(url, timeout=timeout)
        )
        self.public_probe = public_probe or (
            lambda url, timeout: probe_https(url, timeout=timeout)
        )
        self.wait = wait
        self._lock = threading.Lock()
        self._turn_errors: dict[tuple[str, str], dict[str, Any]] = {}
        self._recovery_threads: dict[str, threading.Thread] = {}
        self._handled_failed_turns: set[str] = set()
        self._injected_turns: set[str] = set()
        self._client_turn_revisions: dict[str, int] = {}

    def note_client_turn(self, thread_id: str) -> None:
        """Record TUI input so newer operator turns win recovery races."""

        with self._lock:
            self._client_turn_revisions[thread_id] = (
                self._client_turn_revisions.get(thread_id, 0) + 1
            )

    def observe(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        if method == "error":
            self._record_error(params)
            return
        if method == "model/rerouted":
            self._handle_model_reroute(params)
            return
        if method == "turn/completed":
            self._handle_terminal_turn(params)

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                threads = list(self._recovery_threads.values())
            if not any(thread.is_alive() for thread in threads):
                return True
            time.sleep(0.01)
        return False

    def _record_error(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        error = params.get("error")
        if not isinstance(thread_id, str) or not isinstance(turn_id, str):
            return
        if not isinstance(error, dict):
            return
        with self._lock:
            self._turn_errors[(thread_id, turn_id)] = error
        self.log(
            "turn_error_observed",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "errorClass": codex_error_class(error),
                "willRetry": bool(params.get("willRetry")),
            },
        )

    def _handle_terminal_turn(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn = params.get("turn")
        if not isinstance(thread_id, str) or not isinstance(turn, dict):
            return
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or turn.get("status") != "failed":
            return
        with self._lock:
            if turn_id in self._injected_turns:
                self._handled_failed_turns.add(turn_id)
                self.notify(
                    thread_id,
                    "Reconnect recovery turn also failed; automatic continuation stopped.",
                )
                self.log(
                    "recovery_circuit_open",
                    {"threadId": thread_id, "turnId": turn_id},
                )
                return
        error = turn.get("error")
        if not isinstance(error, dict):
            with self._lock:
                error = self._turn_errors.get((thread_id, turn_id))
        error_class = codex_error_class(error if isinstance(error, dict) else None)
        if error_class in NETWORK_ERRORS:
            failure_kind = "network"
        elif error_class == "serverOverloaded":
            failure_kind = "overload"
        else:
            self.log(
                "failure_out_of_scope",
                {"threadId": thread_id, "turnId": turn_id, "errorClass": error_class},
            )
            return
        control = self._control_for(thread_id)
        if control is None:
            return
        with self._lock:
            if (
                turn_id in self._handled_failed_turns
                or thread_id in self._recovery_threads
            ):
                return
            self._handled_failed_turns.add(turn_id)
            client_turn_revision = self._client_turn_revisions.get(thread_id, 0)
            worker = threading.Thread(
                target=self._recover,
                args=(
                    thread_id,
                    turn_id,
                    failure_kind,
                    error_class,
                    client_turn_revision,
                ),
                name=f"codex-reconnect-recovery-{thread_id[:12]}",
                daemon=True,
            )
            self._recovery_threads[thread_id] = worker
            worker.start()

    def _recover(
        self,
        thread_id: str,
        failed_turn_id: str,
        failure_kind: str,
        error_class: str,
        client_turn_revision: int,
    ) -> None:
        try:
            if failure_kind == "network":
                self.notify(
                    thread_id,
                    "Reconnect detected a terminal network failure; waiting for stable OpenAI HTTPS connectivity.",
                )
            else:
                self.notify(
                    thread_id,
                    "Reconnect detected model service overload; waiting before retrying the same model.",
                )
                if not self._interruptible_wait(
                    max(1.0, full_jitter_delay(2, cap_seconds=8.0)),
                    thread_id=thread_id,
                ):
                    return
            if not self._wait_for_stable_network(thread_id):
                return
            if self._has_new_client_turn(thread_id, client_turn_revision):
                self.log(
                    "recovery_cancelled_new_client_turn",
                    {"threadId": thread_id, "failedTurnId": failed_turn_id},
                )
                return
            if self._control_for(thread_id) is None:
                return
            read_result = self.rpc(
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
                self.request_timeout,
            )
            thread = read_result.get("thread")
            if not isinstance(thread, dict) or thread.get("id") != thread_id:
                raise RecoveryError("thread/read returned a different thread")
            turns = thread.get("turns")
            if not isinstance(turns, list) or not all(
                isinstance(turn, dict) for turn in turns
            ):
                raise RecoveryError("thread/read returned invalid turns")
            active = [turn for turn in turns if turn.get("status") == "inProgress"]
            latest = _latest_turn(turns)
            if active:
                self.log(
                    "recovery_skipped_active_turn",
                    {"threadId": thread_id, "failedTurnId": failed_turn_id},
                )
                return
            if (
                latest is None
                or latest.get("id") != failed_turn_id
                or latest.get("status") != "failed"
            ):
                self.log(
                    "recovery_skipped_newer_progress",
                    {
                        "threadId": thread_id,
                        "failedTurnId": failed_turn_id,
                        "latestTurnId": latest.get("id")
                        if latest is not None
                        else None,
                    },
                )
                return
            if self._has_new_client_turn(thread_id, client_turn_revision):
                self.log(
                    "recovery_cancelled_new_client_turn",
                    {"threadId": thread_id, "failedTurnId": failed_turn_id},
                )
                return
            if self._control_for(thread_id) is None:
                return
            model = self.model_for_thread(thread_id)
            if not isinstance(model, str) or not model:
                raise RecoveryError("the exact current model is unknown")
            client_message_id = f"reconnect-{uuid.uuid4()}"
            result = self.rpc(
                "turn/start",
                {
                    "threadId": thread_id,
                    "model": model,
                    "clientUserMessageId": client_message_id,
                    "input": [{"type": "text", "text": RECOVERY_PROMPT}],
                },
                self.request_timeout,
            )
            turn = result.get("turn")
            recovery_turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(recovery_turn_id, str) or not recovery_turn_id:
                raise RecoveryError("turn/start response is missing a recovery turn id")
            with self._lock:
                self._injected_turns.add(recovery_turn_id)
            self.notify(
                thread_id,
                "Reconnect connectivity gate passed; one reconciled continuation was started on the same model.",
            )
            self.log(
                "recovery_turn_started",
                {
                    "threadId": thread_id,
                    "failedTurnId": failed_turn_id,
                    "recoveryTurnId": recovery_turn_id,
                    "model": model,
                    "failureKind": failure_kind,
                    "errorClass": error_class,
                },
            )
        except (ControlError, RecoveryError, OSError, RuntimeError) as exc:
            self.notify(thread_id, f"Reconnect stopped safely: {exc}")
            self.log(
                "recovery_failed_closed",
                {
                    "threadId": thread_id,
                    "failedTurnId": failed_turn_id,
                    "error": str(exc),
                },
            )
        finally:
            with self._lock:
                self._recovery_threads.pop(thread_id, None)

    def _wait_for_stable_network(self, thread_id: str) -> bool:
        successes = 0
        attempt = 0
        while successes < self.stable_probe_successes:
            if self._control_for(thread_id) is None or self.stop_event.is_set():
                self.log("recovery_cancelled", {"threadId": thread_id})
                return False
            primary = self.primary_probe(self.openai_probe_url, self.probe_timeout)
            public = None
            if self.public_probe_url is not None:
                public = self.public_probe(self.public_probe_url, self.probe_timeout)
            readiness = classify_readiness(primary, public)
            self.log(
                "network_probe",
                {
                    "threadId": thread_id,
                    "attempt": attempt + 1,
                    "readiness": readiness.value,
                    "primaryReachable": primary.reachable,
                    "primaryStatus": primary.status,
                    "publicReachable": public.reachable if public is not None else None,
                    "publicStatus": public.status if public is not None else None,
                },
            )
            if primary.reachable:
                successes += 1
            else:
                successes = 0
            if successes >= self.stable_probe_successes:
                return True
            if primary.reachable:
                delay = 1.0
            else:
                delay = max(0.5, full_jitter_delay(attempt, cap_seconds=60.0))
                attempt += 1
            if not self._interruptible_wait(delay, thread_id=thread_id):
                return False
        return True

    def _handle_model_reroute(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        to_model = params.get("toModel")
        if not isinstance(thread_id, str) or not isinstance(turn_id, str):
            return
        with self._lock:
            is_recovery_turn = turn_id in self._injected_turns
        if not is_recovery_turn:
            return
        expected = self.model_for_thread(thread_id)
        if not isinstance(expected, str) or to_model == expected:
            return
        self.notify(
            thread_id,
            "Reconnect refused a model reroute and interrupted the recovery turn.",
        )
        self.log(
            "model_reroute_refused",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "expectedModel": expected,
                "toModel": to_model,
            },
        )

        def interrupt() -> None:
            try:
                self.rpc(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": turn_id},
                    self.request_timeout,
                )
            except RuntimeError as exc:
                self.log(
                    "reroute_interrupt_failed",
                    {"threadId": thread_id, "turnId": turn_id, "error": str(exc)},
                )

        threading.Thread(
            target=interrupt, name="codex-reconnect-reroute-stop", daemon=True
        ).start()

    def _control_for(self, thread_id: str) -> SessionControl | None:
        try:
            control = load_control(
                self.control_path, expected_instance_id=self.instance_id
            )
        except ControlError as exc:
            self.log("control_read_failed", {"threadId": thread_id, "error": str(exc)})
            return None
        if not control.enabled or control.session_id != thread_id:
            return None
        return control

    def _interruptible_wait(
        self, seconds: float, *, thread_id: str | None = None
    ) -> bool:
        remaining = max(0.0, seconds)
        while remaining > 0:
            if self.stop_event.is_set():
                return False
            if thread_id is not None and self._control_for(thread_id) is None:
                return False
            interval = min(0.5, remaining)
            self.wait(interval)
            remaining -= interval
        return not self.stop_event.is_set()

    def _has_new_client_turn(self, thread_id: str, baseline: int) -> bool:
        with self._lock:
            return self._client_turn_revisions.get(thread_id, 0) != baseline


def _latest_turn(turns: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not turns:
        return None

    def key(indexed: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        index, turn = indexed
        started_at = turn.get("startedAt")
        return (started_at if isinstance(started_at, int) else -1, index)

    return max(enumerate(turns), key=key)[1]
