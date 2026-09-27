from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Mapping

from cryptography.hazmat.primitives.asymmetric import ed25519

from .relay_crypto import RELAY_PROHIBITED_PLAINTEXT_FIELDS, RELAY_PROTOCOL_VERSION


_SCHEMA_VERSION = RELAY_PROTOCOL_VERSION
RENDEZVOUS_CAPABILITY_PREFIX = 'ccb-relay-rv-v1'
ACCESS_GRANT_PREFIX = 'ccb-relay-access-v1'
PHONE_SESSION_PROOF_PREFIX = 'ccb-relay-phone-proof-v1'

_PROHIBITED_CLEARTEXT_KEYS = set(RELAY_PROHIBITED_PLAINTEXT_FIELDS)
_JSON_SEPARATORS = (',', ':')


class MobileRelayError(RuntimeError):
    pass


@dataclass(frozen=True)
class RelayRendezvousCapability:
    host_id: str
    session_id: str
    client_pubkey_b64: str
    phone_nonce_b64: str
    audience: str
    nonce_b64: str
    issued_at: int
    expires_at: int
    signature_b64: str
    schema_version: int = _SCHEMA_VERSION

    @classmethod
    def from_token(cls, token: str) -> 'RelayRendezvousCapability':
        try:
            prefix, payload_b64, signature_b64 = str(token or '').split('.', 2)
        except ValueError as exc:
            raise MobileRelayError('relay rendezvous capability rejected') from exc
        if prefix != RENDEZVOUS_CAPABILITY_PREFIX:
            raise MobileRelayError('relay rendezvous capability rejected')
        try:
            payload = json.loads(_b64decode(payload_b64).decode('utf-8'))
        except Exception as exc:
            raise MobileRelayError('relay rendezvous capability rejected') from exc
        if not isinstance(payload, Mapping):
            raise MobileRelayError('relay rendezvous capability rejected')
        if _required_text(payload.get('typ'), 'rendezvous.typ') != RENDEZVOUS_CAPABILITY_PREFIX:
            raise MobileRelayError('relay rendezvous capability rejected')
        return cls(
            schema_version=_int(payload.get('schema_version'), fallback=0),
            host_id=_required_text(payload.get('host_id'), 'rendezvous.host_id'),
            session_id=_required_text(payload.get('session_id'), 'rendezvous.session_id'),
            client_pubkey_b64=_required_base64_text(payload.get('client_pubkey_b64'), 'rendezvous.client_pubkey_b64'),
            phone_nonce_b64=_required_base64_text(payload.get('phone_nonce_b64'), 'rendezvous.phone_nonce_b64'),
            audience=_required_text(payload.get('aud'), 'rendezvous.aud'),
            nonce_b64=_required_base64_text(payload.get('nonce_b64'), 'rendezvous.nonce_b64'),
            issued_at=_positive_int(payload.get('iat'), 'rendezvous.iat'),
            expires_at=_positive_int(payload.get('exp'), 'rendezvous.exp'),
            signature_b64=_required_base64_text(signature_b64, 'rendezvous.signature_b64'),
        )._validate()

    def verify(
        self,
        *,
        host_public_key_b64: str,
        host_id: str,
        session_id: str,
        client_pubkey_b64: str,
        phone_nonce_b64: str,
        audience: str,
        now: int | None = None,
    ) -> 'RelayRendezvousCapability':
        self._validate()
        current = int(time.time()) if now is None else int(now)
        if self.expires_at <= current or self.issued_at > current + 30:
            raise MobileRelayError('relay rendezvous capability rejected')
        if self.host_id != host_id or self.session_id != session_id:
            raise MobileRelayError('relay rendezvous capability rejected')
        if self.client_pubkey_b64 != client_pubkey_b64 or self.phone_nonce_b64 != phone_nonce_b64:
            raise MobileRelayError('relay rendezvous capability rejected')
        if self.audience != audience:
            raise MobileRelayError('relay rendezvous capability rejected')
        try:
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(_b64decode(host_public_key_b64))
            public_key.verify(_b64decode(self.signature_b64), _rendezvous_signing_payload(self._payload()))
        except Exception as exc:  # pragma: no cover - backend exception class can vary
            raise MobileRelayError('relay rendezvous capability rejected') from exc
        return self

    def replay_key(self) -> str:
        payload = {
            'aud': self.audience,
            'client_pubkey_b64': self.client_pubkey_b64,
            'host_id': self.host_id,
            'nonce_b64': self.nonce_b64,
            'phone_nonce_b64': self.phone_nonce_b64,
            'session_id': self.session_id,
        }
        return hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()

    def _payload(self) -> dict[str, object]:
        return {
            'typ': RENDEZVOUS_CAPABILITY_PREFIX,
            'schema_version': self.schema_version,
            'host_id': self.host_id,
            'session_id': self.session_id,
            'client_pubkey_b64': self.client_pubkey_b64,
            'phone_nonce_b64': self.phone_nonce_b64,
            'aud': self.audience,
            'nonce_b64': self.nonce_b64,
            'iat': self.issued_at,
            'exp': self.expires_at,
        }

    def _validate(self) -> 'RelayRendezvousCapability':
        if self.schema_version != _SCHEMA_VERSION:
            raise MobileRelayError('relay rendezvous capability rejected')
        if self.expires_at <= self.issued_at:
            raise MobileRelayError('relay rendezvous capability rejected')
        _required_text(self.host_id, 'rendezvous.host_id')
        _required_text(self.session_id, 'rendezvous.session_id')
        _required_text(self.audience, 'rendezvous.aud')
        _required_base64_text(self.client_pubkey_b64, 'rendezvous.client_pubkey_b64')
        _required_base64_text(self.phone_nonce_b64, 'rendezvous.phone_nonce_b64')
        _required_base64_text(self.nonce_b64, 'rendezvous.nonce_b64')
        _required_base64_text(self.signature_b64, 'rendezvous.signature_b64')
        return self


@dataclass(frozen=True)
class RelayAccessGrant:
    host_id: str
    device_id: str
    phone_auth_pubkey_b64: str
    audience: str
    scopes: tuple[str, ...]
    nonce_b64: str
    issued_at: int
    expires_at: int
    signature_b64: str
    token: str
    schema_version: int = _SCHEMA_VERSION

    @classmethod
    def from_token(cls, token: str) -> 'RelayAccessGrant':
        payload, signature_b64 = _signed_token_parts(
            token,
            prefix=ACCESS_GRANT_PREFIX,
            rejected='relay access grant rejected',
        )
        if _required_text(payload.get('typ'), 'access.typ') != ACCESS_GRANT_PREFIX:
            raise MobileRelayError('relay access grant rejected')
        return cls(
            schema_version=_int(payload.get('schema_version'), fallback=0),
            host_id=_required_text(payload.get('host_id'), 'access.host_id'),
            device_id=_required_text(payload.get('device_id'), 'access.device_id'),
            phone_auth_pubkey_b64=_required_base64_text(
                payload.get('phone_auth_pubkey_b64'),
                'access.phone_auth_pubkey_b64',
            ),
            audience=_required_text(payload.get('aud'), 'access.aud'),
            scopes=tuple(sorted(_string_set(payload.get('scopes')))),
            nonce_b64=_required_base64_text(payload.get('nonce_b64'), 'access.nonce_b64'),
            issued_at=_positive_int(payload.get('iat'), 'access.iat'),
            expires_at=_positive_int(payload.get('exp'), 'access.exp'),
            signature_b64=_required_base64_text(signature_b64, 'access.signature_b64'),
            token=str(token),
        )._validate()

    def verify(
        self,
        *,
        host_public_key_b64: str,
        host_id: str,
        device_id: str,
        audience: str,
        now: int | None = None,
    ) -> 'RelayAccessGrant':
        self._validate()
        current = int(time.time()) if now is None else int(now)
        if self.expires_at <= current or self.issued_at > current + 30:
            raise MobileRelayError('relay access grant rejected')
        if self.host_id != host_id or self.device_id != device_id or self.audience != audience:
            raise MobileRelayError('relay access grant rejected')
        try:
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(_b64decode(host_public_key_b64))
            public_key.verify(_b64decode(self.signature_b64), _access_grant_signing_payload(self._payload()))
        except Exception as exc:  # pragma: no cover - backend exception class can vary
            raise MobileRelayError('relay access grant rejected') from exc
        return self

    def digest_b64(self) -> str:
        return _b64(hashlib.sha256(self.token.encode('ascii')).digest())

    def _payload(self) -> dict[str, object]:
        return {
            'typ': ACCESS_GRANT_PREFIX,
            'schema_version': self.schema_version,
            'host_id': self.host_id,
            'device_id': self.device_id,
            'phone_auth_pubkey_b64': self.phone_auth_pubkey_b64,
            'aud': self.audience,
            'scopes': list(self.scopes),
            'nonce_b64': self.nonce_b64,
            'iat': self.issued_at,
            'exp': self.expires_at,
        }

    def _validate(self) -> 'RelayAccessGrant':
        if self.schema_version != _SCHEMA_VERSION or self.expires_at <= self.issued_at:
            raise MobileRelayError('relay access grant rejected')
        _required_text(self.host_id, 'access.host_id')
        _required_text(self.device_id, 'access.device_id')
        _required_text(self.audience, 'access.aud')
        _required_base64_text(self.phone_auth_pubkey_b64, 'access.phone_auth_pubkey_b64')
        _required_base64_text(self.nonce_b64, 'access.nonce_b64')
        _required_base64_text(self.signature_b64, 'access.signature_b64')
        return self


@dataclass(frozen=True)
class RelayPhoneSessionProof:
    host_id: str
    device_id: str
    session_id: str
    client_pubkey_b64: str
    phone_nonce_b64: str
    grant_sha256_b64: str
    audience: str
    nonce_b64: str
    issued_at: int
    expires_at: int
    signature_b64: str
    schema_version: int = _SCHEMA_VERSION

    @classmethod
    def from_token(cls, token: str) -> 'RelayPhoneSessionProof':
        payload, signature_b64 = _signed_token_parts(
            token,
            prefix=PHONE_SESSION_PROOF_PREFIX,
            rejected='relay phone session proof rejected',
        )
        if _required_text(payload.get('typ'), 'phone_proof.typ') != PHONE_SESSION_PROOF_PREFIX:
            raise MobileRelayError('relay phone session proof rejected')
        return cls(
            schema_version=_int(payload.get('schema_version'), fallback=0),
            host_id=_required_text(payload.get('host_id'), 'phone_proof.host_id'),
            device_id=_required_text(payload.get('device_id'), 'phone_proof.device_id'),
            session_id=_required_text(payload.get('session_id'), 'phone_proof.session_id'),
            client_pubkey_b64=_required_base64_text(
                payload.get('client_pubkey_b64'),
                'phone_proof.client_pubkey_b64',
            ),
            phone_nonce_b64=_required_base64_text(
                payload.get('phone_nonce_b64'),
                'phone_proof.phone_nonce_b64',
            ),
            grant_sha256_b64=_required_base64_text(
                payload.get('grant_sha256_b64'),
                'phone_proof.grant_sha256_b64',
            ),
            audience=_required_text(payload.get('aud'), 'phone_proof.aud'),
            nonce_b64=_required_base64_text(payload.get('nonce_b64'), 'phone_proof.nonce_b64'),
            issued_at=_positive_int(payload.get('iat'), 'phone_proof.iat'),
            expires_at=_positive_int(payload.get('exp'), 'phone_proof.exp'),
            signature_b64=_required_base64_text(signature_b64, 'phone_proof.signature_b64'),
        )._validate()

    def verify(
        self,
        *,
        grant: RelayAccessGrant,
        host_id: str,
        device_id: str,
        session_id: str,
        client_pubkey_b64: str,
        phone_nonce_b64: str,
        audience: str,
        now: int | None = None,
    ) -> 'RelayPhoneSessionProof':
        self._validate()
        current = int(time.time()) if now is None else int(now)
        if (
            self.expires_at <= current
            or self.issued_at > current + 30
            or self.expires_at - self.issued_at > 120
        ):
            raise MobileRelayError('relay phone session proof rejected')
        expected = (
            host_id,
            device_id,
            session_id,
            client_pubkey_b64,
            phone_nonce_b64,
            audience,
            grant.digest_b64(),
        )
        observed = (
            self.host_id,
            self.device_id,
            self.session_id,
            self.client_pubkey_b64,
            self.phone_nonce_b64,
            self.audience,
            self.grant_sha256_b64,
        )
        if observed != expected:
            raise MobileRelayError('relay phone session proof rejected')
        try:
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(
                _b64decode(grant.phone_auth_pubkey_b64)
            )
            public_key.verify(
                _b64decode(self.signature_b64),
                _phone_session_proof_signing_payload(self._payload()),
            )
        except Exception as exc:  # pragma: no cover - backend exception class can vary
            raise MobileRelayError('relay phone session proof rejected') from exc
        return self

    def replay_key(self) -> str:
        return hashlib.sha256(_canonical_json(self._payload()).encode('utf-8')).hexdigest()

    def _payload(self) -> dict[str, object]:
        return {
            'typ': PHONE_SESSION_PROOF_PREFIX,
            'schema_version': self.schema_version,
            'host_id': self.host_id,
            'device_id': self.device_id,
            'session_id': self.session_id,
            'client_pubkey_b64': self.client_pubkey_b64,
            'phone_nonce_b64': self.phone_nonce_b64,
            'grant_sha256_b64': self.grant_sha256_b64,
            'aud': self.audience,
            'nonce_b64': self.nonce_b64,
            'iat': self.issued_at,
            'exp': self.expires_at,
        }

    def _validate(self) -> 'RelayPhoneSessionProof':
        if self.schema_version != _SCHEMA_VERSION or self.expires_at <= self.issued_at:
            raise MobileRelayError('relay phone session proof rejected')
        _required_text(self.host_id, 'phone_proof.host_id')
        _required_text(self.device_id, 'phone_proof.device_id')
        _required_text(self.session_id, 'phone_proof.session_id')
        _required_text(self.audience, 'phone_proof.aud')
        _required_base64_text(self.client_pubkey_b64, 'phone_proof.client_pubkey_b64')
        _required_base64_text(self.phone_nonce_b64, 'phone_proof.phone_nonce_b64')
        _required_base64_text(self.grant_sha256_b64, 'phone_proof.grant_sha256_b64')
        _required_base64_text(self.nonce_b64, 'phone_proof.nonce_b64')
        _required_base64_text(self.signature_b64, 'phone_proof.signature_b64')
        return self

def issue_host_rendezvous_capability(
    private_key: ed25519.Ed25519PrivateKey,
    *,
    host_id: str,
    session_id: str,
    client_pubkey_b64: str,
    phone_nonce_b64: str,
    audience: str,
    expires_at: int,
    issued_at: int | None = None,
    nonce_b64: str | None = None,
) -> str:
    payload = {
        'typ': RENDEZVOUS_CAPABILITY_PREFIX,
        'schema_version': _SCHEMA_VERSION,
        'host_id': _required_text(host_id, 'rendezvous.host_id'),
        'session_id': _required_text(session_id, 'rendezvous.session_id'),
        'client_pubkey_b64': _required_base64_text(client_pubkey_b64, 'rendezvous.client_pubkey_b64'),
        'phone_nonce_b64': _required_base64_text(phone_nonce_b64, 'rendezvous.phone_nonce_b64'),
        'aud': _required_text(audience, 'rendezvous.aud'),
        'nonce_b64': _required_base64_text(nonce_b64 or _b64(secrets.token_bytes(18)), 'rendezvous.nonce_b64'),
        'iat': int(time.time()) if issued_at is None else int(issued_at),
        'exp': _positive_int(expires_at, 'rendezvous.exp'),
    }
    if int(payload['exp']) <= int(payload['iat']):
        raise MobileRelayError('relay rendezvous capability rejected')
    signature = private_key.sign(_rendezvous_signing_payload(payload))
    payload_b64 = _b64(_canonical_json(payload).encode('utf-8'))
    return f'{RENDEZVOUS_CAPABILITY_PREFIX}.{payload_b64}.{_b64(signature)}'


def issue_host_access_grant(
    private_key: ed25519.Ed25519PrivateKey,
    *,
    host_id: str,
    device_id: str,
    phone_auth_pubkey_b64: str,
    audience: str,
    scopes: tuple[str, ...] | list[str] | set[str],
    expires_at: int,
    issued_at: int | None = None,
    nonce_b64: str | None = None,
) -> str:
    payload = {
        'typ': ACCESS_GRANT_PREFIX,
        'schema_version': _SCHEMA_VERSION,
        'host_id': _required_text(host_id, 'access.host_id'),
        'device_id': _required_text(device_id, 'access.device_id'),
        'phone_auth_pubkey_b64': _required_base64_text(
            phone_auth_pubkey_b64,
            'access.phone_auth_pubkey_b64',
        ),
        'aud': _required_text(audience, 'access.aud'),
        'scopes': sorted(_string_set(scopes)),
        'nonce_b64': _required_base64_text(nonce_b64 or _b64(secrets.token_bytes(18)), 'access.nonce_b64'),
        'iat': int(time.time()) if issued_at is None else int(issued_at),
        'exp': _positive_int(expires_at, 'access.exp'),
    }
    if int(payload['exp']) <= int(payload['iat']):
        raise MobileRelayError('relay access grant rejected')
    signature = private_key.sign(_access_grant_signing_payload(payload))
    return f'{ACCESS_GRANT_PREFIX}.{_b64(_canonical_json(payload).encode("utf-8"))}.{_b64(signature)}'


def issue_phone_session_proof(
    private_key: ed25519.Ed25519PrivateKey,
    *,
    access_grant: str,
    host_id: str,
    device_id: str,
    session_id: str,
    client_pubkey_b64: str,
    phone_nonce_b64: str,
    audience: str,
    expires_at: int,
    issued_at: int | None = None,
    nonce_b64: str | None = None,
) -> str:
    payload = {
        'typ': PHONE_SESSION_PROOF_PREFIX,
        'schema_version': _SCHEMA_VERSION,
        'host_id': _required_text(host_id, 'phone_proof.host_id'),
        'device_id': _required_text(device_id, 'phone_proof.device_id'),
        'session_id': _required_text(session_id, 'phone_proof.session_id'),
        'client_pubkey_b64': _required_base64_text(
            client_pubkey_b64,
            'phone_proof.client_pubkey_b64',
        ),
        'phone_nonce_b64': _required_base64_text(
            phone_nonce_b64,
            'phone_proof.phone_nonce_b64',
        ),
        'grant_sha256_b64': _b64(hashlib.sha256(str(access_grant).encode('ascii')).digest()),
        'aud': _required_text(audience, 'phone_proof.aud'),
        'nonce_b64': _required_base64_text(
            nonce_b64 or _b64(secrets.token_bytes(18)),
            'phone_proof.nonce_b64',
        ),
        'iat': int(time.time()) if issued_at is None else int(issued_at),
        'exp': _positive_int(expires_at, 'phone_proof.exp'),
    }
    if int(payload['exp']) <= int(payload['iat']) or int(payload['exp']) - int(payload['iat']) > 120:
        raise MobileRelayError('relay phone session proof rejected')
    signature = private_key.sign(_phone_session_proof_signing_payload(payload))
    return f'{PHONE_SESSION_PROOF_PREFIX}.{_b64(_canonical_json(payload).encode("utf-8"))}.{_b64(signature)}'


@dataclass(frozen=True)
class RelayHostRegistration:
    host_id: str
    server_fingerprint: str
    host_pubkey_b64: str
    capabilities: tuple[str, ...] = ()
    diagnostics: Mapping[str, str] | None = None
    schema_version: int = _SCHEMA_VERSION

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> 'RelayHostRegistration':
        _reject_cleartext_keys(payload, 'relay_host_registration')
        registration_type = _optional_text(payload.get('type'))
        if registration_type and registration_type != 'relay_host_registration':
            raise MobileRelayError(f'unknown relay host registration type: {registration_type}')
        host_pubkey = _required_base64_text(payload.get('host_pubkey_b64'), 'host_pubkey_b64')
        return cls(
            schema_version=_int(payload.get('schema_version'), fallback=_SCHEMA_VERSION),
            host_id=_required_text(payload.get('host_id'), 'host_id'),
            server_fingerprint=_required_text(payload.get('server_fingerprint'), 'server_fingerprint'),
            host_pubkey_b64=host_pubkey,
            capabilities=tuple(sorted(_string_set(payload.get('capabilities')))),
            diagnostics=_string_map(payload.get('diagnostics')),
        )._validate()

    def to_json(self) -> dict[str, object]:
        self._validate()
        payload: dict[str, object] = {
            'schema_version': self.schema_version,
            'type': 'relay_host_registration',
            'host_id': self.host_id,
            'server_fingerprint': self.server_fingerprint,
            'host_pubkey_b64': self.host_pubkey_b64,
            'capabilities': sorted(self.capabilities),
        }
        diagnostics = _string_map(self.diagnostics or {})
        if diagnostics:
            payload['diagnostics'] = diagnostics
        return payload

    def _validate(self) -> 'RelayHostRegistration':
        if self.schema_version != _SCHEMA_VERSION:
            raise MobileRelayError('relay host registration requires v2 schema_version')
        _required_text(self.host_id, 'host_id')
        _required_text(self.server_fingerprint, 'server_fingerprint')
        _required_base64_text(self.host_pubkey_b64, 'host_pubkey_b64')
        return self


@dataclass(frozen=True)
class RelayFrame:
    session_id: str
    seq: int
    kind: str
    payload: Mapping[str, object]
    schema_version: int = _SCHEMA_VERSION

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> 'RelayFrame':
        frame = cls(
            schema_version=_int(payload.get('schema_version'), fallback=_SCHEMA_VERSION),
            session_id=_required_text(payload.get('session_id'), 'session_id'),
            seq=_positive_int(payload.get('seq'), 'seq'),
            kind=_required_text(payload.get('kind'), 'kind'),
            payload=_object_map(payload.get('payload'), 'payload'),
        )
        frame.validate()
        return frame

    def to_json(self) -> dict[str, object]:
        self.validate()
        return {
            'schema_version': self.schema_version,
            'session_id': self.session_id,
            'seq': self.seq,
            'kind': self.kind,
            'payload': dict(self.payload),
        }

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise MobileRelayError('relay frame requires v2 schema_version')
        _required_text(self.session_id, 'session_id')
        _positive_int(self.seq, 'seq')
        if self.kind not in {
            'client_hello',
            'host_hello',
            'host_register',
            'gateway_envelope',
            'heartbeat',
            'ack',
            'error',
            'close',
        }:
            raise MobileRelayError(f'unknown relay frame kind: {self.kind}')
        _reject_cleartext_keys(self.payload, f'{self.kind}.payload')
        if self.kind == 'host_register':
            _required_text(self.payload.get('host_id'), 'host_register.host_id')
            _required_base64_text(self.payload.get('nonce_b64'), 'host_register.nonce_b64')
            _positive_int(self.payload.get('proof_expires_at'), 'host_register.proof_expires_at')
            _required_base64_text(self.payload.get('signature_b64'), 'host_register.signature_b64')
            versions = _positive_int_list(self.payload.get('supported_versions'), 'host_register.supported_versions')
            if _SCHEMA_VERSION not in versions:
                raise MobileRelayError('host_register.supported_versions must include relay v2')
            capabilities = _string_set(self.payload.get('capabilities'))
            if 'relay.forward' not in capabilities:
                raise MobileRelayError('host_register.capabilities must include relay.forward')
        elif self.kind == 'client_hello':
            _required_text(self.payload.get('host_id'), 'client_hello.host_id')
            _required_text(self.payload.get('device_id'), 'client_hello.device_id')
            _required_base64_text(self.payload.get('client_pubkey_b64'), 'client_hello.client_pubkey_b64')
            versions = _positive_int_list(self.payload.get('supported_versions'), 'client_hello.supported_versions')
            if _SCHEMA_VERSION not in versions:
                raise MobileRelayError('client_hello.supported_versions must include relay v2')
        elif self.kind == 'host_hello':
            _required_text(self.payload.get('host_id'), 'host_hello.host_id')
            _required_text(self.payload.get('server_fingerprint'), 'host_hello.server_fingerprint')
            _required_base64_text(self.payload.get('host_pubkey_b64'), 'host_hello.host_pubkey_b64')
            accepted_version = _positive_int(self.payload.get('accepted_version'), 'host_hello.accepted_version')
            if accepted_version != _SCHEMA_VERSION:
                raise MobileRelayError('relay host_hello downgrade rejected')
        elif self.kind == 'gateway_envelope':
            envelope = _object_map(self.payload.get('envelope'), 'envelope')
            if _required_text(envelope.get('session_id'), 'envelope.session_id') != self.session_id:
                raise MobileRelayError('relay gateway envelope session mismatch')
            _positive_int(envelope.get('seq'), 'envelope.seq')
            _required_text(envelope.get('op'), 'envelope.op')
            _required_base64_text(envelope.get('ciphertext_b64'), 'envelope.ciphertext_b64')
            _required_base64_text(envelope.get('nonce_b64'), 'envelope.nonce_b64')
        elif self.kind == 'ack' and 'ack_seq' in self.payload:
            _positive_int(self.payload.get('ack_seq'), 'ack.ack_seq')
        elif self.kind == 'error':
            _required_text(self.payload.get('code'), 'error.code')
            _required_text(self.payload.get('message'), 'error.message')
        elif self.kind == 'close' and 'reason' in self.payload:
            _required_text(self.payload.get('reason'), 'close.reason')


@dataclass(frozen=True)
class RelayHandshakeTranscript:
    session_id: str
    host_id: str
    device_id: str
    accepted_version: int
    client_pubkey_b64: str
    host_pubkey_b64: str
    server_fingerprint: str

    @classmethod
    def negotiate(cls, *, client_hello: RelayFrame, host_hello: RelayFrame) -> 'RelayHandshakeTranscript':
        client_hello.validate()
        host_hello.validate()
        if client_hello.kind != 'client_hello':
            raise MobileRelayError('relay handshake must start with client_hello')
        if host_hello.kind != 'host_hello':
            raise MobileRelayError('relay handshake requires host_hello')
        if client_hello.session_id != host_hello.session_id:
            raise MobileRelayError('relay handshake session mismatch')
        client_host_id = _required_text(client_hello.payload.get('host_id'), 'client_hello.host_id')
        host_id = _required_text(host_hello.payload.get('host_id'), 'host_hello.host_id')
        if client_host_id != host_id:
            raise MobileRelayError('relay handshake host mismatch')
        supported_versions = _positive_int_list(
            client_hello.payload.get('supported_versions'),
            'client_hello.supported_versions',
        )
        accepted_version = _positive_int(host_hello.payload.get('accepted_version'), 'host_hello.accepted_version')
        if _SCHEMA_VERSION not in supported_versions or accepted_version != _SCHEMA_VERSION:
            raise MobileRelayError('relay handshake downgrade rejected')
        return cls(
            session_id=client_hello.session_id,
            host_id=host_id,
            device_id=_required_text(client_hello.payload.get('device_id'), 'client_hello.device_id'),
            accepted_version=accepted_version,
            client_pubkey_b64=_required_base64_text(
                client_hello.payload.get('client_pubkey_b64'),
                'client_hello.client_pubkey_b64',
            ),
            host_pubkey_b64=_required_base64_text(host_hello.payload.get('host_pubkey_b64'), 'host_hello.host_pubkey_b64'),
            server_fingerprint=_required_text(host_hello.payload.get('server_fingerprint'), 'host_hello.server_fingerprint'),
        )

    def to_json(self) -> dict[str, object]:
        return {
            'session_id': self.session_id,
            'host_id': self.host_id,
            'device_id': self.device_id,
            'accepted_version': self.accepted_version,
            'server_fingerprint': self.server_fingerprint,
        }


class LocalRelayServerHarness:
    """In-memory relay harness for source tests; never opens a public listener."""

    def __init__(self) -> None:
        self._hosts: dict[str, RelayHostRegistration] = {}
        self._sessions: dict[str, RelayHandshakeTranscript] = {}
        self._forwarded: list[dict[str, object]] = []
        self._disconnected_hosts: set[str] = set()
        self._stale_devices: set[tuple[str, str]] = set()
        self._relay_unreachable = False

    def register_host(self, registration_payload: Mapping[str, object]) -> dict[str, object]:
        registration = RelayHostRegistration.from_json(registration_payload)
        self._hosts[registration.host_id] = registration
        self._disconnected_hosts.discard(registration.host_id)
        return {
            'status': 'registered',
            'host_id': registration.host_id,
            'server_fingerprint': registration.server_fingerprint,
            'capabilities': sorted(registration.capabilities),
        }

    def host_hello_for(self, client_hello_payload: Mapping[str, object]) -> dict[str, object]:
        client_hello = RelayFrame.from_json(client_hello_payload)
        if client_hello.kind != 'client_hello':
            raise MobileRelayError('relay client hello required')
        host_id = _required_text(client_hello.payload.get('host_id'), 'client_hello.host_id')
        registration = self._require_host(host_id)
        if host_id in self._disconnected_hosts:
            raise MobileRelayError('relay host disconnected')
        supported_versions = _positive_int_list(
            client_hello.payload.get('supported_versions'),
            'client_hello.supported_versions',
        )
        if _SCHEMA_VERSION not in supported_versions:
            raise MobileRelayError('relay v2 is required')
        accepted_version = _SCHEMA_VERSION
        frame = RelayFrame(
            session_id=client_hello.session_id,
            seq=client_hello.seq + 1,
            kind='host_hello',
            payload={
                'host_id': registration.host_id,
                'server_fingerprint': registration.server_fingerprint,
                'host_pubkey_b64': registration.host_pubkey_b64,
                'accepted_version': accepted_version,
            },
        )
        transcript = RelayHandshakeTranscript.negotiate(client_hello=client_hello, host_hello=frame)
        self._sessions[transcript.session_id] = transcript
        return frame.to_json()

    def forward_from_phone(self, frame_payload: Mapping[str, object]) -> dict[str, object]:
        frame = RelayFrame.from_json(frame_payload)
        transcript = self._sessions.get(frame.session_id)
        if transcript is None:
            raise MobileRelayError('relay session is not established')
        if transcript.host_id in self._disconnected_hosts:
            raise MobileRelayError('relay host disconnected')
        if frame.kind != 'gateway_envelope':
            raise MobileRelayError('relay forwards only opaque gateway envelopes in this harness')
        record = {'direction': 'phone_to_host', 'host_id': transcript.host_id, 'frame': frame.to_json()}
        self._forwarded.append(record)
        return {
            'schema_version': _SCHEMA_VERSION,
            'session_id': frame.session_id,
            'seq': frame.seq + 1,
            'kind': 'ack',
            'payload': {'ack_seq': frame.seq},
        }

    def disconnect_host(self, host_id: str) -> None:
        self._require_host(host_id)
        self._disconnected_hosts.add(host_id)

    def mark_device_stale(self, *, host_id: str, device_id: str) -> None:
        self._require_host(host_id)
        device = _required_text(device_id, 'device_id')
        self._stale_devices.add((host_id, device))

    def set_relay_unreachable(self, unreachable: bool = True) -> None:
        self._relay_unreachable = bool(unreachable)

    def diagnostics_for_host(
        self,
        host_id: str,
        *,
        device_id: str | None = None,
        expected_host_fingerprint: str | None = None,
    ) -> dict[str, object]:
        base_host_id = _required_text(host_id, 'host_id')
        if self._relay_unreachable:
            return {
                'host_id': base_host_id,
                'state': 'relay_unreachable',
                'ready': False,
                'reason': 'relay control plane is unreachable from this harness',
            }
        registration = self._hosts.get(base_host_id)
        if registration is None:
            return {'host_id': base_host_id, 'state': 'unknown_host', 'ready': False}
        expected = _optional_text(expected_host_fingerprint)
        if expected and registration.server_fingerprint != expected:
            return {
                'host_id': base_host_id,
                'state': 'host_fingerprint_mismatch',
                'ready': False,
                'expected_host_fingerprint': expected,
                'observed_host_fingerprint': registration.server_fingerprint,
            }
        device = _optional_text(device_id)
        if device and (base_host_id, device) in self._stale_devices:
            return {
                'host_id': base_host_id,
                'device_id': device,
                'state': 'stale_device',
                'ready': False,
            }
        if base_host_id in self._disconnected_hosts:
            return {'host_id': base_host_id, 'state': 'host_disconnected', 'ready': False}
        sessions = [item for item in self._sessions.values() if item.host_id == base_host_id]
        return {
            'host_id': base_host_id,
            'state': 'ready' if sessions else 'registered',
            'ready': bool(sessions),
            'session_count': len(sessions),
            'forwarded_count': len([item for item in self._forwarded if item['host_id'] == base_host_id]),
        }

    @property
    def forwarded(self) -> tuple[dict[str, object], ...]:
        return tuple(self._forwarded)

    def _require_host(self, host_id: str) -> RelayHostRegistration:
        registration = self._hosts.get(host_id)
        if registration is None:
            raise MobileRelayError('relay host is not registered')
        return registration


class MobileGatewayRelayOutboundClient:
    """Source-side fake outbound relay client used by local tests and harnesses."""

    def __init__(
        self,
        *,
        relay: LocalRelayServerHarness,
        host_id: str,
        server_fingerprint: str,
        host_pubkey_b64: str,
        capabilities: tuple[str, ...] = ('http_json', 'project_view', 'relay_tunnel'),
        diagnostics: Mapping[str, str] | None = None,
    ) -> None:
        self._relay = relay
        self._registration = RelayHostRegistration(
            host_id=host_id,
            server_fingerprint=server_fingerprint,
            host_pubkey_b64=host_pubkey_b64,
            capabilities=capabilities,
            diagnostics=diagnostics or {},
        )

    def connect(self) -> dict[str, object]:
        return self._relay.register_host(self._registration.to_json())

    def diagnostics(
        self,
        *,
        device_id: str | None = None,
        expected_host_fingerprint: str | None = None,
    ) -> dict[str, object]:
        return self._relay.diagnostics_for_host(
            self._registration.host_id,
            device_id=device_id,
            expected_host_fingerprint=expected_host_fingerprint,
        )


def _reject_cleartext_keys(value: object, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            text_key = str(key)
            if text_key in _PROHIBITED_CLEARTEXT_KEYS:
                raise MobileRelayError(f'relay cleartext field is prohibited: {path}.{text_key}')
            _reject_cleartext_keys(item, f'{path}.{text_key}')
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_cleartext_keys(item, f'{path}[{index}]')


def _object_map(value: object, name: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if value is None:
        return {}
    raise MobileRelayError(f'relay field must be an object: {name}')


def _required_text(value: object, name: str) -> str:
    text = _optional_text(value)
    if not text:
        raise MobileRelayError(f'relay field is required: {name}')
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def _required_base64_text(value: object, name: str) -> str:
    text = _required_text(value, name)
    try:
        base64.b64decode(_base64_padding(text), altchars=b'-_', validate=True)
    except Exception as exc:  # pragma: no cover - exact exception varies by Python version
        raise MobileRelayError(f'relay field must be base64url: {name}') from exc
    return text


def _base64_padding(value: str) -> bytes:
    text = value.strip()
    return (text + '=' * (-len(text) % 4)).encode('ascii')


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(_base64_padding(value))


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=_JSON_SEPARATORS)


def _rendezvous_signing_payload(payload: Mapping[str, object]) -> bytes:
    return b'ccb-relay-rendezvous-v1\n' + _canonical_json(payload).encode('utf-8')


def _access_grant_signing_payload(payload: Mapping[str, object]) -> bytes:
    return b'ccb-relay-access-v1\n' + _canonical_json(payload).encode('utf-8')


def _phone_session_proof_signing_payload(payload: Mapping[str, object]) -> bytes:
    return b'ccb-relay-phone-proof-v1\n' + _canonical_json(payload).encode('utf-8')


def _signed_token_parts(
    token: str,
    *,
    prefix: str,
    rejected: str,
) -> tuple[dict[str, object], str]:
    try:
        observed_prefix, payload_b64, signature_b64 = str(token or '').split('.', 2)
    except ValueError as exc:
        raise MobileRelayError(rejected) from exc
    if observed_prefix != prefix:
        raise MobileRelayError(rejected)
    try:
        payload = json.loads(_b64decode(payload_b64).decode('utf-8'))
    except Exception as exc:
        raise MobileRelayError(rejected) from exc
    if not isinstance(payload, Mapping):
        raise MobileRelayError(rejected)
    return ({str(key): value for key, value in payload.items()}, signature_b64)


def _int(value: object, *, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _positive_int(value: object, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MobileRelayError(f'relay field must be a positive integer: {name}') from exc
    if parsed < 1:
        raise MobileRelayError(f'relay field must be a positive integer: {name}')
    return parsed


def _positive_int_list(value: object, name: str) -> list[int]:
    if not isinstance(value, (list, tuple)):
        raise MobileRelayError(f'relay field must be an integer list: {name}')
    return [_positive_int(item, f'{name}.item') for item in value]


def _string_set(value: object) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {text for item in value if (text := _optional_text(item))}


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key).strip(): str(item).strip()
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }


__all__ = [
    'ACCESS_GRANT_PREFIX',
    'LocalRelayServerHarness',
    'MobileGatewayRelayOutboundClient',
    'MobileRelayError',
    'PHONE_SESSION_PROOF_PREFIX',
    'RENDEZVOUS_CAPABILITY_PREFIX',
    'RelayAccessGrant',
    'RelayFrame',
    'RelayHandshakeTranscript',
    'RelayHostRegistration',
    'RelayPhoneSessionProof',
    'RelayRendezvousCapability',
    'issue_host_access_grant',
    'issue_host_rendezvous_capability',
    'issue_phone_session_proof',
]
