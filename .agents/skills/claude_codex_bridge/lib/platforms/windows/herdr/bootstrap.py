"""``ccb herdr open`` — WezTerm-launched Herdr managed startup bootstrap.

Locates the Herdr runtime, verifies its server is running, probes read-only
capabilities, and injects the ``CCB_HERDR_*`` env the CCB herdr backend
consumes (executable, session, and a runtime capability report). Herdr stays
the physical pane owner; CCB remains the agent/provider/recovery authority
(managed mode, never an attached-mode degradation).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

from process_background import no_window_process_kwargs
from platforms.windows.herdr.runtime.capabilities import _KNOWN_CAPABILITIES

from .common import herdr_command_env, query_herdr_server_status, resolve_herdr_executable

_DEFAULT_HERDR_SESSION = 'ccb-herdr'


def _herdr_run(*args, **kwargs):
    """subprocess.run wrapper with Windows CREATE_NO_WINDOW to prevent flash consoles."""
    kwargs.update({key: value for key, value in no_window_process_kwargs().items() if key not in kwargs})
    return subprocess.run(*args, **kwargs)


_READ_PROBES = (
    ('session_attach', ('api', 'snapshot')),
    ('workspace_list', ('workspace', 'list')),
    ('pane_list', ('pane', 'list')),
)


def ensure_herdr_bootstrap_env(
    *,
    herdr_exe: str | None = None,
    herdr_session: str | None = None,
    auto_start_server: bool = False,
    start_session: str | None = None,
) -> dict[str, object]:
    """Locate herdr, ensure the server, probe capabilities, inject runtime env.

    Args:
        herdr_exe: Explicit herdr executable path (``--herdr-exe``).
        herdr_session: Explicit Herdr session name (``--herdr-session``).
        auto_start_server: When no running server is found, start one
            (session-scoped) and wait for readiness instead of failing.  This
            lets ``ccb herdr open`` own the server lifecycle the same way
            ``HerdrCliRequestAdapter`` does at runtime, without a second
            PowerShell-owned server lifecycle.
        start_session: The session name to start when ``auto_start_server`` is
            set and nothing is running.  Defaults to ``herdr_session`` /
            ``CCB_HERDR_SESSION`` / ``ccb-herdr``.

    Returns:
        A dict with ``ok``; on failure ``reason`` carries actionable guidance.
        On success also returns ``herdr_exe``, ``herdr_session``, ``warnings``
        and ``capability_report``. Successful calls set ``CCB_HERDR_EXE``,
        ``CCB_HERDR_SESSION`` and ``CCB_HERDR_CAPABILITY_REPORT`` in the process
        environment so downstream CCB startup picks the herdr backend.
    """
    exe = resolve_herdr_executable(explicit=herdr_exe)
    if not exe:
        return {
            'ok': False,
            'reason': (
                'Herdr executable not found. Install Herdr '
                '(AppData/Local/Programs/Herdr) or set CCB_HERDR_EXE.'
            ),
        }
    preferred_session = (
        str(herdr_session or '').strip()
        or os.environ.get('CCB_HERDR_SESSION', '').strip()
        or None
    )
    server, detected_session, query_error = _resolve_running_server(exe, preferred_session)
    if query_error:
        return {
            'ok': False,
            'reason': query_error,
        }
    if server is None:
        if not auto_start_server:
            return {
                'ok': False,
                'reason': (
                    'Herdr server is not running. Start Herdr first — run `herdr` '
                    'to attach the persistent session, then retry `ccb herdr open`.'
                ),
            }
        target = (
            str(start_session or '').strip()
            or preferred_session
            or _DEFAULT_HERDR_SESSION
        )
        started = _start_herdr_server(exe, target)
        if started.get('ok') is not True:
            return started
        server, detected_session, query_error = _resolve_running_server(exe, preferred_session)
        if query_error:
            return {
                'ok': False,
                'reason': query_error,
            }
        if server is None:
            return {
                'ok': False,
                'reason': (
                    'Herdr server was started but did not become reachable; '
                    'check the Herdr process and retry `ccb herdr open`.'
                ),
            }
    if server.get('compatible') is not True:
        return {
            'ok': False,
            'reason': (
                f'Herdr protocol is not compatible with CCB '
                f'(server protocol={server.get("protocol")!r}). Upgrade Herdr.'
            ),
        }
    probe = _probe_herdr_read_capabilities(exe, session=detected_session)
    failed_probes = [name for name, ok in probe.items() if not ok]
    if failed_probes:
        return {
            'ok': False,
            'reason': (
                'Herdr read-only capability probes failed: '
                + ', '.join(sorted(failed_probes))
                + '. Herdr server may be degraded.'
            ),
        }
    capability_report = _build_capability_report(probe)
    report_path = _write_capability_report(capability_report)
    warnings: list[str] = []
    live_session = str(server.get('session') or '').strip() or detected_session or None
    session = (
        str(herdr_session or '').strip()
        or os.environ.get('CCB_HERDR_SESSION', '').strip()
        or live_session
        or _DEFAULT_HERDR_SESSION
    )
    if not herdr_session and not os.environ.get('CCB_HERDR_SESSION', '').strip():
        warnings.append(
            f'Using Herdr session {session!r}; pass --herdr-session to override.'
        )
    os.environ['CCB_HERDR_EXE'] = exe
    os.environ['CCB_HERDR_SESSION'] = session
    os.environ['CCB_HERDR_CAPABILITY_REPORT'] = report_path
    return {
        'ok': True,
        'herdr_exe': exe,
        'herdr_session': session,
        'capability_report': report_path,
        'warnings': warnings,
    }


def _start_herdr_server(exe: str, session: str) -> dict[str, object]:
    """Start a session-scoped herdr server and wait for readiness.

    Reuses ``HerdrCliRequestAdapter.ensure_server_started`` so the bootstrap and
    the runtime adapter share the same server-start + 20x poll logic — there is
    no third copy of ``herdr --session <name> server`` startup.
    """
    from platforms.windows.herdr.runtime.cli import HerdrCliRequestAdapter

    adapter = HerdrCliRequestAdapter(
        session_name=session,
        herdr_executable=exe,
        run_fn=_herdr_run,
    )
    try:
        adapter.ensure_server_started(session)
    except Exception as exc:
        return {
            'ok': False,
            'reason': f'failed to start Herdr server for session {session!r}: {exc}',
        }
    return {'ok': True, 'herdr_session': session}


def _probe_herdr_read_capabilities(exe: str, session: str | None = None) -> dict[str, bool]:
    """Probe read-only herdr commands; returns ``{capability: ok}``.

    ``session`` routes the probes to a session-scoped server via
    ``--session <name>`` when provided; the probes otherwise target the
    global server.  Herdr session-scoped servers do not answer global
    (no ``--session``) commands, so the probe must match the server the
    bootstrap resolved against.
    """
    result: dict[str, bool] = {}
    for capability, args in _READ_PROBES:
        cmd = [exe, *args]
        if session:
            cmd += ['--session', session]
        try:
            run = subprocess.run(
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
            result[capability] = run.returncode == 0 and bool((run.stdout or '').strip())
        except (OSError, subprocess.SubprocessError):
            result[capability] = False
    return result


def _discover_running_ccb_sessions(exe: str) -> list[str]:
    """Return running ``ccb-`` prefixed session names via ``herdr session list``.

    A session-scoped herdr server may be running while the global server is
    not; ``herdr status server --json`` (without ``--session``) then reports
    ``running: false`` even though CCB's namespace server is up.  ``herdr
    session list --json`` exposes per-session running state, so the bootstrap
    can find the live server instead of failing.
    """
    try:
        result = subprocess.run(
            [exe, 'session', 'list', '--json'],
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
        return []
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout or '{}')
    except json.JSONDecodeError:
        return []
    sessions = payload.get('sessions') if isinstance(payload, dict) else None
    if not isinstance(sessions, list):
        return []
    running: list[str] = []
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        if entry.get('running') is not True:
            continue
        name = str(entry.get('name') or '').strip()
        if name.startswith('ccb-'):
            running.append(name)
    return running


def _unwrap_server_status(status: dict[str, object]) -> dict[str, object]:
    """Unwrap nested ``{"result": {"server": {...}}}`` shapes to a flat dict."""
    server = status
    inner = status.get('result')
    if isinstance(inner, dict):
        nested = inner.get('server')
        server = nested if isinstance(nested, dict) else inner
    return server


def _resolve_running_server(
    exe: str,
    preferred_session: str | None,
) -> tuple[dict[str, object] | None, str | None, str | None]:
    """Find a running herdr server, probing in priority order.

    Order: explicit/preferred session -> running CCB session discovered via
    ``herdr session list`` -> global server (no ``--session``).  Returns
    ``(server, session, None)`` on success, ``(None, None, reason)`` on a
    hard query failure, or ``(None, None, None)`` when nothing is running.
    """
    candidates: list[str | None] = []
    if preferred_session:
        candidates.append(preferred_session)
    candidates.extend(_discover_running_ccb_sessions(exe))
    candidates.append(None)
    seen: set[str | None] = set()
    for session in candidates:
        if session in seen:
            continue
        seen.add(session)
        status = query_herdr_server_status(exe, session)
        if status is None:
            return None, None, 'Failed to query Herdr server status.'
        server = _unwrap_server_status(status)
        if server.get('running') is not True:
            continue
        return server, session, None
    return None, None, None


def _build_capability_report(probe: dict[str, bool]) -> dict[str, object]:
    """Build a capability report covering all known capabilities.

    Probed capabilities reflect the live probe result; unprobed capabilities
    are marked ``supported`` because the server is reachable, protocol is
    compatible, and the read-only probes passed. Failure would surface at the
    operation gate at runtime rather than at bootstrap.
    """
    command_status: dict[str, str] = {}
    for name in sorted(_KNOWN_CAPABILITIES):
        ok = probe.get(name, True)
        command_status[name] = 'supported' if ok else 'unsupported'
    return {
        'backend_impl': 'herdr',
        'adapter_recommendation': 'continue',
        'verdict': 'pass',
        'failure_class': 'none',
        'command_status': command_status,
        'semantic_status': dict(command_status),
        'blocking_gaps': [],
        'windows_beta_gaps': [],
        'source_ref': 'ccb-herdr-open-runtime-probe',
    }


def _write_capability_report(report: dict[str, object]) -> str:
    fd, path = tempfile.mkstemp(prefix='ccb-herdr-capability-', suffix='.json')
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True)
    return path


__all__ = ['ensure_herdr_bootstrap_env']
