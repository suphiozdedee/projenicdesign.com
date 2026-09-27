from __future__ import annotations

from provider_command_defaults import (
    provider_executable,
    provider_start_env_vars,
    provider_start_parts,
)

PROVIDER_COMMAND_PLACEHOLDER = '{command}'


def apply_provider_command_template(command: str, template: str | None) -> str:
    normalized_template = str(template or '').strip()
    if not normalized_template:
        return command
    if normalized_template.count(PROVIDER_COMMAND_PLACEHOLDER) != 1:
        raise ValueError(f'provider_command_template must contain exactly one {PROVIDER_COMMAND_PLACEHOLDER}')
    return normalized_template.replace(PROVIDER_COMMAND_PLACEHOLDER, str(command or '').strip())


def pane_title_marker(*, project_id: str, agent_name: str) -> str:
    suffix = str(project_id or '').strip()[:8]
    if suffix:
        return f'CCB-{agent_name}-{suffix}'
    return f'CCB-{agent_name}'


__all__ = [
    'PROVIDER_COMMAND_PLACEHOLDER',
    'apply_provider_command_template',
    'pane_title_marker',
    'provider_executable',
    'provider_start_env_vars',
    'provider_start_parts',
]
