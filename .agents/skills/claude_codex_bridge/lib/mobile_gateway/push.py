from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
import threading
import time
from typing import Callable, Mapping

from .notifications import MobileNotificationEvent, NOTIFICATION_KIND_TASK_COMPLETED
from .pairing import MobileGatewayPairingStore

_COMPLETION_FIELDS = ('id', 'kind', 'project_id', 'project_short_name', 'agent', 'completed_at', 'dedupe_key')


@dataclass(frozen=True)
class PushSendResult:
    sent: bool = False
    invalid_token: bool = False
    retryable: bool = False
    error_code: str | None = None


PushSender = Callable[[str, dict[str, object], float], PushSendResult]


class MobilePushDispatcher:
    """Pushes canonical completion metadata through an externally injected sender."""

    def __init__(
        self,
        *,
        pairing_store: MobileGatewayPairingStore,
        host_id: str,
        sender: PushSender | None,
        timeout_seconds: float = 2.0,
        max_workers: int = 4,
    ) -> None:
        self._pairing_store = pairing_store
        self._host_id = str(host_id)
        self._sender = sender
        self._timeout_seconds = max(0.1, float(timeout_seconds))
        self._max_workers = max(1, int(max_workers))
        self._max_pending = self._max_workers * 4
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix='ccb-mobile-push',
        )
        self._in_flight: set[str] = set()
        self._pending = 0
        self._closed = False
        self._lock = threading.Lock()
        self._audit: dict[str, int] = {
            'attempted': 0,
            'sent': 0,
            'failed': 0,
            'retryable': 0,
            'invalid_token': 0,
            'timed_out': 0,
            'suppressed_visible': 0,
            'skipped_in_flight': 0,
            'skipped_pending_full': 0,
            'sender_exceptions': 0,
        }

    def deliver(self, event: MobileNotificationEvent) -> None:
        if self._sender is None or event.kind != NOTIFICATION_KIND_TASK_COMPLETED:
            return
        completion = completion_payload(event)
        pending: list[tuple[str, Future[PushSendResult]]] = []
        for device_id, token in self._pairing_store.push_tokens_for_delivery():
            if self._is_visible_target(device_id, completion):
                self._increment_audit('suppressed_visible')
                continue
            if not self._claim(device_id):
                continue
            payload = {
                **completion,
                'host_id': self._host_id,
                'device_id': device_id,
            }
            future = self._submit_send(device_id, token, payload)
            if future is not None:
                pending.append((device_id, future))

        deadline = time.monotonic() + self._timeout_seconds
        for device_id, future in pending:
            try:
                result = future.result(timeout=max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                result = None
            if result is not None and result.invalid_token:
                self._pairing_store.delete_push_token(device_id=device_id, reason='invalid_sender_token')
            self._record_result(result)

    def audit_payload(self) -> dict[str, object]:
        with self._lock:
            return {
                'enabled': self._sender is not None,
                'timeout_seconds': self._timeout_seconds,
                'max_workers': self._max_workers,
                'in_flight': len(self._in_flight),
                'pending': self._pending,
                **dict(self._audit),
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _is_visible_target(self, device_id: str, payload: Mapping[str, object]) -> bool:
        presence = self._pairing_store.presence_for_device(device_id)
        return bool(presence and presence.get('visible') and presence.get('focused_project_id') == payload['project_id'] and presence.get('focused_agent') == payload['agent'])

    def _claim(self, device_id: str) -> bool:
        with self._lock:
            if self._closed:
                self._audit['skipped_pending_full'] += 1
                return False
            if device_id in self._in_flight:
                self._audit['skipped_in_flight'] += 1
                return False
            if self._pending >= self._max_pending:
                self._audit['skipped_pending_full'] += 1
                return False
            self._in_flight.add(device_id)
            self._pending += 1
            self._audit['attempted'] += 1
            return True

    def _submit_send(
        self,
        device_id: str,
        token: str,
        payload: dict[str, object],
    ) -> Future[PushSendResult] | None:
        try:
            future = self._executor.submit(self._call_sender, token, payload)
        except RuntimeError:
            self._release(device_id)
            return None
        future.add_done_callback(lambda _future: self._release(device_id))
        return future

    def _call_sender(self, token: str, payload: dict[str, object]) -> PushSendResult:
        try:
            result = self._sender(token, payload, self._timeout_seconds)  # type: ignore[misc]
        except Exception:
            self._increment_audit('sender_exceptions')
            return PushSendResult(error_code='sender_exception')
        return result if isinstance(result, PushSendResult) else PushSendResult(error_code='invalid_sender_result')

    def _release(self, device_id: str) -> None:
        with self._lock:
            self._in_flight.discard(device_id)
            self._pending = max(0, self._pending - 1)

    def _record_result(self, result: PushSendResult | None) -> None:
        if result is None:
            self._increment_audit('timed_out')
            return
        if result.sent:
            self._increment_audit('sent')
        if result.retryable:
            self._increment_audit('retryable')
        if result.invalid_token:
            self._increment_audit('invalid_token')
        if not result.sent:
            self._increment_audit('failed')

    def _increment_audit(self, key: str) -> None:
        with self._lock:
            self._audit[key] = int(self._audit.get(key, 0)) + 1


def completion_payload(event: MobileNotificationEvent) -> dict[str, object]:
    payload = event.to_payload()
    return {key: payload[key] for key in _COMPLETION_FIELDS}
