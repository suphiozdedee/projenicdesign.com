from __future__ import annotations

from pathlib import Path
import json

from ccbd.api_models import RpcRequest, RpcResponse
from ccbd.control_plane_transport.factory import transport_for_legacy_socket_path

from .errors import CcbdClientError

_MAX_RESPONSE_BYTES = 1024 * 1024


def connect_socket(socket_path: Path, *, timeout_s: float):
    try:
        return transport_for_legacy_socket_path(socket_path).connect(timeout_s=timeout_s)
    except CcbdClientError:
        raise
    except Exception as exc:
        raise CcbdClientError(str(exc)) from exc


def send_request(sock, request: RpcRequest) -> None:
    payload = json.dumps(request.to_record(), ensure_ascii=False) + '\n'
    sock.sendall(payload.encode('utf-8'))


def recv_response_line(sock) -> bytes:
    raw = bytearray()
    while raw.find(b'\n') < 0:
        chunk = sock.recv(65536)
        if not chunk:
            break
        raw.extend(chunk)
        newline_at = raw.find(b'\n')
        if newline_at >= 0:
            frame_end = newline_at + 1
            if frame_end > _MAX_RESPONSE_BYTES:
                raise CcbdClientError('ccbd response exceeds maximum size')
            return bytes(raw[:frame_end])
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise CcbdClientError('ccbd response exceeds maximum size')
    return bytes(raw)


def decode_response(raw: bytes) -> RpcResponse:
    line = raw.split(b'\n', 1)[0].decode('utf-8')
    return RpcResponse.from_record(json.loads(line))


__all__ = ['connect_socket', 'decode_response', 'recv_response_line', 'send_request']
