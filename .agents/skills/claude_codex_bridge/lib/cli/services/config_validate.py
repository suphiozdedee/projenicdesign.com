from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

from agents.config_loader import ConfigValidationError, load_project_config
from agents.config_loader_runtime.defaults_runtime.rendering_runtime.service import _render_toml_document
from agents.config_loader_runtime.io_runtime.documents import _load_config_document
from agents.config_loader_runtime.parsing_runtime.validation import _expand_role_id_shorthand
from agents.models import AgentValidationError, normalize_agent_name, parse_layout_spec
from cli.context import CliContext
from cli.services.loop_effective_capacity import (
    compile_project_effective_capacity_snapshot,
    effective_capacity_digest,
)
from provider_profiles import validate_provider_runtime_home_uniqueness


_RESIDENT_ROLE_ORDER = ('frontdesk', 'planner')
_DYNAMIC_PROFILE_ORDER = (
    'task_detailer',
    'orchestrator',
    'coder',
    'code_reviewer',
    'ccb_round_reviewer',
)


@dataclass(frozen=True)
class ConfigValidationSummary:
    project_root: str
    project_id: str
    source: str | None
    source_kind: str
    used_builtin_default: bool
    default_agents: tuple[str, ...]
    agent_names: tuple[str, ...]
    cmd_enabled: bool
    layout_spec: str
    config_version: int = 2
    workflow: dict[str, object] | None = None
    resident_roles: tuple[dict[str, object], ...] = ()
    dynamic_profiles: tuple[dict[str, object], ...] = ()
    effective_workgroup_capacity: dict[str, object] | None = None
    compiled_topology: dict[str, object] | None = None
    capacity_digest: str | None = None
    style_warnings: tuple[str, ...] = ()

    def to_record(self) -> dict[str, object]:
        payload: dict[str, object] = {
            'config_status': 'valid',
            'project_root': self.project_root,
            'project_id': self.project_id,
            'source': self.source,
            'source_kind': self.source_kind,
            'used_builtin_default': self.used_builtin_default,
            'config_version': self.config_version,
            'default_agents': list(self.default_agents),
            'agent_names': list(self.agent_names),
            'cmd_enabled': self.cmd_enabled,
            'layout_spec': self.layout_spec,
            'warnings': list(self.style_warnings),
        }
        if self.workflow is not None:
            payload.update(
                {
                    'workflow': self.workflow,
                    'resident_roles': list(self.resident_roles),
                    'dynamic_profiles': list(self.dynamic_profiles),
                    'effective_workgroup_capacity': self.effective_workgroup_capacity,
                    'compiled_topology': self.compiled_topology,
                    'capacity_digest': self.capacity_digest,
                }
            )
        return payload


def validate_config_context(context: CliContext) -> ConfigValidationSummary:
    result = load_project_config(context.project.project_root, include_loop_overlays=False)
    if int(result.config.version) == 2:
        result = load_project_config(context.project.project_root)
    config = result.config
    specs = list(config.agents.values())
    workflow = getattr(config, 'workflow', None)
    if workflow is not None:
        specs.extend(workflow.dynamic.values())
    try:
        validate_provider_runtime_home_uniqueness(layout=context.paths, specs=specs)
    except ValueError as exc:
        raise ConfigValidationError(str(exc)) from exc
    summary = ConfigValidationSummary(
        project_root=str(context.project.project_root),
        project_id=context.project.project_id,
        source=str(result.source_path) if result.source_path else None,
        source_kind=result.source_kind,
        used_builtin_default=result.used_default,
        default_agents=config.default_agents,
        agent_names=tuple(sorted(config.agents)),
        cmd_enabled=bool(config.cmd_enabled),
        layout_spec=str(config.layout_spec or ''),
        config_version=int(config.version),
        style_warnings=_config_style_warnings(
            source_path=result.source_path,
            project_root=context.project.project_root,
        ),
    )
    if workflow is None:
        return summary
    snapshot = compile_project_effective_capacity_snapshot(Path(context.project.project_root))
    return ConfigValidationSummary(
        **{
            **summary.__dict__,
            'workflow': {
                'mode': workflow.mode,
                'profile': workflow.profile,
                'entry_role': workflow.entry_role,
            },
            'resident_roles': tuple(
                {'slot': name, 'target': name, **spec.to_safe_record()}
                for name, spec in _ordered_roles(workflow.resident, _RESIDENT_ROLE_ORDER)
            ),
            'dynamic_profiles': tuple(
                {'profile': name, **spec.to_safe_record()}
                for name, spec in _ordered_roles(workflow.dynamic, _DYNAMIC_PROFILE_ORDER)
            ),
            'effective_workgroup_capacity': {
                'enabled': True,
                **workflow.runtime.to_record(),
            },
            'compiled_topology': _compiled_topology(workflow),
            'capacity_digest': effective_capacity_digest(snapshot),
        }
    )


def effective_config_context(context: CliContext) -> dict[str, object]:
    summary = validate_config_context(context)
    payload = summary.to_record()
    payload['record_type'] = 'ccb_config_effective'
    payload['config_digest'] = _source_digest(summary.source)
    snapshot = compile_project_effective_capacity_snapshot(Path(context.project.project_root))
    payload['effective_capacity_snapshot'] = snapshot
    payload['capacity_digest'] = effective_capacity_digest(snapshot)
    return payload


def migrate_config_context(context: CliContext, *, to_version: int, dry_run: bool) -> dict[str, object]:
    if to_version != 3:
        raise ConfigValidationError('config migration supports only --to 3')
    if not dry_run:
        raise ConfigValidationError('config migration is dry-run only; --dry-run is required')
    result = load_project_config(context.project.project_root, include_loop_overlays=False)
    source_path = result.source_path
    if source_path is None or not source_path.is_file():
        raise ConfigValidationError('config migration requires a project .ccb/ccb.config source file')
    if result.config.version == 3:
        return {
            'record_type': 'ccb_config_migration_preview',
            'status': 'already_v3',
            'from_version': 3,
            'to_version': 3,
            'dry_run': True,
            'source': str(source_path),
            'source_digest': _source_digest(str(source_path)),
            'manual_required': [],
            'mappings': [],
            'target_document': None,
            'target_toml': None,
            'wrote_config': False,
        }
    raw = _load_config_document(source_path, project_root=context.project.project_root)
    target, mappings, manual_required = _v2_migration_candidate(result.config, raw)
    return {
        'record_type': 'ccb_config_migration_preview',
        'status': 'manual_required' if manual_required else 'ready',
        'from_version': 2,
        'to_version': 3,
        'dry_run': True,
        'source': str(source_path),
        'source_digest': _source_digest(str(source_path)),
        'manual_required': manual_required,
        'mappings': mappings,
        'target_document': target,
        'target_toml': _render_toml_document(target),
        'wrote_config': False,
    }


def _compiled_topology(workflow) -> dict[str, object]:
    return {
        'resident_windows': [
            {'name': 'ccb-user', 'agents': ['frontdesk']},
            {'name': 'ccb-plan', 'agents': ['planner']},
        ],
        'dynamic_placement': {
            'task_detailer': 'ccb-user',
            'orchestrator': 'ccb-plan',
            'ccb_round_reviewer': 'ccb-plan',
            'workgroups': 'ccb-exec*',
            'execution_window_max_panes': workflow.runtime.execution_window_max_panes,
        },
    }


def _ordered_roles(
    roles: dict[str, object],
    preferred: tuple[str, ...],
) -> tuple[tuple[str, object], ...]:
    names = [name for name in preferred if name in roles]
    names.extend(sorted(set(roles) - set(names)))
    return tuple((name, roles[name]) for name in names)


def _v2_migration_candidate(config, raw: dict[str, object]):
    expected_resident = {
        'agentroles.ccb_frontdesk': 'frontdesk',
        'agentroles.ccb_planner': 'planner',
    }
    expected_dynamic = {
        'agentroles.ccb_task_detailer': 'task_detailer',
        'agentroles.ccb_orchestrator': 'orchestrator',
        'agentroles.coder': 'coder',
        'agentroles.code_reviewer': 'code_reviewer',
        'agentroles.ccb_round_reviewer': 'ccb_round_reviewer',
    }
    resident: dict[str, object] = {}
    dynamic: dict[str, object] = {}
    mappings: list[dict[str, str]] = []
    manual: list[dict[str, str]] = []
    for agent_name, spec in sorted(config.agents.items()):
        slot = expected_resident.get(str(spec.role or ''))
        if slot is None:
            if spec.role:
                manual.append(
                    _manual('unmapped_static_agent', f'agents.{agent_name}', f'cannot map role {spec.role} to required V3 slot')
                )
            continue
        if slot in resident:
            manual.append(
                _manual(
                    'ambiguous_repeated_resident_role',
                    f'agents.{agent_name}',
                    f'multiple V2 agents map to workflow.resident.{slot}',
                )
            )
            continue
        resident[slot] = _role_candidate(spec)
        mappings.append({'source': f'agents.{agent_name}', 'target': f'workflow.resident.{slot}'})
        manual.extend(_sensitive_profile_manual(spec, source=f'agents.{agent_name}'))
    for profile_name, spec in sorted(config.loop_capacity.role_profiles.items()):
        target_name = expected_dynamic.get(str(spec.role or ''))
        if target_name is None:
            manual.append(
                _manual('unmapped_loop_profile', f'loop.role_profiles.{profile_name}', f'cannot map role {spec.role}')
            )
            continue
        if target_name in dynamic:
            manual.append(
                _manual(
                    'ambiguous_repeated_dynamic_role',
                    f'loop.role_profiles.{profile_name}',
                    f'multiple V2 profiles map to workflow.dynamic.{target_name}',
                )
            )
            continue
        candidate = _role_candidate(spec)
        candidate['max_instances'] = spec.max_instances
        if target_name == 'coder' and profile_name == 'worker':
            candidate['legacy_aliases'] = ['worker']
        dynamic[target_name] = candidate
        mappings.append(
            {'source': f'loop.role_profiles.{profile_name}', 'target': f'workflow.dynamic.{target_name}'}
        )
        manual.extend(
            _sensitive_profile_manual(spec, source=f'loop.role_profiles.{profile_name}')
        )
    for slot in ('frontdesk', 'planner'):
        if slot not in resident:
            manual.append(_manual('missing_required_resident', f'workflow.resident.{slot}', 'required role must be selected'))
    for profile in ('task_detailer', 'orchestrator', 'coder', 'code_reviewer', 'ccb_round_reviewer'):
        if profile not in dynamic:
            manual.append(_manual('missing_required_dynamic', f'workflow.dynamic.{profile}', 'required profile must be selected'))
    if isinstance(raw.get('windows'), dict):
        manual.append(
            _manual('static_layout_not_migrated', 'windows', 'review generated resident placement; static pane layout is not copied')
        )
    manual.append(
        _manual(
            'ambiguous_v2_max_nodes',
            'loop.capacity.max_nodes',
            'select max_workgroups and max_parallel_workgroups explicitly; V2 max_nodes remains physical capacity',
        )
    )
    runtime = {
        'max_active_dynamic_agents': config.loop_capacity.max_nodes,
        'max_node_rework_rounds': 1,
        'execution_window_max_panes': 6,
        'multi_workgroup_workspace': 'git-worktree-required',
        'integration_policy': 'controller-owned',
        'default_lifetime': 'current_activation',
        'name_template': 'loop-{loop_id}-{node_id}-{profile}',
        'release_policy': 'auto',
        'window_policy': 'auto',
    }
    target = {
        'version': 3,
        'workflow': {
            'mode': 'agentic-loop',
            'profile': 'agentic_loop_v1',
            'entry_role': 'frontdesk',
            'runtime': runtime,
            'resident': resident,
            'dynamic': dynamic,
        },
    }
    return target, mappings, manual


def _role_candidate(spec) -> dict[str, object]:
    payload: dict[str, object] = {
        'role': spec.role,
        'provider': spec.provider,
        'workspace_mode': spec.workspace_mode.value,
    }
    if getattr(spec, 'model', None):
        payload['model'] = spec.model
    if getattr(spec, 'thinking', None):
        payload['thinking'] = spec.thinking
    return payload


def _sensitive_profile_manual(spec, *, source: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if getattr(spec, 'env', None):
        items.append(
            _manual(
                'sensitive_env_requires_manual_mapping',
                f'{source}.env',
                'environment values are not copied into migration preview output',
            )
        )
    if getattr(spec, 'startup_args', ()):
        items.append(
            _manual(
                'startup_args_require_manual_mapping',
                f'{source}.startup_args',
                'startup arguments are not copied into migration preview output',
            )
        )
    if getattr(spec, 'workspace_group', None):
        items.append(
            _manual(
                'workspace_group_requires_manual_mapping',
                f'{source}.workspace_group',
                'workspace group ownership must be reviewed for the V3 workflow',
            )
        )
    profile = getattr(spec, 'provider_profile', None)
    if profile is not None and profile != type(profile)():
        items.append(
            _manual(
                'provider_profile_requires_manual_mapping',
                f'{source}.provider_profile',
                'provider profile state is not copied into migration preview output',
            )
        )
    return items


def _manual(code: str, path: str, message: str) -> dict[str, str]:
    return {'code': code, 'path': path, 'message': message}


def _source_digest(source: str | None) -> str | None:
    if not source:
        return None
    path = Path(source)
    if not path.is_file():
        return None
    return f'sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}'


def _config_style_warnings(*, source_path: Path | None, project_root: Path) -> tuple[str, ...]:
    if source_path is None or not Path(source_path).is_file():
        return ()
    try:
        document = _load_config_document(Path(source_path), project_root=project_root)
        if document.get('version') == 3:
            return ()
        document = _expand_role_id_shorthand(document, project_root=project_root)
    except Exception:
        return ()
    raw_windows = document.get('windows')
    raw_agents = document.get('agents')
    if not isinstance(raw_windows, dict) or not isinstance(raw_agents, dict):
        return ()

    leaf_defaults = _window_leaf_defaults(raw_windows)
    warnings: list[str] = []
    for raw_name, raw_spec in raw_agents.items():
        if not isinstance(raw_name, str) or not isinstance(raw_spec, dict):
            continue
        try:
            agent_name = normalize_agent_name(raw_name)
        except AgentValidationError:
            continue
        defaults = leaf_defaults.get(agent_name)
        if defaults is None:
            warnings.append(
                f'stale_agent_overlay: agents.{raw_name} is ignored because it is not referenced by [windows]'
            )
            continue
        _agent_overlay_style_warnings(
            warnings,
            raw_name=raw_name,
            raw_spec=raw_spec,
            leaf_defaults=defaults,
        )
    return tuple(warnings)


def _window_leaf_defaults(raw_windows: dict[object, object]) -> dict[str, dict[str, str]]:
    defaults: dict[str, dict[str, str]] = {}
    for layout_text in raw_windows.values():
        try:
            layout = parse_layout_spec(str(layout_text))
        except Exception:
            continue
        for leaf in layout.iter_leaves():
            if str(leaf.name or '').strip().lower() == 'cmd':
                continue
            try:
                agent_name = normalize_agent_name(str(leaf.name or ''))
            except AgentValidationError:
                continue
            defaults[agent_name] = {
                'provider': str(leaf.provider or '').strip().lower(),
                'workspace_mode': 'git-worktree'
                if str(leaf.workspace_mode or '').strip() == 'worktree'
                else 'inplace',
            }
    return defaults


def _agent_overlay_style_warnings(
    warnings: list[str],
    *,
    raw_name: str,
    raw_spec: dict[str, Any],
    leaf_defaults: dict[str, str],
) -> None:
    provider = raw_spec.get('provider')
    if provider is not None and str(provider).strip().lower() == leaf_defaults.get('provider'):
        warnings.append(
            f'redundant_agent_provider: agents.{raw_name}.provider repeats [windows] and should be removed'
        )
    workspace_mode = raw_spec.get('workspace_mode')
    if workspace_mode is None:
        return
    normalized = str(workspace_mode).strip().lower()
    if normalized == leaf_defaults.get('workspace_mode'):
        warnings.append(
            f'redundant_agent_workspace_mode: agents.{raw_name}.workspace_mode repeats [windows] and should be removed'
        )
        return
    if normalized in {'inplace', 'git-worktree'}:
        warnings.append(
            f'agent_workspace_mode_override: agents.{raw_name}.workspace_mode overrides [windows]; prefer encoding inplace/git-worktree in the window leaf'
        )


__all__ = [
    'ConfigValidationSummary',
    'effective_config_context',
    'migrate_config_context',
    'validate_config_context',
]
