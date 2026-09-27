from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict


class EndpointRef(TypedDict, total=False):
    kind: Literal['unix_socket', 'tcp_loopback']
    address: str
    display: str
    legacy_socket_path: str | None
    auth_ref: str | None
    fingerprint: str | None


def endpoint_from_legacy_socket_path(socket_path: str | Path) -> EndpointRef:
    raw = str(socket_path).strip()
    if not raw:
        raise ValueError('legacy socket path is required')
    path = str(Path(raw))
    return {
        'kind': 'unix_socket',
        'address': path,
        'display': path,
        'legacy_socket_path': path,
        'auth_ref': None,
        'fingerprint': None,
    }


def endpoint_from_record(record: dict | str | Path) -> EndpointRef:
    if isinstance(record, (str, Path)):
        return endpoint_from_legacy_socket_path(record)
    kind = str(record.get('kind') or '')
    if kind == 'unix_socket':
        endpoint = endpoint_from_legacy_socket_path(
            record.get('address') or record.get('legacy_socket_path') or ''
        )
        endpoint['display'] = str(record.get('display') or endpoint['address'])
        endpoint['auth_ref'] = record.get('auth_ref')
        endpoint['fingerprint'] = record.get('fingerprint')
        return endpoint
    if kind == 'tcp_loopback':
        host = str(record.get('host') or '').strip() or '127.0.0.1'
        port = _clean_port(record.get('port'))
        address = str(record.get('address') or '').strip()
        if not address and port is not None:
            address = f'{host}:{port}'
        if not address:
            raise ValueError('tcp_loopback endpoint requires address or host/port')
        token_ref = record.get('token_ref') or record.get('auth_ref')
        return {
            'kind': 'tcp_loopback',
            'address': address,
            'display': str(record.get('display') or address),
            'legacy_socket_path': record.get('legacy_socket_path'),
            'auth_ref': token_ref,
            'fingerprint': record.get('fingerprint'),
            'host': host,
            'port': port,
            'token_ref': token_ref,
            'generation': record.get('generation'),
            'acl_status': record.get('acl_status'),
        }
    if 'socket_path' in record:
        return endpoint_from_legacy_socket_path(str(record['socket_path']))
    raise ValueError(f'unsupported ccbd endpoint kind: {kind!r}')


def endpoint_to_record(endpoint: EndpointRef) -> dict[str, object]:
    record = {
        'kind': endpoint['kind'],
        'address': endpoint['address'],
        'display': endpoint['display'],
        'legacy_socket_path': endpoint.get('legacy_socket_path'),
        'auth_ref': endpoint.get('auth_ref'),
        'fingerprint': endpoint.get('fingerprint'),
        'socket_path': endpoint.get('legacy_socket_path'),
    }
    for key in ('host', 'port', 'token_ref', 'generation', 'acl_status'):
        if key in endpoint:
            record[key] = endpoint.get(key)
    return record


def legacy_socket_path(endpoint: EndpointRef) -> Path | None:
    raw = endpoint.get('legacy_socket_path')
    return Path(raw) if raw else None


def tcp_endpoint(
    *,
    host: str,
    port: int,
    token_ref: str,
    generation: str,
    acl_status: str,
    fingerprint: str | None = None,
) -> EndpointRef:
    clean_host = str(host or '').strip()
    clean_port = int(port)
    if clean_host != '127.0.0.1':
        raise ValueError('tcp_loopback endpoint host must be 127.0.0.1')
    if clean_port <= 0 or clean_port > 65535:
        raise ValueError(f'invalid tcp_loopback endpoint port: {port!r}')
    clean_token_ref = str(token_ref or '').strip()
    if not clean_token_ref:
        raise ValueError('tcp_loopback endpoint token_ref is required')
    address = f'{clean_host}:{clean_port}'
    return {
        'kind': 'tcp_loopback',
        'address': address,
        'display': address,
        'legacy_socket_path': None,
        'auth_ref': clean_token_ref,
        'fingerprint': str(fingerprint or '').strip() or None,
        'host': clean_host,
        'port': clean_port,
        'token_ref': clean_token_ref,
        'generation': str(generation or '').strip(),
        'acl_status': str(acl_status or '').strip() or 'unknown',
    }


def _clean_port(value: object) -> int | None:
    if value is None:
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError(f'invalid tcp_loopback endpoint port: {value!r}') from None
    if port <= 0 or port > 65535:
        raise ValueError(f'invalid tcp_loopback endpoint port: {value!r}')
    return port
