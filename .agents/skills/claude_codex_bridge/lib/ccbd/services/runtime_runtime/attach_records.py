from __future__ import annotations

from dataclasses import replace

from agents.models import AgentRuntime

from .attach_models import AttachRuntimeValues
from .common import ACTIVE_RUNTIME_STATES


def should_update_existing(existing) -> bool:
    return existing is not None and existing.state in ACTIVE_RUNTIME_STATES


def updated_runtime(existing, *, values: AttachRuntimeValues, timestamp: str, project_id: str) -> AgentRuntime:
    return replace(
        existing,
        state=values.state,
        started_at=(timestamp if values.authority_epoch_changed else existing.started_at),
        last_seen_at=timestamp,
        pid=values.runtime_pid,
        workspace_path=values.workspace_path,
        backend_type=values.backend_type or existing.backend_type,
        runtime_ref=values.runtime_ref,
        session_ref=values.session_ref,
        project_id=project_id,
        health=values.health,
        provider=values.provider,
        runtime_root=values.runtime_root,
        runtime_pid=values.runtime_pid,
        terminal_backend=values.terminal_backend,
        provider_runtime_backend_ref=values.provider_runtime_backend_ref,
        namespace_ref=values.namespace_ref,
        pane_ref=values.pane_ref,
        namespace_restore_token_present=values.namespace_restore_token_present,
        herdr_auto_restore_mode=values.herdr_auto_restore_mode,
        herdr_agent_state_ref=values.herdr_agent_state_ref,
        pane_id=values.pane_id,
        active_pane_id=values.active_pane_id,
        pane_title_marker=values.pane_title_marker,
        pane_state=values.pane_state,
        tmux_socket_name=values.tmux_socket_name,
        tmux_socket_path=values.tmux_socket_path,
        tmux_window_name=values.tmux_window_name,
        tmux_window_id=values.tmux_window_id,
        session_file=values.session_file,
        session_id=values.session_id,
        slot_key=values.slot_key,
        window_id=values.window_id,
        workspace_epoch=values.workspace_epoch,
        lifecycle_state=values.lifecycle_state,
        binding_generation=values.binding_generation,
        daemon_generation=values.daemon_generation,
        runtime_generation=values.runtime_generation,
        managed_by=values.managed_by,
        binding_source=values.binding_source,
    )


def new_runtime(
    *,
    spec_name: str,
    existing,
    values: AttachRuntimeValues,
    timestamp: str,
    project_id: str,
) -> AgentRuntime:
    return AgentRuntime(
        agent_name=spec_name,
        state=values.state,
        pid=values.runtime_pid,
        started_at=timestamp,
        last_seen_at=timestamp,
        runtime_ref=values.runtime_ref,
        session_ref=values.session_ref,
        workspace_path=values.workspace_path,
        project_id=project_id,
        backend_type=values.backend_type,
        queue_depth=existing.queue_depth if existing else 0,
        socket_path=existing.socket_path if existing else None,
        health=values.health,
        provider=values.provider,
        runtime_root=values.runtime_root,
        runtime_pid=values.runtime_pid,
        terminal_backend=values.terminal_backend,
        provider_runtime_backend_ref=values.provider_runtime_backend_ref,
        namespace_ref=values.namespace_ref,
        pane_ref=values.pane_ref,
        namespace_restore_token_present=values.namespace_restore_token_present,
        herdr_auto_restore_mode=values.herdr_auto_restore_mode,
        herdr_agent_state_ref=values.herdr_agent_state_ref,
        pane_id=values.pane_id,
        active_pane_id=values.active_pane_id,
        pane_title_marker=values.pane_title_marker,
        pane_state=values.pane_state,
        tmux_socket_name=values.tmux_socket_name,
        tmux_socket_path=values.tmux_socket_path,
        tmux_window_name=values.tmux_window_name,
        tmux_window_id=values.tmux_window_id,
        session_file=values.session_file,
        session_id=values.session_id,
        slot_key=values.slot_key,
        window_id=values.window_id,
        workspace_epoch=values.workspace_epoch,
        lifecycle_state=values.lifecycle_state,
        binding_generation=values.binding_generation,
        daemon_generation=values.daemon_generation,
        runtime_generation=values.runtime_generation,
        managed_by=values.managed_by,
        binding_source=values.binding_source,
    )


__all__ = ['new_runtime', 'should_update_existing', 'updated_runtime']
