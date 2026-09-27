from __future__ import annotations

import asyncio
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Mapping

from .relay_host_connector import RelayHostConnector, RelayHostConnectorConfig
from .relay_host_credentials import RelayHostCredentials


class RelayHostRuntimeError(RuntimeError):
    pass


@dataclass
class RelayHostConnectorRuntime:
    credentials: RelayHostCredentials
    gateway_origin: str
    tls_context: ssl.SSLContext | None = None

    def __post_init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connector: RelayHostConnector | None = None
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._failure: BaseException | None = None

    def start(self, *, timeout_seconds: float = 3.0) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._thread_main,
            name='ccb-mobile-relay-host',
            daemon=True,
        )
        self._thread = thread
        thread.start()
        if not self._started.wait(timeout=max(0.1, timeout_seconds)):
            raise RelayHostRuntimeError('relay host connector thread did not start')
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while time.monotonic() < deadline:
            failure = self._failure
            if failure is not None:
                raise RelayHostRuntimeError(
                    f'relay host connector failed to start: {failure.__class__.__name__}'
                ) from failure
            connector = self._connector
            state = str(connector.diagnostics().get('state') or '') if connector else ''
            if state == 'registered':
                return
            if state == 'auth_rejected':
                raise RelayHostRuntimeError('relay host authentication rejected')
            thread = self._thread
            if thread is not None and not thread.is_alive():
                raise RelayHostRuntimeError('relay host connector exited during startup')
            time.sleep(0.02)
        self.stop(timeout_seconds=1.0)
        raise RelayHostRuntimeError('relay host connector did not register')

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        connector = self._connector
        loop = self._loop
        if connector is not None and loop is not None and loop.is_running():
            loop.call_soon_threadsafe(connector.stop)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.1, timeout_seconds))
            if thread.is_alive():
                raise RelayHostRuntimeError('relay host connector thread did not stop')
        self._thread = None

    def diagnostics(self) -> dict[str, object]:
        connector = self._connector
        if connector is not None:
            payload = connector.diagnostics()
        elif self._failure is not None:
            payload = {
                'state': 'runtime_failed',
                'last_error_class': self._failure.__class__.__name__,
            }
        else:
            payload = {'state': 'starting' if self._thread is not None else 'stopped'}
        return {
            **payload,
            'host_id': self.credentials.host_id,
            'relay_mode': self.credentials.relay_mode,
            'relay_origin': self.credentials.relay_origin,
            'gateway_origin': self.gateway_origin,
            'host_fingerprint': self.credentials.host_fingerprint,
        }

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:
            self._failure = exc
            self._started.set()
        finally:
            self._stopped.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        connector = RelayHostConnector(
            RelayHostConnectorConfig(
                relay_origin=self.credentials.relay_origin,
                gateway_origin=self.gateway_origin,
                host_id=self.credentials.host_id,
                host_signing_key=self.credentials.host_signing_key,
                host_crypto_private_key=self.credentials.host_crypto_key,
                tls_context=self.tls_context,
            )
        )
        self._connector = connector
        self._started.set()
        try:
            await connector.run_forever()
        finally:
            self._connector = None
            self._loop = None


def relay_host_runtime_summary(
    credentials: RelayHostCredentials,
    *,
    state: str = 'configured',
    diagnostics: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        'status': state,
        'mode': 'production_outbound_wss',
        'relay_mode': credentials.relay_mode,
        'host_id': credentials.host_id,
        'relay_origin': credentials.relay_origin,
        'server_fingerprint': credentials.host_fingerprint,
        'capabilities': ['relay.forward', 'relay.inner.v1'],
        'diagnostics': dict(diagnostics or {}),
    }


__all__ = [
    'RelayHostConnectorRuntime',
    'RelayHostRuntimeError',
    'relay_host_runtime_summary',
]
