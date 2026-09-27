from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import threading
from typing import Callable

_SCHEMA_VERSION = 1
_PAIRING_HASH_PREFIX = 'ccb-mobile-pairing-v1:'
_DEVICE_HASH_PREFIX = 'ccb-mobile-device-v1:'
_TERMINAL_HASH_PREFIX = 'ccb-mobile-terminal-v1:'
_DEFAULT_PAIRING_EXPIRES_SECONDS = 10 * 60
_DEFAULT_DEVICE_SCOPES = ('view',)
_DEFAULT_TERMINAL_EXPIRES_SECONDS = 5 * 60
_TERMINAL_LOG_COMPACT_BYTES = 8 * 1024 * 1024
_HANDOFF_RECORD_TYPE = 'ccb_mobile_pairing_handoff'
_PRESENCE_RECORD_TYPE = 'ccb_mobile_device_presence'
_PUSH_TOKEN_RECORD_TYPE = 'ccb_mobile_device_push_tokens'
_DEFAULT_PRESENCE_TTL_SECONDS = 90


class MobileGatewayPairingError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400, reason: str = 'invalid') -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.reason = str(reason or 'invalid')


@dataclass(frozen=True)
class AuthenticatedDevice:
    record: dict[str, object]

    @property
    def device_id(self) -> str:
        return str(self.record.get('device_id') or '')

    @property
    def scopes(self) -> set[str]:
        return _scope_set(self.record.get('scopes'))

    def public_payload(self) -> dict[str, object]:
        return _public_device(self.record)


class MobileGatewayPairingStore:
    def __init__(
        self,
        mobile_dir: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[int], str] | None = None,
        id_factory: Callable[[str], str] | None = None,
        presence_ttl_seconds: int = _DEFAULT_PRESENCE_TTL_SECONDS,
    ) -> None:
        self._mobile_dir = Path(mobile_dir)
        self._clock = clock or _utc_now
        self._token_factory = token_factory or _token_urlsafe
        self._id_factory = id_factory or _random_id
        self._presence_ttl_seconds = max(1, int(presence_ttl_seconds))
        self._lock = threading.RLock()
        self._terminal_state_cache: dict[str, dict[str, object]] | None = None
        self._terminal_state_cache_identity: tuple[int, int] | None = None
        self._terminal_state_cache_offset = 0
        self._terminal_state_cache_mtime_ns = 0

    @property
    def gateway_path(self) -> Path:
        return self._mobile_dir / 'gateway.json'

    @property
    def devices_path(self) -> Path:
        return self._mobile_dir / 'devices.json'

    @property
    def pairing_tokens_path(self) -> Path:
        return self._mobile_dir / 'pairing-tokens.jsonl'

    @property
    def handoff_path(self) -> Path:
        """Private local copy of the currently reusable handoff secret."""
        return self._mobile_dir / 'pairing-handoff.json'

    @property
    def presence_path(self) -> Path:
        return self._mobile_dir / 'device-presence.json'

    @property
    def push_tokens_path(self) -> Path:
        return self._mobile_dir / 'push-tokens.json'

    @property
    def terminal_tokens_path(self) -> Path:
        return self._mobile_dir / 'terminal-tokens.jsonl'

    @property
    def audit_path(self) -> Path:
        return self._mobile_dir / 'audit.jsonl'

    def write_gateway_state(
        self,
        *,
        project_id: str,
        gateway_url: str,
        route_provider: str,
        capabilities: Iterable[str],
    ) -> dict[str, object]:
        now = _iso(self._clock())
        payload = {
            'schema_version': _SCHEMA_VERSION,
            'project_id': str(project_id),
            'gateway_url': str(gateway_url),
            'route_provider': str(route_provider),
            'capabilities': sorted({str(item) for item in capabilities}),
            'updated_at': now,
        }
        with self._lock:
            self._ensure_dir()
            _write_json(self.gateway_path, payload)
        return payload

    def create_pairing_payload(
        self,
        *,
        project_id: str,
        gateway_url: str,
        route_provider: str = 'lan',
        scopes: Iterable[str] = _DEFAULT_DEVICE_SCOPES,
        expires_seconds: int | None = _DEFAULT_PAIRING_EXPIRES_SECONDS,
        reusable_claims: bool = False,
    ) -> dict[str, object]:
        now = self._clock()
        expires_at = None if expires_seconds is None else now + timedelta(seconds=max(1, int(expires_seconds)))
        pairing_code = self._token_factory(18)
        pairing_id = self._id_factory('pair')
        scope_list = _scope_list(scopes)
        record = {
            'schema_version': _SCHEMA_VERSION,
            'pairing_id': pairing_id,
            'project_id': str(project_id),
            'token_hash': _token_hash(_PAIRING_HASH_PREFIX, pairing_code),
            'scopes': scope_list,
            'route_provider': str(route_provider),
            'gateway_url': str(gateway_url),
            'created_at': _iso(now),
            'expires_at': _iso(expires_at) if expires_at is not None else None,
            'reusable_claims': bool(reusable_claims),
            'claimed_at': None,
            'claimed_by_device_id': None,
            'revoked_at': None,
        }
        with self._lock:
            self._ensure_dir()
            _append_jsonl(self.pairing_tokens_path, record)
            self._append_audit(
                event='pairing_token_created',
                result='ok',
                project_id=str(project_id),
                pairing_id=pairing_id,
                scopes=scope_list,
            )
        return {
            'schema_version': _SCHEMA_VERSION,
            'pairing_id': pairing_id,
            'pairing_code': pairing_code,
            'project_id': str(project_id),
            'route_provider': str(route_provider),
            'gateway_url': str(gateway_url),
            'claim_endpoint': f'{str(gateway_url).rstrip("/")}/v1/pairing/claim',
            'scopes': scope_list,
            'expires_at': _iso(expires_at) if expires_at is not None else None,
            'reusable_claims': bool(reusable_claims),
        }

    def ensure_reusable_pairing_payload(
        self,
        *,
        project_id: str,
        gateway_url: str,
        route_provider: str,
        scopes: Iterable[str],
    ) -> dict[str, object]:
        """Return the current manual-rotation handoff, creating it once."""
        with self._lock:
            handoff = self._read_handoff()
            if handoff and self.pairing_code_is_claimable(str(handoff.get('pairing_code') or '')):
                return self._public_handoff(handoff)
            generation = max(0, _int(handoff.get('generation') if handoff else 0, 0)) + 1
            payload = self.create_pairing_payload(
                project_id=project_id,
                gateway_url=gateway_url,
                route_provider=route_provider,
                scopes=scopes,
                expires_seconds=None,
                reusable_claims=True,
            )
            self._write_handoff({
                'record_type': _HANDOFF_RECORD_TYPE,
                'schema_version': _SCHEMA_VERSION,
                'generation': generation,
                **payload,
                'rotated_at': _iso(self._clock()),
            })
            self._append_audit(
                event='pairing_handoff_created',
                result='ok',
                project_id=str(project_id),
                pairing_id=str(payload.get('pairing_id') or ''),
                generation=generation,
            )
            return {**payload, 'generation': generation}

    def rotate_reusable_pairing_payload(
        self,
        *,
        project_id: str,
        gateway_url: str,
        route_provider: str,
        scopes: Iterable[str],
    ) -> dict[str, object]:
        """Explicitly replace the handoff without revoking issued devices."""
        with self._lock:
            prior = self._read_handoff()
            if prior:
                prior_id = str(prior.get('pairing_id') or '').strip()
                if prior_id:
                    self.revoke_pairing(prior_id, reason='manual_handoff_rotation')
            generation = max(0, _int(prior.get('generation') if prior else 0, 0)) + 1
            payload = self.create_pairing_payload(
                project_id=project_id,
                gateway_url=gateway_url,
                route_provider=route_provider,
                scopes=scopes,
                expires_seconds=None,
                reusable_claims=True,
            )
            self._write_handoff({
                'record_type': _HANDOFF_RECORD_TYPE,
                'schema_version': _SCHEMA_VERSION,
                'generation': generation,
                **payload,
                'rotated_at': _iso(self._clock()),
            })
            self._append_audit(
                event='pairing_handoff_rotated',
                result='ok',
                project_id=str(project_id),
                pairing_id=str(payload.get('pairing_id') or ''),
                generation=generation,
            )
            return {**payload, 'generation': generation}

    def record_presence(
        self,
        *,
        device_id: str,
        visible: bool,
        focused_project_id: str | None = None,
        focused_agent: str | None = None,
        terminal_id: str | None = None,
        user_activity: bool = False,
    ) -> dict[str, object]:
        """Persist redacted device presence; heartbeats never alter project activity."""
        with self._lock:
            presence = self._read_presence()
            now = _iso(self._clock())
            prior = dict(presence.get(device_id) or {})
            updated = {
                'device_id': device_id,
                'visible': bool(visible),
                'focused_project_id': (
                    str(prior.get('focused_project_id') or '')
                    if focused_project_id is None else _clean_id(focused_project_id)
                ),
                'focused_agent': (
                    str(prior.get('focused_agent') or '')
                    if focused_agent is None else _clean_id(focused_agent)
                ),
                'terminal_id': (
                    str(prior.get('terminal_id') or '')
                    if terminal_id is None else _clean_id(terminal_id)
                ),
                'last_heartbeat_at': now,
                'last_user_activity_at': prior.get('last_user_activity_at'),
            }
            if user_activity:
                updated['last_user_activity_at'] = now
            presence[device_id] = updated
            self._write_presence(presence)
            self._append_audit(
                event='device_presence_updated',
                result='ok',
                device_id=device_id,
                visible=bool(visible),
                user_activity=bool(user_activity),
            )
            return _public_presence(
                updated,
                now=self._clock(),
                freshness_ttl_seconds=self._presence_ttl_seconds,
            )

    def presence_for_device(self, device_id: str) -> dict[str, object] | None:
        with self._lock:
            record = self._read_presence().get(str(device_id or ''))
            return _public_presence(
                record,
                now=self._clock(),
                freshness_ttl_seconds=self._presence_ttl_seconds,
            ) if record else None

    def register_push_token(self, *, device_id: str, token: str) -> None:
        clean_token = str(token or '').strip()
        if not clean_token or len(clean_token) > 4096:
            raise MobileGatewayPairingError('push token is invalid', status_code=400, reason='invalid_push_token')
        with self._lock:
            if not any(record.get('device_id') == device_id and not record.get('revoked_at') for record in self._read_devices()):
                raise MobileGatewayPairingError('device token revoked', status_code=401, reason='revoked')
            tokens = self._read_push_tokens()
            tokens[device_id] = {'device_id': device_id, 'token': clean_token, 'updated_at': _iso(self._clock())}
            self._write_push_tokens(tokens)
            self._append_audit(event='device_push_token_registered', result='ok', device_id=device_id)

    def delete_push_token(self, *, device_id: str, reason: str = 'device_deleted') -> bool:
        with self._lock:
            tokens = self._read_push_tokens()
            removed = tokens.pop(device_id, None) is not None
            if removed:
                self._write_push_tokens(tokens)
                self._append_audit(event='device_push_token_deleted', result='ok', device_id=device_id, reason=reason)
            return removed

    def push_tokens_for_delivery(self) -> list[tuple[str, str]]:
        """Internal-only provider tokens for active devices."""
        with self._lock:
            active_ids = {str(record.get('device_id') or '') for record in self._read_devices() if not record.get('revoked_at')}
            return [
                (device_id, str(record.get('token') or ''))
                for device_id, record in self._read_push_tokens().items()
                if device_id in active_ids and str(record.get('token') or '')
            ]

    def revoke_pairing(self, pairing_id: str, *, reason: str = 'revoked') -> dict[str, object] | None:
        requested = str(pairing_id or '').strip()
        if not requested:
            return None
        with self._lock:
            record = self._pairing_state_by_id().get(requested)
            if record is None:
                return None
            if record.get('revoked_at'):
                return dict(record)
            updated = dict(record)
            updated['revoked_at'] = _iso(self._clock())
            updated['revoked_reason'] = str(reason or 'revoked')
            _append_jsonl(self.pairing_tokens_path, updated)
            self._append_audit(
                event='pairing_token_revoked',
                result='ok',
                project_id=str(updated.get('project_id') or ''),
                pairing_id=requested,
                reason=updated['revoked_reason'],
            )
            return updated

    def claim_pairing(
        self,
        *,
        pairing_code: str,
        device_name: str,
        requested_device_id: str | None = None,
    ) -> dict[str, object]:
        code = str(pairing_code or '').strip()
        if not code:
            raise MobileGatewayPairingError('pairing_code is required', status_code=400, reason='missing_code')
        name = str(device_name or '').strip() or 'Mobile device'
        now = self._clock()
        token_hash = _token_hash(_PAIRING_HASH_PREFIX, code)
        with self._lock:
            pairings = self._pairing_state_by_id()
            record = next(
                (item for item in pairings.values() if item.get('token_hash') == token_hash),
                None,
            )
            if not record:
                self._append_audit(event='pairing_claim_denied', result='denied', reason='invalid_code')
                raise MobileGatewayPairingError('invalid pairing_code', status_code=401, reason='invalid_code')
            pairing_id = str(record.get('pairing_id') or '')
            project_id = str(record.get('project_id') or '')
            if record.get('revoked_at'):
                self._append_audit(
                    event='pairing_claim_denied',
                    result='denied',
                    project_id=project_id,
                    pairing_id=pairing_id,
                    reason='revoked',
                )
                raise MobileGatewayPairingError('pairing_code revoked', status_code=410, reason='revoked')
            reusable_claims = bool(record.get('reusable_claims'))
            if not reusable_claims and (record.get('claimed_at') or record.get('claimed_by_device_id')):
                self._append_audit(
                    event='pairing_claim_denied',
                    result='denied',
                    project_id=project_id,
                    pairing_id=pairing_id,
                    reason='already_claimed',
                )
                raise MobileGatewayPairingError('pairing_code already claimed', status_code=409, reason='already_claimed')
            expires_at = _parse_utc(record.get('expires_at'))
            if expires_at is not None and now > expires_at:
                self._append_audit(
                    event='pairing_claim_denied',
                    result='denied',
                    project_id=project_id,
                    pairing_id=pairing_id,
                    reason='expired',
                )
                raise MobileGatewayPairingError('pairing_code expired', status_code=410, reason='expired')

            device_token = self._token_factory(32)
            device_id = _clean_id(requested_device_id) or self._id_factory('dev')
            existing_device = next(
                (item for item in self._read_devices() if item.get('device_id') == device_id and not item.get('revoked_at')),
                None,
            )
            if existing_device:
                self._append_audit(
                    event='pairing_claim_denied',
                    result='denied',
                    project_id=project_id,
                    pairing_id=pairing_id,
                    device_id=device_id,
                    reason='device_id_exists',
                )
                raise MobileGatewayPairingError('device_id already exists', status_code=409, reason='device_id_exists')

            device_record = {
                'schema_version': _SCHEMA_VERSION,
                'device_id': device_id,
                'name': name,
                'project_id': project_id,
                'pairing_id': pairing_id,
                'token_hash': _token_hash(_DEVICE_HASH_PREFIX, device_token),
                'scopes': _scope_list(record.get('scopes')),
                'route_provider': str(record.get('route_provider') or 'lan'),
                'gateway_url': str(record.get('gateway_url') or ''),
                'created_at': _iso(now),
                'last_seen_at': None,
                'revoked_at': None,
            }
            devices = [item for item in self._read_devices() if item.get('device_id') != device_id]
            devices.append(device_record)
            _write_json(self.devices_path, {'schema_version': _SCHEMA_VERSION, 'devices': devices})

            updated_record = dict(record)
            updated_record['claimed_at'] = _iso(now)
            updated_record['claimed_by_device_id'] = device_id
            _append_jsonl(self.pairing_tokens_path, updated_record)
            self._append_audit(
                event='pairing_claimed',
                result='ok',
                project_id=project_id,
                pairing_id=pairing_id,
                device_id=device_id,
                scopes=device_record['scopes'],
            )

        return {
            'schema_version': _SCHEMA_VERSION,
            'status': 'ok',
            'device': _public_device(device_record),
            'device_token': device_token,
            'host_profile': {
                'host_id': project_id,
                'project_id': project_id,
                'device_id': device_id,
                'route_provider': device_record['route_provider'],
                'gateway_url': device_record['gateway_url'],
                'scopes': list(device_record['scopes']),
            },
        }

    def pairing_code_is_claimable(self, pairing_code: str) -> bool:
        code = str(pairing_code or '').strip()
        if not code:
            return False
        token_hash = _token_hash(_PAIRING_HASH_PREFIX, code)
        now = self._clock()
        with self._lock:
            record = next(
                (item for item in self._pairing_state_by_id().values() if item.get('token_hash') == token_hash),
                None,
            )
            if record is None:
                return False
            if record.get('revoked_at'):
                return False
            if not bool(record.get('reusable_claims')) and (record.get('claimed_at') or record.get('claimed_by_device_id')):
                return False
            expires_at = _parse_utc(record.get('expires_at'))
            return expires_at is None or now <= expires_at

    def authenticate_device(
        self,
        device_token: str,
        *,
        required_scopes: Iterable[str] = (),
    ) -> AuthenticatedDevice:
        token = str(device_token or '').strip()
        if not token:
            raise MobileGatewayPairingError('device bearer token is required', status_code=401, reason='missing_token')
        token_hash = _token_hash(_DEVICE_HASH_PREFIX, token)
        required = _scope_set(required_scopes)
        with self._lock:
            devices = self._read_devices()
            for index, record in enumerate(devices):
                if record.get('token_hash') != token_hash:
                    continue
                device_id = str(record.get('device_id') or '')
                project_id = str(record.get('project_id') or '')
                if record.get('revoked_at'):
                    self._append_audit(
                        event='device_auth_denied',
                        result='denied',
                        project_id=project_id,
                        device_id=device_id,
                        reason='revoked',
                    )
                    raise MobileGatewayPairingError('device token revoked', status_code=401, reason='revoked')
                scopes = _scope_set(record.get('scopes'))
                missing = sorted(required - scopes)
                if missing:
                    self._append_audit(
                        event='device_auth_denied',
                        result='denied',
                        project_id=project_id,
                        device_id=device_id,
                        reason='missing_scope',
                        scopes=missing,
                    )
                    raise MobileGatewayPairingError('device scope denied', status_code=403, reason='missing_scope')
                updated = dict(record)
                updated['last_seen_at'] = _iso(self._clock())
                devices[index] = updated
                _write_json(self.devices_path, {'schema_version': _SCHEMA_VERSION, 'devices': devices})
                self._append_audit(
                    event='device_auth_ok',
                    result='ok',
                    project_id=project_id,
                    device_id=device_id,
                    scopes=sorted(required),
                )
                return AuthenticatedDevice(record=updated)
        self._append_audit(event='device_auth_denied', result='denied', reason='invalid_token')
        raise MobileGatewayPairingError('invalid device token', status_code=401, reason='invalid_token')

    def list_devices(self) -> list[dict[str, object]]:
        with self._lock:
            return [_public_device(record) for record in self._read_devices()]

    def revoke_device_locally(self, *, device_id: str, reason: str = 'host_revoked') -> dict[str, object]:
        requested = _clean_id(device_id)
        if not requested:
            raise MobileGatewayPairingError('device_id is required', status_code=400, reason='missing_device_id')
        with self._lock:
            return self._revoke_device_record(
                device_id=requested,
                revoked_by_device_id=None,
                reason=str(reason or 'host_revoked'),
            )

    def revoke_device(self, *, device_id: str, device_token: str) -> dict[str, object]:
        requested = _clean_id(device_id)
        if not requested:
            raise MobileGatewayPairingError('device_id is required', status_code=400, reason='missing_device_id')
        auth = self.authenticate_device(device_token)
        if auth.device_id != requested:
            self._append_audit(
                event='device_revoke_denied',
                result='denied',
                project_id=str(auth.record.get('project_id') or ''),
                device_id=auth.device_id,
                target_device_id=requested,
                reason='self_revoke_only',
            )
            raise MobileGatewayPairingError('device can only revoke itself in G2', status_code=403, reason='self_revoke_only')
        with self._lock:
            return self._revoke_device_record(
                device_id=requested,
                revoked_by_device_id=auth.device_id,
                reason='self_revoked',
            )

    def create_terminal_handle(
        self,
        *,
        project_id: str,
        device_id: str,
        target_epoch: int,
        target_summary: dict[str, object],
        geometry: dict[str, object],
        expires_seconds: int = _DEFAULT_TERMINAL_EXPIRES_SECONDS,
    ) -> dict[str, object]:
        now = self._clock()
        expires_at = now + timedelta(seconds=max(1, int(expires_seconds)))
        terminal_id = self._id_factory('term')
        terminal_token = self._token_factory(32)
        record = {
            'schema_version': _SCHEMA_VERSION,
            'terminal_id': terminal_id,
            'project_id': str(project_id),
            'device_id': str(device_id),
            'token_hash': _token_hash(_TERMINAL_HASH_PREFIX, terminal_token),
            'created_at': _iso(now),
            'expires_at': _iso(expires_at),
            'target_epoch': int(target_epoch),
            'target_summary': dict(target_summary),
            'geometry': dict(geometry),
            'last_input_seq': 0,
            'last_output_seq': 0,
            'disconnected_at': None,
            'closed_at': None,
            'revoked_at': None,
        }
        with self._lock:
            self._ensure_dir()
            _append_jsonl(self.terminal_tokens_path, record)
            self._append_audit(
                event='terminal_token_created',
                result='ok',
                project_id=str(project_id),
                device_id=str(device_id),
                terminal_id=terminal_id,
                target_epoch=int(target_epoch),
            )
        return {
            'schema_version': _SCHEMA_VERSION,
            'terminal_id': terminal_id,
            'terminal_token': terminal_token,
            'expires_at': _iso(expires_at),
            'target_epoch': int(target_epoch),
            'target_summary': dict(target_summary),
        }

    def authenticate_terminal_token(
        self,
        *,
        terminal_id: str,
        terminal_token: str,
        resume_cursor: int | None = None,
    ) -> dict[str, object]:
        token = str(terminal_token or '').strip()
        requested = str(terminal_id or '').strip()
        if not requested or not token:
            raise MobileGatewayPairingError('terminal token is required', status_code=401, reason='missing_terminal_token')
        token_hash = _token_hash(_TERMINAL_HASH_PREFIX, token)
        with self._lock:
            record = self._terminal_state_by_id().get(requested)
            if record is None or record.get('token_hash') != token_hash:
                self._append_audit(
                    event='terminal_auth_denied',
                    result='denied',
                    terminal_id=requested,
                    reason='invalid_token',
                )
                raise MobileGatewayPairingError('invalid terminal token', status_code=401, reason='invalid_token')
            self._validate_terminal_record(record)
            record = self._validate_resume_cursor(record, resume_cursor)
            self._append_audit(
                event='terminal_auth_ok',
                result='ok',
                project_id=str(record.get('project_id') or ''),
                device_id=str(record.get('device_id') or ''),
                terminal_id=requested,
                resume_cursor=resume_cursor,
            )
            return dict(record)

    def _validate_resume_cursor(
        self,
        record: dict[str, object],
        resume_cursor: int | None,
    ) -> dict[str, object]:
        terminal_id = str(record.get('terminal_id') or '')
        project_id = str(record.get('project_id') or '')
        device_id = str(record.get('device_id') or '')
        disconnected = bool(record.get('disconnected_at'))
        last_output_seq = _int(record.get('last_output_seq'), 0)
        if disconnected and resume_cursor is None:
            self._append_audit(
                event='terminal_resume_denied',
                result='denied',
                project_id=project_id,
                device_id=device_id,
                terminal_id=terminal_id,
                reason='missing_resume_cursor',
                last_output_seq=last_output_seq,
            )
            raise MobileGatewayPairingError(
                'resume_cursor is required after terminal disconnect',
                status_code=409,
                reason='missing_resume_cursor',
            )
        if resume_cursor is not None:
            cursor = int(resume_cursor)
            if cursor > last_output_seq:
                self._append_audit(
                    event='terminal_resume_denied',
                    result='denied',
                    project_id=project_id,
                    device_id=device_id,
                    terminal_id=terminal_id,
                    reason='stale_resume_cursor',
                    last_output_seq=last_output_seq,
                    resume_cursor=cursor,
                )
                raise MobileGatewayPairingError(
                    'terminal resume cursor is stale',
                    status_code=409,
                    reason='stale_resume_cursor',
                )
            updated = dict(record)
            updated['disconnected_at'] = None
            updated['resumed_at'] = _iso(self._clock())
            updated['last_resume_cursor'] = cursor
            updated['last_resume_gap'] = max(0, last_output_seq - cursor)
            _append_jsonl(self.terminal_tokens_path, updated)
            self._append_audit(
                event='terminal_resume_ok',
                result='ok',
                project_id=project_id,
                device_id=device_id,
                terminal_id=terminal_id,
                resume_cursor=cursor,
                last_output_seq=last_output_seq,
                skipped_output_count=updated['last_resume_gap'],
            )
            return updated
        return record

    def record_terminal_input_sequence(
        self,
        *,
        terminal_id: str,
        terminal_token: str,
        sequence: int,
    ) -> dict[str, object]:
        requested = str(terminal_id or '').strip()
        token_hash = _token_hash(_TERMINAL_HASH_PREFIX, str(terminal_token or '').strip())
        seq = int(sequence)
        with self._lock:
            record = self._terminal_state_by_id().get(requested)
            if record is None or record.get('token_hash') != token_hash:
                self._append_audit(
                    event='terminal_input_denied',
                    result='denied',
                    terminal_id=requested,
                    reason='invalid_token',
                )
                raise MobileGatewayPairingError('invalid terminal token', status_code=401, reason='invalid_token')
            self._validate_terminal_record(record)
            last_seq = _int(record.get('last_input_seq'), 0)
            if seq <= last_seq:
                self._append_audit(
                    event='terminal_input_denied',
                    result='denied',
                    project_id=str(record.get('project_id') or ''),
                    device_id=str(record.get('device_id') or ''),
                    terminal_id=requested,
                    reason='replayed_sequence',
                    last_input_seq=last_seq,
                    sequence=seq,
                )
                raise MobileGatewayPairingError('terminal input sequence replayed', status_code=409, reason='replayed_sequence')
            updated = dict(record)
            updated['last_input_seq'] = seq
            _append_jsonl(self.terminal_tokens_path, updated)
            self._append_audit(
                event='terminal_input_accepted',
                result='ok',
                project_id=str(record.get('project_id') or ''),
                device_id=str(record.get('device_id') or ''),
                terminal_id=requested,
                sequence=seq,
            )
            return updated

    def record_terminal_output_sequence(
        self,
        *,
        terminal_id: str,
        terminal_token: str,
        sequence: int,
    ) -> dict[str, object]:
        requested = str(terminal_id or '').strip()
        token_hash = _token_hash(_TERMINAL_HASH_PREFIX, str(terminal_token or '').strip())
        seq = int(sequence)
        with self._lock:
            record = self._terminal_state_by_id().get(requested)
            if record is None or record.get('token_hash') != token_hash:
                self._append_audit(
                    event='terminal_output_denied',
                    result='denied',
                    terminal_id=requested,
                    reason='invalid_token',
                )
                raise MobileGatewayPairingError('invalid terminal token', status_code=401, reason='invalid_token')
            self._validate_terminal_record(record)
            last_seq = _int(record.get('last_output_seq'), 0)
            if seq <= last_seq:
                return dict(record)
            updated = dict(record)
            updated['last_output_seq'] = seq
            _append_jsonl(self.terminal_tokens_path, updated)
            return updated

    def mark_terminal_disconnected(
        self,
        *,
        terminal_id: str,
        terminal_token: str,
        reason: str = 'transport_disconnected',
    ) -> dict[str, object]:
        requested = str(terminal_id or '').strip()
        token_hash = _token_hash(_TERMINAL_HASH_PREFIX, str(terminal_token or '').strip())
        with self._lock:
            record = self._terminal_state_by_id().get(requested)
            if record is None or record.get('token_hash') != token_hash:
                self._append_audit(
                    event='terminal_disconnect_denied',
                    result='denied',
                    terminal_id=requested,
                    reason='invalid_token',
                )
                raise MobileGatewayPairingError('invalid terminal token', status_code=401, reason='invalid_token')
            self._validate_terminal_record(record)
            updated = dict(record)
            updated['disconnected_at'] = _iso(self._clock())
            updated['disconnected_reason'] = str(reason or 'transport_disconnected')
            _append_jsonl(self.terminal_tokens_path, updated)
            self._append_audit(
                event='terminal_disconnected',
                result='ok',
                project_id=str(record.get('project_id') or ''),
                device_id=str(record.get('device_id') or ''),
                terminal_id=requested,
                reason=updated['disconnected_reason'],
            )
            return updated

    def close_terminal_handle(
        self,
        *,
        terminal_id: str,
        terminal_token: str,
        reason: str = 'client_closed',
    ) -> dict[str, object]:
        requested = str(terminal_id or '').strip()
        token_hash = _token_hash(_TERMINAL_HASH_PREFIX, str(terminal_token or '').strip())
        with self._lock:
            record = self._terminal_state_by_id().get(requested)
            if record is None or record.get('token_hash') != token_hash:
                self._append_audit(
                    event='terminal_close_denied',
                    result='denied',
                    terminal_id=requested,
                    reason='invalid_token',
                )
                raise MobileGatewayPairingError('invalid terminal token', status_code=401, reason='invalid_token')
            if record.get('closed_at'):
                return dict(record)
            updated = dict(record)
            updated['closed_at'] = _iso(self._clock())
            updated['closed_reason'] = str(reason or 'client_closed')
            _append_jsonl(self.terminal_tokens_path, updated)
            self._append_audit(
                event='terminal_closed',
                result='ok',
                project_id=str(record.get('project_id') or ''),
                device_id=str(record.get('device_id') or ''),
                terminal_id=requested,
                reason=updated['closed_reason'],
            )
            return updated

    def revoke_host_terminal_handles(
        self,
        *,
        device_id: str,
        client_session_id: str,
        reason: str = 'host_shell_terminated',
    ) -> int:
        requested_device = str(device_id or '').strip()
        requested_session = str(client_session_id or '').strip()
        if not requested_device or not requested_session:
            raise MobileGatewayPairingError(
                'host terminal device and client session are required',
                status_code=400,
                reason='invalid_host_terminal_target',
            )
        now = _iso(self._clock())
        count = 0
        with self._lock:
            for record in self._terminal_state_by_id().values():
                target_summary = record.get('target_summary')
                if not isinstance(target_summary, dict):
                    continue
                if str(record.get('device_id') or '') != requested_device:
                    continue
                if str(record.get('project_id') or '') != '@host':
                    continue
                if str(target_summary.get('kind') or '') != 'host_shell':
                    continue
                if str(target_summary.get('client_session_id') or '') != requested_session:
                    continue
                if record.get('revoked_at') or record.get('closed_at'):
                    continue
                updated = dict(record)
                updated['revoked_at'] = now
                updated['revoked_reason'] = str(reason or 'host_shell_terminated')
                _append_jsonl(self.terminal_tokens_path, updated)
                count += 1
                self._append_audit(
                    event='terminal_revoked',
                    result='ok',
                    project_id='@host',
                    device_id=requested_device,
                    terminal_id=str(record.get('terminal_id') or ''),
                    client_session_id=requested_session,
                    reason=updated['revoked_reason'],
                )
        return count

    def _pairing_state_by_id(self) -> dict[str, dict[str, object]]:
        state: dict[str, dict[str, object]] = {}
        if not self.pairing_tokens_path.exists():
            return state
        for record in _read_jsonl(self.pairing_tokens_path):
            pairing_id = str(record.get('pairing_id') or '')
            if pairing_id:
                state[pairing_id] = record
        return state

    def _terminal_state_by_id(self) -> dict[str, dict[str, object]]:
        if not self.terminal_tokens_path.exists():
            self._reset_terminal_state_cache()
            return {}
        stat = self.terminal_tokens_path.stat()
        identity = (int(stat.st_dev), int(stat.st_ino))
        cache_invalid = (
            self._terminal_state_cache is None
            or self._terminal_state_cache_identity != identity
            or stat.st_size < self._terminal_state_cache_offset
            or (
                stat.st_size == self._terminal_state_cache_offset
                and stat.st_mtime_ns != self._terminal_state_cache_mtime_ns
            )
        )
        if cache_invalid:
            self._terminal_state_cache = {}
            self._terminal_state_cache_identity = identity
            self._terminal_state_cache_offset = 0
        records, offset = _read_jsonl_from_offset(
            self.terminal_tokens_path,
            self._terminal_state_cache_offset,
        )
        state = self._terminal_state_cache or {}
        for record in records:
            terminal_id = str(record.get('terminal_id') or '')
            if terminal_id:
                state[terminal_id] = record
        self._terminal_state_cache = state
        self._terminal_state_cache_offset = offset
        current_stat = self.terminal_tokens_path.stat()
        self._terminal_state_cache_identity = (
            int(current_stat.st_dev),
            int(current_stat.st_ino),
        )
        self._terminal_state_cache_mtime_ns = int(current_stat.st_mtime_ns)
        if (
            self._terminal_state_cache_offset == current_stat.st_size
            and current_stat.st_size > _TERMINAL_LOG_COMPACT_BYTES
        ):
            self._compact_terminal_state_log()
        return dict(self._terminal_state_cache)

    def _reset_terminal_state_cache(self) -> None:
        self._terminal_state_cache = None
        self._terminal_state_cache_identity = None
        self._terminal_state_cache_offset = 0
        self._terminal_state_cache_mtime_ns = 0

    def _compact_terminal_state_log(self) -> None:
        state = self._terminal_state_cache or {}
        _write_jsonl_records(self.terminal_tokens_path, state.values())
        stat = self.terminal_tokens_path.stat()
        self._terminal_state_cache_identity = (int(stat.st_dev), int(stat.st_ino))
        self._terminal_state_cache_offset = int(stat.st_size)
        self._terminal_state_cache_mtime_ns = int(stat.st_mtime_ns)

    def _validate_terminal_record(self, record: dict[str, object]) -> None:
        terminal_id = str(record.get('terminal_id') or '')
        project_id = str(record.get('project_id') or '')
        device_id = str(record.get('device_id') or '')
        if record.get('revoked_at'):
            self._append_audit(
                event='terminal_auth_denied',
                result='denied',
                project_id=project_id,
                device_id=device_id,
                terminal_id=terminal_id,
                reason='revoked',
            )
            raise MobileGatewayPairingError('terminal token revoked', status_code=401, reason='revoked')
        if self._is_device_revoked(device_id):
            self._append_audit(
                event='terminal_auth_denied',
                result='denied',
                project_id=project_id,
                device_id=device_id,
                terminal_id=terminal_id,
                reason='device_revoked',
            )
            raise MobileGatewayPairingError('terminal device revoked', status_code=401, reason='device_revoked')
        if record.get('closed_at'):
            self._append_audit(
                event='terminal_auth_denied',
                result='denied',
                project_id=project_id,
                device_id=device_id,
                terminal_id=terminal_id,
                reason='closed',
            )
            raise MobileGatewayPairingError('terminal already closed', status_code=410, reason='closed')
        expires_at = _parse_utc(record.get('expires_at'))
        if expires_at is not None and self._clock() > expires_at:
            self._append_audit(
                event='terminal_auth_denied',
                result='denied',
                project_id=project_id,
                device_id=device_id,
                terminal_id=terminal_id,
                reason='expired',
            )
            raise MobileGatewayPairingError('terminal token expired', status_code=410, reason='expired')

    def _read_devices(self) -> list[dict[str, object]]:
        if not self.devices_path.exists():
            return []
        try:
            payload = json.loads(self.devices_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return []
        devices = payload.get('devices') if isinstance(payload, dict) else None
        if not isinstance(devices, list):
            return []
        return [dict(item) for item in devices if isinstance(item, dict)]

    def _read_handoff(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self.handoff_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get('record_type') != _HANDOFF_RECORD_TYPE:
            return None
        if not str(payload.get('pairing_code') or '').strip():
            return None
        return dict(payload)

    def _write_handoff(self, payload: dict[str, object]) -> None:
        self._ensure_dir()
        _write_json(self.handoff_path, payload)

    def _public_handoff(self, handoff: dict[str, object]) -> dict[str, object]:
        return {
            'schema_version': _SCHEMA_VERSION,
            'pairing_id': str(handoff.get('pairing_id') or ''),
            'pairing_code': str(handoff.get('pairing_code') or ''),
            'project_id': str(handoff.get('project_id') or ''),
            'route_provider': str(handoff.get('route_provider') or ''),
            'gateway_url': str(handoff.get('gateway_url') or ''),
            'claim_endpoint': str(handoff.get('claim_endpoint') or ''),
            'scopes': _scope_list(handoff.get('scopes')),
            'expires_at': None,
            'reusable_claims': True,
            'generation': _int(handoff.get('generation'), 1),
        }

    def _read_presence(self) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self.presence_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get('record_type') != _PRESENCE_RECORD_TYPE:
            return {}
        records = payload.get('devices')
        if not isinstance(records, list):
            return {}
        return {
            str(record.get('device_id') or ''): dict(record)
            for record in records
            if isinstance(record, dict) and str(record.get('device_id') or '')
        }

    def _write_presence(self, presence: dict[str, dict[str, object]]) -> None:
        self._ensure_dir()
        _write_json(self.presence_path, {
            'schema_version': _SCHEMA_VERSION,
            'record_type': _PRESENCE_RECORD_TYPE,
            'devices': [presence[key] for key in sorted(presence)],
        })

    def _read_push_tokens(self) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self.push_tokens_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get('record_type') != _PUSH_TOKEN_RECORD_TYPE:
            return {}
        records = payload.get('devices')
        if not isinstance(records, list):
            return {}
        return {
            str(record.get('device_id') or ''): dict(record)
            for record in records
            if isinstance(record, dict) and str(record.get('device_id') or '')
        }

    def _write_push_tokens(self, tokens: dict[str, dict[str, object]]) -> None:
        self._ensure_dir()
        _write_json(self.push_tokens_path, {
            'schema_version': _SCHEMA_VERSION,
            'record_type': _PUSH_TOKEN_RECORD_TYPE,
            'devices': [tokens[key] for key in sorted(tokens)],
        })

    def _revoke_device_record(
        self,
        *,
        device_id: str,
        revoked_by_device_id: str | None,
        reason: str,
    ) -> dict[str, object]:
        now = _iso(self._clock())
        devices = self._read_devices()
        for index, record in enumerate(devices):
            if record.get('device_id') != device_id:
                continue
            updated = dict(record)
            if not updated.get('revoked_at'):
                updated['revoked_at'] = now
            devices[index] = updated
            _write_json(self.devices_path, {'schema_version': _SCHEMA_VERSION, 'devices': devices})
            presence = self._read_presence()
            presence_removed = presence.pop(device_id, None) is not None
            if presence_removed:
                self._write_presence(presence)
            revoked_terminal_count = self._revoke_terminal_handles_for_device(
                device_id=device_id,
                now=now,
                reason=reason,
            )
            push_token_removed = self.delete_push_token(device_id=device_id, reason='device_revoked')
            self._append_audit(
                event='device_revoked',
                result='ok',
                project_id=str(record.get('project_id') or ''),
                device_id=device_id,
                revoked_by_device_id=revoked_by_device_id,
                reason=reason,
                revoked_terminal_count=revoked_terminal_count,
                presence_removed=presence_removed,
                push_token_removed=push_token_removed,
            )
            return {
                'schema_version': _SCHEMA_VERSION,
                'status': 'revoked',
                'device': _public_device(updated),
                'revoked_terminal_count': revoked_terminal_count,
                'presence_removed': presence_removed,
                'push_token_removed': push_token_removed,
            }
        raise MobileGatewayPairingError('device not found', status_code=404, reason='not_found')

    def _revoke_terminal_handles_for_device(self, *, device_id: str, now: str, reason: str) -> int:
        count = 0
        for record in self._terminal_state_by_id().values():
            if str(record.get('device_id') or '') != device_id:
                continue
            if record.get('revoked_at') or record.get('closed_at'):
                continue
            updated = dict(record)
            updated['revoked_at'] = now
            updated['revoked_reason'] = reason
            _append_jsonl(self.terminal_tokens_path, updated)
            count += 1
            self._append_audit(
                event='terminal_revoked',
                result='ok',
                project_id=str(record.get('project_id') or ''),
                device_id=device_id,
                terminal_id=str(record.get('terminal_id') or ''),
                reason=reason,
            )
        return count

    def _is_device_revoked(self, device_id: str) -> bool:
        if not device_id:
            return False
        return any(
            record.get('device_id') == device_id and bool(record.get('revoked_at'))
            for record in self._read_devices()
        )

    def _append_audit(self, *, event: str, result: str, **fields: object) -> None:
        entry = {
            'schema_version': _SCHEMA_VERSION,
            'timestamp': _iso(self._clock()),
            'event': str(event),
            'result': str(result),
        }
        for key, value in fields.items():
            if value is not None and value != '':
                entry[key] = value
        _append_jsonl(self.audit_path, entry)

    def _ensure_dir(self) -> None:
        self._mobile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._mobile_dir.chmod(0o700)


def _token_hash(prefix: str, value: str) -> str:
    digest = hashlib.sha256(f'{prefix}{value}'.encode('utf-8')).hexdigest()
    return f'sha256:{digest}'


def _scope_list(value: object) -> list[str]:
    return sorted(_scope_set(value) or set(_DEFAULT_DEVICE_SCOPES))


def _scope_set(value: object) -> set[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, Iterable):
        items = [str(item) for item in value]
    else:
        items = []
    return {item.strip() for item in items if item.strip()}


def _public_device(record: dict[str, object]) -> dict[str, object]:
    return {
        'device_id': record.get('device_id'),
        'name': record.get('name'),
        'project_id': record.get('project_id'),
        'pairing_id': record.get('pairing_id'),
        'scopes': _scope_list(record.get('scopes')),
        'route_provider': record.get('route_provider'),
        'gateway_url': record.get('gateway_url'),
        'created_at': record.get('created_at'),
        'last_seen_at': record.get('last_seen_at'),
        'revoked': bool(record.get('revoked_at')),
        'revoked_at': record.get('revoked_at'),
    }


def _public_presence(
    record: dict[str, object],
    *,
    now: datetime,
    freshness_ttl_seconds: int,
) -> dict[str, object]:
    heartbeat_at = _parse_utc(record.get('last_heartbeat_at'))
    fresh = (
        heartbeat_at is not None
        and now <= heartbeat_at + timedelta(seconds=max(1, int(freshness_ttl_seconds)))
    )
    return {
        # These are authoritative effective values, not the last stale report.
        'visible': bool(record.get('visible')) if fresh else False,
        'freshness': 'fresh' if fresh else 'stale',
        'focused_project_id': str(record.get('focused_project_id') or '') if fresh else '',
        'focused_agent': str(record.get('focused_agent') or '') if fresh else '',
        'terminal_id': str(record.get('terminal_id') or '') if fresh else '',
        'last_heartbeat_at': record.get('last_heartbeat_at'),
        'last_user_activity_at': record.get('last_user_activity_at'),
    }


def _clean_id(value: str | None) -> str:
    text = str(value or '').strip()
    return ''.join(ch for ch in text if ch.isalnum() or ch in {'_', '-'})


def _int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return records
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(dict(payload))
    return records


def _read_jsonl_from_offset(
    path: Path,
    offset: int,
) -> tuple[list[dict[str, object]], int]:
    records: list[dict[str, object]] = []
    start = max(0, int(offset))
    try:
        with path.open('rb') as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return records, start
    complete_end = data.rfind(b'\n') + 1
    if complete_end <= 0:
        return records, start
    for raw_line in data[:complete_end].splitlines():
        text = raw_line.decode('utf-8', errors='replace').strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(dict(payload))
    return records, start + complete_end


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.write('\n')
    path.chmod(0o600)


def _write_jsonl_records(path: Path, records: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        for payload in records:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write('\n')
    tmp.replace(path)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(path)
    path.chmod(0o600)


def _parse_utc(value: object) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _token_urlsafe(size: int) -> str:
    return secrets.token_urlsafe(size)


def _random_id(prefix: str) -> str:
    return f'{prefix}_{secrets.token_hex(8)}'


__all__ = [
    'AuthenticatedDevice',
    'MobileGatewayPairingError',
    'MobileGatewayPairingStore',
]
