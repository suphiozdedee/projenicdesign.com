from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from storage.atomic import atomic_write_json

from .relay_admission import generate_host_private_key, host_public_key_b64
from .relay_crypto import host_fingerprint_for_public_key, public_key_b64
from .relay import issue_host_rendezvous_capability


RELAY_HOST_CREDENTIALS_RECORD_TYPE = 'ccb_relay_host_credentials'
RELAY_HOST_CREDENTIALS_SCHEMA_VERSION = 1
RELAY_MODE_OFFICIAL = 'official'
RELAY_MODE_SELF_HOSTED = 'self_hosted'
RELAY_MODES = frozenset({RELAY_MODE_OFFICIAL, RELAY_MODE_SELF_HOSTED})
CCB_OFFICIAL_RELAY_ORIGIN = 'wss://47.120.71.142'
_CCB_OFFICIAL_RELAY_ORIGIN_ALIASES = frozenset(
    {CCB_OFFICIAL_RELAY_ORIGIN, 'wss://relay.seemlab.top'}
)


class RelayHostCredentialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class RelayHostCredentials:
    relay_origin: str
    host_id: str
    invitation_id: str
    host_signing_private_key_b64: str
    host_crypto_private_key_b64: str
    activated_at: str
    relay_mode: str = RELAY_MODE_SELF_HOSTED

    def __post_init__(self) -> None:
        mode = _validated_relay_mode(self.relay_mode)
        object.__setattr__(self, 'relay_mode', mode)
        if mode == RELAY_MODE_OFFICIAL:
            _validate_official_relay_origin(self.relay_origin)

    @property
    def host_signing_key(self) -> ed25519.Ed25519PrivateKey:
        return ed25519.Ed25519PrivateKey.from_private_bytes(
            _decode_raw_key(self.host_signing_private_key_b64, 'host signing private key')
        )

    @property
    def host_crypto_key(self) -> x25519.X25519PrivateKey:
        return x25519.X25519PrivateKey.from_private_bytes(
            _decode_raw_key(self.host_crypto_private_key_b64, 'host crypto private key')
        )

    @property
    def host_signing_public_key_b64(self) -> str:
        return host_public_key_b64(self.host_signing_key)

    @property
    def host_crypto_public_key_b64(self) -> str:
        return public_key_b64(self.host_crypto_key)

    @property
    def host_fingerprint(self) -> str:
        return host_fingerprint_for_public_key(self.host_crypto_public_key_b64)

    @property
    def relay_http_origin(self) -> str:
        return _relay_http_origin(self.relay_origin)

    def to_json(self) -> dict[str, object]:
        return {
            'schema_version': RELAY_HOST_CREDENTIALS_SCHEMA_VERSION,
            'record_type': RELAY_HOST_CREDENTIALS_RECORD_TYPE,
            'relay_mode': self.relay_mode,
            'relay_origin': self.relay_origin,
            'host_id': self.host_id,
            'invitation_id': self.invitation_id,
            'host_signing_private_key_b64': self.host_signing_private_key_b64,
            'host_crypto_private_key_b64': self.host_crypto_private_key_b64,
            'host_signing_public_key_b64': self.host_signing_public_key_b64,
            'host_crypto_public_key_b64': self.host_crypto_public_key_b64,
            'host_fingerprint': self.host_fingerprint,
            'activated_at': self.activated_at,
        }

    def public_summary(self, *, credential_path: Path) -> dict[str, object]:
        return {
            'relay_status': 'host_activated',
            'relay_mode': self.relay_mode,
            'relay_origin': self.relay_origin,
            'host_id': self.host_id,
            'invitation_id': self.invitation_id,
            'host_fingerprint': self.host_fingerprint,
            'credential_path': str(credential_path),
            'activated_at': self.activated_at,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> 'RelayHostCredentials':
        if int(payload.get('schema_version') or 0) != RELAY_HOST_CREDENTIALS_SCHEMA_VERSION:
            raise RelayHostCredentialsError('relay host credentials schema is unsupported')
        if str(payload.get('record_type') or '') != RELAY_HOST_CREDENTIALS_RECORD_TYPE:
            raise RelayHostCredentialsError('relay host credentials record type is invalid')
        relay_origin = _validated_relay_origin(str(payload.get('relay_origin') or ''))
        relay_mode = _validated_relay_mode(payload.get('relay_mode'), default=_relay_mode_for_legacy_origin(relay_origin))
        if relay_mode == RELAY_MODE_OFFICIAL:
            _validate_official_relay_origin(relay_origin)
        credentials = cls(
            relay_origin=relay_origin,
            host_id=_required_text(payload.get('host_id'), 'host_id'),
            invitation_id=_required_text(payload.get('invitation_id'), 'invitation_id'),
            host_signing_private_key_b64=_required_text(
                payload.get('host_signing_private_key_b64'),
                'host_signing_private_key_b64',
            ),
            host_crypto_private_key_b64=_required_text(
                payload.get('host_crypto_private_key_b64'),
                'host_crypto_private_key_b64',
            ),
            activated_at=_required_text(payload.get('activated_at'), 'activated_at'),
            relay_mode=relay_mode,
        )
        expected_signing = str(payload.get('host_signing_public_key_b64') or '')
        expected_crypto = str(payload.get('host_crypto_public_key_b64') or '')
        expected_fingerprint = str(payload.get('host_fingerprint') or '')
        if expected_signing and expected_signing != credentials.host_signing_public_key_b64:
            raise RelayHostCredentialsError('relay host signing key binding is invalid')
        if expected_crypto and expected_crypto != credentials.host_crypto_public_key_b64:
            raise RelayHostCredentialsError('relay host crypto key binding is invalid')
        if expected_fingerprint and expected_fingerprint != credentials.host_fingerprint:
            raise RelayHostCredentialsError('relay host fingerprint binding is invalid')
        return credentials


def activate_relay_host(
    *,
    relay_mode: str = RELAY_MODE_SELF_HOSTED,
    relay_origin: str,
    invitation: str,
    credential_path: Path,
    timeout_seconds: float = 10.0,
    ssl_context: ssl.SSLContext | None = None,
    allow_insecure_loopback_for_tests: bool = False,
) -> RelayHostCredentials:
    mode = _validated_relay_mode(relay_mode)
    origin = _validated_relay_origin(
        relay_origin,
        allow_insecure_loopback_for_tests=allow_insecure_loopback_for_tests,
    )
    if mode == RELAY_MODE_OFFICIAL:
        _validate_official_relay_origin(origin)
    invitation_text = _required_text(invitation, 'invitation')
    target = Path(credential_path).expanduser()
    if target.exists():
        raise RelayHostCredentialsError(
            f'relay host credentials already exist; revoke or move them before re-activating: {target}'
        )
    signing_key = generate_host_private_key()
    crypto_key = x25519.X25519PrivateKey.generate()
    request = Request(
        _activation_url(origin),
        data=json.dumps(
            {
                'invitation': invitation_text,
                'host_public_key_b64': host_public_key_b64(signing_key),
            },
            ensure_ascii=True,
            separators=(',', ':'),
        ).encode('utf-8'),
        headers={'accept': 'application/json', 'content-type': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            raw = response.read(64 * 1024)
            if int(response.status) != 201:
                raise RelayHostCredentialsError('relay host activation was rejected')
    except HTTPError as exc:
        raise RelayHostCredentialsError(
            f'relay host activation was rejected: HTTP {exc.code}'
        ) from None
    except (OSError, URLError, TimeoutError) as exc:
        raise RelayHostCredentialsError(
            f'relay host activation endpoint is unavailable: {exc.__class__.__name__}'
        ) from None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RelayHostCredentialsError('relay host activation response is invalid') from exc
    if not isinstance(payload, Mapping):
        raise RelayHostCredentialsError('relay host activation response is invalid')
    if str(payload.get('type') or '') != 'ccb_relay_host_credential_v1':
        raise RelayHostCredentialsError('relay host activation credential type is invalid')
    if str(payload.get('host_public_key_b64') or '') != host_public_key_b64(signing_key):
        raise RelayHostCredentialsError('relay host activation key binding is invalid')
    credentials = RelayHostCredentials(
        relay_origin=origin,
        host_id=_required_text(payload.get('host_id'), 'host_id'),
        invitation_id=_required_text(payload.get('invitation_id'), 'invitation_id'),
        host_signing_private_key_b64=_private_key_b64(signing_key),
        host_crypto_private_key_b64=_private_key_b64(crypto_key),
        activated_at=datetime.now(timezone.utc).isoformat(),
        relay_mode=mode,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    atomic_write_json(target, credentials.to_json())
    target.chmod(0o600)
    return credentials


def load_relay_host_credentials(path: Path) -> RelayHostCredentials:
    target = Path(path).expanduser()
    try:
        mode = target.stat().st_mode & 0o777
    except OSError as exc:
        raise RelayHostCredentialsError(f'relay host credentials are unavailable: {target}') from exc
    if mode & 0o077:
        raise RelayHostCredentialsError('relay host credentials must be owner-only')
    try:
        payload = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise RelayHostCredentialsError('relay host credentials are invalid') from exc
    if not isinstance(payload, Mapping):
        raise RelayHostCredentialsError('relay host credentials are invalid')
    return RelayHostCredentials.from_json(payload)


def build_relay_pairing_payload(
    pairing: Mapping[str, object],
    *,
    credentials: RelayHostCredentials,
    lifetime_seconds: int = 10 * 60,
) -> dict[str, object]:
    if lifetime_seconds < 60 or lifetime_seconds > 60 * 60:
        raise RelayHostCredentialsError('relay pairing bootstrap lifetime is invalid')
    client_key = x25519.X25519PrivateKey.generate()
    session_id = f'pair-{secrets.token_urlsafe(18)}'
    phone_nonce_b64 = _b64(secrets.token_bytes(24))
    now = int(time.time())
    relay_http_origin = _relay_http_origin(credentials.relay_origin)
    client_public_key_b64 = public_key_b64(client_key)
    return {
        **{str(key): value for key, value in pairing.items()},
        'host_id': credentials.host_id,
        'relay_mode': credentials.relay_mode,
        'route_provider': 'relay',
        'gateway_url': relay_http_origin,
        'claim_endpoint': f'{relay_http_origin}/v1/pairing/claim',
        'websocket_url': credentials.relay_origin,
        'server_fingerprint': credentials.host_fingerprint,
        'relay_session_id': session_id,
        'relay_client_private_key_b64': _private_key_b64(client_key),
        'relay_phone_nonce_b64': phone_nonce_b64,
        'relay_rendezvous_capability': issue_host_rendezvous_capability(
            credentials.host_signing_key,
            host_id=credentials.host_id,
            session_id=session_id,
            client_pubkey_b64=client_public_key_b64,
            phone_nonce_b64=phone_nonce_b64,
            audience=credentials.relay_origin,
            issued_at=now,
            expires_at=now + lifetime_seconds,
        ),
        'relay_bootstrap_expires_at': datetime.fromtimestamp(
            now + lifetime_seconds,
            tz=timezone.utc,
        ).isoformat(),
        'relay_bootstrap_single_use': True,
    }


def _activation_url(relay_origin: str) -> str:
    parsed = urlparse(relay_origin)
    scheme = 'https' if parsed.scheme == 'wss' else 'http'
    return urlunparse((scheme, parsed.netloc, '/v2/activate', '', '', ''))


def _relay_http_origin(relay_origin: str) -> str:
    parsed = urlparse(_validated_relay_origin(relay_origin))
    return urlunparse(('https', parsed.netloc, '', '', '', ''))


def _validated_relay_mode(value: object, *, default: str | None = None) -> str:
    mode = str(value or default or '').strip().lower().replace('-', '_')
    if mode not in RELAY_MODES:
        raise RelayHostCredentialsError('relay mode is invalid')
    return mode


def _relay_mode_for_legacy_origin(relay_origin: str) -> str:
    return RELAY_MODE_OFFICIAL if relay_origin in _CCB_OFFICIAL_RELAY_ORIGIN_ALIASES else RELAY_MODE_SELF_HOSTED


def _validate_official_relay_origin(relay_origin: str) -> None:
    if relay_origin not in _CCB_OFFICIAL_RELAY_ORIGIN_ALIASES:
        raise RelayHostCredentialsError('official relay mode requires the CCB official relay endpoint')


def _validated_relay_origin(
    value: str,
    *,
    allow_insecure_loopback_for_tests: bool = False,
) -> str:
    parsed = urlparse(str(value or '').strip())
    insecure_test = (
        allow_insecure_loopback_for_tests
        and parsed.scheme == 'ws'
        and parsed.hostname in {'127.0.0.1', '::1', 'localhost'}
    )
    if parsed.scheme != 'wss' and not insecure_test:
        raise RelayHostCredentialsError('relay origin must use wss')
    if not parsed.netloc or parsed.username or parsed.password:
        raise RelayHostCredentialsError('relay origin is invalid')
    if parsed.path not in {'', '/'} or parsed.params or parsed.query or parsed.fragment:
        raise RelayHostCredentialsError('relay origin must not contain a path or query')
    return urlunparse((parsed.scheme, parsed.netloc, '', '', '', ''))


def _private_key_b64(
    key: ed25519.Ed25519PrivateKey | x25519.X25519PrivateKey,
) -> str:
    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _decode_raw_key(value: str, label: str) -> bytes:
    text = _required_text(value, label)
    try:
        raw = base64.urlsafe_b64decode(text + '=' * ((4 - len(text) % 4) % 4))
    except Exception as exc:
        raise RelayHostCredentialsError(f'relay {label} is invalid') from exc
    if len(raw) != 32:
        raise RelayHostCredentialsError(f'relay {label} is invalid')
    return raw


def _required_text(value: object, name: str) -> str:
    text = str(value or '').strip()
    if not text:
        raise RelayHostCredentialsError(f'relay host credentials missing {name}')
    return text


__all__ = [
    'CCB_OFFICIAL_RELAY_ORIGIN',
    'RELAY_HOST_CREDENTIALS_RECORD_TYPE',
    'RELAY_HOST_CREDENTIALS_SCHEMA_VERSION',
    'RELAY_MODE_OFFICIAL',
    'RELAY_MODE_SELF_HOSTED',
    'RelayHostCredentials',
    'RelayHostCredentialsError',
    'activate_relay_host',
    'build_relay_pairing_payload',
    'load_relay_host_credentials',
]
