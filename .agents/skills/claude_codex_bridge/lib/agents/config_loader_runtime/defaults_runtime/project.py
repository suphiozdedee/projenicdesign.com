from __future__ import annotations

import shutil
from collections.abc import Callable

from agents.models import (
    AgentSpec,
    PermissionMode,
    ProjectConfig,
    QueuePolicy,
    RestoreMode,
    RuntimeMode,
    WindowSpec,
    WorkspaceMode,
)
from provider_command_defaults import SUPPORTED_PROVIDER_NAMES, provider_executable

from ..common import DEFAULT_DEFAULT_AGENTS

DEFAULT_AGENT_NAME = 'demo'
DEFAULT_PROVIDER_FALLBACK = 'codex'
DEFAULT_PROVIDER_PRIORITY = SUPPORTED_PROVIDER_NAMES


def select_default_provider(
    *,
    which_fn: Callable[[str], str | None] | None = None,
) -> str:
    resolve = which_fn or shutil.which
    for provider in DEFAULT_PROVIDER_PRIORITY:
        if resolve(provider_executable(provider)) is not None:
            return provider
    return DEFAULT_PROVIDER_FALLBACK


def build_default_project_config(
    *,
    provider: str | None = None,
    which_fn: Callable[[str], str | None] | None = None,
) -> ProjectConfig:
    selected_provider = (
        str(provider or '').strip().lower()
        or select_default_provider(which_fn=which_fn)
    )
    layout = f'{DEFAULT_AGENT_NAME}:{selected_provider}'
    agents = {
        DEFAULT_AGENT_NAME: build_default_agent_spec(
            name=DEFAULT_AGENT_NAME,
            provider=selected_provider,
        ),
    }
    return ProjectConfig(
        version=2,
        default_agents=DEFAULT_DEFAULT_AGENTS,
        agents=agents,
        cmd_enabled=False,
        layout_spec=layout,
        windows=(
            WindowSpec(
                name='main',
                order=0,
                layout_spec=layout,
                agent_names=(DEFAULT_AGENT_NAME,),
            ),
        ),
        tool_windows=(),
        entry_window='main',
        windows_explicit=True,
    )


def build_default_agent_spec(*, name: str, provider: str, role: str | None = None) -> AgentSpec:
    return AgentSpec(
        name=name,
        provider=provider,
        target='.',
        workspace_mode=WorkspaceMode.INPLACE,
        workspace_root=None,
        runtime_mode=RuntimeMode.PANE_BACKED,
        restore_default=RestoreMode.AUTO,
        permission_default=PermissionMode.MANUAL,
        queue_policy=QueuePolicy.SERIAL_PER_AGENT,
        role=role,
    )


__all__ = [
    'DEFAULT_AGENT_NAME',
    'DEFAULT_PROVIDER_FALLBACK',
    'DEFAULT_PROVIDER_PRIORITY',
    'build_default_agent_spec',
    'build_default_project_config',
    'select_default_provider',
]
