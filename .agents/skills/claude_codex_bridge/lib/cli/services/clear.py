from __future__ import annotations

from cli.context import CliContext
from cli.models import ParsedClearCommand

from .daemon import invoke_mounted_daemon


def clear_agent_context(context: CliContext, command: ParsedClearCommand) -> dict:
    return invoke_mounted_daemon(
        context,
        allow_restart_stale=False,
        request_fn=lambda client: client.project_clear_context(command.agent_names),
    )


__all__ = ['clear_agent_context']
