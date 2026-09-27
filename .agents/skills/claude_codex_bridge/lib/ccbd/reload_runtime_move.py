from __future__ import annotations

from ccbd.reload_runtime_mount_models import AdditiveRuntimeMountResult, blocked_mount_result, moved_result


def run_moved_agent_runtime_updates(app, graph, *, patch_result) -> AdditiveRuntimeMountResult:
    del app
    moved_agents = tuple(sorted((getattr(patch_result, 'moved_agents', {}) or {}).keys()))
    preserved_agents = tuple(sorted((getattr(patch_result, 'preserved_before', {}) or {}).keys()))
    if not moved_agents:
        return AdditiveRuntimeMountResult(
            status='noop',
            preserved_runtime_unchanged_agents=preserved_agents,
            diagnostics={
                'reason': 'no_moved_agent_panes',
                'graph_published': False,
                'lease_or_lifecycle_written': False,
                'config_watch_started': False,
                'cleanup_tmux_orphans': False,
            },
        )
    written: list[str] = []
    panes = dict(getattr(patch_result, 'moved_agents', {}) or {})
    windows = dict(getattr(patch_result, 'moved_agent_windows', {}) or {})
    runtime_service = getattr(graph, 'runtime_service', None)
    registry = getattr(graph, 'registry', None)
    if runtime_service is None or registry is None:
        return blocked_mount_result('runtime_service_missing', 'runtime move updates require a target runtime service', requested_agents=moved_agents)
    for agent_name in moved_agents:
        runtime = registry.get(agent_name)
        if runtime is None:
            return blocked_mount_result('runtime_authority_missing', f'cannot update moved agent without runtime authority: {agent_name}', requested_agents=moved_agents)
        window_name = str(windows.get(agent_name) or '').strip()
        pane_id = str(panes.get(agent_name) or '').strip()
        if not window_name or not pane_id:
            return blocked_mount_result('move_evidence_missing', f'move evidence is incomplete for agent: {agent_name}', requested_agents=moved_agents)
        updated = runtime_service.mutate_runtime_authority(
            runtime,
            pane_id=pane_id,
            active_pane_id=pane_id,
            tmux_window_name=window_name,
        )
        if updated != runtime:
            written.append(agent_name)
    return moved_result(
        requested_agents=moved_agents,
        moved_agents=moved_agents,
        written_agents=tuple(written),
        preserved_agents=tuple(agent for agent in preserved_agents if agent not in set(moved_agents)),
    )


__all__ = ['run_moved_agent_runtime_updates']
