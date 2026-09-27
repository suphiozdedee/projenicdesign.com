"""Shared Herdr CLI discovery and environment helpers.

Used by ``ccb config import-herdr`` (A-lite) and ``ccb herdr open``
(WezTerm-launched managed startup bootstrap).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from process_background import no_window_process_kwargs


def resolve_herdr_executable(explicit: str | None = None) -> str | None:
    """Resolve the herdr executable path.

    Priority: explicit argument > ``CCB_HERDR_EXE`` env > ``herdr`` on PATH
    > Windows common install locations (``LOCALAPPDATA``/``ProgramFiles``).
    """
    candidates = [
        str(explicit or '').strip(),
        os.environ.get('CCB_HERDR_EXE', '').strip(),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    exe = shutil.which('herdr')
    if exe:
        return exe
    local = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Herdr', 'herdr.exe')
    programs = os.path.join(os.environ.get('ProgramFiles', ''), 'Herdr', 'herdr.exe')
    for candidate in (local, programs):
        if os.path.isfile(candidate):
            return candidate
    return None


def herdr_command_env() -> dict[str, str]:
    """Environment for invoking the herdr CLI.

    On Windows, drops XDG_* overrides and points ``HERDR_CONFIG_PATH`` at the
    default Windows config location when unset, matching Herdr's config
    discovery.  On other platforms XDG_* variables are preserved so Herdr can
    locate its own config/profile directories correctly.
    """
    env = dict(os.environ)
    if sys.platform == 'win32':
        for key in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME'):
            env.pop(key, None)
        if 'HERDR_CONFIG_PATH' not in env:
            env['HERDR_CONFIG_PATH'] = os.path.join(
                os.environ.get('USERPROFILE', os.path.expanduser('~')),
                'AppData', 'Roaming', 'herdr', 'config.toml',
            )
    return env


def query_herdr_server_status(exe: str, session: str | None = None) -> dict[str, object] | None:
    """Query ``herdr status server --json``; returns the payload or None.

    ``session`` selects a session-scoped server via ``--session <name>`` when
    provided; when None the global (default) server is queried.  None means the
    status could not be queried (binary failure, timeout, or malformed output).
    Callers should distinguish that from an explicit ``running: false`` payload.
    """
    cmd = [exe, 'status', 'server', '--json']
    if session:
        cmd += ['--session', session]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=10,
            env=herdr_command_env(),
            check=False,
            **no_window_process_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or '{}')
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ['resolve_herdr_executable', 'herdr_command_env', 'query_herdr_server_status']
