from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
from pathlib import Path


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HANDSHAKE_BYTES = 64 * 1024
DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class WebSocketError(RuntimeError):
    pass


class UnixWebSocketServer:
    """Minimal RFC 6455 server for one local Codex TUI connection."""

    def __init__(
        self, path: Path, *, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    ):
        self.path = Path(path)
        self.max_message_bytes = max_message_bytes
        self._listener: socket.socket | None = None
        self._connection: socket.socket | None = None
        self._buffer = bytearray()
        self._send_lock = threading.Lock()
        self._closed = False

    def start(self) -> None:
        if self._listener is not None:
            raise WebSocketError("websocket listener is already started")
        if not self.path.is_absolute():
            raise WebSocketError("unix websocket path must be absolute")
        if len(os.fsencode(self.path)) >= 100:
            raise WebSocketError(f"unix websocket path is too long: {self.path}")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            raise WebSocketError(
                f"refusing to replace existing socket path: {self.path}"
            )
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            listener.listen(1)
        except BaseException:
            listener.close()
            raise
        self._listener = listener

    def accept(self) -> None:
        listener = self._listener
        if listener is None:
            raise WebSocketError("websocket listener is not started")
        connection, _ = listener.accept()
        self._connection = connection
        self._perform_handshake()

    def recv_text(self) -> str | None:
        fragmented_opcode: int | None = None
        payload = bytearray()
        while True:
            fin, opcode, frame_payload = self._recv_frame()
            if opcode == 0x8:
                self._send_frame(0x8, frame_payload[:125])
                return None
            if opcode == 0x9:
                self._send_frame(0xA, frame_payload[:125])
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x2:
                self._protocol_close(1003, "binary frames are unsupported")
            if opcode == 0x1:
                if fragmented_opcode is not None:
                    self._protocol_close(1002, "new data frame during fragmentation")
                fragmented_opcode = opcode
                payload.extend(frame_payload)
            elif opcode == 0x0:
                if fragmented_opcode is None:
                    self._protocol_close(1002, "unexpected continuation frame")
                payload.extend(frame_payload)
            else:
                self._protocol_close(1002, f"unsupported websocket opcode {opcode}")
            if len(payload) > self.max_message_bytes:
                self._protocol_close(1009, "websocket message is too large")
            if not fin:
                continue
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                self._protocol_close(1007, "text frame is not valid UTF-8")
                raise AssertionError("unreachable") from exc

    def send_text(self, text: str) -> None:
        encoded = text.encode("utf-8")
        if len(encoded) > self.max_message_bytes:
            raise WebSocketError("outbound websocket message is too large")
        self._send_frame(0x1, encoded)

    def close(self) -> None:
        self._closed = True
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _perform_handshake(self) -> None:
        raw = self._read_until(b"\r\n\r\n", MAX_HANDSHAKE_BYTES)
        try:
            header_text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise WebSocketError("websocket handshake is not ASCII") from exc
        lines = header_text.split("\r\n")
        if not lines or not lines[0].startswith("GET "):
            raise WebSocketError("websocket handshake must start with GET")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, separator, value = line.partition(":")
            if not separator:
                raise WebSocketError("malformed websocket handshake header")
            headers[name.strip().lower()] = value.strip()
        key = headers.get("sec-websocket-key")
        if not key or headers.get("sec-websocket-version") != "13":
            raise WebSocketError("websocket version/key is missing")
        if "upgrade" not in headers.get("connection", "").lower():
            raise WebSocketError("websocket Connection header is invalid")
        if headers.get("upgrade", "").lower() != "websocket":
            raise WebSocketError("websocket Upgrade header is invalid")
        accept = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
        connection = self._require_connection()
        connection.sendall(response)

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2)
        if first & 0x70:
            self._protocol_close(1002, "reserved websocket bits are set")
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        if not masked:
            self._protocol_close(1002, "client websocket frames must be masked")
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
            if length & (1 << 63):
                self._protocol_close(1002, "invalid websocket frame length")
        if opcode >= 0x8 and (not fin or length > 125):
            self._protocol_close(1002, "invalid websocket control frame")
        if length > self.max_message_bytes:
            self._protocol_close(1009, "websocket frame is too large")
        mask = self._read_exact(4)
        encoded = self._read_exact(length)
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(encoded))
        return fin, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = bytes((first, length))
        elif length <= 0xFFFF:
            header = bytes((first, 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 127)) + struct.pack("!Q", length)
        connection = self._require_connection()
        try:
            with self._send_lock:
                connection.sendall(header + payload)
        except OSError as exc:
            if not self._closed:
                raise WebSocketError(f"failed to send websocket frame: {exc}") from exc

    def _protocol_close(self, code: int, reason: str) -> None:
        try:
            self._send_frame(
                0x8, struct.pack("!H", code) + reason.encode("utf-8")[:123]
            )
        finally:
            raise WebSocketError(reason)

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while True:
            index = self._buffer.find(marker)
            if index >= 0:
                end = index + len(marker)
                result = bytes(self._buffer[:end])
                del self._buffer[:end]
                return result
            if len(self._buffer) >= limit:
                raise WebSocketError("websocket handshake exceeds size limit")
            chunk = self._require_connection().recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during websocket handshake")
            self._buffer.extend(chunk)

    def _read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            try:
                chunk = self._require_connection().recv(
                    max(4096, size - len(self._buffer))
                )
            except OSError as exc:
                raise WebSocketError(
                    f"failed to receive websocket frame: {exc}"
                ) from exc
            if not chunk:
                raise WebSocketError("websocket connection closed")
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def _require_connection(self) -> socket.socket:
        if self._connection is None:
            raise WebSocketError("websocket client is not connected")
        return self._connection
