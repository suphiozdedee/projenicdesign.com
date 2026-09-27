from __future__ import annotations

from cli.context import CliContext
from cli.models import ParsedInboxCommand

from .daemon import invoke_mounted_daemon


def inbox_target(context: CliContext, command: ParsedInboxCommand) -> dict:
    return invoke_mounted_daemon(
        context,
        allow_restart_stale=False,
        request_fn=lambda client: client.inbox(command.agent_name, detail=command.detail),
    )


__all__ = ['inbox_target']
