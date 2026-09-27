from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import select
import socket
import stat
import tempfile
import time
import uuid

from ccbd.api_models import RpcRequest

from .endpoint import EndpointRef, legacy_socket_path

_LISTEN_BACKLOG = 128
_CONNECT_RETRY_INTERVAL_S = 0.05
_CONNECT_MAX_RETRIES = 2
_CONNECT_RETRY_ERRNOS = frozenset({
    errno.EAGAIN,
    errno.ECONNREFUSED,
    errno.ENOENT,
})
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_DEFERRED_CONNECTIONS = 128


class UnixControlPlaneTransport:
    def __init__(self, endpoint: EndpointRef) -> None:
        self.endpoint = endpoint
        path = legacy_socket_path(endpoint)
        if path is None:
            raise ValueError('unix_socket endpoint requires legacy_socket_path')
        self.socket_path = path

    def connect(self, *, timeout_s: float):
        if not hasattr(socket, 'AF_UNIX'):
            raise RuntimeError('unix domain sockets are not supported on this platform')
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        last_error: OSError | None = None
        for attempt in range(_CONNECT_MAX_RETRIES + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(remaining)
            try:
                sock.connect(str(self.socket_path))
                return sock
            except OSError as exc:
                sock.close()
                last_error = exc
                if not _is_transient_connect_error(exc):
                    break
                if attempt >= _CONNECT_MAX_RETRIES:
                    break
                sleep_for = min(_CONNECT_RETRY_INTERVAL_S, max(0.0, deadline - time.monotonic()))
                if sleep_for <= 0:
                    break
                time.sleep(sleep_for)
        if last_error is not None:
            raise last_error
        raise TimeoutError('timed out')

    def is_connectable(self, *, timeout_s: float = 0.2) -> bool:
        return socket_path_connectable(self.socket_path, timeout_s=timeout_s)

    def listen(self) -> 'UnixControlPlaneListener':
        if not hasattr(socket, 'AF_UNIX'):
            raise RuntimeError('unix domain sockets are not supported on this platform')
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        remove_stale_socket_path(self.socket_path)
        runtime_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_socket_stat = None
        try:
            runtime_socket.bind(str(self.socket_path))
            bound_socket_stat = bound_socket_identity(self.socket_path)
            if bound_socket_stat is None:
                raise RuntimeError('ccbd bound socket inode is unavailable')
            runtime_socket.listen(_LISTEN_BACKLOG)
            runtime_socket.settimeout(0.2)
        except BaseException:
            try:
                runtime_socket.close()
            finally:
                unlink_bound_socket_path(self.socket_path, bound_identity=bound_socket_stat)
            raise
        return UnixControlPlaneListener(
            endpoint=self.endpoint,
            socket_path=self.socket_path,
            socket=runtime_socket,
            bound_socket_stat=bound_socket_stat,
        )

    def unlink_bound_endpoint(self, *, bound_identity) -> None:
        unlink_bound_socket_path(self.socket_path, bound_identity=bound_identity)

    def bootstrap_readiness_probe(self, server, *, timeout_s: float):
        return unix_bootstrap_readiness_probe(server, timeout_s=timeout_s)


class UnixControlPlaneListener:
    def __init__(self, *, endpoint: EndpointRef, socket_path: Path, socket, bound_socket_stat) -> None:
        self.endpoint = endpoint
        self.socket_path = socket_path
        self._socket = socket
        self.bound_socket_stat = bound_socket_stat

    def settimeout(self, value: float | None) -> None:
        self._socket.settimeout(value)

    def accept(self):
        conn, peer_path = self._socket.accept()
        return conn, {
            'kind': 'unix_peer_path',
            'same_user': True,
            'detail': os.fsdecode(peer_path) if peer_path else '',
        }

    def close(self) -> None:
        self._socket.close()

    def fileno(self) -> int:
        return self._socket.fileno()


def _is_transient_connect_error(exc: OSError) -> bool:
    return int(getattr(exc, 'errno', 0) or 0) in _CONNECT_RETRY_ERRNOS


def bound_socket_identity(path: Path) -> tuple[int, int] | None:
    try:
        record = os.stat(path)
    except OSError:
        return None
    return int(record.st_dev), int(record.st_ino)


def remove_stale_socket_path(path: Path) -> None:
    try:
        initial = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(initial.st_mode):
        raise RuntimeError(f'refusing to replace non-socket ccbd path: {path}')
    if socket_path_connectable(path):
        raise RuntimeError(f'refusing to replace live ccbd socket: {path}')
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return
    initial_identity = (int(initial.st_dev), int(initial.st_ino))
    current_identity = (int(current.st_dev), int(current.st_ino))
    if current_identity != initial_identity or not stat.S_ISSOCK(current.st_mode):
        raise RuntimeError(f'ccbd socket path changed during stale cleanup: {path}')
    path.unlink()


def socket_path_connectable(path: Path, *, timeout_s: float = 0.1) -> bool:
    if not path.exists() or not hasattr(socket, 'AF_UNIX'):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(max(0.01, float(timeout_s)))
        probe.connect(str(path))
        return True
    except FileNotFoundError:
        return False
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
            return False
        raise RuntimeError(f'cannot prove existing ccbd socket is stale: {path}: {exc}') from exc
    finally:
        probe.close()


def unlink_bound_socket_path(
    path: Path,
    *,
    bound_identity: tuple[int, int] | None,
    timeout_s: float = 0.2,
) -> None:
    if bound_identity is None:
        return
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            current = bound_socket_identity(path)
            if current is None or current != bound_identity:
                return
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() >= deadline:
                return
            time.sleep(0.01)


@contextmanager
def unix_bootstrap_readiness_probe(server, *, timeout_s: float):
    from ccbd.socket_client_runtime import decode_response, send_request
    from ccbd.socket_server_runtime.loop import close_connection, enqueue_connection, start_worker

    runtime_socket = server._server
    if runtime_socket is None:
        raise RuntimeError('ccbd bootstrap probe requires a listening socket')
    if server._bootstrap_probe_active:
        raise RuntimeError('ccbd bootstrap probe is already active')
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    nonce = uuid.uuid4().hex
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client_path = _probe_client_path(nonce)
    client_socket_stat = None
    server._bootstrap_probe_active = True
    completed = False
    try:
        start_worker(server, interval=0.0, on_tick=None)
        client.settimeout(max(0.1, deadline - time.monotonic()))
        client.bind(str(client_path))
        client_socket_stat = path_identity(client_path)
        endpoint_path = getattr(runtime_socket, 'socket_path', server._socket_path)
        client.connect(str(endpoint_path))
        send_request(
            client,
            RpcRequest(
                op='ping',
                request={
                    'target': 'ccbd',
                    'bootstrap_probe_nonce': nonce,
                },
            ),
        )
        payload = _pump_until_probe_response(
            server,
            runtime_socket=runtime_socket,
            client=client,
            deadline=deadline,
            nonce=nonce,
            probe_client_path=client_path,
            decode_response=decode_response,
            close_connection=close_connection,
            enqueue_connection=enqueue_connection,
        )
        yield payload
        completed = True
    except BaseException:
        server._stop_event.set()
        raise
    finally:
        server._bootstrap_probe_active = False
        try:
            client.close()
        except OSError:
            pass
        unlink_probe_client_path(
            client_path,
            expected_identity=client_socket_stat,
        )


def _pump_until_probe_response(
    server,
    *,
    runtime_socket,
    client,
    deadline: float,
    nonce: str,
    probe_client_path: Path,
    decode_response,
    close_connection,
    enqueue_connection,
) -> dict:
    raw = b''
    deferred_connections = []
    try:
        while b'\n' not in raw:
            worker_error = server._peek_worker_error()
            if worker_error is not None:
                raise RuntimeError(f'ccbd bootstrap request worker failed: {worker_error}')
            if server._stop_event.is_set():
                raise RuntimeError('ccbd bootstrap request worker stopped')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('timed out waiting for ccbd bootstrap self-ping')
            readable, _, _ = select.select(
                [runtime_socket, client],
                [],
                [],
                remaining,
            )
            if runtime_socket in readable:
                try:
                    conn, peer_evidence = runtime_socket.accept()
                except (BlockingIOError, socket.timeout):
                    conn = None
                    peer_evidence = None
                if conn is not None:
                    peer_path = peer_evidence.get('detail') if isinstance(peer_evidence, dict) else peer_evidence
                    if same_peer_path(peer_path, probe_client_path):
                        enqueue_connection(server, conn)
                    elif len(deferred_connections) >= _MAX_DEFERRED_CONNECTIONS:
                        close_connection(conn)
                    else:
                        deferred_connections.append(conn)
            if client in readable:
                chunk = client.recv(65536)
                if not chunk:
                    break
                raw += chunk
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise RuntimeError('ccbd bootstrap self-ping response is too large')
        if not raw:
            raise RuntimeError('ccbd bootstrap self-ping returned an empty response')
        response = decode_response(raw)
        if not response.ok:
            raise RuntimeError(response.error or 'ccbd bootstrap self-ping failed')
        payload = dict(response.payload)
        if str(payload.get('bootstrap_probe_nonce') or '') != nonce:
            raise RuntimeError('ccbd bootstrap self-ping nonce mismatch')
    except BaseException:
        for connection in deferred_connections:
            close_connection(connection)
        raise
    for connection in deferred_connections:
        enqueue_connection(server, connection)
    return payload


def _probe_client_path(nonce: str) -> Path:
    name = f'ccb-probe-{os.getpid()}-{nonce[:12]}.sock'
    candidate = Path(tempfile.gettempdir()) / name
    if len(os.fsencode(candidate)) < 96:
        return candidate
    return Path('/tmp') / name


def same_peer_path(peer_path, expected: Path) -> bool:
    try:
        return os.path.abspath(os.fsdecode(peer_path)) == os.path.abspath(str(expected))
    except (TypeError, ValueError):
        return False


def path_identity(path: Path) -> tuple[int, int] | None:
    try:
        record = os.stat(path)
    except OSError:
        return None
    return int(record.st_dev), int(record.st_ino)


def unlink_probe_client_path(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None,
) -> None:
    if expected_identity is None or path_identity(path) != expected_identity:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
