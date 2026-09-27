from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from provider_profiles.models import ProviderProfileSpec

from ..enums import WorkspaceMode


CONFIG_SCHEMA_V2 = 2
CONFIG_SCHEMA_V3 = 3
WORKFLOW_MODE_AGENTIC_LOOP = 'agentic-loop'
WORKFLOW_PROFILE_AGENTIC_LOOP_V1 = 'agentic_loop_v1'


@dataclass(frozen=True)
class WorkflowRuntimePolicy:
    max_workgroups: int
    max_parallel_workgroups: int
    max_active_dynamic_agents: int
    max_node_rework_rounds: int = 1
    execution_window_max_panes: int = 6
    multi_workgroup_workspace: str = 'git-worktree-required'
    integration_policy: str = 'controller-owned'
    default_lifetime: str = 'current_activation'
    name_template: str = 'loop-{loop_id}-{node_id}-{profile}'
    release_policy: str = 'auto'
    window_policy: str = 'auto'
    mux_backend: str | None = None

    def to_record(self) -> dict[str, object]:
        return {
            'max_workgroups': self.max_workgroups,
            'max_parallel_workgroups': self.max_parallel_workgroups,
            'max_active_dynamic_agents': self.max_active_dynamic_agents,
            'max_node_rework_rounds': self.max_node_rework_rounds,
            'execution_window_max_panes': self.execution_window_max_panes,
            'multi_workgroup_workspace': self.multi_workgroup_workspace,
            'integration_policy': self.integration_policy,
            'default_lifetime': self.default_lifetime,
            'name_template': self.name_template,
            'release_policy': self.release_policy,
            'window_policy': self.window_policy,
            'mux_backend': self.mux_backend,
        }


@dataclass(frozen=True)
class WorkflowRoleSpec:
    name: str
    kind: str
    role: str
    provider: str
    raw_model: str | None
    model: str | None
    thinking: str | None
    workspace_mode: WorkspaceMode
    workspace_group: str | None = None
    startup_args: tuple[str, ...] = field(default_factory=tuple)
    env: dict[str, str] = field(default_factory=dict)
    provider_profile: ProviderProfileSpec = field(default_factory=ProviderProfileSpec)
    labels: tuple[str, ...] = field(default_factory=tuple)
    description: str | None = None
    max_instances: int = 1
    reuse: str = 'always_new'
    legacy_aliases: tuple[str, ...] = field(default_factory=tuple)
    release_policy: str = 'auto'
    lifecycle: str = 'immaculate'
    window_class: str = 'execution'

    def to_record(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'kind': self.kind,
            'role': self.role,
            'provider': self.provider,
            'raw_model': self.raw_model,
            'normalized_model': self.model,
            'thinking': self.thinking,
            'workspace_mode': self.workspace_mode.value,
            'workspace_group': self.workspace_group,
            'startup_args': list(self.startup_args),
            'env': dict(self.env),
            'provider_profile': self.provider_profile.to_record(),
            'labels': list(self.labels),
            'description': self.description,
            'max_instances': self.max_instances,
            'reuse': self.reuse,
            'legacy_aliases': list(self.legacy_aliases),
            'release_policy': self.release_policy,
            'lifecycle': self.lifecycle,
            'window_class': self.window_class,
            'rolepack_installed': True,
        }

    def to_safe_record(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'kind': self.kind,
            'role': self.role,
            'provider': self.provider,
            'raw_model': self.raw_model,
            'normalized_model': self.model,
            'thinking': self.thinking,
            'workspace_mode': self.workspace_mode.value,
            'workspace_group': self.workspace_group,
            'startup_arg_count': len(self.startup_args),
            'env_keys': sorted(self.env),
            'provider_profile_mode': self.provider_profile.mode,
            'provider_profile_env_keys': sorted(self.provider_profile.env),
            'labels': list(self.labels),
            'description': self.description,
            'max_instances': self.max_instances,
            'reuse': self.reuse,
            'legacy_aliases': list(self.legacy_aliases),
            'release_policy': self.release_policy,
            'lifecycle': self.lifecycle,
            'window_class': self.window_class,
            'rolepack_installed': True,
        }


@dataclass(frozen=True)
class WorkflowConfig:
    mode: str
    profile: str
    entry_role: str
    resident: dict[str, WorkflowRoleSpec]
    dynamic: dict[str, WorkflowRoleSpec]
    runtime: WorkflowRuntimePolicy
    profile_aliases: dict[str, str] = field(default_factory=dict)

    def to_record(self) -> dict[str, object]:
        return {
            'mode': self.mode,
            'profile': self.profile,
            'entry_role': self.entry_role,
            'resident': {
                name: role.to_record() for name, role in sorted(self.resident.items())
            },
            'dynamic': {
                name: role.to_record() for name, role in sorted(self.dynamic.items())
            },
            'runtime': self.runtime.to_record(),
            'profile_aliases': dict(sorted(self.profile_aliases.items())),
        }


__all__ = [
    'CONFIG_SCHEMA_V2',
    'CONFIG_SCHEMA_V3',
    'WORKFLOW_MODE_AGENTIC_LOOP',
    'WORKFLOW_PROFILE_AGENTIC_LOOP_V1',
    'WorkflowConfig',
    'WorkflowRoleSpec',
    'WorkflowRuntimePolicy',
]
