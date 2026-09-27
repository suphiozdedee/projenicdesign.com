from __future__ import annotations

from contextlib import contextmanager
import queue
import time
import uuid

from ccbd.api_models import RpcRequest
from ccbd.socket_client_runtime import decode_response, send_request
from ccbd.socket_server_runtime.loop import enqueue_connection, start_worker

from .endpoint import EndpointRef


class FakeConnection:
    def __init__(self, incoming: bytes = b'') -> None:
        self._incoming = bytearray(incoming)
        self.sent = bytearray()
        self.closed = False
        self.timeout = None

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def sendall(self, payload: bytes) -> None:
        self.sent.extend(payload)
        peer = getattr(self, '_peer', None)
        if peer is not None:
            peer.push_recv(payload)

    def recv(self, size: int) -> bytes:
        deadline = None if self.timeout is None else time.monotonic() + max(0.0, float(self.timeout))
        while not self._incoming:
            peer = getattr(self, '_peer', None)
            if self.closed or peer is None or getattr(peer, 'closed', False):
                return b''
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('fake ccbd connection recv timed out')
            time.sleep(0.01)
        chunk = bytes(self._incoming[:size])
        del self._incoming[:size]
        return chunk

    def push_recv(self, payload: bytes) -> None:
        self._incoming.extend(payload)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> 'FakeConnection':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class FakeControlPlaneListener:
    def __init__(self, endpoint: EndpointRef) -> None:
        self.endpoint = endpoint
        self.bound_socket_stat = ('fake-generation', 'fake-token', False)
        self.closed = False
        self.timeout = None
        self._accepted: queue.Queue[FakeConnection] = queue.Queue()

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def enqueue(self, connection: FakeConnection) -> None:
        if self.closed:
            raise OSError('fake ccbd listener is closed')
        self._accepted.put_nowait(connection)

    def accept(self):
        if self.closed:
            raise OSError('fake ccbd listener is closed')
        try:
            if self.timeout is None:
                conn = self._accepted.get(block=True)
            elif self.timeout <= 0:
                conn = self._accepted.get(block=False)
            else:
                conn = self._accepted.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise TimeoutError('fake ccbd listener has no pending connection') from exc
        return conn, {'kind': 'fake', 'same_user': True, 'detail': 'fake'}

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:
        raise OSError('fake ccbd listener has no file descriptor')


class FakeControlPlaneTransport:
    def __init__(self, endpoint: EndpointRef | None = None) -> None:
        self.endpoint = endpoint or {
            'kind': 'unix_socket',
            'address': 'fake://ccbd',
            'display': 'fake://ccbd',
            'legacy_socket_path': None,
            'auth_ref': None,
            'fingerprint': None,
        }
        self.listener = FakeControlPlaneListener(self.endpoint)

    def connect(self, *, timeout_s: float) -> FakeConnection:
        if self.listener.closed:
            raise OSError('fake ccbd listener is closed')
        client = FakeConnection()
        client.settimeout(timeout_s)
        server = FakeConnection()
        client._peer = server
        server._peer = client
        self.listener.enqueue(server)
        return client

    def is_connectable(self, *, timeout_s: float = 0.2) -> bool:
        del timeout_s
        return not self.listener.closed

    def listen(self) -> FakeControlPlaneListener:
        return self.listener

    def unlink_bound_endpoint(self, *, bound_identity) -> None:
        del bound_identity

    @contextmanager
    def bootstrap_readiness_probe(self, server, *, timeout_s: float):
        if server._server is None:
            raise RuntimeError('ccbd bootstrap probe requires a listening socket')
        if server._bootstrap_probe_active:
            raise RuntimeError('ccbd bootstrap probe is already active')
        nonce = uuid.uuid4().hex
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        probe = FakeConnection()
        server._bootstrap_probe_active = True
        completed = False
        try:
            start_worker(server, interval=0.0, on_tick=None)
            send_request(
                probe,
                RpcRequest(
                    op='ping',
                    request={
                        'target': 'ccbd',
                        'bootstrap_probe_nonce': nonce,
                    },
                ),
            )
            request_bytes = bytes(probe.sent)
            worker_conn = FakeConnection(incoming=request_bytes)
            enqueue_connection(server, worker_conn)
            raw = b''
            while b'\n' not in raw:
                worker_error = server._peek_worker_error()
                if worker_error is not None:
                    raise RuntimeError(f'ccbd bootstrap request worker failed: {worker_error}')
                if time.monotonic() >= deadline:
                    raise TimeoutError('timed out waiting for ccbd bootstrap self-ping')
                raw = bytes(worker_conn.sent)
                if not raw:
                    time.sleep(0.01)
            response = decode_response(raw)
            if not response.ok:
                raise RuntimeError(response.error or 'ccbd bootstrap self-ping failed')
            payload = dict(response.payload)
            if str(payload.get('bootstrap_probe_nonce') or '') != nonce:
                raise RuntimeError('ccbd bootstrap self-ping nonce mismatch')
            yield payload
            completed = True
        except BaseException:
            server._stop_event.set()
            raise
        finally:
            server._bootstrap_probe_active = False
