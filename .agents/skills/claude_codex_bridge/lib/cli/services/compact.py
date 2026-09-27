from __future__ import annotations

from cli.context import CliContext
from cli.models import ParsedCompactCommand

from .daemon import invoke_mounted_daemon


def compact_agent_context(context: CliContext, command: ParsedCompactCommand) -> dict:
    return invoke_mounted_daemon(
        context,
        allow_restart_stale=False,
        request_fn=lambda client: client.project_compact_context(command.agent_names),
    )


__all__ = ['compact_agent_context']
