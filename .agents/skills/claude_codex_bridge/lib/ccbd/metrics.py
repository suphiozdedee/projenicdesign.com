from __future__ import annotations

from dataclasses import dataclass, field
import os
import threading


_PAGE_SIZE = os.sysconf('SC_PAGE_SIZE') if hasattr(os, 'sysconf') else 4096


@dataclass
class ControlPlaneMetrics:
    last_request_queue_wait_s: float | None = None
    last_submit_duration_s: float | None = None
    last_ping_duration_s: float | None = None
    last_handler_latency_s_by_op: dict[str, float] = field(default_factory=dict)
    last_maintenance_duration_s: float | None = None
    last_heartbeat_duration_s: float | None = None
    heartbeat_step_duration_s: dict[str, float] = field(default_factory=dict)
    last_heartbeat_agents_inspected: int | None = None
    last_heartbeat_runtime_store_writes: int | None = None
    pending_maintenance_ticks: int = 0
    last_project_view_response_duration_s: float | None = None
    last_project_view_build_duration_s: float | None = None
    project_view_cache_hits: int = 0
    project_view_cache_misses: int = 0
    last_project_view_tmux_command_count: int | None = None
    last_project_view_capture_pane_count: int | None = None
    last_project_view_store_scan_count: int | None = None
    project_view_sidebar_refreshes: int = 0
    project_view_sidebar_refresh_failures: int = 0
    last_project_view_sidebar_refresh_duration_s: float | None = None
    service_graph_version: int | None = None
    service_graph_created_at: str | None = None
    service_graph_retained_count: int | None = None
    service_graph_retained_count_scope: str | None = None
    last_reload_duration_s: float | None = None
    last_reload_plan_class: str | None = None
    last_reload_error: str | None = None

    def process_snapshot(self) -> dict[str, int | None]:
        return {
            'rss_bytes': _rss_bytes(),
            'virtual_memory_bytes': _virtual_memory_bytes(),
            'fd_count': _fd_count(),
            'thread_count': threading.active_count(),
        }


def _rss_bytes() -> int | None:
    status = _proc_status()
    if status:
        value = _status_kb(status, 'VmRSS:')
        if value is not None:
            return value * 1024
    statm = _proc_statm()
    if statm:
        parts = statm.split()
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1]) * int(_PAGE_SIZE)
    return None


def _virtual_memory_bytes() -> int | None:
    status = _proc_status()
    if status:
        value = _status_kb(status, 'VmSize:')
        if value is not None:
            return value * 1024
    statm = _proc_statm()
    if statm:
        parts = statm.split()
        if parts and parts[0].isdigit():
            return int(parts[0]) * int(_PAGE_SIZE)
    return None


def _fd_count() -> int | None:
    try:
        return len(tuple(os.scandir('/proc/self/fd')))
    except Exception:
        return None


def _proc_status() -> str | None:
    try:
        with open('/proc/self/status', 'r', encoding='utf-8') as handle:
            return handle.read()
    except Exception:
        return None


def _proc_statm() -> str | None:
    try:
        with open('/proc/self/statm', 'r', encoding='utf-8') as handle:
            return handle.read()
    except Exception:
        return None


def _status_kb(status: str, key: str) -> int | None:
    for line in status.splitlines():
        if not line.startswith(key):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
        return None
    return None


__all__ = ['ControlPlaneMetrics']
