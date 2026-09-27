from __future__ import annotations

from terminal_runtime.backend_selection import TerminalBackendSelection, TerminalLayoutService


def resolve_backend(
    *,
    cached_backend,
    terminal_type,
    detect_terminal_fn,
    tmux_backend_factory,
    herdr_backend_factory=None,
    platform_gate_fn=None,
    herdr_capability_report_fn=None,
    herdr_capability_report_ref_fn=None,
):
    return TerminalBackendSelection(
        detect_terminal_fn=detect_terminal_fn,
        tmux_backend_factory=tmux_backend_factory,
        herdr_backend_factory=herdr_backend_factory,
        platform_gate_fn=platform_gate_fn,
        herdr_capability_report_fn=herdr_capability_report_fn,
        herdr_capability_report_ref_fn=herdr_capability_report_ref_fn,
        cached_backend=cached_backend,
    ).get_backend(terminal_type)


def resolve_backend_for_session(
    *,
    session_data: dict,
    detect_terminal_fn,
    tmux_backend_factory,
    herdr_backend_factory=None,
):
    return TerminalBackendSelection(
        detect_terminal_fn=detect_terminal_fn,
        tmux_backend_factory=tmux_backend_factory,
        herdr_backend_factory=herdr_backend_factory,
    ).get_backend_for_session(session_data)


def resolve_pane_id_from_session(session_data: dict):
    return TerminalBackendSelection.get_pane_id_from_session(session_data)


def create_layout(
    *,
    providers: list[str],
    cwd: str,
    root_pane_id: str | None,
    tmux_session_name: str | None,
    percent: int,
    set_markers: bool,
    marker_prefix: str,
    tmux_backend_factory,
    detached_session_name_fn,
    env,
):
    return TerminalLayoutService(
        tmux_backend_factory=tmux_backend_factory,
        detached_session_name_fn=detached_session_name_fn,
        env=env,
    ).create_auto_layout(
        providers,
        cwd=cwd,
        root_pane_id=root_pane_id,
        tmux_session_name=tmux_session_name,
        percent=percent,
        set_markers=set_markers,
        marker_prefix=marker_prefix,
    )
