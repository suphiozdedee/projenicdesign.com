from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time


_WEBSOCKET_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


@dataclass(frozen=True)
class CodexSteerResponse:
    accepted: bool
    turn_id: str | None = None
    reason: str = ''
    error: str = ''


@dataclass(frozen=True)
class _WebSocketExchange:
    initialize: dict[str, object] | None = None
    steer: dict[str, object] | None = None
    reason: str = ''
    error: str = ''


class _WebSocketProtocolError(RuntimeError):
    pass


def app_server_socket_ready(socket_path: str | Path, *, timeout_s: float = 0.3) -> bool:
    raw = str(socket_path or '').strip()
    if not raw:
        return False
    try:
        with _UnixWebSocket(Path(raw).expanduser(), timeout_s=max(0.1, float(timeout_s))):
            return True
    except (OSError, socket.timeout, _WebSocketProtocolError):
        return False


def steer_active_turn(
    socket_path: str | Path,
    *,
    thread_id: str,
    turn_id: str,
    followup_id: str,
    message: str,
    timeout_s: float = 8.0,
) -> CodexSteerResponse:
    normalized_socket = Path(socket_path).expanduser() if str(socket_path or '').strip() else None
    if normalized_socket is None:
        return CodexSteerResponse(False, reason='app_server_socket_missing')
    init_id = f'{followup_id}:initialize'
    steer_id = f'{followup_id}:steer'
    request_lines = (
        {
            'method': 'initialize',
            'id': init_id,
            'params': {
                'clientInfo': {
                    'name': 'ccb',
                    'title': 'Claude Codex Bridge',
                    'version': 'r9',
                }
            },
        },
        {'method': 'initialized', 'params': {}},
        {
            'method': 'turn/steer',
            'id': steer_id,
            'params': {
                'threadId': thread_id,
                'expectedTurnId': turn_id,
                'input': [{'type': 'text', 'text': message}],
                'clientUserMessageId': followup_id,
            },
        },
    )
    exchange = _websocket_exchange(
        normalized_socket,
        initialize=request_lines[0],
        initialized=request_lines[1],
        steer=request_lines[2],
        initialize_id=init_id,
        steer_id=steer_id,
        timeout_s=timeout_s,
    )
    if exchange.reason:
        return CodexSteerResponse(False, reason=exchange.reason, error=exchange.error)
    initialize = exchange.initialize
    if initialize is None or initialize.get('error') is not None:
        return CodexSteerResponse(
            False,
            reason='app_server_initialize_failed',
            error=_response_error(initialize, exchange.error),
        )
    steer = exchange.steer
    if steer is None:
        return CodexSteerResponse(
            False,
            reason='app_server_steer_response_missing',
            error=exchange.error,
        )
    if steer.get('error') is not None:
        error = _response_error(steer, exchange.error)
        return CodexSteerResponse(
            False,
            reason=_steer_error_reason(error),
            error=error,
        )
    result = steer.get('result') if isinstance(steer.get('result'), dict) else {}
    response_turn_id = str(result.get('turnId') or '').strip()
    if response_turn_id != turn_id:
        return CodexSteerResponse(
            False,
            turn_id=response_turn_id or None,
            reason='app_server_steer_turn_mismatch',
        )
    return CodexSteerResponse(True, turn_id=response_turn_id, reason='provider_turn_steered')


def _websocket_exchange(
    socket_path: Path,
    *,
    initialize: dict[str, object],
    initialized: dict[str, object],
    steer: dict[str, object],
    initialize_id: str,
    steer_id: str,
    timeout_s: float,
) -> _WebSocketExchange:
    timeout = max(0.1, float(timeout_s))
    deadline = time.monotonic() + timeout
    try:
        with _UnixWebSocket(socket_path, timeout_s=timeout) as client:
            client.send_json(initialize)
            initialize_response = client.receive_response(initialize_id, deadline=deadline)
            if initialize_response is None:
                return _WebSocketExchange(reason='app_server_websocket_timeout')
            if initialize_response.get('error') is not None:
                return _WebSocketExchange(initialize=initialize_response)
            client.send_json(initialized)
            client.send_json(steer)
            steer_response = client.receive_response(steer_id, deadline=deadline)
            if steer_response is None:
                return _WebSocketExchange(
                    initialize=initialize_response,
                    reason='app_server_websocket_timeout',
                )
            return _WebSocketExchange(
                initialize=initialize_response,
                steer=steer_response,
            )
    except socket.timeout:
        return _WebSocketExchange(reason='app_server_websocket_timeout')
    except (OSError, _WebSocketProtocolError) as exc:
        return _WebSocketExchange(
            reason='app_server_websocket_failed',
            error=f'{type(exc).__name__}: {exc}',
        )


class _UnixWebSocket:
    def __init__(self, path: Path, *, timeout_s: float) -> None:
        self._path = Path(path)
        self._timeout_s = timeout_s
        self._socket: socket.socket | None = None
        self._buffer = bytearray()

    def __enter__(self) -> '_UnixWebSocket':
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self._timeout_s)
        connection.connect(str(self._path))
        self._socket = connection
        self._handshake()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        connection = self._socket
        if connection is None:
            return
        try:
            self._send_frame(0x8, b'')
        except Exception:
            pass
        self._socket = None
        connection.close()

    def send_json(self, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self._send_frame(0x1, encoded)

    def receive_response(self, response_id: str, *, deadline: float) -> dict[str, object] | None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            connection = self._require_socket()
            connection.settimeout(remaining)
            message = self._receive_text()
            if message is None:
                return None
            try:
                record = json.loads(message)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and str(record.get('id') or '') == response_id:
                return record

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        request = (
            'GET / HTTP/1.1\r\n'
            'Host: localhost\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            'Sec-WebSocket-Version: 13\r\n'
            '\r\n'
        ).encode('ascii')
        self._require_socket().sendall(request)
        header = self._read_http_header()
        lines = header.decode('iso-8859-1').split('\r\n')
        if not lines or ' 101 ' not in f' {lines[0]} ':
            raise _WebSocketProtocolError(f'WebSocket upgrade rejected: {lines[0] if lines else "empty"}')
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ':' not in line:
                continue
            name, value = line.split(':', 1)
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(f'{key}{_WEBSOCKET_GUID}'.encode('ascii')).digest()).decode('ascii')
        if headers.get('sec-websocket-accept') != expected:
            raise _WebSocketProtocolError('WebSocket upgrade accept mismatch')

    def _read_http_header(self) -> bytes:
        marker = b'\r\n\r\n'
        while marker not in self._buffer:
            chunk = self._require_socket().recv(4096)
            if not chunk:
                raise _WebSocketProtocolError('WebSocket upgrade connection closed')
            self._buffer.extend(chunk)
            if len(self._buffer) > 65536:
                raise _WebSocketProtocolError('WebSocket upgrade header too large')
        index = self._buffer.index(marker) + len(marker)
        header = bytes(self._buffer[:index])
        del self._buffer[:index]
        return header

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack('!H', length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack('!Q', length))
        header.extend(mask)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._require_socket().sendall(bytes(header) + masked)

    def _receive_text(self) -> str | None:
        fragments: list[bytes] = []
        expecting_continuation = False
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack('!H', self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b''
            payload = self._read_exact(length)
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments = [payload]
                expecting_continuation = not final
                if final:
                    return payload.decode('utf-8')
                continue
            if opcode == 0x0 and expecting_continuation:
                fragments.append(payload)
                if final:
                    return b''.join(fragments).decode('utf-8')
                continue
            raise _WebSocketProtocolError(f'unsupported WebSocket opcode: {opcode}')

    def _read_exact(self, length: int) -> bytes:
        while len(self._buffer) < length:
            chunk = self._require_socket().recv(max(4096, length - len(self._buffer)))
            if not chunk:
                raise _WebSocketProtocolError('WebSocket connection closed')
            self._buffer.extend(chunk)
        payload = bytes(self._buffer[:length])
        del self._buffer[:length]
        return payload

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise _WebSocketProtocolError('WebSocket is not connected')
        return self._socket


def _response_error(response: dict[str, object] | None, fallback: str) -> str:
    error = response.get('error') if isinstance(response, dict) else None
    if isinstance(error, dict):
        message = str(error.get('message') or '').strip()
        code = str(error.get('code') or '').strip()
        return f'{code}: {message}'.strip(': ')
    return str(fallback or '').strip()


def _steer_error_reason(error: str) -> str:
    normalized = str(error or '').lower()
    terminal_tokens = (
        'expected turn',
        'active turn',
        'in-flight turn',
        'in flight turn',
        'no active',
        'not active',
        'turn mismatch',
        'thread not found',
        'turn not found',
    )
    if any(token in normalized for token in terminal_tokens):
        return 'provider_turn_not_active'
    return 'provider_refused_active_followup'


__all__ = ['CodexSteerResponse', 'app_server_socket_ready', 'steer_active_turn']
