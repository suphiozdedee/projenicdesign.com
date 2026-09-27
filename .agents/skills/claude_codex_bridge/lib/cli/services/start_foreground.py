from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import subprocess
import sys
import time
from typing import Mapping

from cli.context import CliContext
from ccbd.socket_client import CcbdClient, CcbdClientError
from terminal_runtime.env import tmux_compatible_env
from terminal_runtime.tmux import tmux_base
from .daemon_runtime.policy import (
    FOREGROUND_ATTACH_RPC_TIMEOUT_S,
    FOREGROUND_ATTACH_TARGET_READY_TIMEOUT_S,
)

_ATTACH_ESTABLISH_TIMEOUT_S = 1.5
_ATTACH_ESTABLISH_POLL_INTERVAL_S = 0.05
_ATTACH_TARGET_READY_TIMEOUT_S = FOREGROUND_ATTACH_TARGET_READY_TIMEOUT_S
_ATTACH_TARGET_READY_POLL_INTERVAL_S = 0.05
_MIN_ATTACH_RPC_TIMEOUT_S = 0.1


@dataclass(frozen=True)
class ForegroundAttachSummary:
    project_id: str
    tmux_socket_path: str
    tmux_session_name: str
    backend_impl: str = 'tmux'
    namespace_id: str | None = None
    session_name: str | None = None
    ipc_kind: str | None = None
    ipc_ref: str | None = None
    namespace_restore_token_present: bool = False


class ForegroundAttachError(RuntimeError):
    pass


def attach_started_project_namespace(context: CliContext) -> ForegroundAttachSummary:
    client = _foreground_attach_client(context)
    env = _attach_env()
    payload = _wait_for_attach_target(client, env=env)
    if _payload_backend_impl(payload) == 'herdr':
        return _attach_herdr_project_namespace(context, payload)
    if shutil.which('tmux') is None:
        raise ForegroundAttachError('tmux is required for interactive `ccb`')
    tmux_socket_path = str(payload.get('namespace_tmux_socket_path') or '').strip()
    tmux_session_name = str(payload.get('namespace_tmux_session_name') or '').strip()
    summary = ForegroundAttachSummary(
        project_id=context.project.project_id,
        tmux_socket_path=tmux_socket_path,
        tmux_session_name=tmux_session_name,
        backend_impl=str(payload.get('namespace_backend_impl') or 'tmux').strip() or 'tmux',
        namespace_id=_clean_optional_payload_text(payload.get('namespace_id')),
        session_name=_clean_optional_payload_text(payload.get('namespace_session_name')) or tmux_session_name,
        ipc_kind=_clean_optional_payload_text(payload.get('namespace_ipc_kind')),
        ipc_ref=_clean_optional_payload_text(payload.get('namespace_ipc_ref')) or tmux_socket_path,
        namespace_restore_token_present=bool(payload.get('namespace_restore_token_present')),
    )
    attach = subprocess.Popen(
        _tmux_cmd(tmux_socket_path, 'attach-session', '-t', tmux_session_name),
        env=env,
    )
    attached = _wait_for_attach_established(
        attach,
        tmux_socket_path=tmux_socket_path,
        tmux_session_name=tmux_session_name,
        env=env,
    )
    if attached:
        _best_effort_refresh_attached_client(
            tmux_socket_path,
            tmux_session_name,
            client_pid=attach.pid,
            env=env,
        )
    returncode = attach.wait()
    if attached:
        return summary
    if returncode != 0 and not _tmux_has_session(tmux_socket_path, tmux_session_name, env=env):
        raise ForegroundAttachError('project namespace session exited before foreground attach completed')
    raise ForegroundAttachError('failed to attach project namespace after successful `ccb` start')


def _wait_for_attach_established(
    attach: subprocess.Popen[bytes] | subprocess.Popen[str],
    *,
    tmux_socket_path: str,
    tmux_session_name: str,
    env: dict[str, str],
) -> bool:
    deadline = time.monotonic() + _ATTACH_ESTABLISH_TIMEOUT_S
    while True:
        if _tmux_client_pid_attached(
            tmux_socket_path,
            tmux_session_name,
            client_pid=attach.pid,
            env=env,
        ):
            return True
        if attach.poll() is not None:
            return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(_ATTACH_ESTABLISH_POLL_INTERVAL_S)


def _tmux_client_pid_attached(
    tmux_socket_path: str,
    tmux_session_name: str,
    *,
    client_pid: int,
    env: dict[str, str],
) -> bool:
    return client_pid in _tmux_list_client_pids(
        tmux_socket_path,
        tmux_session_name,
        env=env,
    )


def _wait_for_attach_target(client, *, env: dict[str, str]) -> dict[str, object]:
    deadline = time.monotonic() + _ATTACH_TARGET_READY_TIMEOUT_S
    attempts = 0
    ping_successes = 0
    last_error = _attach_target_unavailable_error(
        attempts=attempts,
        timeout_s=_ATTACH_TARGET_READY_TIMEOUT_S,
    )
    while True:
        remaining_s = deadline - time.monotonic()
        if remaining_s < _MIN_ATTACH_RPC_TIMEOUT_S:
            raise ForegroundAttachError(last_error)
        attempt_timeout_s = min(FOREGROUND_ATTACH_RPC_TIMEOUT_S, remaining_s)
        try:
            attempts += 1
            payload = _client_for_attach_attempt(client, timeout_s=attempt_timeout_s).ping('ccbd')
        except CcbdClientError as exc:
            last_error = _attach_ping_timeout_error(
                exc,
                attempts=attempts,
                timeout_s=_ATTACH_TARGET_READY_TIMEOUT_S,
                rpc_timeout_s=attempt_timeout_s,
            )
        else:
            ping_successes += 1
            ready, error = _attach_target_ready(payload, env=env)
            if ready:
                return payload
            last_error = _attach_namespace_timeout_error(
                error,
                attempts=attempts,
                ping_successes=ping_successes,
                timeout_s=_ATTACH_TARGET_READY_TIMEOUT_S,
            )
        if time.monotonic() >= deadline:
            raise ForegroundAttachError(last_error)
        time.sleep(min(_ATTACH_TARGET_READY_POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))


def _attach_target_ready(payload: dict[str, object], *, env: dict[str, str]) -> tuple[bool, str]:
    if _payload_backend_impl(payload) == 'herdr':
        return _herdr_attach_target_ready(payload)
    tmux_socket_path = str(payload.get('namespace_tmux_socket_path') or '').strip()
    tmux_session_name = str(payload.get('namespace_tmux_session_name') or '').strip()
    workspace_window_name = str(payload.get('namespace_workspace_window_name') or '').strip()
    ui_attachable = bool(payload.get('namespace_ui_attachable'))
    if not tmux_socket_path or not tmux_session_name or not ui_attachable:
        return False, 'project namespace is not attachable after successful `ccb` start'
    if not _tmux_has_session(tmux_socket_path, tmux_session_name, env=env):
        return False, 'project namespace session is missing after successful `ccb` start'
    if workspace_window_name and not _tmux_select_window(
        tmux_socket_path,
        f'{tmux_session_name}:{workspace_window_name}',
        env=env,
    ):
        return False, 'project namespace workspace window is missing after successful `ccb` start'
    return True, ''


def _attach_herdr_project_namespace(context: CliContext, payload: dict[str, object]) -> ForegroundAttachSummary:
    namespace_ref = _herdr_namespace_ref_from_payload(payload)
    backend_selection = _herdr_backend_selection_from_payload(payload)
    projection_detail = _herdr_surface_projection_detail(payload)
    backend = _build_herdr_attach_backend(
        namespace_ref=namespace_ref,
        backend_selection=backend_selection,
    )
    attach = getattr(backend, 'attach_namespace', None)
    if not callable(attach):
        raise ForegroundAttachError(
            'foreground attach failed: Herdr backend does not support attach_namespace '
            f'(backend_impl={backend_selection.get("backend_impl")}, ipc_kind={namespace_ref.get("ipc_kind")})'
            f'{projection_detail}'
        )
    # Spawn WezTerm to display the Herdr UI BEFORE calling attach_namespace.
    # attach_namespace internally runs ``herdr session attach`` (a foreground
    # terminal op) which only succeeds from WezTerm; on bare ``ccb`` launched
    # from PowerShell it would otherwise block for 5 s and fail silently with
    # no visible UI.  Spawning WezTerm first gives the user an immediate
    # visual session while the backend-side ``workspace focus`` completes
    # during attach.
    _launch_herdr_ui(namespace_ref)
    try:
        attach(namespace_ref, window_name=_clean_optional_payload_text(payload.get('namespace_workspace_window_name')))
    except Exception as exc:
        raise ForegroundAttachError(
            'foreground attach failed: Herdr attach_namespace failed '
            f'(backend_impl={backend_selection.get("backend_impl")}, ipc_kind={namespace_ref.get("ipc_kind")}, '
            f'ipc_ref_present={bool(namespace_ref.get("ipc_ref"))}, detail={exc})'
            f'{projection_detail}'
        ) from exc
    return _foreground_attach_summary_from_payload(context, payload)


def _launch_herdr_ui(namespace_ref: dict[str, object]) -> None:
    """Spawn WezTerm (or a standalone herdr process) to display the Herdr UI.

    Mirrors the ccb8.ps1 one-click WezTerm launch: resolves herdr + wezterm
    paths, derives the session name from the namespace ref, and spawns
    ``wezterm cli spawn -- <herdr> session attach <session>``.  Falls back
    to a detached ``herdr session attach`` if WezTerm CLI is unavailable.
    """
    session_name = str(namespace_ref.get('session_name') or '').strip()
    if not session_name:
        return

    herdr_exe = os.environ.get('CCB_HERDR_EXE', '').strip()
    if not herdr_exe:
        herdr_exe = shutil.which('herdr') or ''
    if not herdr_exe:
        return

    wezterm_cli = shutil.which('wezterm')
    if wezterm_cli:
        cwd = os.getcwd()
        try:
            subprocess.Popen(
                [wezterm_cli, 'cli', 'spawn', '--cwd', cwd, '--', herdr_exe, 'session', 'attach', session_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_subprocess_kwargs_herdr_ui(),
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass

    # Fallback: launch herdr as a standalone detached process.
    # Fire-and-forget with DEVNULL stdio — Herdr connects to the session server
    # and opens its own GUI/TUI window independently.
    try:
        subprocess.Popen(
            [herdr_exe, 'session', 'attach', session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_subprocess_kwargs_herdr_ui(),
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _herdr_attach_target_ready(payload: dict[str, object]) -> tuple[bool, str]:
    projection_detail = _herdr_surface_projection_detail(payload)
    if not bool(payload.get('namespace_ui_attachable')):
        return False, (
            'Herdr project namespace is not attachable after successful `ccb` start'
            f'{projection_detail}'
        )
    try:
        _herdr_namespace_ref_from_payload(payload)
    except ForegroundAttachError as exc:
        return False, f'{exc}{projection_detail}'
    return True, ''


def _herdr_namespace_ref_from_payload(payload: dict[str, object]) -> dict[str, object]:
    namespace_id = _clean_optional_payload_text(payload.get('namespace_id'))
    session_name = _clean_optional_payload_text(payload.get('namespace_session_name'))
    ipc_kind = _clean_optional_payload_text(payload.get('namespace_ipc_kind'))
    ipc_ref = _clean_optional_payload_text(payload.get('namespace_ipc_ref'))
    if not namespace_id or not session_name or ipc_kind != 'herdr_socket' or not ipc_ref:
        raise ForegroundAttachError(
            'foreground attach failed: Herdr namespace payload is incomplete '
            f'(namespace_id_present={bool(namespace_id)}, session_name_present={bool(session_name)}, '
            f'ipc_kind={ipc_kind}, ipc_ref_present={bool(ipc_ref)})'
        )
    return {
        'backend_family': 'herdr-native',
        'backend_impl': 'herdr',
        'namespace_id': namespace_id,
        'session_name': session_name,
        'ipc_kind': ipc_kind,
        'ipc_ref': ipc_ref,
        'restore_token': None,
    }


def _herdr_backend_selection_from_payload(payload: dict[str, object]) -> dict[str, object]:
    return {
        'backend_family': payload.get('namespace_backend_family') or 'herdr-native',
        'backend_impl': 'herdr',
        'ipc_kind': payload.get('namespace_ipc_kind'),
        'ipc_ref_present': bool(_clean_optional_payload_text(payload.get('namespace_ipc_ref'))),
        'namespace_restore_token_present': bool(payload.get('namespace_restore_token_present')),
    }


def _foreground_attach_summary_from_payload(context: CliContext, payload: dict[str, object]) -> ForegroundAttachSummary:
    tmux_socket_path = str(payload.get('namespace_tmux_socket_path') or '').strip()
    tmux_session_name = str(payload.get('namespace_tmux_session_name') or '').strip()
    return ForegroundAttachSummary(
        project_id=context.project.project_id,
        tmux_socket_path=tmux_socket_path,
        tmux_session_name=tmux_session_name,
        backend_impl=_payload_backend_impl(payload),
        namespace_id=_clean_optional_payload_text(payload.get('namespace_id')),
        session_name=_clean_optional_payload_text(payload.get('namespace_session_name')) or tmux_session_name,
        ipc_kind=_clean_optional_payload_text(payload.get('namespace_ipc_kind')),
        ipc_ref=_clean_optional_payload_text(payload.get('namespace_ipc_ref')) or tmux_socket_path,
        namespace_restore_token_present=bool(payload.get('namespace_restore_token_present')),
    )


def _payload_backend_impl(payload: dict[str, object]) -> str:
    return str(payload.get('namespace_backend_impl') or 'tmux').strip() or 'tmux'


def _herdr_surface_projection_detail(payload: dict[str, object]) -> str:
    projection = payload.get('herdr_surface_projection')
    if not isinstance(projection, Mapping):
        return ''
    parts = []
    for field, label in (
        ('capability_status', 'capability_status'),
        ('support_tier_projection', 'support_tier_projection'),
        ('support_tier_projection_source', 'support_tier_source'),
    ):
        value = _clean_optional_payload_text(projection.get(field))
        if value:
            parts.append(f'{label}={value}')
    beta_gaps = _projection_list_text(projection.get('beta_gaps'))
    if beta_gaps:
        parts.append(f'beta_gaps={beta_gaps}')
    blocking_gaps = _projection_list_text(projection.get('blocking_gaps'))
    if blocking_gaps:
        parts.append(f'blocking_gaps={blocking_gaps}')
    next_action = _clean_optional_payload_text(projection.get('degraded_next_action'))
    if next_action:
        parts.append(f'next_action={next_action}')
    return f' ({", ".join(parts)})' if parts else ''


def _build_herdr_attach_backend(*, namespace_ref: dict[str, object], backend_selection: dict[str, object]):
    from terminal_runtime import api as terminal_api

    backend = terminal_api.get_backend('herdr')
    if backend is None:
        raise ForegroundAttachError(
            'foreground attach failed: Herdr backend is unavailable '
            f'(backend_impl={backend_selection.get("backend_impl")}, ipc_kind={namespace_ref.get("ipc_kind")})'
        )
    namespace_builder = getattr(backend, 'namespace_ref', None)
    if not callable(namespace_builder):
        raise ForegroundAttachError(
            'foreground attach failed: Herdr backend cannot validate namespace ref '
            f'(backend_impl={backend_selection.get("backend_impl")}, ipc_kind={namespace_ref.get("ipc_kind")})'
        )
    try:
        resolved_ref = namespace_builder(
            str(namespace_ref.get('session_name') or ''),
            str(namespace_ref.get('namespace_id') or ''),
        )
    except Exception as exc:
        raise ForegroundAttachError(
            'foreground attach failed: Herdr backend namespace validation failed '
            f'(backend_impl={backend_selection.get("backend_impl")}, ipc_kind={namespace_ref.get("ipc_kind")}, '
            f'ipc_ref_present={bool(namespace_ref.get("ipc_ref"))}, detail={exc})'
        ) from exc
    if not isinstance(resolved_ref, Mapping):
        raise ForegroundAttachError(
            'foreground attach failed: Herdr backend namespace validation returned an invalid ref '
            f'(backend_impl={backend_selection.get("backend_impl")}, ipc_kind={namespace_ref.get("ipc_kind")})'
        )
    mismatched_fields = [
        field
        for field in ('backend_impl', 'namespace_id', 'session_name', 'ipc_kind', 'ipc_ref')
        if _clean_optional_payload_text(resolved_ref.get(field)) != _clean_optional_payload_text(namespace_ref.get(field))
    ]
    if mismatched_fields:
        raise ForegroundAttachError(
            'foreground attach failed: Herdr backend namespace ref mismatch '
            f'(backend_impl={backend_selection.get("backend_impl")}, ipc_kind={namespace_ref.get("ipc_kind")}, '
            f'ipc_ref_present={bool(namespace_ref.get("ipc_ref"))}, fields={",".join(mismatched_fields)})'
        )
    return backend


def _client_for_attach_attempt(client, *, timeout_s: float):
    with_timeout = getattr(client, 'with_timeout', None)
    if callable(with_timeout):
        return with_timeout(timeout_s)
    return client


def _clean_optional_payload_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def _projection_list_text(value: object) -> str | None:
    if isinstance(value, (str, bytes)):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    texts = [text for item in items if (text := _clean_optional_payload_text(item))]
    return ','.join(texts) if texts else None


def _attach_target_unavailable_error(*, attempts: int, timeout_s: float) -> str:
    return (
        'foreground attach timed out: project namespace did not become '
        f'attachable within {timeout_s:.1f}s after successful `ccb` start '
        f'(attempts={attempts})'
    )


def _attach_ping_timeout_error(
    exc: Exception,
    *,
    attempts: int,
    timeout_s: float,
    rpc_timeout_s: float,
) -> str:
    detail = str(exc or '').strip() or type(exc).__name__
    return (
        'foreground attach timed out: ccbd did not respond to ping '
        f'within {timeout_s:.1f}s after successful `ccb` start '
        f'(rpc_timeout={rpc_timeout_s:.1f}s, attempts={attempts}, last_error={detail})'
    )


def _attach_namespace_timeout_error(
    error: str,
    *,
    attempts: int,
    ping_successes: int,
    timeout_s: float,
) -> str:
    detail = str(error or '').strip() or 'project namespace is not attachable'
    return (
        'foreground attach timed out: ccbd is responsive but project namespace '
        f'was not attachable within {timeout_s:.1f}s after successful `ccb` start '
        f'(attempts={attempts}, ping_successes={ping_successes}, last_error={detail})'
    )


def _tmux_list_client_pids(
    tmux_socket_path: str,
    tmux_session_name: str,
    *,
    env: dict[str, str],
) -> tuple[int, ...]:
    probe = subprocess.run(
        _tmux_cmd(tmux_socket_path, 'list-clients', '-t', tmux_session_name, '-F', '#{client_pid}'),
        check=False,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if probe.returncode != 0:
        return ()
    client_pids: list[int] = []
    for line in (probe.stdout or '').splitlines():
        value = line.strip()
        if not value:
            continue
        try:
            client_pids.append(int(value))
        except ValueError:
            continue
    return tuple(client_pids)


def _tmux_client_tty(
    tmux_socket_path: str,
    tmux_session_name: str,
    *,
    client_pid: int,
    env: dict[str, str],
) -> str | None:
    probe = subprocess.run(
        _tmux_cmd(tmux_socket_path, 'list-clients', '-t', tmux_session_name, '-F', '#{client_pid}\t#{client_tty}'),
        check=False,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if probe.returncode != 0:
        return None
    for line in (probe.stdout or '').splitlines():
        pid_text, _sep, tty_text = line.partition('\t')
        try:
            listed_pid = int(pid_text.strip())
        except ValueError:
            continue
        if listed_pid != client_pid:
            continue
        tty = tty_text.strip()
        return tty or None
    return None


def _best_effort_refresh_attached_client(
    tmux_socket_path: str,
    tmux_session_name: str,
    *,
    client_pid: int,
    env: dict[str, str],
) -> None:
    client_tty = _tmux_client_tty(
        tmux_socket_path,
        tmux_session_name,
        client_pid=client_pid,
        env=env,
    )
    if not client_tty:
        return
    try:
        subprocess.run(
            _tmux_cmd(tmux_socket_path, 'refresh-client', '-t', client_tty),
            check=False,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return


def _foreground_attach_client(context: CliContext):
    try:
        return _build_foreground_attach_client(context.paths.ccbd_socket_path)
    except CcbdClientError as exc:
        raise ForegroundAttachError(
            'foreground attach failed: ccbd client is unavailable '
            f'after successful `ccb` start: {exc}'
        ) from exc


def _build_foreground_attach_client(socket_path):
    return CcbdClient(socket_path, timeout_s=FOREGROUND_ATTACH_RPC_TIMEOUT_S)


def _attach_env() -> dict[str, str]:
    env = tmux_compatible_env()
    env.pop('TMUX', None)
    env.pop('TMUX_PANE', None)
    return env


def _tmux_cmd(tmux_socket_path: str, *args: str) -> list[str]:
    return [*tmux_base(socket_path=tmux_socket_path), *args]


def _tmux_has_session(tmux_socket_path: str, tmux_session_name: str, *, env: dict[str, str]) -> bool:
    probe = subprocess.run(
        _tmux_cmd(tmux_socket_path, 'has-session', '-t', tmux_session_name),
        check=False,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return probe.returncode == 0


def _tmux_select_window(tmux_socket_path: str, target: str, *, env: dict[str, str]) -> bool:
    probe = subprocess.run(
        _tmux_cmd(tmux_socket_path, 'select-window', '-t', target),
        check=False,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return probe.returncode == 0


def _subprocess_kwargs_herdr_ui() -> dict[str, object]:
    """Hide only the control wrapper; WezTerm/Herdr attach remains intentional UI."""
    if sys.platform == 'win32':
        return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}
    return {}


__all__ = [
    'ForegroundAttachError',
    'ForegroundAttachSummary',
    'attach_started_project_namespace',
]
