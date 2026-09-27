from __future__ import annotations

from cli.context import CliContext
from cli.models import ParsedFollowupCommand

from .daemon import invoke_mounted_daemon


_FOLLOWUP_RPC_TIMEOUT_S = 5.0


def active_job_followup(context: CliContext, command: ParsedFollowupCommand) -> dict:
    return invoke_mounted_daemon(
        context,
        allow_restart_stale=False,
        request_fn=lambda client: client.with_timeout(_FOLLOWUP_RPC_TIMEOUT_S).followup(
            command.job_id,
            command.message,
        ),
    )


__all__ = ['active_job_followup']
