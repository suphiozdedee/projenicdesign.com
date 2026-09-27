from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..names import SCHEMA_VERSION, AgentValidationError
from .loop_capacity import LoopCapacityConfig
from .workflow import CONFIG_SCHEMA_V2, CONFIG_SCHEMA_V3, WorkflowConfig
from .maintenance import MaintenanceHeartbeatConfig
from .topology import (
    SidebarSpec,
    SidebarViewSpec,
    ToolWindowSpec,
    WindowSpec,
    default_sidebar_spec,
    default_sidebar_view_spec,
    normalize_tool_windows,
    normalize_windows,
    topology_signature,
    topology_signature_payload,
    validate_entry_window,
    validate_tool_windows_do_not_conflict,
    validate_windows_reference_agents,
)
from .validation import normalize_agent_specs, normalize_default_agents, resolve_layout_spec


@dataclass(frozen=True)
class ProjectConfig:
    version: int
    default_agents: tuple[str, ...]
    agents: dict[str, object]
    cmd_enabled: bool = False
    layout_spec: str | None = None
    windows: tuple[WindowSpec, ...] | None = None
    tool_windows: tuple[ToolWindowSpec, ...] | None = None
    entry_window: str | None = None
    sidebar: SidebarSpec | None = None
    sidebar_view: SidebarViewSpec | None = None
    source_path: str | None = None
    windows_explicit: bool | None = None
    maintenance_heartbeat: MaintenanceHeartbeatConfig | None = None
    loop_capacity: LoopCapacityConfig | None = None
    runtime_mux_backend: str | None = None
    workflow: WorkflowConfig | None = None

    def __post_init__(self) -> None:
        if self.version not in {CONFIG_SCHEMA_V2, CONFIG_SCHEMA_V3}:
            raise AgentValidationError(
                f'version must be {CONFIG_SCHEMA_V2} or {CONFIG_SCHEMA_V3}'
            )
        if self.version == CONFIG_SCHEMA_V2 and self.workflow is not None:
            raise AgentValidationError('version 2 cannot carry workflow authority')
        if self.version == CONFIG_SCHEMA_V3 and self.workflow is None:
            raise AgentValidationError('version 3 requires workflow authority')
        explicit_windows = bool(self.windows_explicit) if self.windows_explicit is not None else self.windows is not None
        normalized_agents = normalize_agent_specs(self.agents, allow_empty=explicit_windows)
        defaults = normalize_default_agents(self.default_agents, normalized_agents=normalized_agents, allow_empty=explicit_windows)
        if explicit_windows:
            windows_input = tuple(self.windows or ())
            rendered_layout = str(self.layout_spec or (windows_input[0].layout_spec if windows_input else '')).strip()
            if not rendered_layout:
                raise AgentValidationError('layout_spec cannot be empty when windows are configured')
        else:
            rendered_layout = resolve_layout_spec(
                default_agents=defaults,
                normalized_agents=normalized_agents,
                cmd_enabled=bool(self.cmd_enabled),
                layout_spec=self.layout_spec,
            )
        sidebar = self.sidebar if self.sidebar is not None else default_sidebar_spec()
        sidebar_view = self.sidebar_view if self.sidebar_view is not None else default_sidebar_view_spec()
        maintenance_heartbeat = self.maintenance_heartbeat or MaintenanceHeartbeatConfig()
        loop_capacity = self.loop_capacity or LoopCapacityConfig()
        windows = normalize_windows(
            self.windows,
            layout_spec=rendered_layout,
            default_agents=defaults,
            cmd_enabled=bool(self.cmd_enabled),
        )
        tool_windows = normalize_tool_windows(self.tool_windows)
        validate_tool_windows_do_not_conflict(windows, tool_windows)
        validate_windows_reference_agents(windows, normalized_agents=normalized_agents)
        entry_window = validate_entry_window(self.entry_window, windows=windows, tool_windows=tool_windows)
        signature_payload = topology_signature_payload(
            windows=windows,
            tool_windows=tool_windows,
            entry_window=entry_window,
            sidebar=sidebar,
        )
        object.__setattr__(self, 'default_agents', defaults)
        object.__setattr__(self, 'agents', normalized_agents)
        object.__setattr__(self, 'layout_spec', rendered_layout)
        object.__setattr__(self, 'windows', windows)
        object.__setattr__(self, 'tool_windows', tool_windows)
        object.__setattr__(self, 'entry_window', entry_window)
        object.__setattr__(self, 'sidebar', sidebar)
        object.__setattr__(self, 'sidebar_view', sidebar_view)
        object.__setattr__(self, 'windows_explicit', explicit_windows)
        object.__setattr__(self, 'maintenance_heartbeat', maintenance_heartbeat)
        object.__setattr__(self, 'loop_capacity', loop_capacity)
        object.__setattr__(self, 'topology_signature_payload', signature_payload)
        object.__setattr__(self, 'topology_signature', topology_signature(signature_payload))

    def to_record(self) -> dict[str, Any]:
        payload = {
            'schema_version': SCHEMA_VERSION,
            'record_type': 'project_config',
            'version': self.version,
            'default_agents': list(self.default_agents),
            'agents': {name: spec.to_record() for name, spec in self.agents.items()},
            'cmd_enabled': bool(self.cmd_enabled),
            'layout_spec': self.layout_spec,
            'windows': [window.to_record() for window in self.windows],
            'tool_windows': [tool.to_record() for tool in self.tool_windows],
            'entry_window': self.entry_window,
            'sidebar': self.sidebar.to_record(),
            'sidebar_view': self.sidebar_view.to_record(),
            'windows_explicit': self.windows_explicit,
            'maintenance': {
                'heartbeat': self.maintenance_heartbeat.to_record(),
            },
            'loop': {
                'capacity': {
                    key: value
                    for key, value in self.loop_capacity.to_record().items()
                    if key != 'role_profiles'
                },
                'role_profiles': {
                    name: profile.to_record()
                    for name, profile in self.loop_capacity.role_profiles.items()
                },
            },
            'topology_signature_payload': self.topology_signature_payload,
            'topology_signature': self.topology_signature,
            'source_path': self.source_path,
        }
        if self.runtime_mux_backend is not None:
            payload['runtime'] = {'mux': {'backend': self.runtime_mux_backend}}
        if self.workflow is not None:
            payload['workflow'] = self.workflow.to_record()
        return payload


__all__ = ['ProjectConfig']
