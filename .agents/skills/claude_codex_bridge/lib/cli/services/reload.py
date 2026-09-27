from __future__ import annotations

import os

from cli.context import CliContext
from cli.models import ParsedReloadCommand

from .daemon import connect_current_mounted_daemon
from .reload_handoff import begin_cli_reload_handoff, clear_cli_reload_handoff


_DEFAULT_RELOAD_RPC_TIMEOUT_S = 30.0


def reload_config(context: CliContext, command: ParsedReloadCommand) -> dict:
    handoff_started = False if bool(command.dry_run) else begin_cli_reload_handoff(context)
    try:
        handle = connect_current_mounted_daemon(context)
        assert handle.client is not None
        client = _with_reload_timeout(handle.client)
        return client.project_reload_config(dry_run=bool(command.dry_run))
    finally:
        if handoff_started:
            clear_cli_reload_handoff(context)


def _with_reload_timeout(client):
    with_timeout = getattr(client, 'with_timeout', None)
    if not callable(with_timeout):
        return client
    return with_timeout(_reload_rpc_timeout_seconds())


def _reload_rpc_timeout_seconds() -> float:
    raw = os.environ.get('CCB_RELOAD_RPC_TIMEOUT_S')
    if raw:
        try:
            return max(0.1, float(raw))
        except (TypeError, ValueError):
            pass
    return _DEFAULT_RELOAD_RPC_TIMEOUT_S


__all__ = ['reload_config']
