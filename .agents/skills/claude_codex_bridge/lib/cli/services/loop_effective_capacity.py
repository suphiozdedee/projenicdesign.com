from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from agents.config_loader import load_project_config
from agents.models import LoopCapacityConfig


EFFECTIVE_CAPACITY_SNAPSHOT_SCHEMA = 'ccb.loop.effective_capacity_snapshot.v1'
_PROFILE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
_ROOT_KEYS = frozenset(
    {
        'schema',
        'config_version',
        'workflow_profile',
        'workflow_mode',
        'limits',
        'policies',
        'resident_profiles',
        'dynamic_profiles',
        'profile_aliases',
    }
)
_LIMIT_KEYS = frozenset(
    {'max_workgroups', 'max_parallel_workgroups', 'max_active_dynamic_agents'}
)
_POLICY_KEYS = frozenset(
    {'node_rework', 'workspace', 'integration', 'release', 'naming', 'execution_windows'}
)
_PROFILE_KEYS = frozenset(
    {'role_id', 'provider', 'model', 'workspace_mode', 'release_policy', 'max_instances'}
)


def build_effective_capacity_snapshot(
    *,
    config_version: int,
    workflow_profile: str,
    workflow_mode: str,
    max_workgroups: int,
    max_parallel_workgroups: int,
    max_active_dynamic_agents: int,
    policies: dict[str, object],
    resident_profiles: dict[str, object],
    dynamic_profiles: dict[str, object],
    profile_aliases: dict[str, object] | None = None,
) -> dict[str, object]:
    return normalize_effective_capacity_snapshot(
        {
            'schema': EFFECTIVE_CAPACITY_SNAPSHOT_SCHEMA,
            'config_version': config_version,
            'workflow_profile': workflow_profile,
            'workflow_mode': workflow_mode,
            'limits': {
                'max_workgroups': max_workgroups,
                'max_parallel_workgroups': max_parallel_workgroups,
                'max_active_dynamic_agents': max_active_dynamic_agents,
            },
            'policies': policies,
            'resident_profiles': resident_profiles,
            'dynamic_profiles': dynamic_profiles,
            'profile_aliases': profile_aliases or {},
        }
    )


def compile_project_effective_capacity_snapshot(project_root: Path) -> dict[str, object]:
    loaded = load_project_config(Path(project_root), include_loop_overlays=False)
    config = loaded.config
    if int(config.version) == 3:
        return _compile_v3_effective_capacity_snapshot(config)
    if int(config.version) != 2:
        raise ValueError(f'unsupported config version: {config.version}')
    capacity = config.loop_capacity or LoopCapacityConfig()
    resident_profiles = {
        str(name): _resident_profile_record(spec)
        for name, spec in sorted(config.agents.items())
        if str(getattr(spec, 'role', '') or '').strip()
    }
    dynamic_profiles = {
        str(name): _dynamic_profile_record(profile, capacity=capacity)
        for name, profile in sorted(capacity.role_profiles.items())
    }
    profile_aliases = _v2_profile_aliases(dynamic_profiles)
    return build_effective_capacity_snapshot(
        config_version=2,
        workflow_profile='v2_static_compatibility',
        workflow_mode='route_only',
        max_workgroups=1,
        max_parallel_workgroups=1,
        max_active_dynamic_agents=capacity.max_nodes,
        policies={
            'node_rework': {'max_rounds': 1},
            'workspace': {'mode': 'single_workgroup_compatibility'},
            'integration': {'mode': 'single_node_compatibility'},
            'release': {
                'default_lifetime': capacity.default_lifetime,
                'policy': 'auto',
                'idle_only': True,
            },
            'naming': {'template': capacity.name_template},
            'execution_windows': {'policy': 'existing_loop_capacity'},
        },
        resident_profiles=resident_profiles,
        dynamic_profiles=dynamic_profiles,
        profile_aliases=profile_aliases,
    )


def normalize_effective_capacity_snapshot(snapshot: object) -> dict[str, object]:
    if not isinstance(snapshot, dict):
        raise ValueError('effective capacity snapshot must be an object')
    _reject_unknown(snapshot, _ROOT_KEYS, field='effective capacity snapshot')
    missing = sorted(_ROOT_KEYS - set(snapshot))
    if missing:
        raise ValueError(f'effective capacity snapshot missing fields: {", ".join(missing)}')
    if str(snapshot.get('schema') or '') != EFFECTIVE_CAPACITY_SNAPSHOT_SCHEMA:
        raise ValueError(f'effective capacity snapshot schema must be {EFFECTIVE_CAPACITY_SNAPSHOT_SCHEMA}')
    config_version = _positive_int(snapshot.get('config_version'), field='config_version')
    workflow_profile = _non_empty(snapshot.get('workflow_profile'), field='workflow_profile')
    workflow_mode = _non_empty(snapshot.get('workflow_mode'), field='workflow_mode')
    limits = snapshot.get('limits')
    if not isinstance(limits, dict):
        raise ValueError('effective capacity snapshot limits must be an object')
    _reject_unknown(limits, _LIMIT_KEYS, field='limits')
    if set(limits) != _LIMIT_KEYS:
        raise ValueError('effective capacity snapshot limits must contain all required fields')
    max_workgroups = _positive_int(limits.get('max_workgroups'), field='limits.max_workgroups')
    if max_workgroups > 4:
        raise ValueError('limits.max_workgroups must be between 1 and 4')
    max_parallel = _positive_int(
        limits.get('max_parallel_workgroups'),
        field='limits.max_parallel_workgroups',
    )
    if max_parallel > max_workgroups:
        raise ValueError('limits.max_parallel_workgroups cannot exceed limits.max_workgroups')
    max_agents = _positive_int(
        limits.get('max_active_dynamic_agents'),
        field='limits.max_active_dynamic_agents',
    )
    policies = _normalize_policies(snapshot.get('policies'))
    resident_profiles = _normalize_profiles(snapshot.get('resident_profiles'), field='resident_profiles')
    dynamic_profiles = _normalize_profiles(snapshot.get('dynamic_profiles'), field='dynamic_profiles')
    aliases = _normalize_aliases(snapshot.get('profile_aliases'))
    missing_alias_targets = sorted(set(aliases.values()) - set(dynamic_profiles))
    if missing_alias_targets:
        raise ValueError(
            'profile_aliases target missing dynamic profile: '
            + ', '.join(missing_alias_targets)
        )
    return {
        'schema': EFFECTIVE_CAPACITY_SNAPSHOT_SCHEMA,
        'config_version': config_version,
        'workflow_profile': workflow_profile,
        'workflow_mode': workflow_mode,
        'limits': {
            'max_workgroups': max_workgroups,
            'max_parallel_workgroups': max_parallel,
            'max_active_dynamic_agents': max_agents,
        },
        'policies': policies,
        'resident_profiles': resident_profiles,
        'dynamic_profiles': dynamic_profiles,
        'profile_aliases': aliases,
    }


def effective_capacity_digest(snapshot: object) -> str:
    normalized = normalize_effective_capacity_snapshot(snapshot)
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return f'sha256:{hashlib.sha256(encoded).hexdigest()}'


def allows_v2_missing_candidate(snapshot: object) -> bool:
    normalized = normalize_effective_capacity_snapshot(snapshot)
    limits = normalized['limits']
    return (
        normalized['config_version'] == 2
        and normalized['workflow_mode'] == 'route_only'
        and limits['max_workgroups'] == 1
        and limits['max_parallel_workgroups'] == 1
    )


def _normalize_policies(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError('effective capacity snapshot policies must be an object')
    _reject_unknown(value, _POLICY_KEYS, field='policies')
    if set(value) != _POLICY_KEYS:
        raise ValueError('effective capacity snapshot policies must contain all required fields')
    node_rework = _exact_mapping(value['node_rework'], {'max_rounds'}, field='policies.node_rework')
    max_rounds = node_rework['max_rounds']
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 0 <= max_rounds <= 2:
        raise ValueError('policies.node_rework.max_rounds must be an integer from 0 to 2')
    workspace = _exact_mapping(value['workspace'], {'mode'}, field='policies.workspace')
    integration = _exact_mapping(value['integration'], {'mode'}, field='policies.integration')
    release = _exact_mapping(
        value['release'],
        {'default_lifetime', 'policy', 'idle_only'},
        field='policies.release',
    )
    if not isinstance(release['idle_only'], bool):
        raise ValueError('policies.release.idle_only must be a boolean')
    naming = _exact_mapping(value['naming'], {'template'}, field='policies.naming')
    execution_windows = _exact_mapping(
        value['execution_windows'],
        {'policy'},
        field='policies.execution_windows',
    )
    return {
        'node_rework': {'max_rounds': max_rounds},
        'workspace': {'mode': _non_empty(workspace['mode'], field='policies.workspace.mode')},
        'integration': {'mode': _non_empty(integration['mode'], field='policies.integration.mode')},
        'release': {
            'default_lifetime': _non_empty(
                release['default_lifetime'],
                field='policies.release.default_lifetime',
            ),
            'policy': _non_empty(release['policy'], field='policies.release.policy'),
            'idle_only': release['idle_only'],
        },
        'naming': {'template': _non_empty(naming['template'], field='policies.naming.template')},
        'execution_windows': {
            'policy': _non_empty(
                execution_windows['policy'],
                field='policies.execution_windows.policy',
            )
        },
    }


def _normalize_profiles(value: object, *, field: str) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict):
        raise ValueError(f'{field} must be an object')
    profiles: dict[str, dict[str, object]] = {}
    for raw_name, raw_profile in sorted(value.items()):
        name = str(raw_name or '').strip()
        if not _PROFILE_RE.fullmatch(name):
            raise ValueError(f'{field} profile name is invalid: {name!r}')
        if not isinstance(raw_profile, dict):
            raise ValueError(f'{field}.{name} must be an object')
        _reject_unknown(raw_profile, _PROFILE_KEYS, field=f'{field}.{name}')
        if set(raw_profile) != _PROFILE_KEYS:
            raise ValueError(f'{field}.{name} must contain all required fields')
        model = raw_profile.get('model')
        if model is not None:
            model = _non_empty(model, field=f'{field}.{name}.model')
        profiles[name] = {
            'role_id': _non_empty(raw_profile.get('role_id'), field=f'{field}.{name}.role_id'),
            'provider': _non_empty(raw_profile.get('provider'), field=f'{field}.{name}.provider'),
            'model': model,
            'workspace_mode': _non_empty(
                raw_profile.get('workspace_mode'),
                field=f'{field}.{name}.workspace_mode',
            ),
            'release_policy': _non_empty(
                raw_profile.get('release_policy'),
                field=f'{field}.{name}.release_policy',
            ),
            'max_instances': _positive_int(
                raw_profile.get('max_instances'),
                field=f'{field}.{name}.max_instances',
            ),
        }
    return profiles


def _normalize_aliases(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError('profile_aliases must be an object')
    return {
        _non_empty(name, field='profile_aliases key'): _non_empty(target, field=f'profile_aliases.{name}')
        for name, target in sorted(value.items())
    }


def _resident_profile_record(spec) -> dict[str, object]:
    return {
        'role_id': str(spec.role),
        'provider': str(spec.provider),
        'model': getattr(spec, 'model', None),
        'workspace_mode': str(spec.workspace_mode.value),
        'release_policy': 'resident',
        'max_instances': 1,
    }


def _dynamic_profile_record(profile, *, capacity: LoopCapacityConfig) -> dict[str, object]:
    return {
        'role_id': str(profile.role),
        'provider': str(profile.provider),
        'model': profile.model,
        'workspace_mode': str(profile.workspace_mode.value),
        'release_policy': capacity.default_lifetime,
        'max_instances': profile.max_instances,
    }


def _v2_profile_aliases(dynamic_profiles: dict[str, dict[str, object]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for logical_name, role_id in (
        ('coder', 'agentroles.coder'),
        ('code_reviewer', 'agentroles.code_reviewer'),
    ):
        if logical_name in dynamic_profiles:
            continue
        matches = [
            name
            for name, profile in sorted(dynamic_profiles.items())
            if profile.get('role_id') == role_id
        ]
        if len(matches) == 1:
            aliases[logical_name] = matches[0]
    return aliases


def _compile_v3_effective_capacity_snapshot(config) -> dict[str, object]:
    workflow = getattr(config, 'workflow', None)
    if workflow is None:
        raise ValueError('Config V3 effective capacity requires workflow authority')
    runtime = workflow.runtime
    return build_effective_capacity_snapshot(
        config_version=3,
        workflow_profile=workflow.profile,
        workflow_mode=workflow.mode,
        max_workgroups=runtime.max_workgroups,
        max_parallel_workgroups=runtime.max_parallel_workgroups,
        max_active_dynamic_agents=runtime.max_active_dynamic_agents,
        policies={
            'node_rework': {'max_rounds': runtime.max_node_rework_rounds},
            'workspace': {'mode': runtime.multi_workgroup_workspace},
            'integration': {'mode': runtime.integration_policy},
            'release': {
                'default_lifetime': runtime.default_lifetime,
                'policy': runtime.release_policy,
                'idle_only': True,
            },
            'naming': {'template': runtime.name_template},
            'execution_windows': {
                'policy': f'{runtime.window_policy}:max_panes={runtime.execution_window_max_panes}'
            },
        },
        resident_profiles={
            name: _workflow_profile_record(spec)
            for name, spec in sorted(workflow.resident.items())
        },
        dynamic_profiles={
            name: _workflow_profile_record(spec)
            for name, spec in sorted(workflow.dynamic.items())
        },
        profile_aliases=workflow.profile_aliases,
    )


def _workflow_profile_record(spec) -> dict[str, object]:
    return {
        'role_id': spec.role,
        'provider': spec.provider,
        'model': spec.model,
        'workspace_mode': spec.workspace_mode.value,
        'release_policy': 'resident' if spec.kind == 'resident' else spec.release_policy,
        'max_instances': spec.max_instances,
    }


def _exact_mapping(value: object, keys: set[str], *, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f'{field} must be an object')
    if set(value) != keys:
        raise ValueError(f'{field} must contain exactly: {", ".join(sorted(keys))}')
    return dict(value)


def _reject_unknown(value: dict[str, object], allowed: frozenset[str], *, field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f'{field} contains unknown fields: {", ".join(unknown)}')


def _positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{field} must be a positive integer')
    return value


def _non_empty(value: object, *, field: str) -> str:
    text = str(value or '').strip()
    if not text:
        raise ValueError(f'{field} must be non-empty')
    return text


__all__ = [
    'EFFECTIVE_CAPACITY_SNAPSHOT_SCHEMA',
    'allows_v2_missing_candidate',
    'build_effective_capacity_snapshot',
    'compile_project_effective_capacity_snapshot',
    'effective_capacity_digest',
    'normalize_effective_capacity_snapshot',
]
