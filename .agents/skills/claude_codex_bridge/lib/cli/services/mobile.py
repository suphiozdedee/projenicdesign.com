from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import socket
from urllib.parse import urlparse, urlunsplit

from ccbd.socket_client import CcbdClient
from mobile_gateway import (
    MobileGatewayProjectRegistry,
    MobileGatewayPairingStore,
    MobileGatewayService,
    build_mobile_gateway_server,
    discover_running_mobile_gateway_projects,
    load_mobile_gateway_project_registry,
    mobile_host_project_registry_path,
    mobile_host_state_dir,
    parse_listen_address,
)
from mobile_gateway.fcm import build_fcm_sender_from_env, fcm_sender_runtime_options
from mobile_gateway.relay_host_credentials import (
    RelayHostCredentials,
    build_relay_pairing_payload,
    load_relay_host_credentials,
)
from mobile_gateway.relay_host_runtime import relay_host_runtime_summary


@dataclass(frozen=True)
class MobileGatewayServeHandle:
    summary: dict[str, object]
    server: object
    service: object | None = None

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def close(self) -> None:
        close_service = getattr(self.service, 'close', None)
        if callable(close_service):
            close_service()
        self.server.server_close()


def prepare_mobile_gateway(context, command) -> MobileGatewayServeHandle:
    route_provider = str(command.route_provider or 'lan').strip().lower() or 'lan'
    listen = parse_listen_address(command.listen, allow_lan=route_provider == 'lan')
    push_sender, push_diagnostic, push_options = _push_sender_setup()
    service = MobileGatewayService(
        project_id=context.project.project_id,
        project_root=context.project.project_root,
        ccbd_client_factory=lambda: CcbdClient(context.paths.ccbd_socket_path),
        mobile_dir=context.paths.ccbd_mobile_dir,
        push_sender=push_sender,
        push_sender_timeout_seconds=float(push_options['timeout_seconds']),
        push_sender_max_workers=int(push_options['max_workers']),
        push_diagnostic=push_diagnostic,
    )
    server = build_mobile_gateway_server(listen, service)
    host, port = server.server_address[:2]
    local_gateway_url = f'http://{host}:{port}'
    relay_credentials = _relay_host_credentials() if route_provider == 'relay' else None
    gateway_url = (
        relay_credentials.relay_http_origin
        if relay_credentials is not None
        else _public_gateway_url(command.public_url, fallback=local_gateway_url)
    )
    pairing = service.ensure_reusable_pairing_payload(
        gateway_url=gateway_url,
        route_provider=route_provider,
    )
    if relay_credentials is not None:
        pairing = build_relay_pairing_payload(pairing, credentials=relay_credentials)
    relay_outbound = _relay_outbound_summary(relay_credentials) if relay_credentials is not None else None
    summary = {
        'mobile_status': 'serving',
        'listen': f'{host}:{port}',
        'gateway_url': gateway_url,
        'local_gateway_url': local_gateway_url,
        'route_provider': route_provider,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'mode': 'loopback_current_project',
        'pairing': pairing,
        'endpoints': [
            '/v1/health',
            '/v1/projects',
            '/v1/mobile/notifications',
            '/v1/mobile/push/audit',
            '/v1/projects/{project_id}/view',
            '/v1/pairing/claim',
            '/v1/devices/me',
            '/v1/devices/me/presence',
            '/v1/devices/me/push-token',
            '/v1/devices/{device_id}/revoke',
            '/v1/projects/{project_id}/lifecycle',
            '/v1/projects/{project_id}/focus-agent',
            '/v1/projects/{project_id}/focus-window',
            '/v1/projects/{project_id}/terminals',
            '/v1/terminals',
            '/v1/terminals/terminate',
            '/v1/terminals/{terminal_id}',
        ],
        'push_sender': {**push_diagnostic, **push_options},
    }
    if relay_outbound is not None:
        summary['relay_outbound'] = relay_outbound
    return MobileGatewayServeHandle(
        summary=summary,
        server=server,
        service=service,
    )


def prepare_server_mobile_gateway(
    command,
    *,
    project_registry: MobileGatewayProjectRegistry | None = None,
    host_id: str | None = None,
    rotate_pairing: bool = False,
) -> MobileGatewayServeHandle:
    registry = project_registry or _running_server_project_registry()
    route_provider = str(command.route_provider or 'lan').strip().lower() or 'lan'
    listen = parse_listen_address(command.listen, allow_lan=route_provider == 'lan')
    relay_credentials = _relay_host_credentials() if route_provider == 'relay' else None
    requested_host_id = str(host_id or '').strip()
    if relay_credentials is not None and requested_host_id and requested_host_id != relay_credentials.host_id:
        raise ValueError('mobile relay host id does not match activated relay credentials')
    resolved_host_id = (
        relay_credentials.host_id
        if relay_credentials is not None
        else requested_host_id or _server_host_id()
    )
    state_dir = mobile_host_state_dir()
    default_project = registry.default_project
    push_sender, push_diagnostic, push_options = _push_sender_setup()
    service = MobileGatewayService(
        project_id=resolved_host_id,
        project_root=state_dir,
        ccbd_client_factory=default_project.client,
        mobile_dir=state_dir,
        project_registry=registry,
        project_registry_provider=_running_server_project_registry,
        mode='loopback_server_registry',
        push_sender=push_sender,
        push_sender_timeout_seconds=float(push_options['timeout_seconds']),
        push_sender_max_workers=int(push_options['max_workers']),
        push_diagnostic=push_diagnostic,
    )
    # Startup must not synchronously ping every registered project.  The
    # running gateway will fill its bounded health cache in the background.
    project_summaries = [
        {
            'id': project.project_id,
            'display_name': project.public_display_name,
            'root': str(project.project_root),
            'health': 'unknown',
            'health_freshness': 'unknown',
        }
        for project in registry.projects()
    ]
    server = build_mobile_gateway_server(listen, service)
    try:
        host, port = server.server_address[:2]
        local_gateway_url = f'http://{host}:{port}'
        gateway_url = (
            relay_credentials.relay_http_origin
            if relay_credentials is not None
            else _public_gateway_url(command.public_url, fallback=local_gateway_url)
        )
        pairing = (
            service.rotate_reusable_pairing_payload(
                gateway_url=gateway_url,
                route_provider=route_provider,
            )
            if rotate_pairing
            else service.ensure_reusable_pairing_payload(
                gateway_url=gateway_url,
                route_provider=route_provider,
            )
        )
        if relay_credentials is not None:
            pairing = build_relay_pairing_payload(
                pairing,
                credentials=relay_credentials,
            )
        relay_outbound = (
            _relay_outbound_summary(relay_credentials)
            if relay_credentials is not None
            else None
        )
    except Exception:
        server.server_close()
        raise
    summary = {
        'mobile_status': 'serving',
        'listen': f'{host}:{port}',
        'gateway_url': gateway_url,
        'local_gateway_url': local_gateway_url,
        'route_provider': route_provider,
        'host_id': resolved_host_id,
        'project_id': resolved_host_id,
        'project_root': '',
        'mobile_state_dir': str(state_dir),
        'mode': 'loopback_server_registry',
        'project_count': len(project_summaries),
        'projects': project_summaries,
        'pairing': pairing,
        'endpoints': [
            '/v1/health',
            '/v1/projects',
            '/v1/mobile/notifications',
            '/v1/mobile/push/audit',
            '/v1/projects/{project_id}/view',
            '/v1/pairing/claim',
            '/v1/devices/me',
            '/v1/devices/me/presence',
            '/v1/devices/me/push-token',
            '/v1/devices/{device_id}/revoke',
            '/v1/projects/{project_id}/lifecycle',
            '/v1/projects/{project_id}/focus-agent',
            '/v1/projects/{project_id}/focus-window',
            '/v1/projects/{project_id}/terminals',
            '/v1/terminals',
            '/v1/terminals/terminate',
            '/v1/terminals/{terminal_id}',
        ],
        'push_sender': {**push_diagnostic, **push_options},
    }
    if relay_outbound is not None:
        summary['relay_outbound'] = relay_outbound
    return MobileGatewayServeHandle(
        summary=summary,
        server=server,
        service=service,
    )


def _push_sender_setup() -> tuple[object | None, dict[str, object], dict[str, object]]:
    sender, diagnostic = build_fcm_sender_from_env()
    return sender, diagnostic, fcm_sender_runtime_options()


def _running_server_project_registry() -> MobileGatewayProjectRegistry:
    projects_by_id = {}
    try:
        registry = load_mobile_gateway_project_registry(
            registry_path=mobile_host_project_registry_path(
                state_dir=mobile_host_state_dir(),
            ),
        )
        projects_by_id.update(
            {project.project_id: project for project in registry.projects()}
        )
    except ValueError:
        pass
    for project in discover_running_mobile_gateway_projects():
        projects_by_id[project.project_id] = project
    if not projects_by_id:
        raise ValueError('no running CCB projects found for mobile server setup')
    return MobileGatewayProjectRegistry(list(projects_by_id.values()))


def mobile_devices_status(context, command) -> dict[str, object]:
    store = MobileGatewayPairingStore(context.paths.ccbd_mobile_dir)
    return {
        'mobile_status': 'devices',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'mobile_state_dir': str(context.paths.ccbd_mobile_dir),
        'devices': store.list_devices(),
    }


def revoke_mobile_device(context, command) -> dict[str, object]:
    device_id = str(getattr(command, 'device_id', '') or '').strip()
    store = MobileGatewayPairingStore(context.paths.ccbd_mobile_dir)
    result = store.revoke_device_locally(device_id=device_id)
    return {
        'mobile_status': 'revoked',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'mobile_state_dir': str(context.paths.ccbd_mobile_dir),
        **result,
    }


def _public_gateway_url(value: str | None, *, fallback: str) -> str:
    text = str(value or '').strip()
    if not text:
        return fallback
    parsed = urlparse(text)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or not parsed.hostname:
        raise ValueError('--public-url must be an absolute http(s) origin URL')
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError('--public-url port must be valid') from exc
    if parsed.username or parsed.password:
        raise ValueError('--public-url must not include credentials')
    if parsed.path not in {'', '/'}:
        raise ValueError('--public-url must not include a path')
    if parsed.params or parsed.query or parsed.fragment:
        raise ValueError('--public-url must not include params, query, or fragment')
    return urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))


def _relay_outbound_summary(credentials: RelayHostCredentials) -> dict[str, object]:
    return relay_host_runtime_summary(
        credentials,
        diagnostics={'credential_path': str(_relay_host_credentials_path())},
    )


def _relay_host_credentials() -> RelayHostCredentials:
    return load_relay_host_credentials(_relay_host_credentials_path())


def _relay_host_credentials_path() -> Path:
    configured = str(os.environ.get('CCB_RELAY_HOST_CREDENTIALS') or '').strip()
    return Path(configured or (mobile_host_state_dir() / 'relay-host-credentials.json')).expanduser()


def _server_host_id() -> str:
    seed = f'{socket.gethostname()}:{mobile_host_state_dir()}'
    digest = hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]
    return f'host-{digest}'


__all__ = [
    'MobileGatewayServeHandle',
    'mobile_devices_status',
    'prepare_mobile_gateway',
    'prepare_server_mobile_gateway',
    'revoke_mobile_device',
]
