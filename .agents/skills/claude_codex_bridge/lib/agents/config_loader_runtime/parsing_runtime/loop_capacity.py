from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.config_loader_runtime.role_lookup import RoleLookupError, load_installed_role_manifest
from agents.models import AgentValidationError, LoopCapacityConfig, LoopRoleProfileSpec, ProviderProfileSpec, WorkspaceMode

from ..common import ConfigValidationError
from .expectations import expect_bool, expect_mapping, expect_string, expect_string_list
from .provider_profiles import parse_provider_profile

_LOOP_TOP_LEVEL_KEYS = {'capacity', 'role_profiles'}
_LOOP_CAPACITY_KEYS = {'enabled', 'max_nodes', 'default_lifetime', 'name_template', 'reuse'}
_LOOP_ROLE_PROFILE_KEYS = {
    'role',
    'provider',
    'model',
    'thinking',
    'workspace_mode',
    'workspace_group',
    'startup_args',
    'provider_profile',
    'max_instances',
    'reuse',
}


def parse_loop_capacity(raw_loop: Any, *, project_root: Path | None = None) -> LoopCapacityConfig:
    if raw_loop is None:
        return LoopCapacityConfig()
    loop = expect_mapping(raw_loop, field_name='loop')
    unknown_loop = sorted(set(loop) - _LOOP_TOP_LEVEL_KEYS)
    if unknown_loop:
        raise ConfigValidationError(f'loop contains unknown fields: {", ".join(unknown_loop)}')
    capacity = _parse_capacity(loop.get('capacity'))
    role_profiles = _parse_role_profiles(loop.get('role_profiles'), project_root=project_root)
    try:
        return LoopCapacityConfig(
            role_profiles=role_profiles,
            **capacity,
        )
    except AgentValidationError as exc:
        raise ConfigValidationError(str(exc)) from exc


def _parse_capacity(raw_capacity: Any) -> dict[str, object]:
    if raw_capacity is None:
        return {}
    capacity = expect_mapping(raw_capacity, field_name='loop.capacity')
    unknown_capacity = sorted(set(capacity) - _LOOP_CAPACITY_KEYS)
    if unknown_capacity:
        raise ConfigValidationError(
            f'loop.capacity contains unknown fields: {", ".join(unknown_capacity)}'
        )
    parsed: dict[str, object] = {}
    if 'enabled' in capacity:
        parsed['enabled'] = expect_bool(capacity['enabled'], field_name='loop.capacity.enabled')
    if 'max_nodes' in capacity:
        parsed['max_nodes'] = _expect_positive_int(capacity['max_nodes'], field_name='loop.capacity.max_nodes')
    if 'default_lifetime' in capacity:
        parsed['default_lifetime'] = expect_string(
            capacity['default_lifetime'],
            field_name='loop.capacity.default_lifetime',
        )
    if 'name_template' in capacity:
        parsed['name_template'] = expect_string(
            capacity['name_template'],
            field_name='loop.capacity.name_template',
        )
    if 'reuse' in capacity:
        parsed['reuse'] = expect_string(capacity['reuse'], field_name='loop.capacity.reuse')
    return parsed


def _parse_role_profiles(raw_profiles: Any, *, project_root: Path | None = None) -> dict[str, LoopRoleProfileSpec]:
    if raw_profiles is None:
        return {}
    profiles = expect_mapping(raw_profiles, field_name='loop.role_profiles')
    parsed: dict[str, LoopRoleProfileSpec] = {}
    for raw_name, raw_profile in profiles.items():
        if not isinstance(raw_name, str):
            raise ConfigValidationError('loop.role_profiles keys must be strings')
        profile = expect_mapping(raw_profile, field_name=f'loop.role_profiles.{raw_name}')
        unknown_profile = sorted(set(profile) - _LOOP_ROLE_PROFILE_KEYS)
        if unknown_profile:
            raise ConfigValidationError(
                f'loop.role_profiles.{raw_name} contains unknown fields: '
                + ', '.join(unknown_profile)
            )
        role = expect_string(profile.get('role'), field_name=f'loop.role_profiles.{raw_name}.role')
        try:
            load_installed_role_manifest(role, project_root=project_root)
        except RoleLookupError as exc:
            raise ConfigValidationError(str(exc)) from exc
        try:
            parsed[raw_name] = LoopRoleProfileSpec(
                role=role,
                provider=expect_string(profile.get('provider'), field_name=f'loop.role_profiles.{raw_name}.provider'),
                model=(
                    expect_string(profile['model'], field_name=f'loop.role_profiles.{raw_name}.model')
                    if profile.get('model') is not None
                    else None
                ),
                thinking=(
                    expect_string(profile['thinking'], field_name=f'loop.role_profiles.{raw_name}.thinking')
                    if profile.get('thinking') is not None
                    else None
                ),
                workspace_mode=WorkspaceMode(
                    expect_string(
                        profile.get('workspace_mode', WorkspaceMode.INPLACE.value),
                        field_name=f'loop.role_profiles.{raw_name}.workspace_mode',
                    )
                ),
                workspace_group=(
                    expect_string(
                        profile['workspace_group'],
                        field_name=f'loop.role_profiles.{raw_name}.workspace_group',
                    )
                    if profile.get('workspace_group') is not None
                    else None
                ),
                startup_args=expect_string_list(
                    profile.get('startup_args', []),
                    field_name=f'loop.role_profiles.{raw_name}.startup_args',
                ),
                provider_profile=(
                    parse_provider_profile(f'loop.role_profiles.{raw_name}', profile['provider_profile'])
                    if profile.get('provider_profile') is not None
                    else ProviderProfileSpec()
                ),
                max_instances=_expect_positive_int(
                    profile.get('max_instances'),
                    field_name=f'loop.role_profiles.{raw_name}.max_instances',
                ),
                reuse=(
                    expect_string(profile['reuse'], field_name=f'loop.role_profiles.{raw_name}.reuse')
                    if profile.get('reuse') is not None
                    else 'prefer_idle'
                ),
            )
        except AgentValidationError as exc:
            raise ConfigValidationError(str(exc)) from exc
        except ValueError as exc:
            raise ConfigValidationError(f'loop.role_profiles.{raw_name}: {exc}') from exc
    return parsed


def _expect_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ConfigValidationError(f'{field_name} must be a positive integer')
    if not isinstance(value, int):
        raise ConfigValidationError(f'{field_name} must be a positive integer')
    if value <= 0:
        raise ConfigValidationError(f'{field_name} must be a positive integer')
    return int(value)


__all__ = ['parse_loop_capacity']
