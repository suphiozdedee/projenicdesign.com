from __future__ import annotations

import base64
import os
from pathlib import Path

from cli.services.role_command_policy import role_command_policy_disables_inherited_assets
from provider_core.projected_assets import (
    projected_path_is_owned,
    remove_projected_path,
    seed_projected_tree,
)
from provider_core.projected_settings import (
    project_json_mapping_fields,
    rebase_json_path_fields,
)
from provider_core.keyring_read import read_keyring_password
from provider_core.one_way_inheritance import (
    copy_regular_file,
    ensure_private_directory,
    ensure_private_inheritance_directory,
)
from provider_core.inherited_skills import (
    materialize_required_control_skills,
    required_control_skill_names,
    route_inherited_skill_entries,
)
from provider_core.source_home import current_provider_source_home
from storage.atomic import atomic_write_text

_DROID_SKILLS_PROJECTION_LABEL = 'droid-inherited-skills'
_DROID_PLUGINS_PROJECTION_LABEL = 'droid-inherited-plugins'
_DROID_PLUGIN_SETTINGS_PROJECTION_LABEL = 'droid-inherited-plugin-settings'
_DROID_PLUGIN_PATH_KEYS = frozenset({'installLocation', 'installPath'})


def managed_droid_home_for_runtime(runtime_dir: Path) -> Path:
    runtime_dir = Path(runtime_dir).expanduser()
    if runtime_dir.parent.name == 'provider-runtime':
        return runtime_dir.parent.parent / 'provider-state' / 'droid' / 'home'
    return runtime_dir / 'droid-home'


def materialize_droid_home_config(
    target_home: Path,
    *,
    profile=None,
    source_home: Path | None = None,
    command_policy=None,
) -> Path:
    target_home = Path(target_home).expanduser()
    inherit_external_keyring = source_home is None and not os.environ.get('CCB_SOURCE_HOME')
    source_home = Path(source_home).expanduser() if source_home is not None else _system_factory_home()
    target_home = ensure_private_inheritance_directory(target_home, source_home)
    ensure_private_directory(target_home / 'sessions')
    if _inherits_auth(profile):
        for filename in (
            'auth.encrypted',
            'auth.v2.file',
            'auth.v2.key',
            'credentials.json',
        ):
            copy_regular_file(source_home / filename, target_home / filename)
        if inherit_external_keyring:
            _materialize_keyring_v2_as_private_keyfile(source_home, target_home)
    route_inherited_skill_entries(
        source_home / 'skills',
        target_home / 'skills',
        enabled=_inherits_skills(profile),
        label=_DROID_SKILLS_PROJECTION_LABEL,
        exclude=required_control_skill_names('droid'),
    )
    materialize_required_control_skills(
        provider='droid',
        target_dir=target_home / 'skills',
    )
    inherited_plugins_enabled = (
        _inherits_config(profile)
        and not role_command_policy_disables_inherited_assets(command_policy)
    )
    source_plugins = source_home / 'plugins'
    target_plugins = target_home / 'plugins'
    seeded = seed_projected_tree(
        source_plugins,
        target_plugins,
        enabled=inherited_plugins_enabled,
        label=_DROID_PLUGINS_PROJECTION_LABEL,
    )
    plugins_owned = projected_path_is_owned(
        target_plugins,
        label=_DROID_PLUGINS_PROJECTION_LABEL,
    )
    plugins_available = (
        inherited_plugins_enabled
        and target_plugins.is_dir()
        and (seeded or plugins_owned)
    )
    if plugins_available and not rebase_json_path_fields(
        (target_plugins / 'installed_plugins.json', target_plugins / 'known_marketplaces.json'),
        source_root=source_plugins,
        target_root=target_plugins,
        fields=_DROID_PLUGIN_PATH_KEYS,
    ):
        remove_projected_path(target_plugins, label=_DROID_PLUGINS_PROJECTION_LABEL)
        plugins_available = False
    project_json_mapping_fields(
        source_home / 'settings.json',
        target_home / 'settings.json',
        fields=('enabledPlugins',),
        enabled=plugins_available,
        label=_DROID_PLUGIN_SETTINGS_PROJECTION_LABEL,
        marker_path=target_home / '.ccb-plugin-settings-projection.json',
    )
    return target_home


def _system_factory_home() -> Path:
    if os.environ.get('CCB_SOURCE_HOME'):
        return current_provider_source_home() / '.factory'
    # Current Factory/Droid releases interpret FACTORY_HOME_OVERRIDE as an OS
    # home root and append ".factory" themselves.  It therefore differs from
    # the older FACTORY_HOME/FACTORY_ROOT variables, which identify the
    # Factory state directory directly.
    override = str(os.environ.get('FACTORY_HOME_OVERRIDE') or '').strip()
    if override:
        candidate = Path(override).expanduser()
        if not _looks_like_ccb_provider_home(candidate):
            return candidate / '.factory'
    for name in ('FACTORY_HOME', 'FACTORY_ROOT'):
        raw = str(os.environ.get(name) or '').strip()
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if not _looks_like_ccb_provider_home(candidate):
            return candidate
    return current_provider_source_home() / '.factory'


def _materialize_keyring_v2_as_private_keyfile(
    source_home: Path,
    target_home: Path,
) -> None:
    target_ciphertext = target_home / 'auth.v2.file'
    target_key = target_home / 'auth.v2.key'
    if target_ciphertext.is_file() and target_key.is_file():
        return
    source_options = (
        (source_home / 'auth.v2.keyring', 'auth-encryption-key'),
        (
            source_home / 'auth.v2.loginkeychain',
            'auth-encryption-key-security-cli',
        ),
    )
    selected = next(
        (
            (ciphertext, account)
            for ciphertext, account in source_options
            if ciphertext.is_file() and not ciphertext.is_symlink()
        ),
        None,
    )
    if selected is None:
        return
    source_ciphertext, account = selected
    if source_home.name == '.factory-dev':
        account = f'{account}-dev'
    extra_module_paths = [source_home / 'bin' / 'keytar.node']
    configured_keytar_path = str(
        os.environ.get('FACTORY_KEYTAR_PATH') or ''
    ).strip()
    if configured_keytar_path:
        extra_module_paths.append(Path(configured_keytar_path).expanduser())
    key_text = read_keyring_password(
        'Factory CLI',
        account,
        command_name='droid',
        module_names=('keytar', '@github/keytar'),
        extra_module_paths=tuple(extra_module_paths),
    )
    if not _valid_factory_key_text(key_text):
        return
    if not copy_regular_file(source_ciphertext, target_ciphertext):
        return
    atomic_write_text(target_key, f'{str(key_text).strip()}\n')


def _valid_factory_key_text(value: str | None) -> bool:
    if not value:
        return False
    try:
        return len(base64.b64decode(value.strip(), validate=True)) == 32
    except Exception:
        return False


def _inherits_skills(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_skills', True))


def _inherits_config(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_config', True))


def _inherits_auth(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_auth', True))


def _looks_like_ccb_provider_home(path: Path) -> bool:
    parts = Path(path).expanduser().parts
    for index in range(0, max(len(parts) - 4, 0)):
        if parts[index] != 'agents':
            continue
        if parts[index + 2] == 'provider-state' and parts[index + 4] == 'home':
            return True
    return False


__all__ = ['managed_droid_home_for_runtime', 'materialize_droid_home_config']
