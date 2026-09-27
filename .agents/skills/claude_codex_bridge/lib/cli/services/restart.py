from __future__ import annotations

from cli.context import CliContext
from cli.models import ParsedRestartCommand

from .daemon import connect_current_mounted_daemon


def restart_agent(context: CliContext, command: ParsedRestartCommand) -> dict:
    handle = connect_current_mounted_daemon(context)
    assert handle.client is not None
    return handle.client.project_restart_agent(command.agent_name)


__all__ = ['restart_agent']
