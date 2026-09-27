from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from provider_model_shortcuts import provider_model_startup_args, startup_args_contain_model_flag
from provider_profiles.models import ProviderProfileSpec
from role_aliases import canonical_role_id

from ..enums import WorkspaceMode
from ..names import AgentValidationError, normalize_agent_name
from .spec import normalize_provider_profile

LOOP_CAPACITY_LIFETIMES = frozenset({'current_round', 'current_loop', 'manual_release'})
LOOP_CAPACITY_REUSE_POLICIES = frozenset({'prefer_idle', 'always_new', 'pinned'})
LOOP_PROFILE_THINKING_LEVELS = frozenset({'low', 'medium', 'high'})
DEFAULT_LOOP_CAPACITY_LIFETIME = 'current_round'
DEFAULT_LOOP_CAPACITY_NAME_TEMPLATE = 'loop-{loop_id}-{profile}-{index}'
DEFAULT_LOOP_CAPACITY_REUSE = 'prefer_idle'
DEFAULT_LOOP_CAPACITY_MAX_NODES = 4


@dataclass(frozen=True)
class LoopRoleProfileSpec:
    role: str
    provider: str
    max_instances: int
    model: str | None = None
    thinking: str | None = None
    workspace_mode: WorkspaceMode = WorkspaceMode.INPLACE
    workspace_group: str | None = None
    startup_args: tuple[str, ...] = field(default_factory=tuple)
    provider_profile: ProviderProfileSpec = field(default_factory=ProviderProfileSpec)
    reuse: str = DEFAULT_LOOP_CAPACITY_REUSE

    def __post_init__(self) -> None:
        role = _normalize_role_id(self.role, field_name='loop.role_profiles.<profile>.role')
        provider = str(self.provider or '').strip().lower()
        if not provider:
            raise AgentValidationError('loop.role_profiles.<profile>.provider cannot be empty')
        max_instances = _positive_int(
            self.max_instances,
            field_name='loop.role_profiles.<profile>.max_instances',
        )
        model = _optional_non_empty_string(self.model, field_name='loop.role_profiles.<profile>.model')
        thinking = _normalize_thinking(self.thinking)
        workspace_mode = WorkspaceMode(self.workspace_mode)
        workspace_group = _normalize_workspace_group(self.workspace_group, workspace_mode=workspace_mode)
        startup_args = tuple(str(item) for item in self.startup_args)
        provider_profile = normalize_provider_profile(self.provider_profile)
        _validate_model_and_startup_args(provider=provider, model=model, startup_args=startup_args)
        _validate_provider_profile_runtime_home(provider=provider, provider_profile=provider_profile)
        reuse = _normalize_reuse(self.reuse, field_name='loop.role_profiles.<profile>.reuse')

        object.__setattr__(self, 'role', role)
        object.__setattr__(self, 'provider', provider)
        object.__setattr__(self, 'max_instances', max_instances)
        object.__setattr__(self, 'model', model)
        object.__setattr__(self, 'thinking', thinking)
        object.__setattr__(self, 'workspace_mode', workspace_mode)
        object.__setattr__(self, 'workspace_group', workspace_group)
        object.__setattr__(self, 'startup_args', startup_args)
        object.__setattr__(self, 'provider_profile', provider_profile)
        object.__setattr__(self, 'reuse', reuse)

    def to_record(self) -> dict[str, Any]:
        return {
            'role': self.role,
            'provider': self.provider,
            'model': self.model,
            'thinking': self.thinking,
            'workspace_mode': self.workspace_mode.value,
            'workspace_group': self.workspace_group,
            'startup_args': list(self.startup_args),
            'provider_profile': self.provider_profile.to_record(),
            'max_instances': self.max_instances,
            'reuse': self.reuse,
        }


@dataclass(frozen=True)
class LoopCapacityConfig:
    enabled: bool = False
    max_nodes: int = DEFAULT_LOOP_CAPACITY_MAX_NODES
    default_lifetime: str = DEFAULT_LOOP_CAPACITY_LIFETIME
    name_template: str = DEFAULT_LOOP_CAPACITY_NAME_TEMPLATE
    reuse: str = DEFAULT_LOOP_CAPACITY_REUSE
    role_profiles: dict[str, LoopRoleProfileSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise AgentValidationError('loop.capacity.enabled must be a boolean')
        max_nodes = _positive_int(self.max_nodes, field_name='loop.capacity.max_nodes')
        default_lifetime = _normalize_lifetime(self.default_lifetime)
        name_template = str(self.name_template or '').strip()
        if not name_template:
            raise AgentValidationError('loop.capacity.name_template cannot be empty')
        _validate_name_template(name_template)
        reuse = _normalize_reuse(self.reuse, field_name='loop.capacity.reuse')
        profiles: dict[str, LoopRoleProfileSpec] = {}
        for raw_name, raw_profile in dict(self.role_profiles or {}).items():
            profile_name = _normalize_profile_name(raw_name)
            if profile_name in profiles:
                raise AgentValidationError(f'duplicate loop role profile after normalization: {profile_name}')
            if isinstance(raw_profile, LoopRoleProfileSpec):
                profile = raw_profile
            else:
                try:
                    profile = LoopRoleProfileSpec(**dict(raw_profile))
                except Exception as exc:  # pragma: no cover - defensive normalization
                    raise AgentValidationError(f'loop.role_profiles.{profile_name}: {exc}') from exc
            profiles[profile_name] = profile
        if self.enabled and not profiles:
            raise AgentValidationError('loop.capacity.enabled requires at least one loop.role_profiles entry')
        total_profile_limit = sum(profile.max_instances for profile in profiles.values())
        if profiles and max_nodes > total_profile_limit:
            raise AgentValidationError('loop.capacity.max_nodes cannot exceed total loop.role_profiles max_instances')

        object.__setattr__(self, 'max_nodes', max_nodes)
        object.__setattr__(self, 'default_lifetime', default_lifetime)
        object.__setattr__(self, 'name_template', name_template)
        object.__setattr__(self, 'reuse', reuse)
        object.__setattr__(self, 'role_profiles', profiles)

    def to_record(self) -> dict[str, Any]:
        return {
            'enabled': self.enabled,
            'max_nodes': self.max_nodes,
            'default_lifetime': self.default_lifetime,
            'name_template': self.name_template,
            'reuse': self.reuse,
            'role_profiles': {
                name: profile.to_record() for name, profile in self.role_profiles.items()
            },
        }


def _normalize_role_id(value: object, *, field_name: str) -> str:
    text = str(value or '').strip().lower()
    if not text:
        raise AgentValidationError(f'{field_name} cannot be empty')
    allowed = set('abcdefghijklmnopqrstuvwxyz0123456789._-')
    if any(ch not in allowed for ch in text) or '.' not in text:
        raise AgentValidationError(f'{field_name} must use publisher.role form, for example agentroles.coder')
    return canonical_role_id(text)


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise AgentValidationError(f'{field_name} must be a positive integer')
    if not isinstance(value, int):
        raise AgentValidationError(f'{field_name} must be a positive integer')
    if value <= 0:
        raise AgentValidationError(f'{field_name} must be a positive integer')
    return int(value)


def _optional_non_empty_string(value: object | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise AgentValidationError(f'{field_name} cannot be empty')
    return text


def _normalize_thinking(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text not in LOOP_PROFILE_THINKING_LEVELS:
        allowed = ', '.join(sorted(LOOP_PROFILE_THINKING_LEVELS))
        raise AgentValidationError(f'loop.role_profiles.<profile>.thinking must be one of: {allowed}')
    return text


def _normalize_workspace_group(value: object | None, *, workspace_mode: WorkspaceMode) -> str | None:
    if value is None:
        return None
    if workspace_mode is not WorkspaceMode.GIT_WORKTREE:
        raise AgentValidationError('loop.role_profiles.<profile>.workspace_group requires workspace_mode="git-worktree"')
    try:
        return normalize_agent_name(str(value))
    except AgentValidationError as exc:
        raise AgentValidationError(f'loop.role_profiles.<profile>.workspace_group is invalid: {exc}') from exc


def _validate_model_and_startup_args(
    *,
    provider: str,
    model: str | None,
    startup_args: tuple[str, ...],
) -> None:
    if model is None:
        return
    try:
        provider_model_startup_args(provider, model=model)
    except ValueError as exc:
        raise AgentValidationError(str(exc)) from exc
    if startup_args_contain_model_flag(provider, startup_args):
        raise AgentValidationError(
            f'loop.role_profiles.<profile>.model cannot be combined with startup_args model flags for provider {provider}'
        )


def _validate_provider_profile_runtime_home(
    *,
    provider: str,
    provider_profile: ProviderProfileSpec,
) -> None:
    if provider == 'codex' or provider_profile.home is None:
        return
    raise AgentValidationError(
        'loop.role_profiles.<profile>.provider_profile.home is supported only for codex runtime_home overrides'
    )


def _normalize_lifetime(value: object) -> str:
    text = str(value or '').strip().lower()
    if text not in LOOP_CAPACITY_LIFETIMES:
        allowed = ', '.join(sorted(LOOP_CAPACITY_LIFETIMES))
        raise AgentValidationError(f'loop.capacity.default_lifetime must be one of: {allowed}')
    return text


def _normalize_reuse(value: object, *, field_name: str) -> str:
    text = str(value or '').strip().lower()
    if text not in LOOP_CAPACITY_REUSE_POLICIES:
        allowed = ', '.join(sorted(LOOP_CAPACITY_REUSE_POLICIES))
        raise AgentValidationError(f'{field_name} must be one of: {allowed}')
    return text


def _normalize_profile_name(value: object) -> str:
    try:
        return normalize_agent_name(str(value))
    except AgentValidationError as exc:
        raise AgentValidationError(f'loop.role_profiles key is invalid: {exc}') from exc


def _validate_name_template(value: str) -> None:
    required = ('{loop_id}', '{profile}', '{index}')
    missing = [token for token in required if token not in value]
    if missing:
        raise AgentValidationError(
            'loop.capacity.name_template must include {loop_id}, {profile}, and {index}'
        )
    try:
        sample = value.format(loop_id='loop_smoke', profile='worker', index=1)
    except Exception as exc:
        raise AgentValidationError(f'loop.capacity.name_template is invalid: {exc}') from exc
    try:
        normalize_agent_name(sample)
    except AgentValidationError as exc:
        raise AgentValidationError(f'loop.capacity.name_template renders invalid agent names: {exc}') from exc


__all__ = [
    'DEFAULT_LOOP_CAPACITY_LIFETIME',
    'DEFAULT_LOOP_CAPACITY_MAX_NODES',
    'DEFAULT_LOOP_CAPACITY_NAME_TEMPLATE',
    'DEFAULT_LOOP_CAPACITY_REUSE',
    'LOOP_CAPACITY_LIFETIMES',
    'LOOP_CAPACITY_REUSE_POLICIES',
    'LOOP_PROFILE_THINKING_LEVELS',
    'LoopCapacityConfig',
    'LoopRoleProfileSpec',
]
