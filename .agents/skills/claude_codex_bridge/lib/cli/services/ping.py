from __future__ import annotations

from ccbd.models import LeaseHealth
from cli.context import CliContext
from cli.models import ParsedPingCommand

from .daemon import connect_mounted_daemon, ping_local_state


def ping_target(context: CliContext, command: ParsedPingCommand) -> dict:
    local = ping_local_state(context)
    target = command.target
    if local.mount_state == 'unmounted':
        if target == 'ccbd':
            return {
                'project_id': local.project_id,
                'mount_state': local.mount_state,
                'health': local.health,
                'generation': local.generation,
                'project_anchor_path': local.project_anchor_path,
                'runtime_state_root': local.runtime_state_root,
                'runtime_root_kind': local.runtime_root_kind,
                'runtime_relocation_reason': local.runtime_relocation_reason,
                'runtime_filesystem_hint': local.runtime_filesystem_hint,
                'runtime_marker_status': local.runtime_marker_status,
                'socket_path': local.socket_path,
                'preferred_socket_path': local.preferred_socket_path,
                'effective_socket_path': local.effective_socket_path,
                'socket_root_kind': local.socket_root_kind,
                'socket_fallback_reason': local.socket_fallback_reason,
                'socket_filesystem_hint': local.socket_filesystem_hint,
                'tmux_socket_path': local.tmux_socket_path,
                'tmux_preferred_socket_path': local.tmux_preferred_socket_path,
                'tmux_effective_socket_path': local.tmux_effective_socket_path,
                'tmux_socket_root_kind': local.tmux_socket_root_kind,
                'tmux_socket_fallback_reason': local.tmux_socket_fallback_reason,
                'tmux_socket_filesystem_hint': local.tmux_socket_filesystem_hint,
                'last_heartbeat_at': local.last_heartbeat_at,
                'pid_alive': local.pid_alive,
                'socket_connectable': local.socket_connectable,
                'heartbeat_fresh': local.heartbeat_fresh,
                'takeover_allowed': local.takeover_allowed,
                'reason': local.reason,
                'startup_id': local.startup_id,
                'startup_stage': local.startup_stage,
                'last_progress_at': local.last_progress_at,
                'startup_deadline_at': local.startup_deadline_at,
                'last_failure_reason': local.last_failure_reason,
                'shutdown_intent': local.shutdown_intent,
                'last_request_queue_wait_s': None,
                'last_submit_duration_s': None,
                'last_ping_duration_s': None,
                'last_maintenance_duration_s': None,
                'last_heartbeat_duration_s': None,
                'pending_maintenance_ticks': None,
                'service_graph_version': None,
                'service_graph_created_at': None,
                'service_graph_retained_count': None,
                'service_graph_retained_count_scope': None,
            }
        return {
            'project_id': local.project_id,
            'agent_name': target,
            'provider': None,
            'mount_state': local.mount_state,
            'runtime_state': 'stopped',
            'health': 'unmounted',
            'diagnostics': {'reason': local.reason},
        }
    handle = connect_mounted_daemon(context, allow_restart_stale=(target == 'ccbd'))
    assert handle.client is not None
    payload = handle.client.ping(target)
    if target == 'ccbd':
        diagnostics = dict(payload.pop('diagnostics', {}) or {})
        payload.update(diagnostics)
    return payload
