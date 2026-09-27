from __future__ import annotations

import base64
import hashlib
import json
import struct
import threading
from typing import BinaryIO, Mapping


_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
_MAX_FRAME_BYTES = 1024 * 1024


class WebSocketProtocolError(RuntimeError):
    pass


class WebSocketConnection:
    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        self._reader = reader
        self._writer = writer
        self._write_lock = threading.Lock()
        self._closed = False

    def read_json(self) -> dict[str, object] | None:
        message = self._read_message()
        if message is None:
            return None
        if isinstance(message, bytes):
            message = message.decode('utf-8')
        try:
            decoded = json.loads(message)
        except json.JSONDecodeError as exc:
            raise WebSocketProtocolError('invalid JSON frame') from exc
        if isinstance(decoded, dict):
            return {str(key): value for key, value in decoded.items()}
        raise WebSocketProtocolError('terminal frame must be a JSON object')

    def send_json(self, payload: Mapping[str, object]) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode('utf-8')
        self._send_frame(0x1, body)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(0x8, b'')
        except OSError:
            pass

    def _read_message(self) -> str | bytes | None:
        first = self._reader.read(2)
        if not first:
            return None
        if len(first) != 2:
            raise WebSocketProtocolError('truncated frame header')
        byte1, byte2 = first
        opcode = byte1 & 0x0F
        masked = bool(byte2 & 0x80)
        length = byte2 & 0x7F
        if length == 126:
            length = struct.unpack('!H', _read_exact(self._reader, 2))[0]
        elif length == 127:
            length = struct.unpack('!Q', _read_exact(self._reader, 8))[0]
        if length > _MAX_FRAME_BYTES:
            raise WebSocketProtocolError('terminal frame too large')
        mask = _read_exact(self._reader, 4) if masked else b''
        payload = _read_exact(self._reader, length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x8:
            return None
        if opcode == 0x9:
            self._send_frame(0xA, payload)
            return self._read_message()
        if opcode == 0x1:
            return payload.decode('utf-8')
        if opcode == 0x2:
            return payload
        raise WebSocketProtocolError('unsupported terminal frame opcode')

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        with self._write_lock:
            header = bytearray([0x80 | (opcode & 0x0F)])
            length = len(payload)
            if length < 126:
                header.append(length)
            elif length <= 0xFFFF:
                header.append(126)
                header.extend(struct.pack('!H', length))
            else:
                header.append(127)
                header.extend(struct.pack('!Q', length))
            self._writer.write(bytes(header) + payload)
            self._writer.flush()


def accept_websocket(handler) -> WebSocketConnection:
    headers = handler.headers
    if _header_value(headers, 'upgrade').lower() != 'websocket':
        raise WebSocketProtocolError('missing websocket upgrade')
    if 'upgrade' not in _header_value(headers, 'connection').lower():
        raise WebSocketProtocolError('missing websocket connection upgrade')
    key = _header_value(headers, 'sec-websocket-key')
    if not key:
        raise WebSocketProtocolError('missing websocket key')
    accept = base64.b64encode(hashlib.sha1(f'{key}{_GUID}'.encode('ascii')).digest()).decode('ascii')
    handler.send_response(101, 'Switching Protocols')
    handler.send_header('Upgrade', 'websocket')
    handler.send_header('Connection', 'Upgrade')
    handler.send_header('Sec-WebSocket-Accept', accept)
    handler.end_headers()
    return WebSocketConnection(handler.rfile, handler.wfile)


def is_websocket_upgrade(headers) -> bool:
    return _header_value(headers, 'upgrade').lower() == 'websocket'


def _header_value(headers, name: str) -> str:
    value = ''
    get = getattr(headers, 'get', None)
    if callable(get):
        value = str(get(name) or get(name.title()) or '')
    if not value:
        for key, item in headers.items():
            if str(key).lower() == name.lower():
                value = str(item or '')
                break
    return value.strip()


def _read_exact(reader: BinaryIO, length: int) -> bytes:
    data = reader.read(length)
    if len(data) != length:
        raise WebSocketProtocolError('truncated frame payload')
    return data
