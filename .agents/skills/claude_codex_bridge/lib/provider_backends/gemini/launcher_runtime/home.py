from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

from cli.services.role_command_policy import role_command_policy_disables_inherited_assets
from provider_core.memory_projection import (
    materialize_provider_memory_file,
    memory_projection_result,
    record_memory_projection_event,
)
from provider_core.keyring_read import KeyringReadResult, read_keyring_password_state
from provider_core.one_way_inheritance import (
    ensure_private_directory,
    ensure_private_inheritance_directory,
)
from provider_core.inherited_skills import materialize_required_control_skills
from provider_core.projected_assets import seed_projected_tree
from provider_core.source_home import current_provider_source_home
from provider_profiles import provider_api_env_keys
from storage.atomic import atomic_write_text
from storage.paths import ensure_provider_user_cache_dir

from ..home_layout import GeminiHomeLayout, gemini_layout_for_home, gemini_layout_from_session_data
from .env import explicit_api_owned_names
from .session_paths import read_session_payload, session_file_for_runtime_dir, state_dir_for_runtime_dir

_GEMINI_LOGIN_AUTH_FILENAMES = (
    'oauth_creds.json',
    'google_accounts.json',
    # Gemini uses this encrypted file when OS keychain access is disabled.
    'gemini-credentials.json',
    # Preserve existing MCP/A2A authorization only as private ordinary files.
    'mcp-oauth-tokens.json',
    'a2a-oauth-tokens.json',
)
_GEMINI_EXTENSIONS_PROJECTION_LABEL = 'gemini-inherited-extensions'
_GEMINI_AUTH_PROJECTION_MANIFEST = '.ccb-auth-projection.json'
_GEMINI_CREDENTIAL_ENV_KEYS = {
    'GEMINI_API_KEY',
    'GOOGLE_API_KEY',
    'GOOGLE_APPLICATION_CREDENTIALS',
}


def resolve_gemini_home_layout(runtime_dir: Path, profile) -> GeminiHomeLayout:
    explicit_runtime_home = _profile_runtime_home(profile)
    if explicit_runtime_home is not None:
        return gemini_layout_for_home(explicit_runtime_home)

    managed_home = _managed_isolated_home(runtime_dir)
    existing = _existing_layout(runtime_dir, managed_home=managed_home)
    if existing is not None:
        return existing

    return gemini_layout_for_home(managed_home)


def prepare_gemini_home_overrides(
    runtime_dir: Path,
    profile,
    *,
    refresh_home: bool = True,
    project_root: Path | None = None,
    agent_name: str | None = None,
    workspace_path: Path | None = None,
    memory_projection_event_path: Path | None = None,
    memory_projection_marker_path: Path | None = None,
    command_policy=None,
) -> dict[str, str]:
    layout = resolve_gemini_home_layout(runtime_dir, profile)
    if refresh_home:
        materialize_gemini_home_config(
            layout.home_root,
            profile=profile,
            project_root=project_root,
            agent_name=agent_name,
            workspace_path=workspace_path,
            memory_projection_event_path=memory_projection_event_path,
            memory_projection_marker_path=memory_projection_marker_path,
            command_policy=command_policy,
        )
    materialize_required_control_skills(
        provider='gemini',
        target_dir=layout.gemini_dir / 'skills',
    )
    cache_root = _gemini_shared_cache_root()
    overrides = {
        'HOME': str(layout.home_root),
        'GEMINI_CLI_HOME': str(layout.home_root),
        'GEMINI_ROOT': str(layout.tmp_root),
        'NPM_CONFIG_CACHE': str(cache_root / 'npm'),
        'npm_config_cache': str(cache_root / 'npm'),
        'XDG_CACHE_HOME': str(cache_root / 'xdg'),
        # Gemini's supported file-storage switch prevents managed MCP/OAuth
        # refresh or logout from mutating the user's OS keychain.
        'GEMINI_FORCE_FILE_STORAGE': 'true',
        'GEMINI_FORCE_ENCRYPTED_FILE_STORAGE': 'true',
    }
    if "WSL_DISTRO_NAME" in os.environ:
        overrides['USERPROFILE'] = str(layout.home_root)
        wslenv_additions = (
            "HOME/p:USERPROFILE/p:GEMINI_CLI_HOME/p:GEMINI_ROOT/p:"
            "NPM_CONFIG_CACHE/p:npm_config_cache/p:XDG_CACHE_HOME/p:"
            "GEMINI_FORCE_FILE_STORAGE:GEMINI_FORCE_ENCRYPTED_FILE_STORAGE"
        )
        existing_wslenv = os.environ.get("WSLENV", "")
        overrides['WSLENV'] = (
            f"{wslenv_additions}:{existing_wslenv}"
            if existing_wslenv
            else wslenv_additions
        )
    return overrides


def _profile_runtime_home(profile) -> Path | None:
    del profile
    return None


def _existing_layout(runtime_dir: Path, *, managed_home: Path) -> GeminiHomeLayout | None:
    session_file = session_file_for_runtime_dir(runtime_dir)
    if session_file is None or not session_file.is_file():
        return None
    data = read_session_payload(session_file)
    if not isinstance(data, dict):
        return None
    layout = gemini_layout_from_session_data(data)
    if layout is None:
        return None
    return layout if _is_within_home_root(layout.home_root, managed_home) else None


def _managed_isolated_home(runtime_dir: Path) -> Path:
    state_dir = state_dir_for_runtime_dir(runtime_dir)
    if state_dir is not None:
        return state_dir / 'home'
    return Path(runtime_dir).expanduser() / 'gemini-home'


def _gemini_shared_cache_root() -> Path:
    root = ensure_provider_user_cache_dir('gemini')
    (root / 'npm').mkdir(parents=True, exist_ok=True)
    (root / 'xdg').mkdir(parents=True, exist_ok=True)
    return root


def _is_within_home_root(candidate: Path, managed_home: Path) -> bool:
    normalized_candidate = _normalize_path(candidate)
    normalized_managed = _normalize_path(managed_home)
    if normalized_candidate is None or normalized_managed is None:
        return False
    try:
        normalized_candidate.relative_to(normalized_managed)
        return True
    except Exception:
        return False


def _normalize_path(value: object) -> Path | None:
    try:
        return Path(value).expanduser().resolve()
    except Exception:
        try:
            return Path(value).expanduser()
        except Exception:
            return None


def _prepare_managed_home(layout: GeminiHomeLayout, *, source_home: Path) -> None:
    ensure_private_inheritance_directory(layout.home_root, source_home)
    ensure_private_directory(layout.gemini_dir)
    ensure_private_directory(layout.tmp_root)
    _ensure_json_file(layout.settings_path)
    _ensure_json_file(layout.trusted_folders_path)


def _ensure_json_file(path: Path) -> None:
    if path.exists() and not path.is_symlink():
        return
    atomic_write_text(path, '{}\n')


def materialize_gemini_home_config(
    target_home: Path,
    *,
    profile=None,
    source_home: Path | None = None,
    project_root: Path | None = None,
    agent_name: str | None = None,
    workspace_path: Path | None = None,
    memory_projection_event_path: Path | None = None,
    memory_projection_marker_path: Path | None = None,
    command_policy=None,
) -> GeminiHomeLayout:
    layout = gemini_layout_for_home(target_home)
    inherit_external_keyring = (
        source_home is None
        and not os.environ.get('CCB_SOURCE_HOME')
    )
    source_root = Path(source_home).expanduser() if source_home is not None else _system_home_root()
    source_settings = _read_source_json_object(
        source_root / '.gemini' / 'settings.json',
        label='Gemini settings',
        enabled=bool(_inherits_config(profile) or _inherits_api(profile) or _inherits_external_auth(profile)),
    )
    source_env = _read_source_env_file(
        source_root / '.gemini' / '.env',
        enabled=_inherits_api(profile),
    )
    auth_snapshot = _snapshot_gemini_auth_sources(
        source_root,
        source_settings=source_settings,
        profile=profile,
    )
    keyring_result = _read_external_keyring_oauth(
        enabled=(
            inherit_external_keyring
            and _inherits_external_auth(profile)
            and _selected_auth_type(source_settings) == 'oauth-personal'
            and not any(value is not None for value in auth_snapshot.values())
        )
    )
    _prepare_managed_home(layout, source_home=source_root)
    previous_auth_projection = _read_gemini_auth_projection(layout)
    projected_env_keys = _materialize_env_file(
        source_env,
        layout,
        profile=profile,
        previous_projection=previous_auth_projection,
    )
    _materialize_auth(
        source_root,
        layout,
        profile=profile,
        source_settings=source_settings,
        source_snapshot=auth_snapshot,
        keyring_result=keyring_result,
        previous_projection=previous_auth_projection,
        projected_env_keys=projected_env_keys,
    )
    _materialize_settings(
        source_settings,
        layout,
        profile=profile,
        previous_projection=previous_auth_projection,
    )
    _materialize_trusted_folders(source_root, layout)
    seed_projected_tree(
        source_root / '.gemini' / 'extensions',
        layout.gemini_dir / 'extensions',
        enabled=(
            _inherits_config(profile)
            and not role_command_policy_disables_inherited_assets(command_policy)
        ),
        label=_GEMINI_EXTENSIONS_PROJECTION_LABEL,
    )
    materialize_required_control_skills(
        provider='gemini',
        target_dir=layout.gemini_dir / 'skills',
    )
    memory_result = _materialize_gemini_memory(
        source_root,
        layout,
        profile=profile,
        project_root=project_root,
        agent_name=agent_name,
        workspace_path=workspace_path,
    )
    record_memory_projection_event(
        memory_result,
        provider='gemini',
        event_path=memory_projection_event_path,
        marker_path=memory_projection_marker_path,
        agent_name=agent_name,
    )
    return layout


def _materialize_settings(
    source_payload: dict[str, object],
    layout: GeminiHomeLayout,
    *,
    profile,
    previous_projection: dict[str, object],
) -> None:
    projected = _projected_settings_payload(source_payload, profile=profile)
    existing = _read_json_object(layout.settings_path)
    merged = _merge_settings_payload(projected, existing=existing)
    if merged is None:
        return
    if (
        _selected_auth_type(merged) is None
        and _selected_auth_type(existing) is not None
        and _manifest_projected_selected_type(previous_projection) is None
    ):
        _set_selected_auth_type(merged, _selected_auth_type(existing) or '')
    _write_json_object(layout.settings_path, merged)


def _materialize_trusted_folders(source_home: Path, layout: GeminiHomeLayout) -> None:
    projected = _read_json_object(source_home / '.gemini' / 'trustedFolders.json')
    existing = _read_json_object(layout.trusted_folders_path)
    merged = _merge_object_payload(projected, existing=existing)
    if merged is None:
        return
    _write_json_object(layout.trusted_folders_path, merged)


def _materialize_env_file(
    source_payload: dict[str, str],
    layout: GeminiHomeLayout,
    *,
    profile,
    previous_projection: dict[str, object],
) -> set[str]:
    target_env = layout.gemini_dir / '.env'
    env_payload = _projected_dotenv_payload(source_payload, profile=profile)
    previous_keys = _manifest_projected_env_keys(previous_projection)
    # Preserve Agent-private values and remove only keys CCB previously
    # projected.  This makes external inheritance one-way without treating a
    # managed home as disposable on every restart.
    existing_payload = _read_env_file(target_env)
    merged = {
        key: value
        for key, value in existing_payload.items()
        if key not in previous_keys
    }
    merged.update(env_payload)
    if merged:
        _write_env_file(target_env, merged)
    elif previous_keys or target_env.is_file():
        _remove_file(target_env)
    return set(env_payload)


def _materialize_auth(
    source_home: Path,
    layout: GeminiHomeLayout,
    *,
    profile,
    source_settings: dict[str, object],
    source_snapshot: dict[str, bytes | None],
    keyring_result: KeyringReadResult,
    previous_projection: dict[str, object],
    projected_env_keys: set[str],
) -> None:
    previous = previous_projection
    previous_files = _manifest_projected_files(previous)
    projected_selected_type = _source_projected_auth_selected_type(
        source_settings,
        profile=profile,
    )
    should_project = _should_project_login_auth(source_settings, profile=profile)
    if not should_project:
        for filename in previous_files:
            _remove_file(layout.gemini_dir / filename)
        _write_gemini_auth_projection(
            layout,
            source_home=source_home,
            projected_files=(),
            keyring_projected=False,
            projected_selected_type=projected_selected_type,
            projected_env_keys=tuple(sorted(projected_env_keys)),
            status=(
                'explicit_api_authority'
                if _profile_has_explicit_credential(profile)
                else 'inherit_auth_disabled'
                if not _inherits_external_auth(profile)
                else 'inherited_api_selection'
                if projected_selected_type is not None
                else 'source_auth_absent'
                if (
                    previous_files
                    or _manifest_keyring_projected(previous)
                    or _manifest_projected_selected_type(previous) is not None
                )
                else 'agent_private_or_unmanaged'
            ),
        )
        return

    intended = {
        filename: content
        for filename, content in source_snapshot.items()
        if content is not None
    }
    keyring_projected = False
    if not intended:
        if keyring_result.status == 'present':
            payload = _gemini_keyring_oauth_payload(keyring_result.value)
            if payload is None:
                raise RuntimeError('cannot parse external Gemini keyring credential')
            intended['oauth_creds.json'] = (
                json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
            ).encode('utf-8')
            keyring_projected = True
        elif (
            keyring_result.status in {'error', 'unavailable'}
            and _manifest_keyring_projected(previous)
        ):
            raise RuntimeError(
                'cannot determine external Gemini keyring login state: '
                + (keyring_result.detail or keyring_result.status)
            )
    projected = set(intended)
    for filename in _GEMINI_LOGIN_AUTH_FILENAMES:
        target = layout.gemini_dir / filename
        content = intended.get(filename)
        if content is not None:
            _write_private_bytes(target, content)
        elif filename in previous_files:
            _remove_file(target)

    _write_gemini_auth_projection(
        layout,
        source_home=source_home,
        projected_files=tuple(sorted(projected)),
        keyring_projected=keyring_projected,
        projected_selected_type=projected_selected_type,
        projected_env_keys=tuple(sorted(projected_env_keys)),
        status=(
            'inherited_auth'
            if projected
            else 'source_auth_absent'
            if previous_files or _manifest_keyring_projected(previous)
            else 'agent_private_or_unmanaged'
        ),
    )


def _read_external_keyring_oauth(*, enabled: bool) -> KeyringReadResult:
    if not enabled:
        return KeyringReadResult('unavailable', detail='keyring inheritance is not selected')
    return read_keyring_password_state(
        'gemini-cli-oauth',
        'main-account',
        command_name='gemini',
        module_names=('@github/keytar', 'keytar'),
    )


def _gemini_keyring_oauth_payload(raw: str | None) -> dict[str, object] | None:
    if not raw:
        return None
    try:
        stored = json.loads(raw)
    except Exception:
        return None
    if not isinstance(stored, dict):
        return None
    token = stored.get('token')
    if not isinstance(token, dict):
        return None
    access_token = token.get('accessToken')
    refresh_token = token.get('refreshToken')
    if not str(access_token or '').strip() and not str(refresh_token or '').strip():
        return None
    field_map = (
        ('accessToken', 'access_token'),
        ('refreshToken', 'refresh_token'),
        ('tokenType', 'token_type'),
        ('scope', 'scope'),
        ('expiresAt', 'expiry_date'),
    )
    payload = {
        target: token[source]
        for source, target in field_map
        if token.get(source) is not None
    }
    return payload or None


def _projected_settings_payload(
    source_payload: dict[str, object],
    *,
    profile,
) -> dict[str, object] | None:
    if not source_payload:
        payload: dict[str, object] = {}
        explicit_selected_type = _explicit_auth_selected_type(profile)
        if explicit_selected_type is not None:
            _set_selected_auth_type(payload, explicit_selected_type)
        return _disable_managed_gemini_autoupdate(payload)

    source_env = dict(source_payload.get('env') or {}) if isinstance(source_payload.get('env'), dict) else {}
    env_payload = dict(source_env) if _inherits_config(profile) else {}
    for key in provider_api_env_keys('gemini'):
        env_payload.pop(key, None)
    if _inherits_api(profile):
        inherited_keys = provider_api_env_keys('gemini') - _explicit_api_owned_names(profile)
        for key in inherited_keys:
            value = source_env.get(key)
            if value is not None:
                env_payload[key] = value

    payload: dict[str, object] = dict(source_payload) if _inherits_config(profile) else {}
    explicit_selected_type = _explicit_auth_selected_type(profile)
    projected_selected_type = explicit_selected_type or _projected_auth_selected_type(
        _selected_auth_type(source_payload),
        profile=profile,
    )
    if projected_selected_type is not None:
        _set_selected_auth_type(payload, projected_selected_type)
    else:
        _clear_selected_auth_type(payload)
    if env_payload:
        payload['env'] = env_payload
    else:
        payload.pop('env', None)
    return _disable_managed_gemini_autoupdate(payload)


def _disable_managed_gemini_autoupdate(payload: dict[str, object]) -> dict[str, object]:
    managed = dict(payload)
    raw_general = managed.get('general')
    general = dict(raw_general) if isinstance(raw_general, dict) else {}
    general['enableAutoUpdate'] = False
    general['enableAutoUpdateNotification'] = False
    managed['general'] = general
    return managed


def _merge_settings_payload(
    projected: dict[str, object] | None,
    *,
    existing: dict[str, object] | None,
) -> dict[str, object] | None:
    projected_payload = dict(projected or {})
    existing_payload = dict(existing or {})
    merged = dict(projected_payload)
    hooks = existing_payload.get('hooks')
    if hooks is not None:
        merged['hooks'] = hooks
    context_file_name = existing_payload.get('contextFileName')
    if context_file_name is not None:
        merged['contextFileName'] = context_file_name
    if merged:
        return merged
    if existing_payload:
        return existing_payload
    return None


def _merge_object_payload(
    projected: dict[str, object] | None,
    *,
    existing: dict[str, object] | None,
) -> dict[str, object] | None:
    merged = dict(projected or {})
    merged.update(dict(existing or {}))
    return merged if merged else None


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _read_source_json_object(
    path: Path,
    *,
    label: str,
    enabled: bool,
) -> dict[str, object]:
    if not enabled:
        return {}
    source = Path(path).expanduser()
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError(f'cannot inspect inherited {label} source: {source}: {exc}') from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f'inherited {label} source must be a regular file: {source}')
    try:
        payload = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RuntimeError(f'cannot read inherited {label} source: {source}: {exc}') from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f'inherited {label} source must contain an object: {source}')
    return payload


def _write_json_object(path: Path, payload: dict[str, object]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


def _projected_dotenv_payload(
    source_payload: dict[str, str],
    *,
    profile,
) -> dict[str, str]:
    if not _inherits_api(profile):
        return {}
    if not source_payload:
        return {}
    allowed = provider_api_env_keys('gemini') - _explicit_api_owned_names(profile)
    return {
        key: value
        for key, value in source_payload.items()
        if key in allowed and str(value).strip()
    }


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except Exception:
        return {}
    payload: dict[str, str] = {}
    for line in lines:
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        payload[key] = value
    return payload


def _read_source_env_file(path: Path, *, enabled: bool) -> dict[str, str]:
    if not enabled:
        return {}
    source = Path(path).expanduser()
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError(
            f'cannot inspect inherited Gemini environment source: {source}: {exc}'
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(
            f'inherited Gemini environment source must be a regular file: {source}'
        )
    try:
        lines = source.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            f'cannot read inherited Gemini environment source: {source}: {exc}'
        ) from exc
    payload: dict[str, str] = {}
    for line in lines:
        parsed = _parse_env_line(line)
        if parsed is not None:
            payload[parsed[0]] = parsed[1]
    return payload


def _parse_env_line(line: str) -> tuple[str, str] | None:
    raw = str(line or '').strip()
    if not raw or raw.startswith('#'):
        return None
    if raw.startswith('export '):
        raw = raw[len('export ') :].lstrip()
    if '=' not in raw:
        return None
    key, value = raw.split('=', 1)
    key = key.strip()
    if not _is_env_key(key):
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return key, value


def _is_env_key(value: str) -> bool:
    if not value:
        return False
    first = value[0]
    if not (first == '_' or first.isalpha()):
        return False
    return all(ch == '_' or ch.isalnum() for ch in value)


def _write_env_file(path: Path, payload: dict[str, str]) -> None:
    lines = [f'{key}={_quote_env_value(value)}' for key, value in sorted(payload.items())]
    atomic_write_text(path, '\n'.join(lines) + '\n')
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _quote_env_value(value: object) -> str:
    raw = str(value)
    escaped = raw.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return f'"{escaped}"'


def _inherits_api(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_api', True))


def _inherits_auth(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_auth', True))


def _inherits_external_auth(profile) -> bool:
    return _inherits_auth(profile) and not _profile_has_explicit_credential(profile)


def _profile_has_explicit_credential(profile) -> bool:
    env = _profile_env(profile)
    return any(str(env.get(key) or '').strip() for key in _GEMINI_CREDENTIAL_ENV_KEYS)


def _explicit_auth_selected_type(profile) -> str | None:
    env = _profile_env(profile)
    if any(str(env.get(key) or '').strip() for key in ('GEMINI_API_KEY', 'GOOGLE_API_KEY')):
        return 'gemini-api-key'
    if str(env.get('GOOGLE_APPLICATION_CREDENTIALS') or '').strip():
        return 'compute-default-credentials'
    return None


def _explicit_api_owned_names(profile) -> set[str]:
    return explicit_api_owned_names(_profile_env(profile))


def _profile_env(profile) -> dict[str, str]:
    if profile is None:
        return {}
    return {
        str(key): str(value)
        for key, value in dict(getattr(profile, 'env', {}) or {}).items()
        if str(value).strip()
    }


def _inherits_config(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_config', True))


def _inherits_memory(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_memory', True))


def _materialize_gemini_memory(
    source_home: Path,
    layout: GeminiHomeLayout,
    *,
    profile,
    project_root: Path | None,
    agent_name: str | None,
    workspace_path: Path | None,
) -> dict[str, object]:
    target = layout.gemini_dir / 'GEMINI.md'
    if not _inherits_memory(profile):
        _remove_file(target)
        _clear_context_file_name(layout)
        return memory_projection_result(
            status='skipped',
            reason='inherit_memory_disabled',
            path=target,
        )
    if project_root is None or agent_name is None:
        return memory_projection_result(
            status='failed',
            reason='missing_project_context',
            path=target,
        )
    result = materialize_provider_memory_file(
        project_root=project_root,
        agent_name=agent_name,
        provider='gemini',
        target=target,
        provider_memory_path=source_home / '.gemini' / 'GEMINI.md',
        provider_memory_title='Provider User Memory',
        workspace_path=workspace_path,
    )
    if result.get('status') in {'ok', 'skipped'}:
        _ensure_context_file_name(layout)
    return result

def _ensure_context_file_name(layout: GeminiHomeLayout) -> None:
    payload = _read_json_object(layout.settings_path) or {}
    if payload.get('contextFileName') == 'GEMINI.md':
        return
    payload['contextFileName'] = 'GEMINI.md'
    _write_json_object(layout.settings_path, payload)


def _clear_context_file_name(layout: GeminiHomeLayout) -> None:
    payload = _read_json_object(layout.settings_path) or {}
    if payload.get('contextFileName') != 'GEMINI.md':
        return
    payload.pop('contextFileName', None)
    _write_json_object(layout.settings_path, payload)

def _should_project_login_auth(
    source_settings: dict[str, object],
    *,
    profile,
) -> bool:
    if not _inherits_external_auth(profile):
        return False
    selected_type = _selected_auth_type(source_settings)
    return selected_type in {'oauth-personal'}


def _snapshot_gemini_auth_sources(
    source_home: Path,
    *,
    source_settings: dict[str, object],
    profile,
) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {
        filename: None for filename in _GEMINI_LOGIN_AUTH_FILENAMES
    }
    if not _should_project_login_auth(source_settings, profile=profile):
        return snapshot
    for filename in _GEMINI_LOGIN_AUTH_FILENAMES:
        source = Path(source_home).expanduser() / '.gemini' / filename
        try:
            metadata = source.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(
                f'cannot inspect inherited Gemini auth source: {source}: {exc}'
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(
                f'inherited Gemini auth source must be a regular file: {source}'
            )
        try:
            snapshot[filename] = source.read_bytes()
        except OSError as exc:
            raise RuntimeError(
                f'cannot read inherited Gemini auth source: {source}: {exc}'
            ) from exc
    return snapshot


def _selected_auth_type(payload: dict[str, object] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    security = payload.get('security')
    if not isinstance(security, dict):
        return None
    auth = security.get('auth')
    if not isinstance(auth, dict):
        return None
    raw = str(auth.get('selectedType') or '').strip()
    return raw or None


def _projected_auth_selected_type(selected_type: str | None, *, profile) -> str | None:
    normalized = str(selected_type or '').strip()
    if not normalized:
        return None
    if normalized in {'oauth-personal', 'compute-default-credentials'}:
        return normalized if _inherits_auth(profile) else None
    if normalized in {'gemini-api-key', 'vertex-ai'}:
        return normalized if _inherits_api(profile) else None
    return normalized if (_inherits_api(profile) or _inherits_auth(profile)) else None


def _source_projected_auth_selected_type(
    source_settings: dict[str, object],
    *,
    profile,
) -> str | None:
    if _explicit_auth_selected_type(profile) is not None:
        return None
    return _projected_auth_selected_type(
        _selected_auth_type(source_settings),
        profile=profile,
    )


def _set_selected_auth_type(payload: dict[str, object], selected_type: str) -> None:
    security = payload.get('security')
    if not isinstance(security, dict):
        security = {}
    auth = security.get('auth')
    if not isinstance(auth, dict):
        auth = {}
    auth['selectedType'] = selected_type
    security['auth'] = auth
    payload['security'] = security


def _clear_selected_auth_type(payload: dict[str, object]) -> None:
    security = payload.get('security')
    if not isinstance(security, dict):
        return
    auth = security.get('auth')
    if isinstance(auth, dict):
        auth.pop('selectedType', None)
        if auth:
            security['auth'] = auth
        else:
            security.pop('auth', None)
    if security:
        payload['security'] = security
    else:
        payload.pop('security', None)


def _write_private_bytes(path: Path, content: bytes) -> None:
    target = Path(path)
    ensure_private_directory(target.parent)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f'.{target.name}.',
        suffix='.tmp',
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            fd = -1
            handle.write(bytes(content))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_gemini_auth_projection(layout: GeminiHomeLayout) -> dict[str, object]:
    return _read_json_object(layout.home_root / _GEMINI_AUTH_PROJECTION_MANIFEST) or {}


def _valid_gemini_auth_projection(payload: dict[str, object]) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get('schema_version') == 1
        and payload.get('record_type') == 'ccb_gemini_auth_projection'
    )


def _manifest_projected_files(payload: dict[str, object]) -> set[str]:
    if not _valid_gemini_auth_projection(payload):
        return set()
    raw = payload.get('projected_files')
    if not isinstance(raw, list):
        return set()
    allowed = set(_GEMINI_LOGIN_AUTH_FILENAMES)
    return {
        str(item).strip()
        for item in raw
        if str(item).strip() in allowed
    }


def _manifest_keyring_projected(payload: dict[str, object]) -> bool:
    return bool(
        _valid_gemini_auth_projection(payload)
        and payload.get('keyring_projected') is True
    )


def _manifest_projected_selected_type(payload: dict[str, object]) -> str | None:
    if not _valid_gemini_auth_projection(payload):
        return None
    value = str(payload.get('projected_selected_type') or '').strip()
    return value or None


def _manifest_projected_env_keys(payload: dict[str, object]) -> set[str]:
    if not _valid_gemini_auth_projection(payload):
        return set()
    raw = payload.get('projected_env_keys')
    if not isinstance(raw, list):
        return set()
    allowed = provider_api_env_keys('gemini')
    return {
        str(item).strip()
        for item in raw
        if str(item).strip() in allowed
    }


def _write_gemini_auth_projection(
    layout: GeminiHomeLayout,
    *,
    source_home: Path,
    projected_files: tuple[str, ...],
    keyring_projected: bool,
    projected_selected_type: str | None,
    projected_env_keys: tuple[str, ...] = (),
    status: str,
) -> None:
    payload = {
        'schema_version': 1,
        'record_type': 'ccb_gemini_auth_projection',
        'status': str(status),
        'source_home': str(Path(source_home).expanduser()),
        'projected_files': list(projected_files),
        'keyring_projected': bool(keyring_projected),
        'projected_selected_type': projected_selected_type,
        'projected_env_keys': list(projected_env_keys),
    }
    path = layout.home_root / _GEMINI_AUTH_PROJECTION_MANIFEST
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + '\n',
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except Exception:
        return


def _system_home_root() -> Path:
    return current_provider_source_home()


__all__ = [
    'materialize_gemini_home_config',
    'prepare_gemini_home_overrides',
    'resolve_gemini_home_layout',
]
