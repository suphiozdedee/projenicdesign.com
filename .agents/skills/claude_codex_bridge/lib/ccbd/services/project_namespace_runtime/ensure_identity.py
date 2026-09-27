from __future__ import annotations

import time

from cli.services.tmux_ui import apply_project_tmux_ui

from .backend import (
    _is_mux_backend,
    apply_pane_identity,
    create_session,
    ensure_server_policy,
    ensure_window,
    list_windows as list_windows_for_session,
    session_window_target,
    window_root_pane,
)


def prepare_namespace_root_pane(
    controller,
    context,
    *,
    epoch: int,
    terminal_size: tuple[int, int] | None = None,
    timeout_s: float | None = None,
) -> bool:
    if not context.session_is_alive:
        create_session(
            context.backend,
            session_name=context.desired_session_name,
            project_root=controller._layout.project_root,
            window_name=context.desired_control_window_name,
            terminal_size=terminal_size,
            timeout_s=timeout_s,
        )
        verified = _verify_herdr_session_socket(
            context.backend,
            session_name=context.desired_session_name,
            timeout_s=timeout_s,
        )
    else:
        verified = True
    ensure_server_policy(context.backend, timeout_s=timeout_s)
    ensure_window(
        context.backend,
        session_name=context.desired_session_name,
        window_name=context.desired_control_window_name,
        project_root=controller._layout.project_root,
        select=False,
        timeout_s=timeout_s,
    )
    ensure_window(
        context.backend,
        session_name=context.desired_session_name,
        window_name=context.desired_workspace_window_name,
        project_root=controller._layout.project_root,
        select=True,
        timeout_s=timeout_s,
    )
    root_pane = window_root_pane(
        context.backend,
        target_window=session_window_target(
            context.desired_session_name,
            context.desired_workspace_window_name,
        ),
        timeout_s=timeout_s,
    )
    apply_namespace_identity(
        controller,
        backend=context.backend,
        pane_id=root_pane,
        namespace_epoch=epoch,
        tmux_socket_path=context.desired_socket_path,
        tmux_session_name=context.desired_session_name,
    )
    return bool(verified)


def apply_namespace_identity(
    controller,
    *,
    backend,
    pane_id: str,
    namespace_epoch: int,
    tmux_socket_path: str,
    tmux_session_name: str,
) -> None:
    apply_pane_identity(
        backend,
        pane_id=pane_id,
        title='cmd',
        agent_label='cmd',
        project_id=controller._project_id,
        is_cmd=True,
        slot_key='cmd',
        namespace_epoch=namespace_epoch,
        managed_by='ccbd',
    )
    if callable(getattr(backend, '_tmux_run', None)):
        apply_project_tmux_ui(
            tmux_socket_path=tmux_socket_path,
            ccbd_socket_path=str(controller._layout.ccbd_socket_path),
            tmux_session_name=tmux_session_name,
            backend=backend,
        )


def _verify_herdr_session_socket(backend, *, session_name: str, timeout_s: float | None = None) -> bool:
    """After create_session for a Herdr backend, verify the session socket is
    actually reachable.  The Herdr server process may need a brief moment after
    the initial readiness probe before its IPC socket accepts requests.  We try
    a lightweight operation (list_windows) with retries; failures are logged but
    not raised — the session may still become available later.

    Returns True when the live socket was verified reachable, False otherwise.
    Callers should surface False so a "mounted but not live" state is never
    misreported as a fully verified mount."""
    if not _is_mux_backend(backend):
        return True
    if timeout_s is not None and timeout_s <= 0:
        return True
    deadline = time.monotonic() + (timeout_s if timeout_s is not None and timeout_s > 0 else 10.0)
    attempt = 0
    while True:
        attempt += 1
        try:
            # Use list_windows_for_session (from backend.py) which correctly
            # builds a namespace_ref payload including namespace_id.  Calling
            # backend.list_windows() directly with a bare {'session_name': ...}
            # dict would bypass _mux_namespace_ref and return [] immediately
            # without ever touching the Herdr socket.
            list_windows_for_session(backend, session_name=session_name)
            return True
        except Exception:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                import logging
                _logger = logging.getLogger(__name__)
                _logger.warning(
                    "Herdr session %s socket not reachable after %d attempts; "
                    "session may become available later",
                    session_name, attempt,
                )
                return False
            time.sleep(min(0.5, remaining))


__all__ = ['apply_namespace_identity', 'prepare_namespace_root_pane']
