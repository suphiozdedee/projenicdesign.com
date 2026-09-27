from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import re
from typing import Any

from agents.config_loader_runtime.role_lookup import RoleLookupError, load_installed_role_manifest, normalize_role_id
from agents.models import (
    AgentSpec,
    AgentValidationError,
    LoopCapacityConfig,
    LoopRoleProfileSpec,
    MaintenanceHeartbeatConfig,
    PermissionMode,
    ProjectConfig,
    ProviderProfileSpec,
    QueuePolicy,
    RestoreMode,
    RuntimeMode,
    WindowSpec,
    WorkflowConfig,
    WorkflowRoleSpec,
    WorkflowRuntimePolicy,
    WorkspaceMode,
    normalize_agent_name,
)
from provider_core.registry import CORE_PROVIDER_NAMES, OPTIONAL_PROVIDER_NAMES
from provider_model_shortcuts import provider_model_startup_args, startup_args_contain_model_flag
from rolepacks.manifest import RoleManifestError, role_manifest_from_mapping

from ..common import StructuredConfigValidationError
from .expectations import expect_mapping
from .provider_profiles import parse_provider_profile
from .topology import parse_sidebar, parse_sidebar_view, parse_tool_windows


_TOP_LEVEL_KEYS = frozenset({'version', 'workflow', 'ui', 'tool_windows', 'maintenance', 'config_ui'})
_FORBIDDEN_STATIC_KEYS = frozenset({'windows', 'agents', 'default_agents', 'layout', 'cmd_enabled', 'loop'})
_WORKFLOW_KEYS = frozenset(
    {'mode', 'profile', 'entry_role', 'defaults', 'provider_defaults', 'runtime', 'resident', 'dynamic'}
)
_DEFAULT_KEYS = frozenset({'provider', 'model', 'thinking', 'startup_args', 'resident', 'dynamic'})
_KIND_DEFAULT_KEYS = frozenset(
    {'provider', 'model', 'thinking', 'startup_args', 'workspace_mode', 'reuse', 'release_policy'}
)
_PROVIDER_DEFAULT_KEYS = frozenset({'model', 'thinking', 'startup_args'})
_BASE_ROLE_KEYS = frozenset(
    {
        'role',
        'provider',
        'model',
        'thinking',
        'workspace_mode',
        'workspace_group',
        'startup_args',
        'provider_profile',
        'env',
        'labels',
        'description',
        'window_class',
    }
)
_RESIDENT_ROLE_KEYS = _BASE_ROLE_KEYS | {'lifecycle'}
_DYNAMIC_ROLE_KEYS = _BASE_ROLE_KEYS | {'max_instances', 'reuse', 'legacy_aliases', 'release_policy'}
_RUNTIME_KEYS = frozenset(
    {
        'max_workgroups',
        'max_parallel_workgroups',
        'max_active_dynamic_agents',
        'max_node_rework_rounds',
        'execution_window_max_panes',
        'multi_workgroup_workspace',
        'integration_policy',
        'default_lifetime',
        'name_template',
        'release_policy',
        'window_policy',
        'mux',
    }
)
_REQUIRED_RESIDENT = {
    'frontdesk': 'agentroles.ccb_frontdesk',
    'planner': 'agentroles.ccb_planner',
}
_REQUIRED_DYNAMIC = {
    'task_detailer': 'agentroles.ccb_task_detailer',
    'orchestrator': 'agentroles.ccb_orchestrator',
    'coder': 'agentroles.coder',
    'code_reviewer': 'agentroles.code_reviewer',
    'ccb_round_reviewer': 'agentroles.ccb_round_reviewer',
}
_CONTROL_DYNAMIC = frozenset({'task_detailer', 'orchestrator', 'ccb_round_reviewer'})
_KNOWN_PROVIDERS = frozenset((*CORE_PROVIDER_NAMES, *OPTIONAL_PROVIDER_NAMES))
_THINKING = frozenset({'low', 'medium', 'high'})
_REUSE = frozenset({'prefer_idle', 'always_new', 'pinned'})
_RELEASE = frozenset({'auto', 'hide', 'park', 'retain', 'unload'})
_LIFETIMES = frozenset({'current_activation', 'current_round'})
_WINDOW_CLASSES = frozenset({'user', 'plan', 'execution'})
_REQUIRED_WINDOW_CLASSES = {
    'frontdesk': 'user',
    'planner': 'plan',
    'task_detailer': 'user',
    'orchestrator': 'plan',
    'coder': 'execution',
    'code_reviewer': 'execution',
    'ccb_round_reviewer': 'plan',
}
_MODEL_NORMALIZE_RE = re.compile(r'[\s_]+')


def validate_v3_project_config(
    document: dict[str, Any],
    *,
    source_path: Path | None,
    project_root: Path | None,
    maintenance_heartbeat: MaintenanceHeartbeatConfig,
) -> ProjectConfig:
    _validate_top_level(document)
    workflow_raw = _mapping(document.get('workflow'), path='workflow')
    _reject_unknown(workflow_raw, _WORKFLOW_KEYS, path='workflow')
    mode = _required_string(workflow_raw, 'mode', path='workflow.mode')
    profile = _required_string(workflow_raw, 'profile', path='workflow.profile')
    entry_role = _required_string(workflow_raw, 'entry_role', path='workflow.entry_role')
    if mode != 'agentic-loop':
        _fail('v3_workflow_mode_invalid', 'workflow.mode', 'must be agentic-loop')
    if profile != 'agentic_loop_v1':
        _fail('v3_workflow_profile_invalid', 'workflow.profile', 'must be agentic_loop_v1')
    if entry_role != 'frontdesk':
        _fail('v3_entry_role_invalid', 'workflow.entry_role', 'must be frontdesk')

    defaults = _parse_defaults(workflow_raw.get('defaults'))
    provider_defaults = _parse_provider_defaults(workflow_raw.get('provider_defaults'))
    runtime = _parse_runtime(workflow_raw.get('runtime'))
    resident_raw = _mapping(workflow_raw.get('resident'), path='workflow.resident')
    dynamic_raw = _mapping(workflow_raw.get('dynamic'), path='workflow.dynamic')
    _validate_role_table_names(resident_raw, kind='resident')
    _validate_role_table_names(dynamic_raw, kind='dynamic')
    _require_roles(resident_raw, required=_REQUIRED_RESIDENT, kind='resident')
    _require_roles(dynamic_raw, required=_REQUIRED_DYNAMIC, kind='dynamic')
    if 'worker' in dynamic_raw:
        _fail(
            'v3_worker_profile_not_canonical',
            'workflow.dynamic.worker',
            'use workflow.dynamic.coder with legacy_aliases = ["worker"]',
        )

    resident = {
        name: _parse_role(
            name,
            raw,
            kind='resident',
            defaults=defaults,
            provider_defaults=provider_defaults,
            runtime=runtime,
            project_root=project_root,
        )
        for name, raw in sorted(resident_raw.items())
    }
    dynamic = {
        name: _parse_role(
            name,
            raw,
            kind='dynamic',
            defaults=defaults,
            provider_defaults=provider_defaults,
            runtime=runtime,
            project_root=project_root,
        )
        for name, raw in sorted(dynamic_raw.items())
    }
    _validate_role_bindings(resident, dynamic, runtime=runtime)
    aliases = _profile_aliases(dynamic)
    workflow = WorkflowConfig(
        mode=mode,
        profile=profile,
        entry_role=entry_role,
        resident=resident,
        dynamic=dynamic,
        runtime=runtime,
        profile_aliases=aliases,
    )
    agents = {name: _resident_agent_spec(role) for name, role in resident.items()}
    loop_capacity = _compatibility_loop_capacity(workflow)
    windows = (
        WindowSpec(
            name='ccb-user',
            order=0,
            layout_spec=f'frontdesk:{agents["frontdesk"].provider}',
            agent_names=('frontdesk',),
        ),
        WindowSpec(
            name='ccb-plan',
            order=1,
            layout_spec=f'planner:{agents["planner"].provider}',
            agent_names=('planner',),
        ),
    )
    tool_windows = parse_tool_windows(document.get('tool_windows'))
    sidebar = parse_sidebar(document.get('ui'))
    sidebar_view = parse_sidebar_view(document.get('ui'))
    try:
        return ProjectConfig(
            version=3,
            default_agents=('frontdesk', 'planner'),
            agents=agents,
            cmd_enabled=False,
            layout_spec=windows[0].layout_spec,
            windows=windows,
            tool_windows=tool_windows,
            entry_window='ccb-user',
            sidebar=sidebar,
            sidebar_view=sidebar_view,
            maintenance_heartbeat=maintenance_heartbeat,
            loop_capacity=loop_capacity,
            workflow=workflow,
            source_path=str(source_path) if source_path else None,
            windows_explicit=True,
        )
    except AgentValidationError as exc:
        _fail('v3_compilation_invalid', 'workflow', str(exc))


def _validate_top_level(document: dict[str, Any]) -> None:
    if type(document.get('version')) is not int or document.get('version') != 3:
        _fail('v3_version_invalid', 'version', 'must be integer 3')
    forbidden = sorted(set(document) & _FORBIDDEN_STATIC_KEYS)
    if forbidden:
        _fail(
            'v3_static_layout_field_forbidden',
            forbidden[0],
            f'version 3 cannot mix static authority fields: {", ".join(forbidden)}',
        )
    unknown = sorted(set(document) - _TOP_LEVEL_KEYS)
    if unknown:
        _fail('v3_unknown_field', unknown[0], f'unknown top-level field: {unknown[0]}')
    from .validation import validate_config_ui_settings

    validate_config_ui_settings(document.get('config_ui'))


def _parse_defaults(value: object) -> dict[str, dict[str, object]]:
    raw = _mapping(value if value is not None else {}, path='workflow.defaults')
    _reject_unknown(raw, _DEFAULT_KEYS, path='workflow.defaults')
    common = {key: raw[key] for key in ('provider', 'model', 'thinking', 'startup_args') if key in raw}
    resident_value = raw.get('resident')
    dynamic_value = raw.get('dynamic')
    resident = _mapping(
        resident_value if resident_value is not None else {},
        path='workflow.defaults.resident',
    )
    dynamic = _mapping(
        dynamic_value if dynamic_value is not None else {},
        path='workflow.defaults.dynamic',
    )
    _reject_unknown(resident, _KIND_DEFAULT_KEYS, path='workflow.defaults.resident')
    _reject_unknown(dynamic, _KIND_DEFAULT_KEYS, path='workflow.defaults.dynamic')
    _validate_default_table(common, path='workflow.defaults')
    _validate_default_table(resident, path='workflow.defaults.resident', kind='resident')
    _validate_default_table(dynamic, path='workflow.defaults.dynamic', kind='dynamic')
    return {'common': common, 'resident': resident, 'dynamic': dynamic}


def _parse_provider_defaults(value: object) -> dict[str, dict[str, object]]:
    raw = _mapping(
        value if value is not None else {},
        path='workflow.provider_defaults',
    )
    parsed: dict[str, dict[str, object]] = {}
    for provider, provider_raw in sorted(raw.items()):
        normalized = _provider(provider, path=f'workflow.provider_defaults.{provider}')
        if normalized in parsed:
            _fail(
                'v3_duplicate_provider_default',
                f'workflow.provider_defaults.{provider}',
                f'duplicate provider after normalization: {normalized}',
            )
        table = _mapping(provider_raw, path=f'workflow.provider_defaults.{provider}')
        _reject_unknown(table, _PROVIDER_DEFAULT_KEYS, path=f'workflow.provider_defaults.{provider}')
        _validate_default_table(
            table,
            path=f'workflow.provider_defaults.{provider}',
            effective_provider=normalized,
        )
        parsed[normalized] = table
    return parsed


def _parse_runtime(value: object) -> WorkflowRuntimePolicy:
    raw = _mapping(value, path='workflow.runtime')
    _reject_unknown(raw, _RUNTIME_KEYS, path='workflow.runtime')
    max_workgroups = _bounded_int(raw.get('max_workgroups'), path='workflow.runtime.max_workgroups', low=1, high=4)
    max_parallel = _bounded_int(
        raw.get('max_parallel_workgroups'),
        path='workflow.runtime.max_parallel_workgroups',
        low=1,
        high=4,
    )
    if max_parallel > max_workgroups:
        _fail(
            'v3_workgroup_limit_invalid',
            'workflow.runtime.max_parallel_workgroups',
            'cannot exceed max_workgroups',
        )
    max_agents = _positive_int(raw.get('max_active_dynamic_agents'), path='workflow.runtime.max_active_dynamic_agents')
    rework = _bounded_int(
        raw.get('max_node_rework_rounds', 1),
        path='workflow.runtime.max_node_rework_rounds',
        low=0,
        high=2,
    )
    panes = _bounded_int(
        raw.get('execution_window_max_panes', 6),
        path='workflow.runtime.execution_window_max_panes',
        low=1,
        high=6,
    )
    workspace = _optional_string(
        raw.get('multi_workgroup_workspace'),
        'git-worktree-required',
        path='workflow.runtime.multi_workgroup_workspace',
    )
    integration = _optional_string(
        raw.get('integration_policy'),
        'controller-owned',
        path='workflow.runtime.integration_policy',
    )
    if max_workgroups > 1 and workspace != 'git-worktree-required':
        _fail(
            'v3_multi_workgroup_requires_git_worktree',
            'workflow.runtime.multi_workgroup_workspace',
            'must be git-worktree-required when max_workgroups > 1',
        )
    if integration != 'controller-owned':
        _fail('v3_integration_policy_invalid', 'workflow.runtime.integration_policy', 'must be controller-owned')
    lifetime = _enum(
        raw.get('default_lifetime', 'current_activation'),
        _LIFETIMES,
        code='v3_lifecycle_invalid',
        path='workflow.runtime.default_lifetime',
    )
    release = _enum(
        raw.get('release_policy', 'auto'),
        _RELEASE,
        code='v3_release_policy_invalid',
        path='workflow.runtime.release_policy',
    )
    window_policy = _optional_string(
        raw.get('window_policy'),
        'auto',
        path='workflow.runtime.window_policy',
    )
    if window_policy != 'auto':
        _fail('v3_window_policy_invalid', 'workflow.runtime.window_policy', 'must be auto')
    name_template = _optional_string(
        raw.get('name_template'),
        'loop-{loop_id}-{node_id}-{profile}',
        path='workflow.runtime.name_template',
    )
    missing_tokens = [token for token in ('{loop_id}', '{node_id}', '{profile}') if token not in name_template]
    if missing_tokens:
        _fail(
            'v3_name_template_invalid',
            'workflow.runtime.name_template',
            f'missing required tokens: {", ".join(missing_tokens)}',
        )
    mux_backend = _parse_runtime_mux_backend_v3(raw, path='workflow.runtime.mux')
    return WorkflowRuntimePolicy(
        max_workgroups=max_workgroups,
        max_parallel_workgroups=max_parallel,
        max_active_dynamic_agents=max_agents,
        max_node_rework_rounds=rework,
        execution_window_max_panes=panes,
        multi_workgroup_workspace=workspace,
        integration_policy=integration,
        default_lifetime=lifetime,
        name_template=name_template,
        release_policy=release,
        window_policy=window_policy,
        mux_backend=mux_backend,
    )


def _parse_runtime_mux_backend_v3(raw: dict[str, object], *, path: str) -> str | None:
    """解析 v3 workflow.runtime.mux.backend（原生 Windows 后端声明）。

    与 v2 runtime.mux.backend 语义对齐；rmux 未实现，fail-closed。
    """
    if 'mux' not in raw:
        return None
    mux_value = raw.get('mux')
    if mux_value is None:
        # 显式 null mux 视为 absent（与 v2 runtime.mux 语义对齐）
        return None
    mux = _mapping(mux_value, path=path)
    _reject_unknown(mux, frozenset({'backend'}), path=path)
    backend = mux.get('backend')
    if backend is None:
        return None
    if not isinstance(backend, str) or backend not in {'herdr', 'rmux'}:
        _fail('v3_mux_backend_invalid', f'{path}.backend', 'must be one of: herdr, rmux')
    if backend == 'rmux':
        _fail(
            'v3_rmux_backend_not_implemented',
            f'{path}.backend',
            'rmux backend is not implemented yet',
        )
    return backend


def _parse_role(
    name: str,
    value: object,
    *,
    kind: str,
    defaults: dict[str, dict[str, object]],
    provider_defaults: dict[str, dict[str, object]],
    runtime: WorkflowRuntimePolicy,
    project_root: Path | None,
) -> WorkflowRoleSpec:
    path = f'workflow.{kind}.{name}'
    try:
        normalized_name = normalize_agent_name(name)
    except AgentValidationError as exc:
        _fail('v3_reserved_name', path, str(exc))
    raw = _mapping(value, path=path)
    allowed = _RESIDENT_ROLE_KEYS if kind == 'resident' else _DYNAMIC_ROLE_KEYS
    _reject_unknown(raw, allowed, path=path)
    role_id = _required_string(raw, 'role', path=f'{path}.role')
    try:
        role_id = normalize_role_id(role_id)
        role_root, role_manifest_raw = load_installed_role_manifest(role_id, project_root=project_root)
        role_manifest = role_manifest_from_mapping(role_root, role_manifest_raw)
    except RoleLookupError as exc:
        _fail('v3_rolepack_not_installed', f'{path}.role', str(exc))
    except RoleManifestError as exc:
        _fail('v3_rolepack_invalid', f'{path}.role', str(exc))

    common = defaults['common']
    kind_defaults = defaults[kind]
    provider_value = raw.get('provider', kind_defaults.get('provider', common.get('provider', 'codex')))
    provider = _provider(provider_value, path=f'{path}.provider')
    if not role_manifest.providers:
        _fail(
            'v3_rolepack_provider_compatibility_missing',
            f'{path}.role',
            f'role {role_id} does not declare supported providers',
        )
    if provider not in role_manifest.providers and not _allows_source_test_fake_provider(provider):
        _fail(
            'v3_role_provider_unsupported',
            f'{path}.provider',
            f'role {role_id} does not support provider {provider}; supported: {", ".join(role_manifest.providers)}',
        )
    role_model = raw.get('model')
    if role_model is None:
        role_model = provider_defaults.get(provider, {}).get('model')
    if role_model is None:
        role_model = _inherit_provider_scoped_default(
            common=common,
            kind_defaults=kind_defaults,
            field='model',
            provider=provider,
            path=f'{path}.model',
        )
    if role_model is not None and not isinstance(role_model, str):
        _fail('v3_type_invalid', f'{path}.model', 'must be a string')
    raw_model = role_model.strip() if isinstance(role_model, str) else None
    model = _normalize_model(provider, raw_model, path=f'{path}.model')
    thinking = raw.get(
        'thinking',
        provider_defaults.get(provider, {}).get('thinking', kind_defaults.get('thinking', common.get('thinking'))),
    )
    if thinking is not None:
        thinking = _enum(thinking, _THINKING, code='v3_thinking_invalid', path=f'{path}.thinking')
    startup_value = raw.get('startup_args')
    if startup_value is None:
        startup_value = provider_defaults.get(provider, {}).get('startup_args')
    if startup_value is None:
        startup_value = _inherit_provider_scoped_default(
            common=common,
            kind_defaults=kind_defaults,
            field='startup_args',
            provider=provider,
            path=f'{path}.startup_args',
        )
    if startup_value is None:
        startup_value = []
    startup_args = _string_list(startup_value, path=f'{path}.startup_args')
    if model is not None:
        try:
            provider_model_startup_args(provider, model=model)
        except ValueError as exc:
            _fail('v3_model_unsupported_for_provider', f'{path}.model', str(exc))
        if startup_args_contain_model_flag(provider, startup_args):
            _fail(
                'v3_model_startup_args_conflict',
                f'{path}.startup_args',
                'model cannot be combined with startup_args model flags',
            )
    workspace_mode = _workspace_mode(
        raw.get('workspace_mode', kind_defaults.get('workspace_mode', 'inplace')),
        path=f'{path}.workspace_mode',
    )
    workspace_group = raw.get('workspace_group')
    if workspace_group is not None and workspace_mode is not WorkspaceMode.GIT_WORKTREE:
        _fail(
            'v3_workspace_group_requires_worktree',
            f'{path}.workspace_group',
            'requires workspace_mode = "git-worktree"',
        )
    if workspace_group is not None:
        if not isinstance(workspace_group, str):
            _fail('v3_type_invalid', f'{path}.workspace_group', 'must be a string')
        try:
            workspace_group = normalize_agent_name(workspace_group)
        except AgentValidationError as exc:
            _fail('v3_workspace_group_invalid', f'{path}.workspace_group', str(exc))
    if workspace_group is not None and name in {'coder', 'code_reviewer'}:
        _fail(
            'v3_workspace_group_controller_owned',
            f'{path}.workspace_group',
            'coder/reviewer workspace groups are generated per node by the controller',
        )
    env = _string_mapping(raw.get('env', {}), path=f'{path}.env')
    try:
        provider_profile = (
            parse_provider_profile(path, raw['provider_profile'])
            if raw.get('provider_profile') is not None
            else ProviderProfileSpec()
        )
    except Exception as exc:
        _fail('v3_provider_profile_invalid', f'{path}.provider_profile', str(exc))
    if provider != 'codex' and provider_profile.home is not None:
        _fail(
            'v3_provider_profile_home_invalid',
            f'{path}.provider_profile.home',
            'runtime home overrides are supported only for codex',
        )
    if env and kind == 'dynamic':
        provider_profile = replace(provider_profile, env={**provider_profile.env, **env})
    labels = _string_list(raw.get('labels', []), path=f'{path}.labels')
    description = _optional_string(raw.get('description'), None, path=f'{path}.description')
    window_default = 'user' if name in {'frontdesk', 'task_detailer'} else 'plan'
    if name in {'coder', 'code_reviewer'}:
        window_default = 'execution'
    window_class = _enum(
        raw.get('window_class', window_default),
        _WINDOW_CLASSES,
        code='v3_window_class_invalid',
        path=f'{path}.window_class',
    )
    required_window_class = _REQUIRED_WINDOW_CLASSES.get(name)
    if required_window_class is not None and window_class != required_window_class:
        _fail(
            'v3_window_class_invalid',
            f'{path}.window_class',
            f'{name} requires window_class = "{required_window_class}"',
        )
    if kind == 'resident':
        lifecycle = _optional_string(raw.get('lifecycle'), 'resident', path=f'{path}.lifecycle')
        if lifecycle != 'resident':
            _fail(
                'v3_immaculate_role_declared_resident',
                f'{path}.lifecycle',
                'resident roles require lifecycle = "resident"',
            )
        max_instances = 1
        reuse = 'pinned'
        aliases: tuple[str, ...] = ()
        release_policy = 'retain'
    else:
        lifecycle = 'immaculate'
        max_instances = _positive_int(raw.get('max_instances'), path=f'{path}.max_instances')
        reuse = _enum(
            raw.get('reuse', kind_defaults.get('reuse', 'always_new')),
            _REUSE,
            code='v3_reuse_invalid',
            path=f'{path}.reuse',
        )
        aliases = _string_list(raw.get('legacy_aliases', []), path=f'{path}.legacy_aliases')
        release_policy = _enum(
            raw.get('release_policy', kind_defaults.get('release_policy', runtime.release_policy)),
            _RELEASE,
            code='v3_release_policy_invalid',
            path=f'{path}.release_policy',
        )
    return WorkflowRoleSpec(
        name=normalized_name,
        kind=kind,
        role=role_id,
        provider=provider,
        raw_model=raw_model,
        model=model,
        thinking=thinking,
        workspace_mode=workspace_mode,
        workspace_group=workspace_group,
        startup_args=startup_args,
        env=env,
        provider_profile=provider_profile,
        labels=labels,
        description=description,
        max_instances=max_instances,
        reuse=reuse,
        legacy_aliases=aliases,
        release_policy=release_policy,
        lifecycle=lifecycle,
        window_class=window_class,
    )


def _validate_role_bindings(
    resident: dict[str, WorkflowRoleSpec],
    dynamic: dict[str, WorkflowRoleSpec],
    *,
    runtime: WorkflowRuntimePolicy,
) -> None:
    conflicts = sorted(set(resident) & set(dynamic))
    if conflicts:
        _fail(
            'v3_resident_dynamic_conflict',
            f'workflow.dynamic.{conflicts[0]}',
            f'logical name {conflicts[0]} is declared as both resident and dynamic',
        )
    all_roles = {**resident, **dynamic}
    seen_role_ids: dict[str, str] = {}
    for name, spec in sorted(all_roles.items()):
        previous = seen_role_ids.get(spec.role)
        if previous is not None:
            _fail(
                'v3_duplicate_logical_role',
                f'workflow.{spec.kind}.{name}.role',
                f'role {spec.role} is already bound to {previous}',
            )
        seen_role_ids[spec.role] = f'{spec.kind}.{name}'
    for name, expected in _REQUIRED_RESIDENT.items():
        if resident[name].role != expected:
            _fail('v3_required_role_mismatch', f'workflow.resident.{name}.role', f'must be {expected}')
    for name, expected in _REQUIRED_DYNAMIC.items():
        if dynamic[name].role != expected:
            _fail('v3_required_role_mismatch', f'workflow.dynamic.{name}.role', f'must be {expected}')
    for name in _CONTROL_DYNAMIC:
        if dynamic[name].max_instances != 1:
            _fail('v3_control_profile_limit_invalid', f'workflow.dynamic.{name}.max_instances', 'must be 1')
    for name in ('coder', 'code_reviewer'):
        spec = dynamic[name]
        if spec.max_instances < runtime.max_workgroups:
            _fail(
                'v3_capacity_exceeds_profiles',
                f'workflow.dynamic.{name}.max_instances',
                f'must be at least max_workgroups={runtime.max_workgroups}',
            )
        if runtime.max_workgroups > 1 and spec.workspace_mode is not WorkspaceMode.GIT_WORKTREE:
            _fail(
                'v3_multi_workgroup_requires_git_worktree',
                f'workflow.dynamic.{name}.workspace_mode',
                'must be git-worktree when max_workgroups > 1',
            )
    required_peak = runtime.max_parallel_workgroups * 2 + 1
    if runtime.max_active_dynamic_agents < required_peak:
        _fail(
            'v3_dynamic_agent_limit_invalid',
            'workflow.runtime.max_active_dynamic_agents',
            f'must be at least {required_peak} for configured parallel workgroups and control review',
        )
    profile_ceiling = sum(spec.max_instances for spec in dynamic.values())
    if runtime.max_active_dynamic_agents > profile_ceiling:
        _fail(
            'v3_dynamic_agent_limit_invalid',
            'workflow.runtime.max_active_dynamic_agents',
            f'cannot exceed configured dynamic profile ceiling {profile_ceiling}',
        )


def _profile_aliases(dynamic: dict[str, WorkflowRoleSpec]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for name, spec in sorted(dynamic.items()):
        for alias in spec.legacy_aliases:
            try:
                normalized = normalize_agent_name(alias)
            except AgentValidationError as exc:
                _fail('v3_profile_alias_invalid', f'workflow.dynamic.{name}.legacy_aliases', str(exc))
            if normalized in dynamic or normalized in aliases:
                _fail(
                    'v3_profile_alias_conflict',
                    f'workflow.dynamic.{name}.legacy_aliases',
                    f'alias {normalized} conflicts with an existing profile or alias',
                )
            aliases[normalized] = name
    return aliases


def _resident_agent_spec(spec: WorkflowRoleSpec) -> AgentSpec:
    try:
        return AgentSpec(
            name=spec.name,
            provider=spec.provider,
            target='.',
            workspace_mode=spec.workspace_mode,
            workspace_group=spec.workspace_group,
            workspace_root=None,
            runtime_mode=RuntimeMode.PANE_BACKED,
            restore_default=RestoreMode.AUTO,
            permission_default=PermissionMode.MANUAL,
            queue_policy=QueuePolicy.SERIAL_PER_AGENT,
            model=spec.model,
            startup_args=spec.startup_args,
            env=spec.env,
            provider_profile=spec.provider_profile,
            labels=spec.labels,
            description=spec.description,
            role=spec.role,
        )
    except AgentValidationError as exc:
        _fail('v3_resident_compile_invalid', f'workflow.resident.{spec.name}', str(exc))


def _compatibility_loop_capacity(workflow: WorkflowConfig) -> LoopCapacityConfig:
    profiles: dict[str, LoopRoleProfileSpec] = {}
    for name, spec in sorted(workflow.dynamic.items()):
        try:
            profiles[name] = LoopRoleProfileSpec(
                role=spec.role,
                provider=spec.provider,
                max_instances=spec.max_instances,
                model=spec.model,
                thinking=spec.thinking,
                workspace_mode=spec.workspace_mode,
                workspace_group=spec.workspace_group,
                startup_args=spec.startup_args,
                provider_profile=spec.provider_profile,
                reuse=spec.reuse,
            )
        except AgentValidationError as exc:
            _fail('v3_dynamic_compile_invalid', f'workflow.dynamic.{name}', str(exc))
    return LoopCapacityConfig(
        enabled=True,
        max_nodes=workflow.runtime.max_active_dynamic_agents,
        default_lifetime=(
            'current_round' if workflow.runtime.default_lifetime == 'current_activation'
            else workflow.runtime.default_lifetime
        ),
        name_template='loop-{loop_id}-{profile}-{index}',
        reuse='always_new',
        role_profiles=profiles,
    )


def _require_roles(raw: dict[str, object], *, required: dict[str, str], kind: str) -> None:
    missing = sorted(set(required) - set(raw))
    if missing:
        _fail(
            f'v3_required_{kind}_missing',
            f'workflow.{kind}.{missing[0]}',
            f'missing required {kind} roles: {", ".join(missing)}',
        )


def _validate_default_table(
    raw: dict[str, object],
    *,
    path: str,
    kind: str | None = None,
    effective_provider: str | None = None,
) -> None:
    provider = effective_provider
    if 'provider' in raw:
        provider = _provider(raw['provider'], path=f'{path}.provider')
    model = raw.get('model')
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            _fail('v3_type_invalid', f'{path}.model', 'must be a non-empty string')
        if provider is not None:
            normalized_model = _normalize_model(provider, model, path=f'{path}.model')
            try:
                provider_model_startup_args(provider, model=normalized_model)
            except ValueError as exc:
                _fail('v3_model_unsupported_for_provider', f'{path}.model', str(exc))
    if 'thinking' in raw:
        _enum(raw['thinking'], _THINKING, code='v3_thinking_invalid', path=f'{path}.thinking')
    startup_args = None
    if 'startup_args' in raw:
        startup_args = _string_list(raw['startup_args'], path=f'{path}.startup_args')
    if model is not None and provider is not None and startup_args is not None:
        if startup_args_contain_model_flag(provider, startup_args):
            _fail(
                'v3_model_startup_args_conflict',
                f'{path}.startup_args',
                'model cannot be combined with startup_args model flags',
            )
    if kind is not None and 'workspace_mode' in raw:
        _workspace_mode(raw['workspace_mode'], path=f'{path}.workspace_mode')
    if kind == 'dynamic' and 'reuse' in raw:
        _enum(raw['reuse'], _REUSE, code='v3_reuse_invalid', path=f'{path}.reuse')
    if kind == 'dynamic' and 'release_policy' in raw:
        _enum(
            raw['release_policy'],
            _RELEASE,
            code='v3_release_policy_invalid',
            path=f'{path}.release_policy',
        )


def _inherit_provider_scoped_default(
    *,
    common: dict[str, object],
    kind_defaults: dict[str, object],
    field: str,
    provider: str,
    path: str,
) -> object | None:
    for table in (kind_defaults, common):
        if field not in table:
            continue
        declared_provider = table.get('provider')
        if declared_provider is None:
            return table[field]
        normalized_provider = _provider(declared_provider, path=path.rsplit('.', 1)[0] + '.provider')
        if normalized_provider != provider:
            _fail(
                f'v3_cross_provider_{field}_inheritance',
                path,
                f'provider {provider} cannot inherit {field} from provider {normalized_provider}',
            )
        return table[field]
    return None


def _validate_role_table_names(raw: dict[str, object], *, kind: str) -> None:
    seen: set[str] = set()
    for name in raw:
        if not isinstance(name, str):
            _fail('v3_type_invalid', f'workflow.{kind}', 'role names must be strings')
        try:
            normalized = normalize_agent_name(name)
        except AgentValidationError as exc:
            _fail('v3_reserved_name', f'workflow.{kind}.{name}', str(exc))
        if name != normalized:
            _fail(
                'v3_role_name_not_canonical',
                f'workflow.{kind}.{name}',
                f'role name must use canonical lowercase spelling: {normalized}',
            )
        if normalized in seen:
            _fail('v3_duplicate_logical_role', f'workflow.{kind}.{name}', f'duplicate role name: {normalized}')
        seen.add(normalized)


def _provider(value: object, *, path: str) -> str:
    if not isinstance(value, str):
        _fail('v3_type_invalid', path, 'provider must be a string')
    provider = value.strip().lower()
    if provider not in _KNOWN_PROVIDERS and not _allows_source_test_fake_provider(provider):
        _fail('v3_provider_unknown', path, f'unknown provider: {provider or "<empty>"}')
    return provider


def _allows_source_test_fake_provider(provider: str) -> bool:
    return provider == 'fake' and os.environ.get('CCB_TEST_ENTRYPOINT') == '1'


def _normalize_model(provider: str, value: str | None, *, path: str) -> str | None:
    if value is None:
        return None
    model = str(value).strip().lower()
    if not model:
        _fail('v3_model_invalid', path, 'model cannot be empty')
    model = _MODEL_NORMALIZE_RE.sub('-', model)
    if provider == 'codex' and model == 'gpt5.5':
        model = 'gpt-5.5'
    return model


def _workspace_mode(value: object, *, path: str) -> WorkspaceMode:
    if not isinstance(value, str):
        _fail('v3_type_invalid', path, 'workspace_mode must be a string')
    try:
        return WorkspaceMode(value.strip().lower())
    except ValueError:
        _fail('v3_workspace_mode_invalid', path, 'must be inplace, copy, or git-worktree')


def _mapping(value: object, *, path: str) -> dict[str, Any]:
    try:
        return dict(expect_mapping(value, field_name=path))
    except Exception as exc:
        _fail('v3_type_invalid', path, str(exc))


def _reject_unknown(raw: dict[str, object], allowed: frozenset[str], *, path: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        _fail('v3_unknown_field', f'{path}.{unknown[0]}', f'unknown field: {unknown[0]}')


def _required_string(raw: dict[str, object], key: str, *, path: str) -> str:
    if key not in raw:
        _fail('v3_required_field_missing', path, 'field is required')
    raw_value = raw.get(key)
    if not isinstance(raw_value, str):
        _fail('v3_type_invalid', path, 'must be a non-empty string')
    value = raw_value.strip()
    if not value:
        _fail('v3_type_invalid', path, 'must be a non-empty string')
    return value


def _optional_string(value: object, default: str | None, *, path: str) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        _fail('v3_type_invalid', path, 'must be a string')
    text = value.strip()
    if not text:
        _fail('v3_type_invalid', path, 'must be a non-empty string')
    return text


def _string_list(value: object, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail('v3_type_invalid', path, 'must be a list of strings')
    if any(not isinstance(item, str) for item in value):
        _fail('v3_type_invalid', path, 'must be a list of strings')
    result = tuple(item.strip() for item in value)
    if any(not item for item in result):
        _fail('v3_type_invalid', path, 'must contain non-empty strings')
    return result


def _string_mapping(value: object, *, path: str) -> dict[str, str]:
    if not isinstance(value, dict):
        _fail('v3_type_invalid', path, 'must be a string table')
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        _fail('v3_type_invalid', path, 'must be a string table')
    result = dict(value)
    if any(not key.strip() for key in result):
        _fail('v3_type_invalid', path, 'keys must be non-empty strings')
    return result


def _positive_int(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        code = 'v3_dynamic_agent_limit_invalid' if path.endswith('max_active_dynamic_agents') else 'v3_type_invalid'
        _fail(code, path, 'must be a positive integer')
    return int(value)


def _bounded_int(value: object, *, path: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail('v3_workgroup_limit_invalid', path, f'must be an integer from {low} to {high}')
    return int(value)


def _enum(value: object, allowed: frozenset[str], *, code: str, path: str) -> str:
    if not isinstance(value, str):
        _fail('v3_type_invalid', path, 'must be a string')
    text = value.strip().lower()
    if text not in allowed:
        _fail(code, path, f'must be one of: {", ".join(sorted(allowed))}')
    return text


def _fail(code: str, path: str, message: str):
    raise StructuredConfigValidationError(code=code, path=path, message=message)


__all__ = ['validate_v3_project_config']
