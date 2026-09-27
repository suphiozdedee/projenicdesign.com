from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from rolepacks.manifest import RoleManifest, read_toml_manifest
from rolepacks.runtime_lookup import load_installed_role


_SUPPORTED_HARD_ENFORCEMENT_PROVIDERS = frozenset({'claude'})


@dataclass(frozen=True)
class AllowedRoleCommand:
    id: str
    argv_prefix: tuple[str, ...]
    required_args: tuple[str, ...]
    stdin_schema: str
    output_schema: str
    idempotency_key: str


@dataclass(frozen=True)
class RoleCommandPolicy:
    role_id: str
    path: Path
    mode: str
    enforcement: str
    if_unsupported: str
    generic_shell: bool
    generic_ccb: bool
    supported_providers: tuple[str, ...]
    provider_tools: tuple[tuple[str, str], ...]
    allowed_effects: tuple[str, ...]
    forbidden_effects: tuple[str, ...]
    allowed: tuple[AllowedRoleCommand, ...]


class RoleCommandPolicyError(ValueError):
    pass


def load_role_command_policy(role: RoleManifest) -> RoleCommandPolicy | None:
    adapters = role.table('adapters')
    ccb_adapter = adapters.get('ccb') if isinstance(adapters.get('ccb'), dict) else {}
    surface_path = str(ccb_adapter.get('command_surface') or 'adapters/ccb/command-surface.toml').strip()
    path = Path(surface_path)
    if path.is_absolute():
        raise RoleCommandPolicyError(f'{role.root}: command surface path must be relative')
    path = Path(role.root) / path
    if not path.is_file():
        return None
    payload = read_toml_manifest(path)
    surface = payload.get('command_surface') if isinstance(payload.get('command_surface'), dict) else payload
    if not isinstance(surface, dict):
        raise RoleCommandPolicyError(f'{path}: command surface must be a table')
    allowed_payload = payload.get('allowed')
    if allowed_payload is None:
        allowed_payload = surface.get('allowed')
    allowed = tuple(_allowed_command(item, path=path) for item in _table_list(allowed_payload))
    policy = RoleCommandPolicy(
        role_id=role.id,
        path=path,
        mode=_required_string(surface, 'mode', path=path),
        enforcement=_required_string(surface, 'enforcement', path=path),
        if_unsupported=_required_string(surface, 'if_unsupported', path=path),
        generic_shell=_required_bool(surface, 'generic_shell', path=path),
        generic_ccb=_required_bool(surface, 'generic_ccb', path=path),
        supported_providers=_string_tuple(surface.get('supported_providers')),
        provider_tools=_provider_tools(payload.get('provider_tools')),
        allowed_effects=_string_tuple(surface.get('allowed_effects')),
        forbidden_effects=_string_tuple(surface.get('forbidden_effects')),
        allowed=allowed,
    )
    _validate_policy(policy)
    return policy


def role_command_policy_for_spec(spec) -> RoleCommandPolicy | None:
    role_id = str(getattr(spec, 'role', '') or '').strip()
    if not role_id:
        return None
    role = load_installed_role(role_id)
    if role is None:
        return None
    return load_role_command_policy(role)


def ensure_role_command_policy_supported(*, spec) -> RoleCommandPolicy | None:
    policy = role_command_policy_for_spec(spec)
    if policy is None:
        return None
    provider = str(getattr(spec, 'provider', '') or '').strip().lower()
    supported = policy.supported_providers or tuple(sorted(_SUPPORTED_HARD_ENFORCEMENT_PROVIDERS))
    if (
        role_command_policy_requires_enforcement(policy)
        and provider not in supported
        and not _allows_source_test_fake_provider(provider)
    ):
        raise RuntimeError(
            'role command surface requires hard provider enforcement: '
            f'role={policy.role_id} provider={provider or "unknown"} supported_providers={",".join(supported)}'
        )
    if (
        role_command_policy_requires_enforcement(policy)
        and bool(policy.allowed)
        and provider not in _SUPPORTED_HARD_ENFORCEMENT_PROVIDERS
        and not role_command_policy_provider_tool(policy, provider)
        and not _allows_source_test_fake_provider(provider)
    ):
        raise RuntimeError(
            'role command surface requires a managed provider capability: '
            f'role={policy.role_id} provider={provider or "unknown"}'
        )
    return policy


def role_command_policy_requires_enforcement(policy: RoleCommandPolicy | None) -> bool:
    if policy is None:
        return False
    return policy.mode == 'deny_all_except' and policy.enforcement == 'required'


def _allows_source_test_fake_provider(provider: str) -> bool:
    return provider == 'fake' and os.environ.get('CCB_TEST_ENTRYPOINT') == '1'


def role_command_policy_disables_inherited_assets(policy: RoleCommandPolicy | None) -> bool:
    return role_command_policy_requires_enforcement(policy)


def role_command_policy_provider_tool(policy: RoleCommandPolicy | None, provider: str) -> str | None:
    if policy is None:
        return None
    normalized = str(provider or '').strip().lower()
    for name, tool in policy.provider_tools:
        if name == normalized:
            return tool
    return None


def claude_permission_allowlist(policy: RoleCommandPolicy | None) -> tuple[str, ...]:
    if not role_command_policy_requires_enforcement(policy):
        return ()
    assert policy is not None
    return tuple(f'Bash({" ".join(command.argv_prefix)} *)' for command in policy.allowed)


def _allowed_command(value: dict[str, Any], *, path: Path) -> AllowedRoleCommand:
    if not isinstance(value, dict):
        raise RoleCommandPolicyError(f'{path}: allowed command entries must be tables')
    return AllowedRoleCommand(
        id=_required_string(value, 'id', path=path),
        argv_prefix=_string_tuple(value.get('argv_prefix')),
        required_args=_string_tuple(value.get('required_args')),
        stdin_schema=_required_string(value, 'stdin_schema', path=path),
        output_schema=_required_string(value, 'output_schema', path=path),
        idempotency_key=_required_string(value, 'idempotency_key', path=path),
    )


def _validate_policy(policy: RoleCommandPolicy) -> None:
    if policy.mode != 'deny_all_except':
        raise RoleCommandPolicyError(f'{policy.path}: unsupported command surface mode: {policy.mode}')
    if policy.enforcement != 'required':
        raise RoleCommandPolicyError(f'{policy.path}: command surface enforcement must be required')
    if policy.if_unsupported != 'fail_mount':
        raise RoleCommandPolicyError(f'{policy.path}: command surface if_unsupported must be fail_mount')
    if policy.generic_shell or policy.generic_ccb:
        raise RoleCommandPolicyError(f'{policy.path}: command surface must disable generic_shell and generic_ccb')
    for command in policy.allowed:
        if not command.argv_prefix:
            raise RoleCommandPolicyError(f'{policy.path}: allowed command {command.id} requires argv_prefix')


def _required_string(payload: dict[str, Any], key: str, *, path: Path) -> str:
    value = str(payload.get(key) or '').strip()
    if not value:
        raise RoleCommandPolicyError(f'{path}: command surface requires {key}')
    return value


def _required_bool(payload: dict[str, Any], key: str, *, path: Path) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise RoleCommandPolicyError(f'{path}: command surface {key} must be boolean')
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value or '').strip()
    return (text,) if text else ()


def _table_list(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _provider_tools(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(
        sorted(
            (str(provider).strip().lower(), str(tool).strip())
            for provider, tool in value.items()
            if str(provider).strip() and str(tool).strip()
        )
    )


__all__ = [
    'AllowedRoleCommand',
    'RoleCommandPolicy',
    'RoleCommandPolicyError',
    'claude_permission_allowlist',
    'ensure_role_command_policy_supported',
    'load_role_command_policy',
    'role_command_policy_disables_inherited_assets',
    'role_command_policy_for_spec',
    'role_command_policy_provider_tool',
    'role_command_policy_requires_enforcement',
]
