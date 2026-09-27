from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import tempfile

from terminal_runtime.tmux import tmux_base
from platforms.windows.herdr.surface import (
    herdr_surface_projection_from_namespace_state,
    herdr_surface_projection_from_remote,
)
from .stores import report_summary_fields, safe_report_load


def ccbd_summary(*, local, stores: dict[str, object], errors: list[str], remote: dict | None = None) -> dict:
    implementation = _implementation_summary(getattr(local, 'ccbd_pid', None))
    namespace_state = safe_report_load(stores['namespace_state'].load, errors, label='namespace_state')
    namespace_event = safe_report_load(stores['namespace_event'].load_latest, errors, label='namespace_event')
    herdr_projection = (
        herdr_surface_projection_from_remote(remote)
        or herdr_surface_projection_from_namespace_state(namespace_state)
    )
    return {
        'state': local.mount_state,
        'pid': local.ccbd_pid,
        'keeper_pid': local.keeper_pid,
        'implementation_root': implementation['root'],
        'implementation_status': implementation['status'],
        'implementation_reason': implementation['reason'],
        'implementation_cmdline': implementation['cmdline'],
        'socket_path': local.socket_path,
        'project_anchor_path': local.project_anchor_path,
        'runtime_state_root': local.runtime_state_root,
        'runtime_root_kind': local.runtime_root_kind,
        'runtime_relocation_reason': local.runtime_relocation_reason,
        'runtime_filesystem_hint': local.runtime_filesystem_hint,
        'runtime_marker_status': local.runtime_marker_status,
        'preferred_socket_path': local.preferred_socket_path,
        'effective_socket_path': local.effective_socket_path,
        'preferred_socket_path_bytes': _path_bytes(local.preferred_socket_path),
        'effective_socket_path_bytes': _path_bytes(local.effective_socket_path),
        'socket_root_kind': local.socket_root_kind,
        'socket_fallback_reason': local.socket_fallback_reason,
        'socket_filesystem_hint': local.socket_filesystem_hint,
        'tmux_socket_path': local.tmux_socket_path,
        'tmux_preferred_socket_path': local.tmux_preferred_socket_path,
        'tmux_effective_socket_path': local.tmux_effective_socket_path,
        'tmux_preferred_socket_path_bytes': _path_bytes(local.tmux_preferred_socket_path),
        'tmux_effective_socket_path_bytes': _path_bytes(local.tmux_effective_socket_path),
        'tmux_start_server_command': _tmux_start_server_command(local.tmux_effective_socket_path),
        'tmux_socket_root_kind': local.tmux_socket_root_kind,
        'tmux_socket_fallback_reason': local.tmux_socket_fallback_reason,
        'tmux_socket_filesystem_hint': local.tmux_socket_filesystem_hint,
        'generation': local.generation,
        'health': local.health,
        'last_heartbeat_at': local.last_heartbeat_at,
        'pid_alive': local.pid_alive,
        'socket_connectable': local.socket_connectable,
        'heartbeat_fresh': local.heartbeat_fresh,
        'takeover_allowed': local.takeover_allowed,
        'reason': local.reason,
        'last_request_queue_wait_s': _remote_metric(remote, 'last_request_queue_wait_s'),
        'last_submit_duration_s': _remote_metric(remote, 'last_submit_duration_s'),
        'last_ping_duration_s': _remote_metric(remote, 'last_ping_duration_s'),
        'last_handler_latency_s_by_op': _remote_mapping(remote, 'last_handler_latency_s_by_op'),
        'last_maintenance_duration_s': _remote_metric(remote, 'last_maintenance_duration_s'),
        'last_heartbeat_duration_s': _remote_metric(remote, 'last_heartbeat_duration_s'),
        'heartbeat_step_duration_s': _remote_mapping(remote, 'heartbeat_step_duration_s'),
        'last_heartbeat_agents_inspected': _remote_metric(remote, 'last_heartbeat_agents_inspected'),
        'last_heartbeat_runtime_store_writes': _remote_metric(
            remote,
            'last_heartbeat_runtime_store_writes',
        ),
        'pending_maintenance_ticks': _remote_metric(remote, 'pending_maintenance_ticks'),
        'last_project_view_response_duration_s': _remote_metric(remote, 'last_project_view_response_duration_s'),
        'last_project_view_build_duration_s': _remote_metric(remote, 'last_project_view_build_duration_s'),
        'project_view_cache_hits': _remote_metric(remote, 'project_view_cache_hits'),
        'project_view_cache_misses': _remote_metric(remote, 'project_view_cache_misses'),
        'last_project_view_tmux_command_count': _remote_metric(remote, 'last_project_view_tmux_command_count'),
        'last_project_view_capture_pane_count': _remote_metric(remote, 'last_project_view_capture_pane_count'),
        'last_project_view_store_scan_count': _remote_metric(remote, 'last_project_view_store_scan_count'),
        'rss_bytes': _remote_metric(remote, 'rss_bytes'),
        'virtual_memory_bytes': _remote_metric(remote, 'virtual_memory_bytes'),
        'fd_count': _remote_metric(remote, 'fd_count'),
        'thread_count': _remote_metric(remote, 'thread_count'),
        'service_graph_version': _remote_value(remote, 'service_graph_version'),
        'service_graph_created_at': _remote_value(remote, 'service_graph_created_at'),
        'service_graph_retained_count': _remote_value(remote, 'service_graph_retained_count'),
        'service_graph_retained_count_scope': _remote_value(remote, 'service_graph_retained_count_scope'),
        'last_reload_duration_s': _remote_metric(remote, 'last_reload_duration_s'),
        'last_reload_plan_class': _remote_value(remote, 'last_reload_plan_class'),
        'last_reload_error': _remote_value(remote, 'last_reload_error'),
        'startup_id': local.startup_id,
        'startup_stage': local.startup_stage,
        'last_progress_at': local.last_progress_at,
        'startup_deadline_at': local.startup_deadline_at,
        **stores['execution_state'].summary(),
        **report_summary_fields(safe_report_load(stores['restore_report'].load, errors, label='restore_report')),
        **report_summary_fields(safe_report_load(stores['startup_report'].load, errors, label='startup_report')),
        **report_summary_fields(safe_report_load(stores['shutdown_report'].load, errors, label='shutdown_report')),
        **report_summary_fields(namespace_state),
        **report_summary_fields(namespace_event),
        **({'herdr_surface_projection': herdr_projection} if herdr_projection is not None else {}),
        **report_summary_fields(safe_report_load(stores['start_policy'].load, errors, label='start_policy')),
        **report_summary_fields(safe_report_load(stores['tmux_cleanup'].load_latest, errors, label='tmux_cleanup')),
        'diagnostic_errors': errors,
    }


def _path_bytes(path: object) -> int | None:
    text = str(path or '').strip()
    if not text:
        return None
    return len(os.fsencode(text))


def _implementation_summary(pid: object) -> dict[str, object]:
    numeric_pid = _coerce_pid(pid)
    if numeric_pid is None:
        return {'root': None, 'status': 'unknown', 'reason': 'pid_unavailable', 'cmdline': None}
    cmdline = _process_cmdline(numeric_pid)
    if not cmdline:
        return {'root': None, 'status': 'unknown', 'reason': 'cmdline_unavailable', 'cmdline': None}
    root = _implementation_root_from_cmdline(cmdline)
    if root is None:
        return {
            'root': None,
            'status': 'unknown',
            'reason': 'ccbd_entrypoint_not_found_in_cmdline',
            'cmdline': shlex.join(cmdline),
        }
    if _path_is_temporary(root):
        return {
            'root': str(root),
            'status': 'degraded',
            'reason': 'ccbd_implementation_root_is_temporary',
            'cmdline': shlex.join(cmdline),
        }
    return {
        'root': str(root),
        'status': 'ok',
        'reason': 'ccbd_implementation_root_is_durable',
        'cmdline': shlex.join(cmdline),
    }


def _coerce_pid(pid: object) -> int | None:
    try:
        value = int(str(pid).strip())
    except Exception:
        return None
    return value if value > 0 else None


def _process_cmdline(pid: int, *, proc_root: Path = Path('/proc')) -> tuple[str, ...]:
    if os.name == 'nt':
        return ()
    try:
        raw = (proc_root / str(pid) / 'cmdline').read_bytes()
    except Exception:
        return ()
    return tuple(part.decode(errors='replace') for part in raw.split(b'\0') if part)


def _implementation_root_from_cmdline(cmdline: tuple[str, ...]) -> Path | None:
    for arg in cmdline:
        path = Path(arg)
        if path.parts[-3:] in {('lib', 'ccbd', 'main.py'), ('lib', 'ccbd', 'keeper_main.py')}:
            try:
                return path.expanduser().resolve(strict=False).parents[2]
            except Exception:
                return path.expanduser().parents[2]
    return None


def _path_is_temporary(path: Path) -> bool:
    text = path.as_posix()
    temporary_roots = ('/tmp', '/var/tmp', '/dev/shm', '/private/tmp', _resolved_tempdir())
    return any(
        text == root
        or text.startswith(f'{root}/')
        or re.match(rf'^[A-Za-z]:{re.escape(root)}(?:/|$)', text)
        for root in temporary_roots
    )


def _resolved_tempdir() -> str:
    try:
        return Path(tempfile.gettempdir()).expanduser().resolve(strict=False).as_posix()
    except Exception:
        return Path(tempfile.gettempdir()).expanduser().as_posix()


def _tmux_start_server_command(socket_path: object) -> str | None:
    text = str(socket_path or '').strip()
    if not text:
        return None
    return shlex.join([*tmux_base(socket_path=text), 'start-server'])


def _remote_metric(remote: dict | None, key: str) -> float | None:
    if not isinstance(remote, dict):
        return None
    diagnostics = remote.get('diagnostics')
    if not isinstance(diagnostics, dict):
        return None
    value = diagnostics.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _remote_mapping(remote: dict | None, key: str) -> dict:
    value = _remote_value(remote, key)
    return dict(value) if isinstance(value, dict) else {}


def _remote_value(remote: dict | None, key: str):
    if not isinstance(remote, dict):
        return None
    diagnostics = remote.get('diagnostics')
    if not isinstance(diagnostics, dict):
        return None
    return diagnostics.get(key)


__all__ = ['ccbd_summary']
