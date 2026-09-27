from __future__ import annotations

import os
from pathlib import Path

from .endpoint import EndpointRef, endpoint_from_legacy_socket_path, endpoint_from_record
from .token_auth import RpcTransportAuthError
from .unix import UnixControlPlaneTransport
from platforms.windows.control_plane.tcp import WindowsTcpControlPlaneTransport


def _is_windows() -> bool:
    return os.name == 'nt'


def transport_for_endpoint(endpoint: EndpointRef | dict | str | Path):
    try:
        resolved = endpoint_from_record(endpoint)
    except ValueError as exc:
        if _is_windows() and isinstance(endpoint, dict):
            raise RpcTransportAuthError('endpoint-invalid', str(exc)) from exc
        raise
    if _is_windows() and isinstance(endpoint, dict) and resolved['kind'] != 'tcp_loopback':
        raise RpcTransportAuthError('endpoint-invalid', 'ccbd Windows control-plane endpoint must use tcp_loopback')
    if resolved['kind'] == 'unix_socket':
        return UnixControlPlaneTransport(resolved)
    if resolved['kind'] == 'tcp_loopback':
        token_ref = str(resolved.get('token_ref') or resolved.get('auth_ref') or '').strip()
        legacy_socket_path = resolved.get('legacy_socket_path') or token_ref or '.'
        return WindowsTcpControlPlaneTransport(resolved, legacy_socket_path=legacy_socket_path)
    raise RuntimeError(f'unsupported ccbd control-plane endpoint: {resolved["kind"]}')


def connect_endpoint(endpoint: EndpointRef | dict | str | Path, *, timeout_s: float):
    return transport_for_endpoint(endpoint).connect(timeout_s=timeout_s)


def endpoint_connectable(endpoint: EndpointRef | dict | str | Path, *, timeout_s: float = 0.2) -> bool:
    try:
        return bool(transport_for_endpoint(endpoint).is_connectable(timeout_s=timeout_s))
    except Exception:
        return False


def transport_for_legacy_socket_path(socket_path: str | Path, *, prefer_windows: bool = False):
    if _is_windows():
        transport = WindowsTcpControlPlaneTransport.from_legacy_socket_path(socket_path)
        if getattr(transport, '_endpoint_error', None) is not None:
            return transport
        endpoint = getattr(transport, 'endpoint', None)
        if endpoint is None or endpoint.get('kind') == 'tcp_loopback':
            return transport
        if prefer_windows:
            return WindowsTcpControlPlaneTransport(None, legacy_socket_path=socket_path)
        return transport
    return UnixControlPlaneTransport(endpoint_from_legacy_socket_path(socket_path))
