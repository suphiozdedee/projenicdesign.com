from __future__ import annotations

import os
from pathlib import Path

from mobile_gateway.relay_admission import RelayAdmissionSecrets, RelayAdmissionStore

from .relay_host_activation import relay_host_activate_command


def relay_operator_command(context, command) -> dict[str, object]:
    target = str(getattr(command, 'target', '') or '')
    action = str(getattr(command, 'action', '') or '')
    if target == 'host' and action == 'activate':
        return relay_host_activate_command(context, command)
    store = RelayAdmissionStore(
        _relay_db_path(context, command),
        admission_secrets=RelayAdmissionSecrets.from_operator_config(_relay_secrets_path(command)),
    )
    if target == 'invite' and action == 'issue':
        issued = store.issue_invitation(
            ttl_seconds=int(getattr(command, 'ttl_seconds', 0) or 0),
            label=getattr(command, 'label', None),
            max_sessions=int(getattr(command, 'max_sessions', 0) or 0),
            max_bytes_per_day=int(getattr(command, 'max_bytes_per_day', 0) or 0),
        )
        payload = issued.to_operator_json()
    elif target == 'invite' and action == 'status':
        payload = store.invitation_status(str(getattr(command, 'invite_id', '') or ''))
    elif target == 'invite' and action == 'list':
        payload = {'relay_status': 'invite_list', 'invitations': store.list_invitations()}
    elif target == 'invite' and action == 'revoke':
        payload = store.revoke_invitation(
            str(getattr(command, 'invite_id', '') or ''),
            reason=getattr(command, 'reason', None),
        )
    elif target == 'host' and action == 'status':
        payload = store.host_status(str(getattr(command, 'host_id', '') or ''))
    elif target == 'host' and action == 'list':
        payload = {'relay_status': 'host_list', 'hosts': store.list_hosts()}
    elif target == 'host' and action == 'revoke':
        payload = store.revoke_host(
            str(getattr(command, 'host_id', '') or ''),
            reason=getattr(command, 'reason', None),
        )
    else:
        raise ValueError(
            'relay supports invite issue/status/list/revoke and host activate/status/list/revoke'
        )
    payload['db_path'] = _redacted_db_path(_relay_db_path(context, command))
    return payload


def _relay_db_path(context, command) -> Path:
    explicit = str(getattr(command, 'db_path', '') or '').strip()
    if explicit:
        return Path(explicit).expanduser()
    configured = str(os.environ.get('CCB_RELAY_ADMISSION_DB') or '').strip()
    if configured:
        return Path(configured).expanduser()
    mobile_dir = Path(getattr(context.paths, 'ccbd_mobile_dir'))
    return mobile_dir / 'relay-admission.sqlite3'


def _redacted_db_path(path: Path) -> str:
    resolved = Path(path).expanduser()
    return str(resolved)


def _relay_secrets_path(command) -> Path | None:
    explicit = str(getattr(command, 'secrets_path', '') or '').strip()
    if explicit:
        return Path(explicit).expanduser()
    return None


__all__ = ['relay_operator_command']
