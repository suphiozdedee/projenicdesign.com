from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import ipaddress
import json
import os
import signal
import ssl
import sqlite3
import time
from collections import OrderedDict, deque
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from aiohttp import WSMsgType, web

from .relay import (
    MobileRelayError,
    RelayAccessGrant,
    RelayFrame,
    RelayHandshakeTranscript,
    RelayPhoneSessionProof,
    RelayRendezvousCapability,
)
from .relay_admission import RelayAdmissionError, RelayAdmissionSecrets, RelayAdmissionStore
from .relay_crypto import RelayDirection, RelayV2Envelope


_DEFAULT_LOOPBACK_PORT = 18444
_DEFAULT_ADMIN_PORT = 18445
_DEFAULT_MAX_FRAME_BYTES = 768 * 1024
_JSON_SEPARATORS = (',', ':')
_PUBLIC_ERROR_MESSAGES = {
    'relay_auth_rejected': 'relay authentication rejected',
    'relay_frame_rejected': 'relay frame rejected',
    'relay_rate_limited': 'relay rate limited',
    'relay_rejected': 'relay request rejected',
    'relay_unavailable': 'relay unavailable',
}


@dataclass
class ProductionRelayConfig:
    listen_host: str = '127.0.0.1'
    listen_port: int = _DEFAULT_LOOPBACK_PORT
    admin_host: str = '127.0.0.1'
    admin_port: int = _DEFAULT_ADMIN_PORT
    public_origin: str = 'wss://relay.seemlab.top'
    admission_db_path: Path = Path('/var/lib/ccb-mobile-relay/relay-admission.sqlite3')
    state_dir: Path = Path('/var/lib/ccb-mobile-relay')
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    unsafe_plaintext_for_tests: bool = False
    max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES
    websocket_max_msg_bytes: int | None = None
    peer_queue_limit: int = 8
    write_timeout: float = 5.0
    handshake_timeout: float = 10.0
    idle_timeout: float = 60.0
    heartbeat_interval: float = 20.0
    unauth_rate_limit: int = 30
    unauth_rate_limit_window: float = 60.0
    unauth_rate_limit_max_keys: int = 10_000
    trusted_proxy_cidrs: tuple[str, ...] = ('127.0.0.1/32', '::1/128')

    def validate(self) -> 'ProductionRelayConfig':
        if self.listen_port < 0 or self.listen_port > 65535:
            raise ValueError('relay listen port is invalid')
        if self.admin_port < 0 or self.admin_port > 65535:
            raise ValueError('relay admin port is invalid')
        if not _is_loopback_host(self.admin_host):
            raise ValueError('relay admin listener must be loopback-only')
        if self.max_frame_bytes <= 0:
            raise ValueError('relay max frame bytes must be positive')
        if self.protocol_max_msg_bytes() < self.max_frame_bytes:
            raise ValueError('relay websocket max message bytes must cover semantic frame limit')
        if self.peer_queue_limit <= 0:
            raise ValueError('relay peer queue limit must be positive')
        if self.write_timeout <= 0 or self.handshake_timeout <= 0 or self.idle_timeout <= 0:
            raise ValueError('relay timeouts must be positive')
        if self.heartbeat_interval <= 0:
            raise ValueError('relay heartbeat interval must be positive')
        if (
            self.unauth_rate_limit <= 0
            or self.unauth_rate_limit_window <= 0
            or self.unauth_rate_limit_max_keys <= 0
        ):
            raise ValueError('relay unauthenticated rate limit must be positive')
        if not self.public_origin.startswith('wss://'):
            raise ValueError('relay public origin must be wss://')
        _parse_trusted_proxy_networks(self.trusted_proxy_cidrs)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        if self.unsafe_plaintext_for_tests:
            if not _is_loopback_host(self.listen_host):
                raise ValueError('unsafe plaintext relay test mode is restricted to loopback')
            return self
        if self.tls_cert_file is None or self.tls_key_file is None:
            raise ValueError('TLS certificate and key are required for the relay listener')
        _require_readable_file(self.tls_cert_file, 'TLS certificate')
        _require_owner_only_file(self.tls_key_file, 'TLS private key')
        return self

    def ssl_context(self) -> ssl.SSLContext | None:
        self.validate()
        if self.unsafe_plaintext_for_tests:
            return None
        assert self.tls_cert_file is not None
        assert self.tls_key_file is not None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(str(self.tls_cert_file), str(self.tls_key_file))
        return context

    def protocol_max_msg_bytes(self) -> int:
        return int(self.websocket_max_msg_bytes or (self.max_frame_bytes + 4096))

    def rendezvous_replay_db_path(self) -> Path:
        return self.state_dir / 'relay-rendezvous-replay.sqlite3'

    @classmethod
    def from_env(cls) -> 'ProductionRelayConfig':
        listen = os.environ.get('CCB_RELAY_LISTEN', f'127.0.0.1:{_DEFAULT_LOOPBACK_PORT}')
        host, port = _parse_listen(listen)
        admin_listen = os.environ.get('CCB_RELAY_ADMIN_LISTEN', f'127.0.0.1:{_DEFAULT_ADMIN_PORT}')
        admin_host, admin_port = _parse_listen(admin_listen)
        return cls(
            listen_host=host,
            listen_port=port,
            admin_host=admin_host,
            admin_port=admin_port,
            public_origin=os.environ.get('CCB_RELAY_PUBLIC_ORIGIN', 'wss://relay.seemlab.top'),
            admission_db_path=Path(os.environ.get('CCB_RELAY_ADMISSION_DB', '/var/lib/ccb-mobile-relay/relay-admission.sqlite3')),
            state_dir=Path(os.environ.get('CCB_RELAY_STATE_DIR', '/var/lib/ccb-mobile-relay')),
            tls_cert_file=_optional_path(os.environ.get('CCB_RELAY_TLS_CERT')),
            tls_key_file=_optional_path(os.environ.get('CCB_RELAY_TLS_KEY')),
            max_frame_bytes=int(
                os.environ.get('CCB_RELAY_MAX_FRAME_BYTES', str(_DEFAULT_MAX_FRAME_BYTES))
            ),
            websocket_max_msg_bytes=_optional_int(os.environ.get('CCB_RELAY_WEBSOCKET_MAX_MSG_BYTES')),
            peer_queue_limit=int(os.environ.get('CCB_RELAY_PEER_QUEUE_LIMIT', '8')),
            write_timeout=float(os.environ.get('CCB_RELAY_WRITE_TIMEOUT_SECONDS', '5')),
            handshake_timeout=float(os.environ.get('CCB_RELAY_HANDSHAKE_TIMEOUT_SECONDS', '10')),
            idle_timeout=float(os.environ.get('CCB_RELAY_IDLE_TIMEOUT_SECONDS', '60')),
            heartbeat_interval=float(os.environ.get('CCB_RELAY_HEARTBEAT_INTERVAL_SECONDS', '20')),
            unauth_rate_limit=int(os.environ.get('CCB_RELAY_UNAUTH_RATE_LIMIT', '30')),
            unauth_rate_limit_window=float(os.environ.get('CCB_RELAY_UNAUTH_RATE_LIMIT_WINDOW_SECONDS', '60')),
            unauth_rate_limit_max_keys=int(os.environ.get('CCB_RELAY_UNAUTH_RATE_LIMIT_MAX_KEYS', '10000')),
            trusted_proxy_cidrs=_csv_tuple(os.environ.get('CCB_RELAY_TRUSTED_PROXIES'), default=('127.0.0.1/32', '::1/128')),
            unsafe_plaintext_for_tests=os.environ.get('CCB_RELAY_UNSAFE_PLAINTEXT_FOR_TESTS') == '1',
        )


@dataclass
class _Metrics:
    activation_attempts: int = 0
    activation_successes: int = 0
    host_connections: int = 0
    phone_connections: int = 0
    sessions_opened: int = 0
    sessions_closed: int = 0
    frames_forwarded: int = 0
    bytes_forwarded: int = 0
    rejected_frames: int = 0
    rate_limited: int = 0
    slow_consumer_disconnects: int = 0
    payload_bytes_persisted: int = 0

    def snapshot(self, *, draining: bool, active_hosts: int, active_sessions: int) -> dict[str, object]:
        return {
            'activation_attempts': self.activation_attempts,
            'activation_successes': self.activation_successes,
            'host_connections': self.host_connections,
            'phone_connections': self.phone_connections,
            'sessions_opened': self.sessions_opened,
            'sessions_closed': self.sessions_closed,
            'frames_forwarded': self.frames_forwarded,
            'bytes_forwarded': self.bytes_forwarded,
            'rejected_frames': self.rejected_frames,
            'rate_limited': self.rate_limited,
            'slow_consumer_disconnects': self.slow_consumer_disconnects,
            'payload_bytes_persisted': self.payload_bytes_persisted,
            'active_hosts': active_hosts,
            'active_sessions': active_sessions,
            'draining': draining,
        }


@dataclass
class _PeerEndpoint:
    role: str
    websocket: web.WebSocketResponse
    queue_limit: int
    write_timeout: float
    queue: asyncio.Queue[dict[str, object] | None] = field(init=False)
    writer_task: asyncio.Task[None] | None = None
    host_id: str = ''
    closed: bool = False

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.queue_limit)

    def start_writer(self, on_slow_consumer) -> None:
        self.writer_task = asyncio.create_task(self._writer(on_slow_consumer))

    async def send_frame(self, frame: Mapping[str, object]) -> None:
        if self.closed:
            raise MobileRelayError('relay peer is closed')
        try:
            await asyncio.wait_for(
                self.queue.put(dict(frame)),
                timeout=self.write_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise MobileRelayError('relay peer queue full') from exc

    async def close(self, *, code: int = 1000, message: str = '') -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        await self.websocket.close(code=code, message=message.encode('utf-8'))
        task = self.writer_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _writer(self, on_slow_consumer) -> None:
        try:
            while True:
                frame = await self.queue.get()
                if frame is None:
                    return
                try:
                    await asyncio.wait_for(
                        self.websocket.send_str(_canonical_json(frame)),
                        timeout=self.write_timeout,
                    )
                except Exception:
                    on_slow_consumer(self)
                    await self.close(code=1011, message='slow_consumer')
                    return
        except asyncio.CancelledError:
            return


@dataclass
class _RelaySessionState:
    session_id: str
    host_id: str
    client_hello: RelayFrame
    host: _PeerEndpoint
    phone: _PeerEndpoint
    outer_seq_by_role: dict[str, int] = field(default_factory=lambda: {'host': 0, 'phone': 0})
    envelope_seq_by_direction: dict[RelayDirection, int] = field(
        default_factory=lambda: {
            RelayDirection.PHONE_TO_HOST: 0,
            RelayDirection.HOST_TO_PHONE: 0,
        }
    )
    closed: bool = False


class _SlidingWindowRateLimiter:
    def __init__(self, *, limit: int, window_seconds: float, max_keys: int) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        self._prune(now)
        hits = self._hits.get(key)
        if hits is None:
            while len(self._hits) >= self._max_keys:
                self._hits.popitem(last=False)
            hits = deque()
            self._hits[key] = hits
        else:
            self._hits.move_to_end(key)
        while hits and now - hits[0] > self._window_seconds:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True

    @property
    def key_count(self) -> int:
        return len(self._hits)

    def _prune(self, now: float) -> None:
        for key in tuple(self._hits):
            hits = self._hits[key]
            while hits and now - hits[0] > self._window_seconds:
                hits.popleft()
            if hits:
                continue
            del self._hits[key]


class _RendezvousReplayStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._initialized = False

    def claim(self, *, replay_key: str, host_id: str, expires_at: int) -> None:
        self._ensure_initialized()
        now = int(time.time())
        digest = hashlib.sha256(str(replay_key).encode('utf-8')).hexdigest()
        with closing(sqlite3.connect(self._path)) as conn:
            conn.execute('DELETE FROM relay_rendezvous_claims WHERE expires_at <= ?', (now,))
            try:
                conn.execute(
                    '''
                    INSERT INTO relay_rendezvous_claims(claim_hash, host_id, claimed_at, expires_at)
                    VALUES (?, ?, ?, ?)
                    ''',
                    (digest, host_id, now, int(expires_at)),
                )
            except sqlite3.IntegrityError as exc:
                raise MobileRelayError('relay rendezvous capability replay rejected') from exc
            conn.commit()

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)
        with closing(sqlite3.connect(self._path)) as conn:
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS relay_rendezvous_claims(
                    claim_hash TEXT PRIMARY KEY,
                    host_id TEXT NOT NULL,
                    claimed_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )
                '''
            )
            conn.execute('CREATE INDEX IF NOT EXISTS idx_relay_rendezvous_expires ON relay_rendezvous_claims(expires_at)')
            conn.commit()
        self._path.chmod(0o600)
        self._initialized = True


class _RelayInstanceLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError('another relay service instance owns the admission database') from exc
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class ProductionRelayService:
    def __init__(self, config: ProductionRelayConfig, *, admission_store: RelayAdmissionStore) -> None:
        self.config = config
        self._store = admission_store
        self._rendezvous_replays = _RendezvousReplayStore(config.rendezvous_replay_db_path())
        self._instance_lock = _RelayInstanceLock(config.state_dir / 'relay-service.lock')
        self._metrics = _Metrics()
        self._rate_limiter = _SlidingWindowRateLimiter(
            limit=config.unauth_rate_limit,
            window_seconds=config.unauth_rate_limit_window,
            max_keys=config.unauth_rate_limit_max_keys,
        )
        self._trusted_proxy_networks = _parse_trusted_proxy_networks(config.trusted_proxy_cidrs)
        self._hosts: dict[str, _PeerEndpoint] = {}
        self._sessions: dict[str, _RelaySessionState] = {}
        self._sessions_by_host: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._bound_port: int | None = None
        self._admin_app: web.Application | None = None
        self._admin_runner: web.AppRunner | None = None
        self._admin_site: web.TCPSite | None = None
        self._admin_bound_port: int | None = None
        self._draining = False

    @property
    def port(self) -> int:
        if self._bound_port is None:
            raise RuntimeError('relay service is not started')
        return self._bound_port

    def url(self, path: str) -> str:
        scheme = 'ws' if self.config.unsafe_plaintext_for_tests else 'wss'
        host = '127.0.0.1' if self.config.listen_host in {'0.0.0.0', '::'} else self.config.listen_host
        return f'{scheme}://{host}:{self.port}{path}'

    @property
    def admin_port(self) -> int:
        if self._admin_bound_port is None:
            raise RuntimeError('relay admin service is not started')
        return self._admin_bound_port

    def admin_url(self, path: str) -> str:
        host = '127.0.0.1' if self.config.admin_host in {'0.0.0.0', '::', 'localhost'} else self.config.admin_host
        return f'http://{host}:{self.admin_port}{path}'

    async def start(self) -> None:
        self.config.validate()
        self._instance_lock.acquire()
        try:
            self._store.reconcile_active_sessions()
            self._draining = False
            app = web.Application(client_max_size=self.config.protocol_max_msg_bytes())
            app.router.add_post('/v2/activate', self._activate_host)
            app.router.add_get('/v2/host', self._host_socket)
            app.router.add_get('/v2/phone', self._phone_socket)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(
                runner,
                host=self.config.listen_host,
                port=self.config.listen_port,
                ssl_context=self.config.ssl_context(),
            )
            await site.start()
            server = site._server
            if server is None or not server.sockets:
                raise RuntimeError('relay service did not bind a socket')
            self._bound_port = int(server.sockets[0].getsockname()[1])
            self._app = app
            self._runner = runner
            self._site = site
            admin_app = web.Application(client_max_size=4096)
            admin_app.router.add_get('/healthz', self._health)
            admin_app.router.add_get('/readyz', self._ready)
            admin_app.router.add_get('/metrics', self._metrics_response)
            admin_runner = web.AppRunner(admin_app, access_log=None)
            await admin_runner.setup()
            admin_site = web.TCPSite(admin_runner, host=self.config.admin_host, port=self.config.admin_port)
            await admin_site.start()
            admin_server = admin_site._server
            if admin_server is None or not admin_server.sockets:
                raise RuntimeError('relay admin service did not bind a socket')
            self._admin_bound_port = int(admin_server.sockets[0].getsockname()[1])
            self._admin_app = admin_app
            self._admin_runner = admin_runner
            self._admin_site = admin_site
        except Exception:
            await self._cleanup_runners()
            self._instance_lock.release()
            raise

    async def stop(self) -> None:
        if self._runner is not None or self._admin_runner is not None:
            await self.drain()
        await self._cleanup_runners()
        self._instance_lock.release()

    async def _cleanup_runners(self) -> None:
        admin_runner = self._admin_runner
        if admin_runner is not None:
            await admin_runner.cleanup()
        runner = self._runner
        if runner is not None:
            await runner.cleanup()
        self._admin_runner = None
        self._admin_site = None
        self._admin_app = None
        self._admin_bound_port = None
        self._runner = None
        self._site = None
        self._app = None
        self._bound_port = None

    async def drain(self, *, timeout: float = 2.0) -> None:
        self._draining = True
        async with self._lock:
            sessions = list(self._sessions.values())
            hosts = list(self._hosts.values())
        await asyncio.gather(*(self._close_session(session.session_id, reason='relay_draining') for session in sessions), return_exceptions=True)
        await asyncio.gather(*(host.close(code=1001, message='relay_draining') for host in hosts), return_exceptions=True)
        if timeout > 0:
            await asyncio.sleep(min(timeout, 0.05))

    async def serve_forever(self) -> None:
        await self.start()
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stop_event.set)
            except NotImplementedError:  # pragma: no cover - Windows fallback
                pass
        try:
            await stop_event.wait()
        finally:
            await self.stop()

    def metrics_snapshot(self) -> dict[str, object]:
        return self._metrics.snapshot(
            draining=self._draining,
            active_hosts=len(self._hosts),
            active_sessions=len(self._sessions),
        )

    async def _health(self, request: web.Request) -> web.Response:
        self._reject_non_loopback_admin(request)
        return web.json_response(
            {
                'status': 'ok',
                'schema_version': 2,
                'service': 'ccb-mobile-production-relay',
            }
        )

    async def _ready(self, request: web.Request) -> web.Response:
        self._reject_non_loopback_admin(request)
        status = 503 if self._draining else 200
        return web.json_response({'ready': not self._draining, 'draining': self._draining}, status=status)

    async def _metrics_response(self, request: web.Request) -> web.Response:
        self._reject_non_loopback_admin(request)
        metrics = self.metrics_snapshot()
        lines = [f'ccb_relay_{key} {int(value) if isinstance(value, bool) else value}' for key, value in sorted(metrics.items())]
        return web.Response(text='\n'.join(lines) + '\n', content_type='text/plain')

    async def _activate_host(self, request: web.Request) -> web.Response:
        self._reject_if_draining()
        self._check_rate_limit(request)
        self._metrics.activation_attempts += 1
        try:
            payload = await request.json(loads=json.loads)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise web.HTTPBadRequest(text=_PUBLIC_ERROR_MESSAGES['relay_rejected']) from None
        if not isinstance(payload, Mapping):
            raise web.HTTPBadRequest(text=_PUBLIC_ERROR_MESSAGES['relay_rejected'])
        invitation = str(payload.get('invitation') or '').strip()
        host_public_key_b64 = str(payload.get('host_public_key_b64') or '').strip()
        if not invitation or not host_public_key_b64:
            raise web.HTTPBadRequest(text=_PUBLIC_ERROR_MESSAGES['relay_rejected'])
        try:
            credential = self._store.claim_invitation(
                invitation,
                host_public_key_b64=host_public_key_b64,
            )
        except RelayAdmissionError:
            raise web.HTTPUnauthorized(text=_PUBLIC_ERROR_MESSAGES['relay_auth_rejected']) from None
        self._metrics.activation_successes += 1
        return web.json_response(credential.to_json(), status=201)

    async def _host_socket(self, request: web.Request) -> web.StreamResponse:
        self._reject_if_draining()
        self._check_rate_limit(request)
        ws = web.WebSocketResponse(
            max_msg_size=self.config.protocol_max_msg_bytes(),
            heartbeat=self.config.heartbeat_interval,
        )
        await ws.prepare(request)
        endpoint = _PeerEndpoint(
            role='host',
            websocket=ws,
            queue_limit=self.config.peer_queue_limit,
            write_timeout=self.config.write_timeout,
        )
        endpoint.start_writer(self._note_slow_consumer)
        host_id = ''
        try:
            frame = await self._receive_frame(
                ws,
                timeout=min(self.config.handshake_timeout, self.config.idle_timeout),
            )
            host_id = await self._register_host(endpoint, frame)
            endpoint.host_id = host_id
            self._metrics.host_connections += 1
            await endpoint.send_frame(_ack_frame(frame, {'host_id': host_id}))
            await self._host_reader(endpoint)
        except Exception as exc:
            await self._send_error_and_close(endpoint, _public_error_code(exc))
        finally:
            if host_id:
                await self._disconnect_host(host_id)
            await endpoint.close()
        return ws

    async def _phone_socket(self, request: web.Request) -> web.StreamResponse:
        self._reject_if_draining()
        self._check_rate_limit(request)
        ws = web.WebSocketResponse(
            max_msg_size=self.config.protocol_max_msg_bytes(),
            heartbeat=self.config.heartbeat_interval,
        )
        await ws.prepare(request)
        endpoint = _PeerEndpoint(
            role='phone',
            websocket=ws,
            queue_limit=self.config.peer_queue_limit,
            write_timeout=self.config.write_timeout,
        )
        endpoint.start_writer(self._note_slow_consumer)
        session_id = ''
        try:
            frame = await self._receive_frame(
                ws,
                timeout=min(self.config.handshake_timeout, self.config.idle_timeout),
            )
            session = await self._open_phone_session(endpoint, frame)
            session_id = session.session_id
            self._metrics.phone_connections += 1
            await session.host.send_frame(frame.to_json())
            await self._phone_reader(endpoint, session.session_id)
        except Exception as exc:
            await self._send_error_and_close(endpoint, _public_error_code(exc))
        finally:
            if session_id:
                await self._close_session(session_id, reason='phone_disconnected')
            await endpoint.close()
        return ws

    async def _register_host(self, endpoint: _PeerEndpoint, frame: RelayFrame) -> str:
        if frame.kind != 'host_register':
            raise MobileRelayError('relay host_register frame required')
        payload = frame.payload
        host_id = str(payload['host_id'])
        capability = self._store.issue_session_capability(
            host_id=host_id,
            nonce_b64=str(payload['nonce_b64']),
            proof_expires_at=int(payload['proof_expires_at']),
            signature_b64=str(payload['signature_b64']),
            scopes=('relay.connect', 'relay.forward'),
        )
        self._store.verify_session_capability(str(capability['capability']))
        async with self._lock:
            if host_id in self._hosts:
                raise MobileRelayError('relay host identity already connected')
            self._hosts[host_id] = endpoint
            self._sessions_by_host.setdefault(host_id, set())
        return host_id

    async def _open_phone_session(self, endpoint: _PeerEndpoint, frame: RelayFrame) -> _RelaySessionState:
        if frame.kind != 'client_hello':
            raise MobileRelayError('relay client_hello frame required')
        authorization = self._verify_phone_authorization(frame)
        host_id = str(frame.payload['host_id'])
        session_id = frame.session_id
        async with self._lock:
            host = self._hosts.get(host_id)
            if host is None or host.closed:
                raise MobileRelayError('relay host is not connected')
            if session_id in self._sessions:
                raise MobileRelayError('relay session identity already connected')
        reservation = self._store.reserve_host_session(
            host_id=host_id,
            session_id=session_id,
        )
        if reservation.get('idempotent') is True:
            raise MobileRelayError('relay session identity already connected')
        committed = False
        try:
            self._rendezvous_replays.claim(
                replay_key=authorization.replay_key(),
                host_id=host_id,
                expires_at=authorization.expires_at,
            )
            session = _RelaySessionState(
                session_id=session_id,
                host_id=host_id,
                client_hello=frame,
                host=host,
                phone=endpoint,
            )
            async with self._lock:
                current_host = self._hosts.get(host_id)
                if current_host is not host or host.closed:
                    raise MobileRelayError('relay host is not connected')
                if session_id in self._sessions:
                    raise MobileRelayError('relay session identity already connected')
                self._sessions[session_id] = session
                self._sessions_by_host.setdefault(host_id, set()).add(session_id)
                self._metrics.sessions_opened += 1
                committed = True
            return session
        finally:
            if not committed:
                await self._release_reserved_session(host_id, session_id)

    def _verify_phone_authorization(
        self,
        frame: RelayFrame,
    ) -> RelayRendezvousCapability | RelayPhoneSessionProof:
        payload = frame.payload
        host_id = str(payload.get('host_id') or '')
        device_id = str(payload.get('device_id') or '')
        client_pubkey_b64 = str(payload.get('client_pubkey_b64') or '')
        phone_nonce_b64 = str(payload.get('phone_nonce_b64') or '')
        access_token = str(payload.get('access_grant') or '')
        proof_token = str(payload.get('phone_session_proof') or '')
        host_public_key_b64 = self._store.host_public_key_for_rendezvous(host_id)
        if access_token or proof_token:
            if not access_token or not proof_token or not phone_nonce_b64:
                raise MobileRelayError('relay phone session proof required')
            grant = RelayAccessGrant.from_token(access_token).verify(
                host_public_key_b64=host_public_key_b64,
                host_id=host_id,
                device_id=device_id,
                audience=self.config.public_origin,
                now=int(time.time()),
            )
            return RelayPhoneSessionProof.from_token(proof_token).verify(
                grant=grant,
                host_id=host_id,
                device_id=device_id,
                session_id=frame.session_id,
                client_pubkey_b64=client_pubkey_b64,
                phone_nonce_b64=phone_nonce_b64,
                audience=self.config.public_origin,
                now=int(time.time()),
            )
        token = str(payload.get('rendezvous_capability') or '')
        if not token or not phone_nonce_b64:
            raise MobileRelayError('relay rendezvous capability required')
        return RelayRendezvousCapability.from_token(token).verify(
            host_public_key_b64=host_public_key_b64,
            host_id=host_id,
            session_id=frame.session_id,
            client_pubkey_b64=client_pubkey_b64,
            phone_nonce_b64=phone_nonce_b64,
            audience=self.config.public_origin,
            now=int(time.time()),
        )

    async def _host_reader(self, endpoint: _PeerEndpoint) -> None:
        while not endpoint.closed and not self._draining:
            frame = await self._receive_frame(endpoint.websocket, timeout=None)
            if frame.kind == 'heartbeat':
                await endpoint.send_frame(_ack_frame(frame))
                continue
            if frame.kind == 'close':
                await self._close_session(frame.session_id, reason='host_closed')
                continue
            session = self._sessions.get(frame.session_id)
            if session is None or session.host is not endpoint:
                raise MobileRelayError('relay session is not established')
            if frame.kind == 'host_hello':
                RelayHandshakeTranscript.negotiate(client_hello=session.client_hello, host_hello=frame)
                await session.phone.send_frame(frame.to_json())
                continue
            if frame.kind == 'gateway_envelope':
                await self._forward_gateway_envelope(
                    session,
                    frame,
                    source_role='host',
                    expected_direction=RelayDirection.HOST_TO_PHONE,
                    destination=session.phone,
                )
                continue
            raise MobileRelayError(f'relay host frame is not allowed: {frame.kind}')

    async def _phone_reader(self, endpoint: _PeerEndpoint, session_id: str) -> None:
        while not endpoint.closed and not self._draining:
            frame = await self._receive_frame(endpoint.websocket, timeout=None)
            if frame.kind == 'heartbeat':
                await endpoint.send_frame(_ack_frame(frame))
                continue
            if frame.kind == 'close':
                await self._close_session(session_id, reason='phone_closed')
                return
            session = self._sessions.get(session_id)
            if session is None or session.phone is not endpoint:
                raise MobileRelayError('relay session is not established')
            if frame.kind != 'gateway_envelope':
                raise MobileRelayError(f'relay phone frame is not allowed: {frame.kind}')
            await self._forward_gateway_envelope(
                session,
                frame,
                source_role='phone',
                expected_direction=RelayDirection.PHONE_TO_HOST,
                destination=session.host,
            )

    async def _forward_gateway_envelope(
        self,
        session: _RelaySessionState,
        frame: RelayFrame,
        *,
        source_role: str,
        expected_direction: RelayDirection,
        destination: _PeerEndpoint,
    ) -> None:
        self._require_host_active(session.host_id)
        last_outer_seq = session.outer_seq_by_role[source_role]
        if frame.seq <= last_outer_seq:
            raise MobileRelayError('relay outer frame replay or reorder rejected')
        envelope = RelayV2Envelope.from_json(_object_map(frame.payload.get('envelope'), 'gateway_envelope.envelope'))
        if envelope.session_id != session.session_id:
            raise MobileRelayError('relay gateway envelope session mismatch')
        if envelope.direction != expected_direction:
            raise MobileRelayError('relay gateway envelope direction mismatch')
        last_envelope_seq = session.envelope_seq_by_direction[expected_direction]
        if envelope.seq <= last_envelope_seq:
            raise MobileRelayError('relay gateway envelope replay or reorder rejected')
        encoded = _canonical_json(frame.to_json()).encode('utf-8')
        self._store.record_host_bytes(host_id=session.host_id, byte_count=len(encoded))
        session.outer_seq_by_role[source_role] = frame.seq
        session.envelope_seq_by_direction[expected_direction] = envelope.seq
        try:
            await destination.send_frame(frame.to_json())
        except MobileRelayError as exc:
            if 'queue full' in str(exc):
                self._note_slow_consumer(destination)
                await self._close_session(session.session_id, reason='slow_consumer')
            raise
        self._metrics.frames_forwarded += 1
        self._metrics.bytes_forwarded += len(encoded)

    async def _receive_frame(self, ws: web.WebSocketResponse, *, timeout: float | None) -> RelayFrame:
        try:
            message = await ws.receive() if timeout is None else await asyncio.wait_for(ws.receive(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise MobileRelayError('relay peer idle timeout') from exc
        if message.type == WSMsgType.TEXT:
            raw = message.data
            if len(raw.encode('utf-8')) > self.config.max_frame_bytes:
                raise MobileRelayError('relay frame too large')
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MobileRelayError('relay frame JSON invalid') from exc
            if not isinstance(payload, Mapping):
                raise MobileRelayError('relay frame must be a JSON object')
            try:
                return RelayFrame.from_json(payload)
            except MobileRelayError:
                self._metrics.rejected_frames += 1
                raise
        if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
            raise MobileRelayError('relay peer closed')
        if message.type == WSMsgType.ERROR:
            raise MobileRelayError('relay websocket error')
        raise MobileRelayError('relay frame rejected')

    async def _send_error_and_close(self, endpoint: _PeerEndpoint, code: str) -> None:
        self._metrics.rejected_frames += 1
        if not endpoint.websocket.closed:
            public_code = code if code in _PUBLIC_ERROR_MESSAGES else 'relay_rejected'
            error_frame = {
                'schema_version': 2,
                'session_id': 'relay-control',
                'seq': 1,
                'kind': 'error',
                'payload': {
                    'code': public_code,
                    'message': _PUBLIC_ERROR_MESSAGES[public_code],
                },
            }
            try:
                await asyncio.wait_for(
                    endpoint.websocket.send_str(_canonical_json(error_frame)),
                    timeout=self.config.write_timeout,
                )
            except Exception:
                pass
        await endpoint.close(code=1008, message='relay_error')

    async def _close_session(self, session_id: str, *, reason: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None or session.closed:
                return
            session.closed = True
            self._sessions_by_host.get(session.host_id, set()).discard(session_id)
            self._metrics.sessions_closed += 1
        await self._release_reserved_session(session.host_id, session.session_id)
        close_frame = {
            'schema_version': 2,
            'session_id': session.session_id,
            'seq': 1,
            'kind': 'close',
            'payload': {'reason': reason},
        }
        await asyncio.gather(
            session.host.send_frame(close_frame),
            session.phone.send_frame(close_frame),
            return_exceptions=True,
        )
        await asyncio.gather(
            session.phone.close(code=1000, message=reason),
            return_exceptions=True,
        )

    async def _disconnect_host(self, host_id: str) -> None:
        async with self._lock:
            self._hosts.pop(host_id, None)
            session_ids = tuple(self._sessions_by_host.get(host_id, set()))
        await asyncio.gather(
            *(self._close_session(session_id, reason='host_disconnected') for session_id in session_ids),
            return_exceptions=True,
        )

    async def _release_reserved_session(self, host_id: str, session_id: str) -> None:
        try:
            self._store.release_host_session(host_id=host_id, session_id=session_id)
        except RelayAdmissionError:
            pass

    def _require_host_active(self, host_id: str) -> None:
        status = self._store.host_status(host_id)
        if status.get('state') != 'active':
            raise MobileRelayError('relay host is not active')

    def _reject_if_draining(self) -> None:
        if self._draining:
            raise web.HTTPServiceUnavailable(text=_PUBLIC_ERROR_MESSAGES['relay_unavailable'])

    def _check_rate_limit(self, request: web.Request) -> None:
        peer = self._client_rate_limit_key(request)
        if not self._rate_limiter.allow(peer):
            self._metrics.rate_limited += 1
            raise web.HTTPTooManyRequests(text=_PUBLIC_ERROR_MESSAGES['relay_rate_limited'])

    def _client_rate_limit_key(self, request: web.Request) -> str:
        peer = _parse_ip_address(request.remote or '')
        if peer is None:
            return 'unknown'
        if any(peer in network for network in self._trusted_proxy_networks):
            header = request.headers.get('X-CCB-Client-IP')
            if header:
                client = _strict_single_ip_header(header)
                forwarded_for = request.headers.get('X-Forwarded-For')
                if forwarded_for and _strict_single_ip_header(forwarded_for) != client:
                    raise web.HTTPBadRequest(text=_PUBLIC_ERROR_MESSAGES['relay_rejected'])
                return str(client)
        return str(peer)

    def _reject_non_loopback_admin(self, request: web.Request) -> None:
        peer = _parse_ip_address(request.remote or '')
        if peer is None or not peer.is_loopback:
            raise web.HTTPForbidden(text=_PUBLIC_ERROR_MESSAGES['relay_rejected'])

    def _note_slow_consumer(self, _endpoint: _PeerEndpoint) -> None:
        self._metrics.slow_consumer_disconnects += 1


def _ack_frame(frame: RelayFrame, extra: Mapping[str, object] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {'ack_seq': frame.seq}
    if extra:
        payload.update(dict(extra))
    return {
        'schema_version': 2,
        'session_id': frame.session_id,
        'seq': frame.seq + 1,
        'kind': 'ack',
        'payload': payload,
    }


def _public_error_code(error: BaseException) -> str:
    if isinstance(error, RelayAdmissionError):
        return 'relay_auth_rejected'
    text = str(error)
    if 'rendezvous' in text or 'proof' in text or 'host is not active' in text:
        return 'relay_auth_rejected'
    if 'too large' in text or 'JSON invalid' in text or 'unknown relay frame kind' in text:
        return 'relay_frame_rejected'
    if 'unavailable' in text or 'draining' in text:
        return 'relay_unavailable'
    return 'relay_rejected'


def _object_map(value: object, name: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    raise MobileRelayError(f'relay field must be an object: {name}')


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=_JSON_SEPARATORS)


def _parse_listen(value: str) -> tuple[str, int]:
    text = str(value or '').strip()
    if ':' not in text:
        raise ValueError('relay listen must be host:port')
    host, port_text = text.rsplit(':', 1)
    return host.strip() or '127.0.0.1', int(port_text)


def _csv_tuple(value: str | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(',') if item.strip())


def _optional_path(value: str | None) -> Path | None:
    text = str(value or '').strip()
    return Path(text).expanduser() if text else None


def _optional_int(value: str | None) -> int | None:
    text = str(value or '').strip()
    return int(text) if text else None


def _is_loopback_host(value: str) -> bool:
    text = str(value or '').strip().lower()
    if text == 'localhost':
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _parse_ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(str(value or '').strip())
    except ValueError:
        return None


def _parse_trusted_proxy_networks(values: tuple[str, ...]) -> tuple[ipaddress._BaseNetwork, ...]:
    networks: list[ipaddress._BaseNetwork] = []
    for value in values:
        networks.append(ipaddress.ip_network(value, strict=False))
    return tuple(networks)


def _strict_single_ip_header(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    text = str(value or '').strip()
    if not text or ',' in text or ' ' in text or '\t' in text:
        raise web.HTTPBadRequest(text=_PUBLIC_ERROR_MESSAGES['relay_rejected'])
    parsed = _parse_ip_address(text)
    if parsed is None:
        raise web.HTTPBadRequest(text=_PUBLIC_ERROR_MESSAGES['relay_rejected'])
    return parsed


def _require_readable_file(path: Path, label: str) -> None:
    if not path.exists() or not path.is_file():
        raise ValueError(f'{label} is unavailable')


def _require_owner_only_file(path: Path, label: str) -> None:
    _require_readable_file(path, label)
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(f'{label} must be owner-only')


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Run the production CCB Mobile Relay service')
    parser.add_argument('--listen', default=os.environ.get('CCB_RELAY_LISTEN', f'127.0.0.1:{_DEFAULT_LOOPBACK_PORT}'))
    parser.add_argument('--admin-listen', default=os.environ.get('CCB_RELAY_ADMIN_LISTEN', f'127.0.0.1:{_DEFAULT_ADMIN_PORT}'))
    parser.add_argument('--public-origin', default=os.environ.get('CCB_RELAY_PUBLIC_ORIGIN', 'wss://relay.seemlab.top'))
    parser.add_argument('--db', dest='admission_db_path', default=os.environ.get('CCB_RELAY_ADMISSION_DB', '/var/lib/ccb-mobile-relay/relay-admission.sqlite3'))
    parser.add_argument('--secrets', dest='secrets_path', default=None)
    parser.add_argument('--state-dir', default=os.environ.get('CCB_RELAY_STATE_DIR', '/var/lib/ccb-mobile-relay'))
    parser.add_argument('--tls-cert', default=os.environ.get('CCB_RELAY_TLS_CERT'))
    parser.add_argument('--tls-key', default=os.environ.get('CCB_RELAY_TLS_KEY'))
    parser.add_argument(
        '--max-frame-bytes',
        type=int,
        default=int(
            os.environ.get('CCB_RELAY_MAX_FRAME_BYTES', str(_DEFAULT_MAX_FRAME_BYTES))
        ),
    )
    parser.add_argument('--websocket-max-msg-bytes', type=int, default=_optional_int(os.environ.get('CCB_RELAY_WEBSOCKET_MAX_MSG_BYTES')))
    parser.add_argument(
        '--peer-queue-limit',
        type=int,
        default=int(os.environ.get('CCB_RELAY_PEER_QUEUE_LIMIT', '8')),
    )
    parser.add_argument('--write-timeout-seconds', type=float, default=float(os.environ.get('CCB_RELAY_WRITE_TIMEOUT_SECONDS', '5')))
    parser.add_argument('--handshake-timeout-seconds', type=float, default=float(os.environ.get('CCB_RELAY_HANDSHAKE_TIMEOUT_SECONDS', '10')))
    parser.add_argument('--idle-timeout-seconds', type=float, default=float(os.environ.get('CCB_RELAY_IDLE_TIMEOUT_SECONDS', '60')))
    parser.add_argument('--heartbeat-interval-seconds', type=float, default=float(os.environ.get('CCB_RELAY_HEARTBEAT_INTERVAL_SECONDS', '20')))
    parser.add_argument('--unauth-rate-limit', type=int, default=int(os.environ.get('CCB_RELAY_UNAUTH_RATE_LIMIT', '30')))
    parser.add_argument('--unauth-rate-window-seconds', type=float, default=float(os.environ.get('CCB_RELAY_UNAUTH_RATE_LIMIT_WINDOW_SECONDS', '60')))
    parser.add_argument('--unauth-rate-limit-max-keys', type=int, default=int(os.environ.get('CCB_RELAY_UNAUTH_RATE_LIMIT_MAX_KEYS', '10000')))
    parser.add_argument('--trusted-proxy', action='append', default=None)
    parser.add_argument('--unsafe-plaintext-for-tests', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    listen_host, listen_port = _parse_listen(str(args.listen))
    admin_host, admin_port = _parse_listen(str(args.admin_listen))
    trusted_proxy_cidrs = tuple(args.trusted_proxy or _csv_tuple(os.environ.get('CCB_RELAY_TRUSTED_PROXIES'), default=('127.0.0.1/32', '::1/128')))
    config = ProductionRelayConfig(
        listen_host=listen_host,
        listen_port=listen_port,
        admin_host=admin_host,
        admin_port=admin_port,
        public_origin=str(args.public_origin),
        admission_db_path=Path(args.admission_db_path).expanduser(),
        state_dir=Path(args.state_dir).expanduser(),
        tls_cert_file=_optional_path(args.tls_cert),
        tls_key_file=_optional_path(args.tls_key),
        unsafe_plaintext_for_tests=bool(args.unsafe_plaintext_for_tests),
        max_frame_bytes=int(args.max_frame_bytes),
        websocket_max_msg_bytes=args.websocket_max_msg_bytes,
        peer_queue_limit=int(args.peer_queue_limit),
        write_timeout=float(args.write_timeout_seconds),
        handshake_timeout=float(args.handshake_timeout_seconds),
        idle_timeout=float(args.idle_timeout_seconds),
        heartbeat_interval=float(args.heartbeat_interval_seconds),
        unauth_rate_limit=int(args.unauth_rate_limit),
        unauth_rate_limit_window=float(args.unauth_rate_window_seconds),
        unauth_rate_limit_max_keys=int(args.unauth_rate_limit_max_keys),
        trusted_proxy_cidrs=trusted_proxy_cidrs,
    )
    store = RelayAdmissionStore(
        config.admission_db_path,
        admission_secrets=RelayAdmissionSecrets.from_operator_config(args.secrets_path),
    )
    service = ProductionRelayService(config, admission_store=store)
    asyncio.run(service.serve_forever())
    return 0


if __name__ == '__main__':  # pragma: no cover - exercised by service smoke commands
    raise SystemExit(main())


__all__ = [
    'ProductionRelayConfig',
    'ProductionRelayService',
    'main',
]
