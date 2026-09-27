from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path

from provider_core.source_home import current_provider_source_home
from provider_backends.session_authority import remember_bound_provider_session_authority
from provider_profiles.codex_home_config import (
    codex_auth_sidecar_names,
    codex_provider_authority_fingerprint,
    codex_source_authority_config_payload,
)
from storage.atomic import atomic_write_text

from .start_cmd import extract_resume_session_id

_MEMORY_PROJECTION_MARKER = 'codex-memory-projection.json'
_AUTHORITY_KEY_NAME = '.ccb-authority-hmac-key'
_AUTHORITY_FILE_NAMES = (
    'auth.json',
    'company-codex-api-key',
    'company-codex.config.toml',
)


def current_provider_authority_fingerprint(profile, runtime_dir: Path | None = None) -> str:
    if runtime_dir is None:
        return _normalized_fingerprint(codex_provider_authority_fingerprint(profile))
    return _runtime_authority_fingerprint(profile, Path(runtime_dir))


def current_memory_projection_fingerprint(runtime_dir: Path | None) -> str:
    if runtime_dir is None:
        return ''
    marker_path = Path(runtime_dir) / _MEMORY_PROJECTION_MARKER
    try:
        data = json.loads(marker_path.read_text(encoding='utf-8'))
    except Exception:
        return ''
    if not isinstance(data, dict):
        return ''
    return _normalized_fingerprint(data.get('sha256'))


def stored_provider_authority_fingerprint(data: Mapping[str, object]) -> str:
    return _normalized_fingerprint(data.get('codex_provider_authority_fingerprint'))


def stored_session_authority_fingerprint(data: Mapping[str, object]) -> str:
    return _normalized_fingerprint(data.get('codex_session_authority_fingerprint'))


def stored_memory_projection_fingerprint(data: Mapping[str, object]) -> str:
    return _normalized_fingerprint(data.get('codex_memory_projection_sha256'))


def resume_authority_matches(
    data: Mapping[str, object],
    *,
    profile=None,
    current_fingerprint: str | None = None,
    current_memory_fingerprint: str | None = None,
    runtime_dir: Path | None = None,
) -> bool:
    del current_memory_fingerprint
    current = (
        _normalized_fingerprint(current_fingerprint)
        if current_fingerprint is not None
        else current_provider_authority_fingerprint(profile, runtime_dir=runtime_dir)
    )
    if stored_provider_authority_fingerprint(data) != current:
        return False
    if not has_resume_candidate(data):
        return True
    stored_binding = stored_session_authority_fingerprint(data)
    if stored_binding:
        return stored_binding == current
    return not current


def remember_bound_session_authority(data: dict[str, object]) -> None:
    if remember_bound_provider_session_authority(data, 'codex'):
        return
    current = stored_provider_authority_fingerprint(data)
    if current:
        data['codex_session_authority_fingerprint'] = current
    else:
        data.pop('codex_session_authority_fingerprint', None)
    if (
        str(data.get('ccb_resume_compatibility') or '').strip() == 'linked_continuation'
        and str(data.get('ccb_continuation_launch_mode') or '').strip() == 'fork'
    ):
        data['ccb_resume_compatibility'] = 'native_fork_continuation'
        data['ccb_continuity_status'] = 'continued_on_new_authority'


def has_resume_candidate(data: Mapping[str, object]) -> bool:
    if str(data.get('codex_session_id') or '').strip():
        return True
    for key in ('codex_start_cmd', 'start_cmd'):
        if extract_resume_session_id(data.get(key)):
            return True
    return False


def _normalized_fingerprint(value: object) -> str:
    return str(value or '').strip()


def _runtime_authority_fingerprint(profile, runtime_dir: Path) -> str:
    state_dir = _state_dir(runtime_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    key_path = state_dir / _AUTHORITY_KEY_NAME
    key = _load_or_create_key(key_path)
    home = _managed_home(profile, state_dir)
    profile_env = {
        str(name): str(value)
        for name, value in dict(getattr(profile, 'env', {}) or {}).items()
        if str(value).strip()
    }
    explicit_credential = bool(profile_env.get('OPENAI_API_KEY'))
    inherit_auth = bool(getattr(profile, 'inherit_auth', True)) and not explicit_credential
    use_managed_auth = not bool(getattr(profile, 'inherit_auth', True)) and not explicit_credential
    explicit_api_names = _explicit_api_owned_names(profile_env)
    source_home = _source_codex_home()
    explicit_route = bool(explicit_api_names & {'OPENAI_BASE_URL', 'OPENAI_API_BASE'})
    source_config = source_home / 'config.toml'
    payload = {
        'profile': {
            'mode': str(getattr(profile, 'mode', 'inherit') or 'inherit'),
            'inherit_api': bool(getattr(profile, 'inherit_api', True)),
            'inherit_auth': bool(getattr(profile, 'inherit_auth', True)),
            'env': dict(sorted(profile_env.items())),
        },
        'inherited_api_env': {
            name: str(os.environ.get(name) or '')
            for name in sorted(
                {
                    'OPENAI_API_KEY',
                    'OPENAI_BASE_URL',
                    'OPENAI_API_BASE',
                    'OPENAI_ORG_ID',
                    'OPENAI_ORGANIZATION',
                }
            )
            if bool(getattr(profile, 'inherit_api', True))
            and name not in explicit_api_names
        },
        'managed_files': _authority_file_payload(home) if use_managed_auth else {},
        'source_auth_files': (
            _authority_file_payload(
                source_home,
                names=('auth.json', *codex_auth_sidecar_names(source_home, source_config)),
            )
            if inherit_auth
            else {}
        ),
        'source_config_authority': (
            codex_source_authority_config_payload(
                source_config,
                include_route=not explicit_route,
                include_login=inherit_auth,
            )
            if bool(getattr(profile, 'inherit_config', True))
            else {}
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()[:32]


def _state_dir(runtime_dir: Path) -> Path:
    runtime = Path(runtime_dir).expanduser()
    return runtime.parent.parent / 'provider-state' / 'codex'


def _managed_home(profile, state_dir: Path) -> Path:
    explicit = str(getattr(profile, 'runtime_home', '') or '').strip()
    return Path(explicit).expanduser() if explicit else state_dir / 'home'


def _source_codex_home() -> Path:
    raw = str(os.environ.get('CODEX_HOME') or '').strip()
    if raw:
        candidate = Path(raw).expanduser()
        if 'provider-state' not in candidate.parts:
            return candidate
    return current_provider_source_home() / '.codex'


def _file_bytes(path: Path) -> bytes:
    source = Path(path).expanduser()
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        return b''
    except OSError as exc:
        raise RuntimeError(f'cannot inspect Codex authority source: {source}: {exc}') from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f'Codex authority source must be a regular file: {source}')
    try:
        return source.read_bytes()
    except OSError as exc:
        raise RuntimeError(f'cannot read Codex authority source: {source}: {exc}') from exc


def _authority_file_payload(root: Path, *, names: tuple[str, ...] | None = None) -> dict[str, str]:
    payload: dict[str, str] = {}
    for name in names or _AUTHORITY_FILE_NAMES:
        path = Path(root) / name
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f'cannot inspect Codex authority source: {path}: {exc}') from exc
        payload[name] = _file_bytes(path).hex()
    return payload


def _explicit_api_owned_names(profile_env: Mapping[str, str]) -> set[str]:
    configured = set(profile_env)
    groups = (
        {'OPENAI_API_KEY'},
        {'OPENAI_BASE_URL', 'OPENAI_API_BASE'},
        {'OPENAI_ORG_ID', 'OPENAI_ORGANIZATION'},
    )
    owned: set[str] = set()
    for group in groups:
        if group & configured:
            owned.update(group)
    return owned


def _load_or_create_key(path: Path) -> bytes:
    try:
        key = bytes.fromhex(path.read_text(encoding='ascii').strip())
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_bytes(32)
    atomic_write_text(path, key.hex() + '\n')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


__all__ = [
    'current_memory_projection_fingerprint',
    'current_provider_authority_fingerprint',
    'has_resume_candidate',
    'remember_bound_session_authority',
    'resume_authority_matches',
    'stored_memory_projection_fingerprint',
    'stored_provider_authority_fingerprint',
    'stored_session_authority_fingerprint',
]
