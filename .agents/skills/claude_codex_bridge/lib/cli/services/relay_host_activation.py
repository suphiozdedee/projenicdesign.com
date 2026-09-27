from __future__ import annotations

import os
from pathlib import Path

from mobile_gateway import mobile_host_state_dir
from mobile_gateway.relay_host_credentials import (
    CCB_OFFICIAL_RELAY_ORIGIN,
    RELAY_MODE_OFFICIAL,
    RELAY_MODE_SELF_HOSTED,
    activate_relay_host,
)


def relay_host_activate_command(context, command) -> dict[str, object]:
    del context
    invitation = _invitation(command)
    relay_mode, relay_origin = resolve_relay_host_target(command)
    credential_path = _credential_path(command)
    credentials = activate_relay_host(
        relay_mode=relay_mode,
        relay_origin=relay_origin,
        invitation=invitation,
        credential_path=credential_path,
    )
    return credentials.public_summary(credential_path=credential_path)


def resolve_relay_host_target(command) -> tuple[str, str]:
    requested_mode = str(getattr(command, 'relay_mode', '') or '').strip().lower().replace('_', '-')
    explicit_origin = str(getattr(command, 'relay_origin', '') or '').strip()
    legacy_origin = str(os.environ.get('CCB_RELAY_PUBLIC_ORIGIN') or '').strip()
    if requested_mode == 'self-hosted':
        origin = explicit_origin or legacy_origin
        if not origin:
            raise ValueError('self-hosted relay activation requires --relay-origin wss://relay.example.com')
        return RELAY_MODE_SELF_HOSTED, origin
    if requested_mode == RELAY_MODE_OFFICIAL:
        if explicit_origin:
            raise ValueError('official relay activation uses the CCB official endpoint; remove --relay-origin or choose --mode self-hosted')
        return RELAY_MODE_OFFICIAL, CCB_OFFICIAL_RELAY_ORIGIN
    if explicit_origin or legacy_origin:
        return RELAY_MODE_SELF_HOSTED, explicit_origin or legacy_origin
    return RELAY_MODE_OFFICIAL, CCB_OFFICIAL_RELAY_ORIGIN


def _invitation(command) -> str:
    direct = str(getattr(command, 'invitation', '') or '').strip()
    if direct:
        return direct
    path_text = str(getattr(command, 'invitation_file', '') or '').strip()
    if path_text:
        path = Path(path_text).expanduser()
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise ValueError('relay invitation file must be owner-only')
        invitation = path.read_text(encoding='utf-8').strip()
        if invitation:
            return invitation
    environment = str(os.environ.get('CCB_RELAY_INVITATION') or '').strip()
    if environment:
        return environment
    raise ValueError(
        'relay host activate requires --invitation-file, --invitation, or CCB_RELAY_INVITATION'
    )


def _credential_path(command) -> Path:
    return relay_host_credential_path(command)


def relay_host_credential_path(command, *, environ=None) -> Path:
    env = os.environ if environ is None else environ
    explicit = str(getattr(command, 'credential_path', '') or '').strip()
    configured = str(env.get('CCB_RELAY_HOST_CREDENTIALS') or '').strip()
    return Path(explicit or configured or (mobile_host_state_dir() / 'relay-host-credentials.json')).expanduser()


__all__ = [
    'relay_host_activate_command',
    'relay_host_credential_path',
    'resolve_relay_host_target',
]
