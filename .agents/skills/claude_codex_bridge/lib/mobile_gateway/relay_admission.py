from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from .relay_crypto import RELAY_PROTOCOL_VERSION


INVITATION_PREFIX = 'ccb-relay-inv-v2'
CAPABILITY_PREFIX = 'ccb-relay-cap-v1'
ADMISSION_SCHEMA_VERSION = 1
DEFAULT_INVITATION_TTL_SECONDS = 15 * 60
DEFAULT_SESSION_TTL_SECONDS = 5 * 60
QUOTA_WINDOW_SECONDS = 24 * 60 * 60
OPERATOR_SECRETS_PATH_ENV = 'CCB_RELAY_ADMISSION_SECRETS'
OPERATOR_VERIFIER_KEY_ENV = 'CCB_RELAY_VERIFIER_KEY_B64'
OPERATOR_CAPABILITY_KEY_ENV = 'CCB_RELAY_CAPABILITY_KEY_B64'


class RelayAdmissionError(ValueError):
    pass


@dataclass(frozen=True)
class RelayAdmissionSecrets:
    verifier_key: bytes
    capability_key: bytes
    source: str = 'injected'

    def __post_init__(self) -> None:
        if len(self.verifier_key) != 32:
            raise RelayAdmissionError('relay verifier key must be 32 bytes')
        if len(self.capability_key) != 32:
            raise RelayAdmissionError('relay capability key must be 32 bytes')

    @classmethod
    def generate_for_testing(cls) -> 'RelayAdmissionSecrets':
        return cls(
            verifier_key=secrets.token_bytes(32),
            capability_key=secrets.token_bytes(32),
            source='test-generated',
        )

    @classmethod
    def from_json_file(cls, path: str | Path) -> 'RelayAdmissionSecrets':
        secret_path = Path(path).expanduser()
        _require_owner_only_file(secret_path, 'relay admission secret file')
        try:
            payload = json.loads(secret_path.read_text(encoding='utf-8'))
        except OSError as exc:
            raise RelayAdmissionError('relay admission secrets are unavailable') from exc
        except json.JSONDecodeError as exc:
            raise RelayAdmissionError('relay admission secrets JSON invalid') from exc
        if not isinstance(payload, Mapping):
            raise RelayAdmissionError('relay admission secrets JSON invalid')
        return cls(
            verifier_key=_b64decode(_required_text(payload.get('verifier_key_b64'), 'verifier_key_b64')),
            capability_key=_b64decode(_required_text(payload.get('capability_key_b64'), 'capability_key_b64')),
            source=f'file:{secret_path}',
        )

    @classmethod
    def from_operator_config(cls, path: str | Path | None = None) -> 'RelayAdmissionSecrets':
        explicit_path = str(path or '').strip()
        if explicit_path:
            return cls.from_json_file(explicit_path)
        env_path = str(os.environ.get(OPERATOR_SECRETS_PATH_ENV) or '').strip()
        if env_path:
            return cls.from_json_file(env_path)
        verifier = str(os.environ.get(OPERATOR_VERIFIER_KEY_ENV) or '').strip()
        capability = str(os.environ.get(OPERATOR_CAPABILITY_KEY_ENV) or '').strip()
        if verifier and capability:
            return cls(
                verifier_key=_b64decode(verifier),
                capability_key=_b64decode(capability),
                source='env',
            )
        raise RelayAdmissionError('relay admission secrets are required')

    def fingerprint(self) -> str:
        payload = _canonical_json(
            {
                'capability_key_sha256': _b64(hashlib.sha256(self.capability_key).digest()),
                'verifier_key_sha256': _b64(hashlib.sha256(self.verifier_key).digest()),
            }
        )
        return 'sha256:' + _b64(hashlib.sha256(payload.encode('utf-8')).digest())


@dataclass(frozen=True)
class RelayIssuedInvitation:
    invite_id: str
    invitation: str
    state: str
    issued_at: int
    expires_at: int
    quota: Mapping[str, object]

    def to_operator_json(self) -> dict[str, object]:
        return {
            'relay_status': 'invite_issued',
            'invite_id': self.invite_id,
            'invitation': self.invitation,
            'state': self.state,
            'issued_at': self.issued_at,
            'expires_at': self.expires_at,
            'quota': dict(self.quota),
        }


@dataclass(frozen=True)
class RelayHostCredential:
    host_id: str
    invitation_id: str
    host_public_key_b64: str
    host_public_key_sha256: str
    issued_at: int
    quota: Mapping[str, object]

    def to_json(self) -> dict[str, object]:
        return {
            'type': 'ccb_relay_host_credential_v1',
            'relay_protocol_version': RELAY_PROTOCOL_VERSION,
            'host_id': self.host_id,
            'invitation_id': self.invitation_id,
            'host_public_key_b64': self.host_public_key_b64,
            'host_public_key_sha256': self.host_public_key_sha256,
            'issued_at': self.issued_at,
            'quota': dict(self.quota),
        }


class RelayAdmissionStore:
    def __init__(
        self,
        path: str | Path,
        *,
        admission_secrets: RelayAdmissionSecrets | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        if admission_secrets is None:
            raise RelayAdmissionError('relay admission secrets are required')
        self.path = Path(path).expanduser()
        self._secrets = admission_secrets
        self._secrets_fingerprint = admission_secrets.fingerprint()
        self._now = now or time.time
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not parent_existed:
            self.path.parent.chmod(0o700)
        self._prepare_storage_file()
        self._migrate()
        self._harden_storage_permissions()

    def issue_invitation(
        self,
        *,
        ttl_seconds: int = DEFAULT_INVITATION_TTL_SECONDS,
        label: str | None = None,
        max_sessions: int = 4,
        max_bytes_per_day: int = 1024 * 1024 * 1024,
    ) -> RelayIssuedInvitation:
        if ttl_seconds <= 0:
            raise RelayAdmissionError('relay invitation ttl must be positive')
        quota = _quota(max_sessions=max_sessions, max_bytes_per_day=max_bytes_per_day)
        now = self._now_s()
        expires_at = now + int(ttl_seconds)
        invite_id = 'rinv_' + secrets.token_urlsafe(12)
        secret = secrets.token_urlsafe(32)
        invitation = f'{INVITATION_PREFIX}.{invite_id}.{secret}'
        verifier = _verifier(self._verifier_key(), invitation)
        with self._transaction() as conn:
            conn.execute(
                '''
                INSERT INTO relay_invitations(
                    invite_id, verifier_b64, state, issued_at, expires_at,
                    consumed_at, revoked_at, host_id, label, quota_json, updated_at
                ) VALUES (?, ?, 'unused', ?, ?, NULL, NULL, NULL, ?, ?, ?)
                ''',
                (invite_id, verifier, now, expires_at, _redacted_text(label), _canonical_json(quota), now),
            )
            self._audit(conn, 'invite_issued', 'invite', invite_id, {'label': _redacted_text(label), 'quota': quota})
        return RelayIssuedInvitation(
            invite_id=invite_id,
            invitation=invitation,
            state='unused',
            issued_at=now,
            expires_at=expires_at,
            quota=quota,
        )

    def claim_invitation(self, invitation: str, *, host_public_key_b64: str) -> RelayHostCredential:
        invite_id = _invite_id_from_secret(invitation)
        verifier = _verifier(self._verifier_key(), invitation)
        host_public_key = _decode_ed25519_public_key(host_public_key_b64)
        host_public_key_hash = _sha256_b64(host_public_key)
        now = self._now_s()
        with self._transaction() as conn:
            row = conn.execute(
                'SELECT * FROM relay_invitations WHERE invite_id = ?',
                (invite_id,),
            ).fetchone()
            if row is None or not hmac.compare_digest(str(row['verifier_b64']), verifier):
                raise RelayAdmissionError('relay invitation is not claimable')
            if str(row['state']) == 'unused' and int(row['expires_at']) <= now:
                conn.execute(
                    'UPDATE relay_invitations SET state = ?, updated_at = ? WHERE invite_id = ? AND state = ?',
                    ('expired', now, invite_id, 'unused'),
                )
                self._audit(conn, 'invite_expired', 'invite', invite_id, {})
                raise RelayAdmissionError('relay invitation is not claimable')
            if str(row['state']) != 'unused':
                raise RelayAdmissionError('relay invitation is not claimable')
            host_id = 'rhost_' + secrets.token_urlsafe(16)
            quota = _json_object(str(row['quota_json'] or '{}'))
            conn.execute(
                '''
                INSERT INTO relay_hosts(
                    host_id, invitation_id, host_public_key_b64, host_public_key_sha256,
                    state, issued_at, revoked_at, quota_json, active_sessions,
                    bytes_used, bytes_window_started_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, NULL, ?, 0, 0, ?, ?)
                ''',
                (
                    host_id,
                    invite_id,
                    host_public_key_b64,
                    host_public_key_hash,
                    now,
                    _canonical_json(quota),
                    now,
                    now,
                ),
            )
            updated = conn.execute(
                '''
                UPDATE relay_invitations
                SET state = 'consumed', consumed_at = ?, host_id = ?, updated_at = ?
                WHERE invite_id = ? AND state = 'unused'
                ''',
                (now, host_id, now, invite_id),
            ).rowcount
            if updated != 1:
                raise RelayAdmissionError('relay invitation is not claimable')
            self._audit(conn, 'invite_consumed', 'invite', invite_id, {'host_id': host_id})
            self._audit(conn, 'host_credential_issued', 'host', host_id, {'invitation_id': invite_id})
        return RelayHostCredential(
            host_id=host_id,
            invitation_id=invite_id,
            host_public_key_b64=host_public_key_b64,
            host_public_key_sha256=host_public_key_hash,
            issued_at=now,
            quota=quota,
        )

    def invitation_status(self, invite_id: str) -> dict[str, object]:
        self.expire_due_invitations()
        with closing(self._connect()) as conn:
            row = conn.execute('SELECT * FROM relay_invitations WHERE invite_id = ?', (invite_id,)).fetchone()
        if row is None:
            raise RelayAdmissionError('relay invitation not found')
        return self._invitation_row(row)

    def list_invitations(self) -> list[dict[str, object]]:
        self.expire_due_invitations()
        with closing(self._connect()) as conn:
            rows = conn.execute('SELECT * FROM relay_invitations ORDER BY issued_at, invite_id').fetchall()
        return [self._invitation_row(row) for row in rows]

    def revoke_invitation(self, invite_id: str, *, reason: str | None = None) -> dict[str, object]:
        now = self._now_s()
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM relay_invitations WHERE invite_id = ?', (invite_id,)).fetchone()
            if row is None:
                raise RelayAdmissionError('relay invitation not found')
            if str(row['state']) == 'unused':
                conn.execute(
                    '''
                    UPDATE relay_invitations
                    SET state = 'revoked', revoked_at = ?, updated_at = ?
                    WHERE invite_id = ? AND state = 'unused'
                    ''',
                    (now, now, invite_id),
                )
                self._audit(conn, 'invite_revoked', 'invite', invite_id, {'reason': _redacted_text(reason)})
                row = conn.execute('SELECT * FROM relay_invitations WHERE invite_id = ?', (invite_id,)).fetchone()
        return self._invitation_row(row)

    def host_status(self, host_id: str) -> dict[str, object]:
        with closing(self._connect()) as conn:
            row = conn.execute('SELECT * FROM relay_hosts WHERE host_id = ?', (host_id,)).fetchone()
        if row is None:
            raise RelayAdmissionError('relay host not found')
        return self._host_row(row)

    def host_public_key_for_rendezvous(self, host_id: str) -> str:
        row = self._host_record(host_id)
        self._require_active_host_row(row)
        return str(row['host_public_key_b64'])

    def list_hosts(self) -> list[dict[str, object]]:
        with closing(self._connect()) as conn:
            rows = conn.execute('SELECT * FROM relay_hosts ORDER BY issued_at, host_id').fetchall()
        return [self._host_row(row) for row in rows]

    def revoke_host(self, host_id: str, *, reason: str | None = None) -> dict[str, object]:
        now = self._now_s()
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM relay_hosts WHERE host_id = ?', (host_id,)).fetchone()
            if row is None:
                raise RelayAdmissionError('relay host not found')
            if str(row['state']) == 'active':
                conn.execute(
                    '''
                    UPDATE relay_hosts
                    SET state = 'revoked', revoked_at = ?, active_sessions = 0, updated_at = ?
                    WHERE host_id = ? AND state = 'active'
                    ''',
                    (now, now, host_id),
                )
                conn.execute(
                    '''
                    UPDATE relay_host_sessions
                    SET state = 'released', released_at = ?
                    WHERE host_id = ? AND state = 'active'
                    ''',
                    (now, host_id),
                )
                self._audit(conn, 'host_revoked', 'host', host_id, {'reason': _redacted_text(reason)})
                row = conn.execute('SELECT * FROM relay_hosts WHERE host_id = ?', (host_id,)).fetchone()
        return self._host_row(row)

    def reserve_host_session(self, *, host_id: str, session_id: str) -> dict[str, object]:
        base_host_id = _required_text(host_id, 'host_id')
        base_session_id = _required_text(session_id, 'session_id')
        now = self._now_s()
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM relay_hosts WHERE host_id = ?', (base_host_id,)).fetchone()
            host = self._require_active_host_row(row)
            existing = conn.execute(
                'SELECT * FROM relay_host_sessions WHERE session_id = ?',
                (base_session_id,),
            ).fetchone()
            if existing is not None:
                if str(existing['host_id']) == base_host_id and str(existing['state']) == 'active':
                    payload = self._host_row(host)
                    payload['session_id'] = base_session_id
                    payload['relay_status'] = 'host_quota_reserved'
                    payload['idempotent'] = True
                    return payload
                raise RelayAdmissionError('relay host session identity conflict')
            quota = _json_object(str(host['quota_json'] or '{}'))
            active_sessions = int(host['active_sessions'] or 0)
            max_sessions = _quota_limit(quota, 'max_sessions')
            if active_sessions >= max_sessions:
                self._audit(conn, 'host_session_quota_rejected', 'host', base_host_id, {'limit': max_sessions})
                raise RelayAdmissionError('relay host session quota exceeded')
            conn.execute(
                '''
                INSERT INTO relay_host_sessions(session_id, host_id, state, reserved_at, released_at)
                VALUES (?, ?, 'active', ?, NULL)
                ''',
                (base_session_id, base_host_id, now),
            )
            conn.execute(
                '''
                UPDATE relay_hosts
                SET active_sessions = active_sessions + 1, updated_at = ?
                WHERE host_id = ? AND state = 'active'
                ''',
                (now, base_host_id),
            )
            self._audit(conn, 'host_session_reserved', 'host', base_host_id, {'session_id': base_session_id})
            row = conn.execute('SELECT * FROM relay_hosts WHERE host_id = ?', (base_host_id,)).fetchone()
        payload = self._host_row(row)
        payload['session_id'] = base_session_id
        payload['relay_status'] = 'host_quota_reserved'
        payload['idempotent'] = False
        return payload

    def release_host_session(self, *, host_id: str, session_id: str) -> dict[str, object]:
        base_host_id = _required_text(host_id, 'host_id')
        base_session_id = _required_text(session_id, 'session_id')
        now = self._now_s()
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM relay_hosts WHERE host_id = ?', (base_host_id,)).fetchone()
            host = self._require_active_host_row(row)
            session = conn.execute(
                'SELECT * FROM relay_host_sessions WHERE session_id = ?',
                (base_session_id,),
            ).fetchone()
            if session is None or str(session['host_id']) != base_host_id:
                raise RelayAdmissionError('relay host session not found')
            if str(session['state']) == 'active':
                conn.execute(
                    '''
                    UPDATE relay_host_sessions
                    SET state = 'released', released_at = ?
                    WHERE session_id = ? AND state = 'active'
                    ''',
                    (now, base_session_id),
                )
                conn.execute(
                    '''
                    UPDATE relay_hosts
                    SET active_sessions = CASE
                        WHEN active_sessions > 0 THEN active_sessions - 1
                        ELSE 0
                    END,
                    updated_at = ?
                    WHERE host_id = ? AND state = 'active'
                    ''',
                    (now, base_host_id),
                )
                self._audit(conn, 'host_session_released', 'host', base_host_id, {'session_id': base_session_id})
                row = conn.execute('SELECT * FROM relay_hosts WHERE host_id = ?', (base_host_id,)).fetchone()
            else:
                row = host
        payload = self._host_row(row)
        payload['session_id'] = base_session_id
        payload['relay_status'] = 'host_quota_released'
        return payload

    def reconcile_active_sessions(self, *, reason: str = 'relay_service_restart') -> int:
        """Release reservations left behind by a stopped single relay instance.

        The production relay is intentionally a single-instance service backed by
        one admission database. WebSocket sessions are process-local, so any row
        still marked active before the listener starts cannot have a live peer.
        """
        now = self._now_s()
        released = 0
        with self._transaction() as conn:
            rows = conn.execute(
                '''
                SELECT host_id, COUNT(*) AS session_count
                FROM relay_host_sessions
                WHERE state = 'active'
                GROUP BY host_id
                '''
            ).fetchall()
            if not rows:
                return 0
            released = sum(int(row['session_count'] or 0) for row in rows)
            conn.execute(
                '''
                UPDATE relay_host_sessions
                SET state = 'released', released_at = ?
                WHERE state = 'active'
                ''',
                (now,),
            )
            conn.execute(
                '''
                UPDATE relay_hosts
                SET active_sessions = 0, updated_at = ?
                WHERE active_sessions != 0
                ''',
                (now,),
            )
            for row in rows:
                self._audit(
                    conn,
                    'host_sessions_reconciled',
                    'host',
                    str(row['host_id']),
                    {
                        'count': int(row['session_count'] or 0),
                        'reason': _redacted_text(reason),
                    },
                )
        return released

    def record_host_bytes(self, *, host_id: str, byte_count: int) -> dict[str, object]:
        base_host_id = _required_text(host_id, 'host_id')
        if int(byte_count) < 0:
            raise RelayAdmissionError('relay host byte count must be non-negative')
        now = self._now_s()
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM relay_hosts WHERE host_id = ?', (base_host_id,)).fetchone()
            host = self._require_active_host_row(row)
            quota = _json_object(str(host['quota_json'] or '{}'))
            max_bytes = _quota_limit(quota, 'max_bytes_per_day')
            window_started_at = int(host['bytes_window_started_at'] or 0)
            bytes_used = int(host['bytes_used'] or 0)
            if window_started_at <= 0 or now - window_started_at >= QUOTA_WINDOW_SECONDS:
                window_started_at = now
                bytes_used = 0
            new_total = bytes_used + int(byte_count)
            if new_total > max_bytes:
                self._audit(conn, 'host_byte_quota_rejected', 'host', base_host_id, {'limit': max_bytes})
                raise RelayAdmissionError('relay host byte quota exceeded')
            conn.execute(
                '''
                UPDATE relay_hosts
                SET bytes_used = ?, bytes_window_started_at = ?, updated_at = ?
                WHERE host_id = ? AND state = 'active'
                ''',
                (new_total, window_started_at, now, base_host_id),
            )
            row = conn.execute('SELECT * FROM relay_hosts WHERE host_id = ?', (base_host_id,)).fetchone()
        payload = self._host_row(row)
        payload['relay_status'] = 'host_quota_recorded'
        return payload

    def issue_session_capability(
        self,
        *,
        host_id: str,
        nonce_b64: str,
        proof_expires_at: int,
        signature_b64: str,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        scopes: Iterable[str] = ('relay.connect',),
    ) -> dict[str, object]:
        if ttl_seconds <= 0:
            raise RelayAdmissionError('relay session ttl must be positive')
        now = self._now_s()
        if proof_expires_at < now:
            raise RelayAdmissionError('relay host proof expired')
        host = self._host_record(host_id)
        if str(host['state']) != 'active':
            raise RelayAdmissionError('relay host is not active')
        proof_message = host_proof_payload(host_id=host_id, nonce_b64=nonce_b64, expires_at=proof_expires_at)
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(_b64decode(str(host['host_public_key_b64'])))
        try:
            public_key.verify(_b64decode(signature_b64), proof_message)
        except Exception as exc:  # pragma: no cover - backend exception class can vary
            raise RelayAdmissionError('relay host proof rejected') from exc
        capability_payload = {
            'typ': CAPABILITY_PREFIX,
            'host_id': host_id,
            'host_public_key_sha256': str(host['host_public_key_sha256']),
            'iat': now,
            'exp': now + int(ttl_seconds),
            'nonce_b64': nonce_b64,
            'scopes': sorted({str(scope).strip() for scope in scopes if str(scope).strip()}),
            'quota': _json_object(str(host['quota_json'] or '{}')),
        }
        payload_b64 = _b64(_canonical_json(capability_payload).encode('utf-8'))
        signature = _hmac_b64(self._capability_key(), payload_b64.encode('ascii'))
        token = f'{CAPABILITY_PREFIX}.{payload_b64}.{signature}'
        with self._transaction() as conn:
            self._audit(conn, 'host_session_capability_issued', 'host', host_id, {'scopes': capability_payload['scopes']})
        return {
            'relay_status': 'host_session_capability_issued',
            'host_id': host_id,
            'expires_at': capability_payload['exp'],
            'capability': token,
            'scopes': capability_payload['scopes'],
        }

    def verify_session_capability(self, token: str) -> dict[str, object]:
        try:
            prefix, payload_b64, signature_b64 = str(token).split('.', 2)
        except ValueError as exc:
            raise RelayAdmissionError('relay capability malformed') from exc
        if prefix != CAPABILITY_PREFIX:
            raise RelayAdmissionError('relay capability malformed')
        expected = _hmac_b64(self._capability_key(), payload_b64.encode('ascii'))
        if not hmac.compare_digest(expected, signature_b64):
            raise RelayAdmissionError('relay capability signature rejected')
        payload = _json_object(_b64decode(payload_b64).decode('utf-8'))
        now = self._now_s()
        if int(payload.get('exp') or 0) < now:
            raise RelayAdmissionError('relay capability expired')
        host_id = str(payload.get('host_id') or '')
        host = self._host_record(host_id)
        if str(host['state']) != 'active':
            raise RelayAdmissionError('relay host is not active')
        if str(host['host_public_key_sha256']) != str(payload.get('host_public_key_sha256') or ''):
            raise RelayAdmissionError('relay capability binding rejected')
        return payload

    def expire_due_invitations(self) -> int:
        now = self._now_s()
        with self._transaction() as conn:
            rows = conn.execute(
                'SELECT invite_id FROM relay_invitations WHERE state = ? AND expires_at <= ?',
                ('unused', now),
            ).fetchall()
            if rows:
                conn.executemany(
                    'UPDATE relay_invitations SET state = ?, updated_at = ? WHERE invite_id = ? AND state = ?',
                    [('expired', now, str(row['invite_id']), 'unused') for row in rows],
                )
                for row in rows:
                    self._audit(conn, 'invite_expired', 'invite', str(row['invite_id']), {})
        return len(rows)

    def audit_records(self) -> list[dict[str, object]]:
        with closing(self._connect()) as conn:
            rows = conn.execute('SELECT * FROM relay_audit ORDER BY id').fetchall()
        return [
            {
                'event': str(row['event']),
                'subject_type': str(row['subject_type']),
                'subject_id': str(row['subject_id']),
                'at': int(row['at']),
                'detail': _json_object(str(row['detail_json'] or '{}')),
            }
            for row in rows
        ]

    def _migrate(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS relay_metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS relay_invitations(
                    invite_id TEXT PRIMARY KEY,
                    verifier_b64 TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('unused', 'consumed', 'expired', 'revoked')),
                    issued_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER,
                    revoked_at INTEGER,
                    host_id TEXT,
                    label TEXT,
                    quota_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS relay_hosts(
                    host_id TEXT PRIMARY KEY,
                    invitation_id TEXT NOT NULL,
                    host_public_key_b64 TEXT NOT NULL,
                    host_public_key_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active', 'revoked')),
                    issued_at INTEGER NOT NULL,
                    revoked_at INTEGER,
                    quota_json TEXT NOT NULL,
                    active_sessions INTEGER NOT NULL DEFAULT 0,
                    bytes_used INTEGER NOT NULL DEFAULT 0,
                    bytes_window_started_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(invitation_id) REFERENCES relay_invitations(invite_id)
                )
                '''
            )
            self._ensure_column(conn, 'relay_hosts', 'active_sessions', 'INTEGER NOT NULL DEFAULT 0')
            self._ensure_column(conn, 'relay_hosts', 'bytes_used', 'INTEGER NOT NULL DEFAULT 0')
            self._ensure_column(conn, 'relay_hosts', 'bytes_window_started_at', 'INTEGER NOT NULL DEFAULT 0')
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS relay_host_sessions(
                    session_id TEXT PRIMARY KEY,
                    host_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active', 'released')),
                    reserved_at INTEGER NOT NULL,
                    released_at INTEGER,
                    FOREIGN KEY(host_id) REFERENCES relay_hosts(host_id)
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS relay_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    at INTEGER NOT NULL,
                    detail_json TEXT NOT NULL
                )
                '''
            )
            self._ensure_metadata(conn, 'schema_version', str(ADMISSION_SCHEMA_VERSION))
            self._verify_external_secret_fingerprint(conn)
            self._harden_storage_permissions()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('PRAGMA foreign_keys=ON')
        self._harden_storage_permissions()
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            self._harden_storage_permissions()

    def _verifier_key(self) -> bytes:
        return self._secrets.verifier_key

    def _capability_key(self) -> bytes:
        return self._secrets.capability_key

    def _host_record(self, host_id: str) -> sqlite3.Row:
        with closing(self._connect()) as conn:
            row = conn.execute('SELECT * FROM relay_hosts WHERE host_id = ?', (host_id,)).fetchone()
        if row is None:
            raise RelayAdmissionError('relay host not found')
        return row

    def _invitation_row(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            'relay_status': 'invite_status',
            'invite_id': str(row['invite_id']),
            'state': str(row['state']),
            'issued_at': int(row['issued_at']),
            'expires_at': int(row['expires_at']),
            'consumed_at': _optional_int(row['consumed_at']),
            'revoked_at': _optional_int(row['revoked_at']),
            'host_id': str(row['host_id'] or ''),
            'label': str(row['label'] or ''),
            'quota': _json_object(str(row['quota_json'] or '{}')),
        }

    def _host_row(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            'relay_status': 'host_status',
            'host_id': str(row['host_id']),
            'invitation_id': str(row['invitation_id']),
            'host_public_key_sha256': str(row['host_public_key_sha256']),
            'state': str(row['state']),
            'issued_at': int(row['issued_at']),
            'revoked_at': _optional_int(row['revoked_at']),
            'quota': _json_object(str(row['quota_json'] or '{}')),
            'quota_usage': _quota_usage(row),
        }

    def _audit(
        self,
        conn: sqlite3.Connection,
        event: str,
        subject_type: str,
        subject_id: str,
        detail: Mapping[str, object],
    ) -> None:
        conn.execute(
            '''
            INSERT INTO relay_audit(event, subject_type, subject_id, at, detail_json)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (event, subject_type, subject_id, self._now_s(), _canonical_json(dict(detail))),
        )

    @staticmethod
    def _ensure_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            'INSERT OR IGNORE INTO relay_metadata(key, value) VALUES (?, ?)',
            (key, value),
        )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {str(row['name']) for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
        if column not in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')

    def _verify_external_secret_fingerprint(self, conn: sqlite3.Connection) -> None:
        embedded = conn.execute(
            '''
            SELECT key FROM relay_metadata
            WHERE key IN ('verifier_key_b64', 'capability_key_b64')
            '''
        ).fetchall()
        if embedded:
            raise RelayAdmissionError('relay admission DB contains persisted secrets')
        row = conn.execute('SELECT value FROM relay_metadata WHERE key = ?', ('secrets_fingerprint',)).fetchone()
        if row is None:
            if self._has_admission_data(conn):
                raise RelayAdmissionError('relay admission secrets fingerprint missing')
            conn.execute(
                'INSERT INTO relay_metadata(key, value) VALUES (?, ?)',
                ('secrets_fingerprint', self._secrets_fingerprint),
            )
            return
        if not hmac.compare_digest(str(row['value']), self._secrets_fingerprint):
            raise RelayAdmissionError('relay admission secrets changed')

    @staticmethod
    def _has_admission_data(conn: sqlite3.Connection) -> bool:
        for table in ('relay_invitations', 'relay_hosts'):
            row = conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
            if row is not None:
                return True
        return False

    def _require_active_host_row(self, row: sqlite3.Row | None) -> sqlite3.Row:
        if row is None:
            raise RelayAdmissionError('relay host not found')
        if str(row['state']) != 'active':
            raise RelayAdmissionError('relay host is not active')
        return row

    def _prepare_storage_file(self) -> None:
        if not self.path.exists():
            self.path.touch(mode=0o600, exist_ok=False)
        self._harden_storage_permissions()

    def _harden_storage_permissions(self) -> None:
        for path in _sqlite_storage_paths(self.path):
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                # SQLite may remove WAL/SHM files between discovery and chmod.
                continue

    def _now_s(self) -> int:
        return int(self._now())


def generate_host_private_key() -> ed25519.Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.generate()


def host_public_key_b64(private_key: ed25519.Ed25519PrivateKey) -> str:
    return _b64(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def host_proof_payload(*, host_id: str, nonce_b64: str, expires_at: int) -> bytes:
    _required_text(host_id, 'host_id')
    _b64decode(nonce_b64)
    return (
        'ccb-relay-host-proof-v1\n'
        f'host_id:{host_id}\n'
        f'nonce_b64:{nonce_b64}\n'
        f'expires_at:{int(expires_at)}\n'
    ).encode('utf-8')


def sign_host_session_proof(
    private_key: ed25519.Ed25519PrivateKey,
    *,
    host_id: str,
    nonce_b64: str,
    expires_at: int,
) -> str:
    return _b64(private_key.sign(host_proof_payload(host_id=host_id, nonce_b64=nonce_b64, expires_at=expires_at)))


def _decode_ed25519_public_key(value: str) -> bytes:
    decoded = _b64decode(value)
    if len(decoded) != 32:
        raise RelayAdmissionError('relay host public key must be Ed25519 raw public key')
    ed25519.Ed25519PublicKey.from_public_bytes(decoded)
    return decoded


def _invite_id_from_secret(invitation: str) -> str:
    parts = str(invitation or '').strip().split('.')
    if len(parts) != 3 or parts[0] != INVITATION_PREFIX or not parts[1] or not parts[2]:
        raise RelayAdmissionError('relay invitation is not claimable')
    return parts[1]


def _verifier(key: bytes, invitation: str) -> str:
    return _hmac_b64(key, str(invitation).encode('utf-8'))


def _hmac_b64(key: bytes, value: bytes) -> str:
    return _b64(hmac.digest(key, value, hashlib.sha256))


def _quota(*, max_sessions: int, max_bytes_per_day: int) -> dict[str, object]:
    if max_sessions <= 0:
        raise RelayAdmissionError('relay max_sessions must be positive')
    if max_bytes_per_day <= 0:
        raise RelayAdmissionError('relay max_bytes_per_day must be positive')
    return {'max_sessions': int(max_sessions), 'max_bytes_per_day': int(max_bytes_per_day)}


def _quota_limit(quota: Mapping[str, object], name: str) -> int:
    try:
        value = int(quota.get(name) or 0)
    except (TypeError, ValueError) as exc:
        raise RelayAdmissionError(f'relay quota limit invalid: {name}') from exc
    if value <= 0:
        raise RelayAdmissionError(f'relay quota limit invalid: {name}')
    return value


def _quota_usage(row: sqlite3.Row) -> dict[str, object]:
    return {
        'active_sessions': int(row['active_sessions'] or 0),
        'bytes_used': int(row['bytes_used'] or 0),
        'bytes_window_started_at': int(row['bytes_window_started_at'] or 0),
        'window_seconds': QUOTA_WINDOW_SECONDS,
    }


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def _json_object(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RelayAdmissionError('relay admission JSON metadata invalid') from exc
    if not isinstance(payload, dict):
        raise RelayAdmissionError('relay admission JSON metadata invalid')
    return {str(key): item for key, item in payload.items()}


def _required_text(value: object, name: str) -> str:
    text = str(value or '').strip()
    if not text:
        raise RelayAdmissionError(f'relay field is required: {name}')
    return text


def _redacted_text(value: object) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    return text[:80]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(value)).decode('ascii').rstrip('=')


def _b64decode(value: str) -> bytes:
    try:
        text = str(value).strip()
        return base64.urlsafe_b64decode((text + '=' * (-len(text) % 4)).encode('ascii'))
    except Exception as exc:  # pragma: no cover - exact exception varies
        raise RelayAdmissionError('relay field must be base64url') from exc


def _sha256_b64(value: bytes) -> str:
    return 'sha256:' + _b64(hashlib.sha256(value).digest())


def _optional_int(value: object) -> int | None:
    if value is None or value == '':
        return None
    return int(value)


def _sqlite_storage_paths(path: Path) -> tuple[Path, Path, Path]:
    return (path, path.with_name(path.name + '-wal'), path.with_name(path.name + '-shm'))


def _require_owner_only_file(path: Path, label: str) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise RelayAdmissionError(f'{label} is unavailable') from exc
    if mode & 0o077:
        raise RelayAdmissionError(f'{label} must be owner-only')


__all__ = [
    'ADMISSION_SCHEMA_VERSION',
    'CAPABILITY_PREFIX',
    'DEFAULT_INVITATION_TTL_SECONDS',
    'DEFAULT_SESSION_TTL_SECONDS',
    'INVITATION_PREFIX',
    'RelayAdmissionError',
    'RelayAdmissionSecrets',
    'RelayAdmissionStore',
    'RelayHostCredential',
    'RelayIssuedInvitation',
    'generate_host_private_key',
    'host_proof_payload',
    'host_public_key_b64',
    'sign_host_session_proof',
]
