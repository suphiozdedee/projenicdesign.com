from __future__ import annotations

from typing import Any

from agents.models import ProviderProfileSpec, SkillOverlaySpec

from ..common import ALLOWED_PROVIDER_PROFILE_KEYS, ConfigValidationError
from .expectations import expect_bool, expect_mapping, expect_string, expect_string_list, expect_string_mapping


def parse_provider_profile(agent_name: str, value: Any) -> ProviderProfileSpec:
    raw = expect_mapping(value, field_name=f'agents.{agent_name}.provider_profile')
    unknown = sorted(set(raw) - ALLOWED_PROVIDER_PROFILE_KEYS)
    if unknown:
        raise ConfigValidationError(
            f'agents.{agent_name}.provider_profile contains unknown fields: {", ".join(unknown)}'
        )
    try:
        return ProviderProfileSpec(
            mode=(
                expect_string(raw['mode'], field_name=f'agents.{agent_name}.provider_profile.mode')
                if raw.get('mode') is not None
                else 'inherit'
            ),
            home=(
                expect_string(raw['home'], field_name=f'agents.{agent_name}.provider_profile.home')
                if raw.get('home') is not None
                else None
            ),
            env=expect_string_mapping(
                raw.get('env', {}),
                field_name=f'agents.{agent_name}.provider_profile.env',
            ),
            mcp_servers=expect_mapping(
                raw.get('mcp_servers', {}),
                field_name=f'agents.{agent_name}.provider_profile.mcp_servers',
            ),
            plugins=expect_mapping(
                raw.get('plugins', {}),
                field_name=f'agents.{agent_name}.provider_profile.plugins',
            ),
            inherited_skill_include=expect_string_list(
                raw.get('inherited_skill_include', []),
                field_name=f'agents.{agent_name}.provider_profile.inherited_skill_include',
            ),
            inherited_skill_exclude=expect_string_list(
                raw.get('inherited_skill_exclude', []),
                field_name=f'agents.{agent_name}.provider_profile.inherited_skill_exclude',
            ),
            skill_overlays=_parse_skill_overlays(
                agent_name,
                raw.get('skill_overlays', {}),
            ),
            inherit_api=(
                expect_bool(raw['inherit_api'], field_name=f'agents.{agent_name}.provider_profile.inherit_api')
                if 'inherit_api' in raw
                else True
            ),
            inherit_auth=(
                expect_bool(raw['inherit_auth'], field_name=f'agents.{agent_name}.provider_profile.inherit_auth')
                if 'inherit_auth' in raw
                else True
            ),
            inherit_config=(
                expect_bool(raw['inherit_config'], field_name=f'agents.{agent_name}.provider_profile.inherit_config')
                if 'inherit_config' in raw
                else True
            ),
            inherit_skills=(
                expect_bool(raw['inherit_skills'], field_name=f'agents.{agent_name}.provider_profile.inherit_skills')
                if 'inherit_skills' in raw
                else True
            ),
            inherit_commands=(
                expect_bool(
                    raw['inherit_commands'],
                    field_name=f'agents.{agent_name}.provider_profile.inherit_commands',
                )
                if 'inherit_commands' in raw
                else True
            ),
            inherit_memory=(
                expect_bool(raw['inherit_memory'], field_name=f'agents.{agent_name}.provider_profile.inherit_memory')
                if 'inherit_memory' in raw
                else True
            ),
        )
    except ValueError as exc:
        raise ConfigValidationError(f'agents.{agent_name}.provider_profile: {exc}') from exc


def _parse_skill_overlays(agent_name: str, value: Any) -> dict[str, SkillOverlaySpec]:
    raw = expect_mapping(value, field_name=f'agents.{agent_name}.provider_profile.skill_overlays')
    overlays: dict[str, SkillOverlaySpec] = {}
    for raw_name, raw_payload in raw.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ConfigValidationError(
                f'agents.{agent_name}.provider_profile.skill_overlays keys must be non-empty strings'
            )
        name = raw_name.strip()
        payload = expect_mapping(
            raw_payload,
            field_name=f'agents.{agent_name}.provider_profile.skill_overlays.{name}',
        )
        unknown = sorted(set(payload) - {'source', 'include', 'exclude'})
        if unknown:
            raise ConfigValidationError(
                f'agents.{agent_name}.provider_profile.skill_overlays.{name} contains unknown fields: '
                f'{", ".join(unknown)}'
            )
        if payload.get('source') is None:
            raise ConfigValidationError(
                f'agents.{agent_name}.provider_profile.skill_overlays.{name}.source is required'
            )
        overlays[name] = SkillOverlaySpec(
            source=expect_string(
                payload['source'],
                field_name=f'agents.{agent_name}.provider_profile.skill_overlays.{name}.source',
            ),
            include=expect_string_list(
                payload.get('include', ['*']),
                field_name=f'agents.{agent_name}.provider_profile.skill_overlays.{name}.include',
            ),
            exclude=expect_string_list(
                payload.get('exclude', []),
                field_name=f'agents.{agent_name}.provider_profile.skill_overlays.{name}.exclude',
            ),
        )
    return overlays


__all__ = ['parse_provider_profile']
