from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


RELAY_PROTOCOL_VERSION = 2
RELAY_PROTOCOL_NAME = 'ccb-relay-v2'
RELAY_KEY_ID = 'ccb-relay-v2-session'
RELAY_MAX_SEQUENCE = (1 << 64) - 1

RELAY_CLEAR_ENVELOPE_FIELDS = frozenset(
    {
        'schema_version',
        'session_id',
        'seq',
        'direction',
        'op',
        'nonce_b64',
        'ciphertext_b64',
        'key_id',
    }
)

RELAY_PROHIBITED_PLAINTEXT_FIELDS = frozenset(
    {
        'agent',
        'args',
        'authorization',
        'bearer_token',
        'body',
        'command',
        'content',
        'device_token',
        'file',
        'file_content',
        'file_name',
        'gateway_url',
        'message',
        'pairing_code',
        'paste_text',
        'path',
        'payload',
        'project_id',
        'project_name',
        'prompt',
        'reply',
        'route_provider',
        'terminal_id',
        'terminal_token',
        'text',
        'websocket_url',
    }
)


class RelayCryptoError(ValueError):
    pass


class RelayDirection(str, Enum):
    PHONE_TO_HOST = 'phone_to_host'
    HOST_TO_PHONE = 'host_to_phone'


@dataclass(frozen=True)
class RelayV2Envelope:
    session_id: str
    seq: int
    direction: RelayDirection
    op: str
    nonce_b64: str
    ciphertext_b64: str
    key_id: str = RELAY_KEY_ID
    schema_version: int = RELAY_PROTOCOL_VERSION

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> 'RelayV2Envelope':
        unknown = set(str(key) for key in payload) - RELAY_CLEAR_ENVELOPE_FIELDS
        if unknown:
            raise RelayCryptoError(f'relay v2 envelope contains non-envelope fields: {sorted(unknown)[0]}')
        envelope = cls(
            schema_version=_int(payload.get('schema_version'), fallback=0),
            session_id=_required_text(payload.get('session_id'), 'session_id'),
            seq=_sequence_int(payload.get('seq'), 'seq'),
            direction=_direction(payload.get('direction')),
            op=_required_text(payload.get('op'), 'op'),
            nonce_b64=_required_base64_text(payload.get('nonce_b64'), 'nonce_b64'),
            ciphertext_b64=_required_base64_text(payload.get('ciphertext_b64'), 'ciphertext_b64'),
            key_id=_required_text(payload.get('key_id'), 'key_id'),
        )
        envelope.validate()
        return envelope

    def to_json(self) -> dict[str, object]:
        self.validate()
        return {
            'schema_version': self.schema_version,
            'session_id': self.session_id,
            'seq': self.seq,
            'direction': self.direction.value,
            'op': self.op,
            'nonce_b64': self.nonce_b64,
            'ciphertext_b64': self.ciphertext_b64,
            'key_id': self.key_id,
        }

    def validate(self) -> None:
        if self.schema_version != RELAY_PROTOCOL_VERSION:
            raise RelayCryptoError('relay v2 envelope schema_version mismatch')
        _required_text(self.session_id, 'session_id')
        _sequence_int(self.seq, 'seq')
        _required_text(self.op, 'op')
        _required_base64_text(self.nonce_b64, 'nonce_b64')
        _required_base64_text(self.ciphertext_b64, 'ciphertext_b64')
        if self.key_id != RELAY_KEY_ID:
            raise RelayCryptoError('relay v2 envelope key_id mismatch')


@dataclass(frozen=True)
class RelayKeyConfirmation:
    phone_b64: str
    host_b64: str


@dataclass(frozen=True)
class RelayV2KeySchedule:
    session_id: str
    client_public_key_b64: str
    host_public_key_b64: str
    host_fingerprint: str
    transcript_hash_b64: str
    key_confirmation: RelayKeyConfirmation
    _phone_to_host_key: bytes
    _host_to_phone_key: bytes
    _phone_to_host_nonce_prefix: bytes
    _host_to_phone_nonce_prefix: bytes

    def to_public_json(self) -> dict[str, object]:
        return {
            'protocol': RELAY_PROTOCOL_NAME,
            'schema_version': RELAY_PROTOCOL_VERSION,
            'session_id': self.session_id,
            'client_public_key_b64': self.client_public_key_b64,
            'host_public_key_b64': self.host_public_key_b64,
            'host_fingerprint': self.host_fingerprint,
            'transcript_hash_b64': self.transcript_hash_b64,
            'key_confirmation': {
                'phone_b64': self.key_confirmation.phone_b64,
                'host_b64': self.key_confirmation.host_b64,
            },
        }

    def session(self, *, role: str) -> 'RelayCryptoSession':
        if role == 'phone':
            return RelayCryptoSession(
                session_id=self.session_id,
                send_direction=RelayDirection.PHONE_TO_HOST,
                receive_direction=RelayDirection.HOST_TO_PHONE,
                send_key=self._phone_to_host_key,
                receive_key=self._host_to_phone_key,
                send_nonce_prefix=self._phone_to_host_nonce_prefix,
                receive_nonce_prefix=self._host_to_phone_nonce_prefix,
            )
        if role == 'host':
            return RelayCryptoSession(
                session_id=self.session_id,
                send_direction=RelayDirection.HOST_TO_PHONE,
                receive_direction=RelayDirection.PHONE_TO_HOST,
                send_key=self._host_to_phone_key,
                receive_key=self._phone_to_host_key,
                send_nonce_prefix=self._host_to_phone_nonce_prefix,
                receive_nonce_prefix=self._phone_to_host_nonce_prefix,
            )
        raise RelayCryptoError('relay v2 role must be phone or host')


class RelayCryptoSession:
    def __init__(
        self,
        *,
        session_id: str,
        send_direction: RelayDirection,
        receive_direction: RelayDirection,
        send_key: bytes,
        receive_key: bytes,
        send_nonce_prefix: bytes,
        receive_nonce_prefix: bytes,
    ) -> None:
        self.session_id = _required_text(session_id, 'session_id')
        self.send_direction = send_direction
        self.receive_direction = receive_direction
        self._send_key = bytearray(send_key)
        self._receive_key = bytearray(receive_key)
        self._send_nonce_prefix = bytes(send_nonce_prefix)
        self._receive_nonce_prefix = bytes(receive_nonce_prefix)
        self._next_send_seq = 1
        self._next_receive_seq = 1
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def seal(self, *, op: str, plaintext: bytes) -> RelayV2Envelope:
        self._require_open()
        if self._next_send_seq > RELAY_MAX_SEQUENCE:
            self.close()
            raise RelayCryptoError('relay v2 sequence exhausted')
        sequence = self._next_send_seq
        self._next_send_seq += 1
        nonce = _nonce(self._send_nonce_prefix, sequence)
        aad = relay_v2_aad(
            session_id=self.session_id,
            seq=sequence,
            direction=self.send_direction,
            op=op,
            key_id=RELAY_KEY_ID,
        )
        ciphertext = ChaCha20Poly1305(bytes(self._send_key)).encrypt(nonce, bytes(plaintext), aad)
        return RelayV2Envelope(
            session_id=self.session_id,
            seq=sequence,
            direction=self.send_direction,
            op=op,
            nonce_b64=_b64(nonce),
            ciphertext_b64=_b64(ciphertext),
        )

    def open(self, envelope: RelayV2Envelope | Mapping[str, object]) -> bytes:
        self._require_open()
        if self._next_receive_seq > RELAY_MAX_SEQUENCE:
            self.close()
            raise RelayCryptoError('relay v2 sequence exhausted')
        frame = envelope if isinstance(envelope, RelayV2Envelope) else RelayV2Envelope.from_json(envelope)
        if frame.session_id != self.session_id:
            raise RelayCryptoError('relay v2 session mismatch')
        if frame.direction != self.receive_direction:
            raise RelayCryptoError('relay v2 direction mismatch')
        if frame.seq != self._next_receive_seq:
            raise RelayCryptoError('relay v2 sequence replay or reorder rejected')
        nonce = _b64decode(frame.nonce_b64)
        expected_nonce = _nonce(self._receive_nonce_prefix, frame.seq)
        if not hmac.compare_digest(nonce, expected_nonce):
            raise RelayCryptoError('relay v2 nonce mismatch')
        aad = relay_v2_aad(
            session_id=frame.session_id,
            seq=frame.seq,
            direction=frame.direction,
            op=frame.op,
            key_id=frame.key_id,
        )
        try:
            plaintext = ChaCha20Poly1305(bytes(self._receive_key)).decrypt(nonce, _b64decode(frame.ciphertext_b64), aad)
        except Exception as exc:  # pragma: no cover - cryptography backend owns exact exception class
            raise RelayCryptoError('relay v2 ciphertext authentication failed') from exc
        self._next_receive_seq += 1
        return plaintext

    def close(self) -> None:
        self._wipe(self._send_key)
        self._wipe(self._receive_key)
        self._closed = True

    def key_material_erased(self) -> bool:
        return all(value == 0 for value in self._send_key) and all(value == 0 for value in self._receive_key)

    def _require_open(self) -> None:
        if self._closed:
            raise RelayCryptoError('relay v2 session is closed')

    @staticmethod
    def _wipe(value: bytearray) -> None:
        for index in range(len(value)):
            value[index] = 0


def negotiate_relay_v2(supported_versions: object) -> int:
    if not isinstance(supported_versions, (list, tuple, set)):
        raise RelayCryptoError('relay v2 negotiation requires version list')
    versions = {int(item) for item in supported_versions}
    if RELAY_PROTOCOL_VERSION not in versions:
        raise RelayCryptoError('relay v2 negotiation failed closed')
    return RELAY_PROTOCOL_VERSION


def relay_v2_aad(
    *,
    session_id: str,
    seq: int,
    direction: RelayDirection | str,
    op: str,
    key_id: str,
) -> bytes:
    direction_value = direction.value if isinstance(direction, RelayDirection) else str(direction)
    payload = (
        '{"direction":'
        + json.dumps(direction_value, ensure_ascii=True, separators=(',', ':'))
        + ',"key_id":'
        + json.dumps(key_id, ensure_ascii=True, separators=(',', ':'))
        + ',"op":'
        + json.dumps(op, ensure_ascii=True, separators=(',', ':'))
        + ',"schema_version":2,"seq":'
        + str(int(seq))
        + ',"session_id":'
        + json.dumps(session_id, ensure_ascii=True, separators=(',', ':'))
        + '}'
    )
    return payload.encode('utf-8')


def host_fingerprint_for_public_key(host_public_key_b64: str) -> str:
    return 'sha256:' + _b64(hashlib.sha256(_b64decode(host_public_key_b64)).digest())


def derive_relay_v2_key_schedule(
    *,
    local_private_key: x25519.X25519PrivateKey,
    peer_public_key_b64: str,
    role: str,
    session_id: str,
    client_public_key_b64: str,
    host_public_key_b64: str,
    expected_host_fingerprint: str,
) -> RelayV2KeySchedule:
    if role not in {'phone', 'host'}:
        raise RelayCryptoError('relay v2 role must be phone or host')
    observed_fingerprint = host_fingerprint_for_public_key(host_public_key_b64)
    if not hmac.compare_digest(observed_fingerprint, expected_host_fingerprint):
        raise RelayCryptoError('relay v2 host fingerprint confirmation failed')
    accepted = negotiate_relay_v2([RELAY_PROTOCOL_VERSION])
    if accepted != RELAY_PROTOCOL_VERSION:
        raise RelayCryptoError('relay v2 downgrade rejected')
    peer_public_key = x25519.X25519PublicKey.from_public_bytes(_b64decode(peer_public_key_b64))
    shared = local_private_key.exchange(peer_public_key)
    transcript = _transcript(
        session_id=session_id,
        client_public_key_b64=client_public_key_b64,
        host_public_key_b64=host_public_key_b64,
        host_fingerprint=expected_host_fingerprint,
    )
    transcript_hash = hashlib.sha256(transcript).digest()
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=104,
        salt=transcript_hash,
        info=b'ccb-relay-v2 key schedule',
    ).derive(shared)
    phone_key = derived[0:32]
    host_key = derived[32:64]
    phone_nonce_prefix = derived[64:68]
    host_nonce_prefix = derived[68:72]
    confirm_key = derived[72:104]
    confirmation = RelayKeyConfirmation(
        phone_b64=_b64(hmac.digest(confirm_key, b'phone' + transcript_hash, hashlib.sha256)),
        host_b64=_b64(hmac.digest(confirm_key, b'host' + transcript_hash, hashlib.sha256)),
    )
    return RelayV2KeySchedule(
        session_id=session_id,
        client_public_key_b64=client_public_key_b64,
        host_public_key_b64=host_public_key_b64,
        host_fingerprint=expected_host_fingerprint,
        transcript_hash_b64=_b64(transcript_hash),
        key_confirmation=confirmation,
        _phone_to_host_key=phone_key,
        _host_to_phone_key=host_key,
        _phone_to_host_nonce_prefix=phone_nonce_prefix,
        _host_to_phone_nonce_prefix=host_nonce_prefix,
    )


def key_pair_from_private_bytes(value: bytes) -> x25519.X25519PrivateKey:
    if len(value) != 32:
        raise RelayCryptoError('relay v2 X25519 private key must be 32 bytes')
    return x25519.X25519PrivateKey.from_private_bytes(bytes(value))


def public_key_b64(private_key: x25519.X25519PrivateKey) -> str:
    return _b64(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def deterministic_test_vector() -> dict[str, object]:
    client_private = key_pair_from_private_bytes(bytes(range(1, 33)))
    host_private = key_pair_from_private_bytes(bytes(range(101, 133)))
    client_public = public_key_b64(client_private)
    host_public = public_key_b64(host_private)
    fingerprint = host_fingerprint_for_public_key(host_public)
    schedule = derive_relay_v2_key_schedule(
        local_private_key=client_private,
        peer_public_key_b64=host_public,
        role='phone',
        session_id='relay-v2-vector-session',
        client_public_key_b64=client_public,
        host_public_key_b64=host_public,
        expected_host_fingerprint=fingerprint,
    )
    host_schedule = derive_relay_v2_key_schedule(
        local_private_key=host_private,
        peer_public_key_b64=client_public,
        role='host',
        session_id='relay-v2-vector-session',
        client_public_key_b64=client_public,
        host_public_key_b64=host_public,
        expected_host_fingerprint=fingerprint,
    )
    if schedule.to_public_json() != host_schedule.to_public_json():
        raise RelayCryptoError('relay v2 deterministic vector key schedule mismatch')
    phone = schedule.session(role='phone')
    host = host_schedule.session(role='host')
    plaintext = b'{"kind":"relay_vector","value":"hello"}'
    sealed = phone.seal(op='vector_ping', plaintext=plaintext)
    opened = host.open(sealed)
    if opened != plaintext:
        raise RelayCryptoError('relay v2 deterministic vector failed round trip')
    return {
        'protocol': RELAY_PROTOCOL_NAME,
        'schema_version': RELAY_PROTOCOL_VERSION,
        'client_seed_b64': _b64(bytes(range(1, 33))),
        'host_seed_b64': _b64(bytes(range(101, 133))),
        'client_public_key_b64': client_public,
        'host_public_key_b64': host_public,
        'host_fingerprint': fingerprint,
        'session_id': schedule.session_id,
        'transcript_hash_b64': schedule.transcript_hash_b64,
        'key_confirmation': {
            'phone_b64': schedule.key_confirmation.phone_b64,
            'host_b64': schedule.key_confirmation.host_b64,
        },
        'plaintext_b64': _b64(plaintext),
        'frame': sealed.to_json(),
    }


def assert_no_prohibited_plaintext(value: object, path: str = 'relay') -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            text_key = str(key)
            if text_key in RELAY_PROHIBITED_PLAINTEXT_FIELDS:
                raise RelayCryptoError(f'relay prohibited plaintext field: {path}.{text_key}')
            assert_no_prohibited_plaintext(item, f'{path}.{text_key}')
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_no_prohibited_plaintext(item, f'{path}[{index}]')


def _transcript(
    *,
    session_id: str,
    client_public_key_b64: str,
    host_public_key_b64: str,
    host_fingerprint: str,
) -> bytes:
    payload = {
        'protocol': RELAY_PROTOCOL_NAME,
        'schema_version': RELAY_PROTOCOL_VERSION,
        'session_id': _required_text(session_id, 'session_id'),
        'client_public_key_b64': _required_base64_text(client_public_key_b64, 'client_public_key_b64'),
        'host_public_key_b64': _required_base64_text(host_public_key_b64, 'host_public_key_b64'),
        'host_fingerprint': _required_text(host_fingerprint, 'host_fingerprint'),
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _nonce(prefix: bytes, seq: int) -> bytes:
    if len(prefix) != 4:
        raise RelayCryptoError('relay v2 nonce prefix invalid')
    return bytes(prefix) + _sequence_int(seq, 'seq').to_bytes(8, 'big')


def _direction(value: object) -> RelayDirection:
    try:
        return RelayDirection(_required_text(value, 'direction'))
    except ValueError as exc:
        raise RelayCryptoError('relay v2 direction invalid') from exc


def _required_text(value: object, name: str) -> str:
    text = str(value or '').strip()
    if not text:
        raise RelayCryptoError(f'relay v2 field is required: {name}')
    return text


def _required_base64_text(value: object, name: str) -> str:
    text = _required_text(value, name)
    _b64decode(text)
    return text


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(value)).decode('ascii').rstrip('=')


def _b64decode(value: str) -> bytes:
    try:
        text = str(value).strip()
        return base64.urlsafe_b64decode((text + '=' * (-len(text) % 4)).encode('ascii'))
    except Exception as exc:  # pragma: no cover - exact exception varies
        raise RelayCryptoError('relay v2 field must be base64url') from exc


def _int(value: object, *, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _positive_int(value: object, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RelayCryptoError(f'relay v2 field must be positive integer: {name}') from exc
    if parsed < 1:
        raise RelayCryptoError(f'relay v2 field must be positive integer: {name}')
    return parsed


def _sequence_int(value: object, name: str) -> int:
    parsed = _positive_int(value, name)
    if parsed > RELAY_MAX_SEQUENCE:
        raise RelayCryptoError(f'relay v2 sequence exceeds uint64: {name}')
    return parsed


__all__ = [
    'RELAY_CLEAR_ENVELOPE_FIELDS',
    'RELAY_KEY_ID',
    'RELAY_MAX_SEQUENCE',
    'RELAY_PROHIBITED_PLAINTEXT_FIELDS',
    'RELAY_PROTOCOL_NAME',
    'RELAY_PROTOCOL_VERSION',
    'RelayCryptoError',
    'RelayCryptoSession',
    'RelayDirection',
    'RelayKeyConfirmation',
    'RelayV2Envelope',
    'RelayV2KeySchedule',
    'assert_no_prohibited_plaintext',
    'derive_relay_v2_key_schedule',
    'deterministic_test_vector',
    'host_fingerprint_for_public_key',
    'key_pair_from_private_bytes',
    'negotiate_relay_v2',
    'public_key_b64',
    'relay_v2_aad',
]
