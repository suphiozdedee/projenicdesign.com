from __future__ import annotations

from cli.context import CliContext
from project_command_trust import (
    ProjectCommandApproval,
    approve_project_commands,
    inspect_project_command_approval,
)


def inspect_project_commands(context: CliContext) -> ProjectCommandApproval:
    return inspect_project_command_approval(context.project.project_root)


def approve_project_commands_context(
    context: CliContext,
    *,
    expected_digest: str,
) -> ProjectCommandApproval:
    return approve_project_commands(
        context.project.project_root,
        expected_digest=expected_digest,
    )


__all__ = ['approve_project_commands_context', 'inspect_project_commands']
