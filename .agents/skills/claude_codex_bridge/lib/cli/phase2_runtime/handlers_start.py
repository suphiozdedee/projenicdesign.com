from __future__ import annotations

import json
import os
import sys

from agents.config_loader import StructuredConfigValidationError

from cli.services.start_foreground import attach_started_project_namespace


def _stream_is_tty(stream: object) -> bool:
    checker = getattr(stream, 'isatty', None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        return False


def handle_config_validate(context, command, out, services) -> int:
    try:
        if command.action == 'approve-commands':
            approval = _approve_project_commands(context, out, services, require_output_tty=False)
            services.write_lines(out, _render_project_command_approval(approval))
            return 0
        elif command.action == 'effective':
            payload = services.effective_config_context(context)
        elif command.action == 'migrate':
            payload = services.migrate_config_context(
                context,
                to_version=command.to_version,
                dry_run=command.dry_run,
            )
        else:
            summary = services.validate_config_context(context)
            if not command.json_output:
                services.write_lines(out, services.render_config_validate(summary))
                return 0
            payload = summary.to_record()
    except StructuredConfigValidationError as exc:
        if not command.json_output:
            raise
        print(
            json.dumps(
                {'config_status': 'invalid', 'errors': [exc.to_record()]},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=out,
        )
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=out)
    return 0


def handle_config_ui(context, command, out, services) -> int:
    handle = services.prepare_config_ui(context, command)
    launch_summary = dict(handle.summary)
    # The token-free URL in ConfigUiHandle.summary is safe for diagnostics, but
    # this command's stdout is also the sidebar/manual-open handoff.  That
    # consumer needs the authenticated URL or every copied fallback link is
    # guaranteed to receive HTTP 403.
    launch_summary['url'] = handle.url
    services.write_lines(
        out,
        tuple(f'{key}: {value}' for key, value in launch_summary.items()),
    )
    flush = getattr(out, 'flush', None)
    if callable(flush):
        flush()
    if not command.no_open and not services.open_config_ui_url(handle.url):
        services.write_lines(
            out,
            ('browser_open: failed; open the authenticated loopback URL above manually',),
        )
        if callable(flush):
            flush()
    try:
        handle.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        handle.close()
    return 0


def handle_start(context, command, out, services) -> int:
    _ensure_project_commands_approved(context, out, services)
    # When the project config selects the Herdr backend, make sure a usable
    # capability report is injected before backend selection.  The installed
    # `ccb` (source-dev-independent) entrypoint does not run
    # ``ensure_herdr_bootstrap_env`` the way ``ccb herdr open`` does, so a bare
    # `ccb` under `[runtime.mux] backend = "herdr"` used to fail-closed with
    # "capability evidence is unavailable" unless a stale/foreign
    # CCB_HERDR_CAPABILITY_REPORT happened to be exported.  Probe here and set
    # the env so selection has real evidence (matches ccb8 one-click behavior).
    _ensure_herdr_runtime_evidence(context)
    interactive_attach = (
        not _env_truthy('CCB_NO_ATTACH')
        and _stream_is_tty(sys.stdin)
        and _stream_is_tty(out)
    )
    terminal_size = _terminal_size_for_streams(out, sys.stdin) if interactive_attach else None
    if terminal_size is not None:
        summary = services.start_agents(context, command, terminal_size=terminal_size)
    else:
        summary = services.start_agents(context, command)
    if interactive_attach:
        attach_started_project_namespace(context)
        return 0
    services.write_lines(out, services.render_start(summary))
    return 0


def _ensure_project_commands_approved(context, out, services) -> None:
    approval = services.inspect_project_commands(context)
    if not approval.required:
        return
    _approve_project_commands(context, out, services, require_output_tty=True, approval=approval)


def _approve_project_commands(
    context,
    out,
    services,
    *,
    require_output_tty: bool,
    approval=None,
):
    approval = approval or services.inspect_project_commands(context)
    if not approval.fields:
        return approval
    interactive = _stream_is_tty(sys.stdin) and (
        _stream_is_tty(out) if require_output_tty else True
    )
    if not interactive:
        raise RuntimeError(
            'project command approval required; review and approve interactively with '
            '`ccb config approve-commands`'
        )
    print('This project requests local command execution:', file=out)
    for field in approval.fields:
        print(
            f'  {json.dumps(field.path, ensure_ascii=True)} = '
            f'{json.dumps(field.value, ensure_ascii=True)}',
            file=out,
        )
    print(
        f'Approve these exact command fields for {approval.project_root}? [y/N] ',
        end='',
        file=out,
        flush=True,
    )
    reply = sys.stdin.readline()
    if str(reply or '').strip().lower() not in {'y', 'yes'}:
        raise RuntimeError('project command approval cancelled')
    return services.approve_project_commands_context(
        context,
        expected_digest=approval.digest,
    )


def _render_project_command_approval(approval) -> tuple[str, ...]:
    return (
        f'approval_status: {approval.status}',
        f'project_root: {approval.project_root}',
        f'command_authority_digest: {approval.digest}',
        f'command_fields: {len(approval.fields)}',
        f'receipt_path: {approval.receipt_path}',
    )


def _ensure_herdr_runtime_evidence(context) -> None:
    """Probe Herdr and inject ``CCB_HERDR_CAPABILITY_REPORT`` when needed.

    Probes when the effective backend is or may become Herdr — explicitly via
    ``CCB_RUNTIME_MUX_BACKEND=herdr``, or implicitly when running on native
    Windows x64 (where the platform gate auto-selects Herdr and an existing
    ccbd may already use it).  Reuses ``ensure_herdr_bootstrap_env`` so the
    plain ``ccb`` start path and ``ccb herdr open`` share one evidence source
    (runtime probe -> temp report), never a stale source-dev spike file.
    """
    if _herdr_capability_evidence_usable():
        return
    env_backend = os.environ.get('CCB_RUNTIME_MUX_BACKEND', '').strip().lower()
    if env_backend and env_backend != 'herdr':
        # Explicit non-Herdr backend — user has opted out.
        return
    if not _is_herdr_relevant_platform():
        return
    from platforms.windows.herdr.bootstrap import ensure_herdr_bootstrap_env

    result = ensure_herdr_bootstrap_env(
        auto_start_server=True,
        start_session=_ccbd_herdr_session_name(context),
    )
    if result.get('ok') is not True:
        # Non-fatal: selection will still fail-closed with an actionable
        # diagnostic; the daemon start below must not be silently hijacked.
        return
    for warning in (result.get('warnings') or ()):
        # Bare `ccb` auto-detects the session from the project name — the
        # "Using Herdr session … pass --herdr-session to override" hint is
        # only actionable under `ccb herdr open`; suppress it here so the
        # user gets a clean start with zero manual handling.
        if isinstance(warning, str) and warning.startswith('Using Herdr session '):
            continue
        print(f'Warning: {warning}', file=sys.stderr)


def _is_herdr_relevant_platform() -> bool:
    """Check whether native Windows x64 — where Herdr auto-selection applies.

    Mirrors the platform-gate logic in ``resolve_mux_backend_v2`` so the
    evidence probe matches the same platform the backend selector uses.
    Only native Windows x64 (not WSL) with 64-bit Python is relevant.
    """
    import platform as _platform
    from terminal_runtime.env import is_wsl as _is_wsl
    try:
        if sys.platform != 'win32':
            return False
        if _is_wsl():
            return False
        machine = _platform.machine().lower()
        if machine not in ('amd64', 'x86_64'):
            return False
        if _platform.architecture()[0] != '64bit':
            return False
    except Exception:
        return False
    return True


def _herdr_capability_evidence_usable() -> bool:
    path = os.environ.get('CCB_HERDR_CAPABILITY_REPORT', '').strip()
    if not path:
        return False
    try:
        import json as _json

        from platforms.windows.herdr.runtime.capabilities import (
            herdr_capability_report_supported,
        )

        payload = _json.loads(open(path, encoding='utf-8').read())
    except Exception:
        return False
    return isinstance(payload, dict) and herdr_capability_report_supported(payload)


def _env_truthy(name: str) -> bool:
    value = str(os.environ.get(name) or '').strip().lower()
    return value in {'1', 'true', 'yes', 'on'}


def _terminal_size_for_streams(*streams: object) -> tuple[int, int] | None:
    for stream in streams:
        fileno = getattr(stream, 'fileno', None)
        if not callable(fileno):
            continue
        try:
            fd = int(fileno())
        except Exception:
            continue
        try:
            size = os.get_terminal_size(fd)
        except OSError:
            continue
        columns = int(getattr(size, 'columns', 0) or 0)
        lines = int(getattr(size, 'lines', 0) or 0)
        if columns > 0 and lines > 0:
            return columns, lines
    return None


def handle_herdr_open(context, command, out, services) -> int:
    """``ccb herdr open`` — WezTerm-launched Herdr managed startup bootstrap.

    Locates Herdr, ensures the server is running (auto-starting it when
    ``--wait-ready`` is used so ``ccb8.ps1`` no longer pre-starts it), injects
    the herdr runtime env, then starts agents through the herdr backend
    (managed mode; CCB stays the provider/recovery authority). Foreground
    attach by default; ``--no-attach`` starts headless.  ``--wait-ready`` blocks
    until ccbd is mounted (replacing the ``ccb8.ps1`` lifecycle.json poll).
    """
    from cli.models_start import ParsedStartCommand
    from platforms.windows.herdr.bootstrap import ensure_herdr_bootstrap_env

    # P0: let Python own the Herdr server lifecycle.  When nothing is running,
    # start the ccbd-derived session server here instead of in ccb8.ps1.
    result = ensure_herdr_bootstrap_env(
        herdr_exe=command.herdr_exe,
        herdr_session=command.herdr_session,
        auto_start_server=True,
        start_session=_ccbd_herdr_session_name(context),
    )
    if result.get('ok') is not True:
        print(str(result.get('reason') or 'herdr open failed'), file=sys.stderr)
        return 1
    for warning in (result.get('warnings') or ()):
        print(f'Warning: {warning}', file=sys.stderr)
    running, backend = _daemon_running_and_backend(context)
    if running and backend != 'herdr':
        _print_herdr_daemon_conflict(backend)
        return 1
    start_command = ParsedStartCommand(
        project=command.project,
        agent_names=(),
        restore=True,
        auto_permission=True,
    )
    previous_no_attach = os.environ.get('CCB_NO_ATTACH')
    if command.no_attach:
        os.environ['CCB_NO_ATTACH'] = '1'
    try:
        rc = handle_start(context, start_command, out, services)
    finally:
        if command.no_attach:
            if previous_no_attach is None:
                os.environ.pop('CCB_NO_ATTACH', None)
            else:
                os.environ['CCB_NO_ATTACH'] = previous_no_attach
    if rc == 0 and command.wait_ready:
        ready, phase = _wait_for_ccbd_mounted(context)
        if not ready:
            print(
                f'ccb herdr open: ccbd not ready after waiting (phase={phase}); '
                'check `ccb ping` and the keeper/lifecycle state.',
                file=sys.stderr,
            )
    return rc


def _ccbd_herdr_session_name(context) -> str | None:
    """Return the ccbd-derived Herdr session name for this project.

    The ccbd namespace uses ``ccb-<project_slug>`` (e.g. ``ccb-myproj-abc12345``)
    as its Herdr session; the bootstrap must start that session's server so the
    daemon's ``HerdrCliRequestAdapter`` connects to the same server.  Defensive:
    tests may pass ``context=None`` or a context without ``paths``.
    """
    try:
        return str(getattr(context, 'paths', None).ccbd_tmux_session_name or '').strip() or None
    except Exception:
        return None


def _wait_for_ccbd_mounted(
    context,
    *,
    timeout_s: float = 90.0,
    sleep_s: float = 2.0,
) -> tuple[bool, str]:
    """Poll lifecycle state until ``phase == 'mounted'`` (or timeout)."""
    import time

    from ccbd.services.lifecycle import CcbdLifecycleStore

    deadline = time.monotonic() + timeout_s
    lifecycle = None
    while time.monotonic() < deadline:
        try:
            lifecycle = CcbdLifecycleStore(context.paths).load()
        except Exception:
            lifecycle = None
        if lifecycle is not None and lifecycle.phase == 'mounted':
            return True, 'mounted'
        time.sleep(sleep_s)
    phase = str(getattr(lifecycle, 'phase', '') or '').strip() or 'unknown'
    return False, phase


def _daemon_running_and_backend(context):
    """Return ``(running, backend_impl)`` for the current project's daemon.

    ``running`` reflects a live keeper/daemon (pid alive + socket connectable);
    ``backend_impl`` is the recorded namespace backend (``herdr``/``tmux``) or
    None when unknown or when no namespace state exists.
    """
    try:
        from cli.services.daemon import inspect_daemon

        _manager, _guard, inspection = inspect_daemon(context)
    except (ImportError, ModuleNotFoundError):
        # herdr/daemon module genuinely unavailable — safe to say "not running"
        return False, None
    except Exception as exc:
        # Inspection raised unexpectedly — daemon state unknown.
        # DEC-3: fail-closed.  Treat as a potential daemon conflict so the
        # user is warned rather than silently proceeding into a collision.
        import sys
        print(
            f"ccb herdr open: daemon inspection failed ({exc}); "
            "treating as potential conflict — stop any running CCB session "
            "(`ccb kill`) before retrying.",
            file=sys.stderr,
        )
        return True, None
    running = bool(
        getattr(inspection, 'pid_alive', False)
        and getattr(inspection, 'socket_connectable', False)
    )
    if not running:
        return False, None
    try:
        from ccbd.services.project_namespace_state_runtime.stores import (
            ProjectNamespaceStateStore,
        )

        state = ProjectNamespaceStateStore(context.paths).load()
    except Exception:
        return True, None
    if state is None:
        return True, None
    return True, str(getattr(state, 'backend_impl', '') or '').strip() or None


def _print_herdr_daemon_conflict(backend: str | None) -> None:
    if backend:
        print(f'An existing CCB daemon is running with {backend} backend.', file=sys.stderr)
    else:
        print('An existing CCB daemon is running (backend unknown).', file=sys.stderr)
    print('ccb herdr open requires the daemon in Herdr managed mode.', file=sys.stderr)
    print('Stop the existing session first: `ccb kill`, then retry `ccb herdr open`.', file=sys.stderr)


def handle_config_import_herdr(context, command, out, services) -> int:
    """A-lite: import Herdr workspace/pane topology as a CCB config draft."""
    from platforms.windows.herdr.config_import import import_herdr_config

    project_dir = str(getattr(context, 'project_dir', '') or os.getcwd())
    result = import_herdr_config(
        project_dir=project_dir,
        output_path=command.output_path,
        dry_run=command.dry_run,
        force=command.force,
    )
    if result.get("ok") is not True:
        msg = str(result.get("reason") or "import-herdr failed")
        print(msg, file=sys.stderr)
        return 1
    warnings = result.get("warnings")
    if isinstance(warnings, list) and warnings:
        for w in warnings:
            print(f"Warning: {w}", file=sys.stderr)
    if command.dry_run:
        print(f"\n# Dry-run: config draft shown above. Use --no-dry-run to write to {result.get('written_path')}",
              file=sys.stderr)
    else:
        print(f"Config draft written to {result.get('written_path')}", file=sys.stderr)
    return 0


__all__ = [
    'handle_config_import_herdr',
    'handle_config_ui',
    'handle_config_validate',
    'handle_herdr_open',
    'handle_start',
]
