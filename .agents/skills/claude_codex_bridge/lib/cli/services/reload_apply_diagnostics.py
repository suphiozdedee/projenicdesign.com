from __future__ import annotations


def reload_apply_summary(payload: dict[str, object], *, action: str) -> dict[str, object]:
    namespace_patch = dict(payload.get('namespace_patch') or {})
    runtime_mount = dict(payload.get('runtime_mount') or {})
    summary = {
        'apply_status': 'applied',
        'action': action,
        'reload_status': payload.get('status'),
        'plan_class': payload.get('plan_class'),
        'stage': payload.get('stage'),
        'published_graph_version': payload.get('published_graph_version'),
        'namespace_patch_status': namespace_patch.get('status'),
        'namespace_agent_panes': dict(namespace_patch.get('agent_panes') or {}),
        'namespace_removed_agents': dict(namespace_patch.get('removed_agents') or {}),
        'namespace_removed_panes': list(namespace_patch.get('removed_panes') or ()),
        'namespace_removed_windows': list(namespace_patch.get('removed_windows') or ()),
        'namespace_moved_agents': dict(namespace_patch.get('moved_agents') or {}),
        'namespace_moved_agent_windows': dict(namespace_patch.get('moved_agent_windows') or {}),
        'namespace_reflowed_windows': list(namespace_patch.get('reflowed_windows') or ()),
        'namespace_reflow_errors': dict(namespace_patch.get('reflow_errors') or {}),
        'namespace_preserved_before': dict(namespace_patch.get('preserved_before') or {}),
        'namespace_preserved_after': dict(namespace_patch.get('preserved_after') or {}),
        'runtime_mount_status': runtime_mount.get('status'),
        'mounted_agents': list(runtime_mount.get('mounted_agents') or ()),
        'moved_agents': list(runtime_mount.get('moved_agents') or ()),
        'unloaded_agents': list(runtime_mount.get('unloaded_agents') or ()),
        'runtime_authority_written_agents': list(runtime_mount.get('runtime_authority_written_agents') or ()),
        'runtime_authority_moved_agents': list(runtime_mount.get('runtime_authority_moved_agents') or ()),
        'runtime_authority_stopped_agents': list(runtime_mount.get('runtime_authority_stopped_agents') or ()),
        'pane_identity_report': pane_identity_report(namespace_patch, runtime_mount),
    }
    return summary


def pane_identity_report(namespace_patch: dict[str, object], runtime_mount: dict[str, object]) -> dict[str, object]:
    preserved_before = dict(namespace_patch.get('preserved_before') or {})
    preserved_after = dict(namespace_patch.get('preserved_after') or {})
    preserved_agents = sorted({*preserved_before.keys(), *preserved_after.keys()})
    return {
        'status': 'reported',
        'source': 'reload_namespace_patch',
        'added_agents': _agent_panes(namespace_patch.get('agent_panes'), source='namespace_agent_panes'),
        'removed_agents': _agent_panes(namespace_patch.get('removed_agents'), source='namespace_removed_agents'),
        'moved_agents': _moved_agent_panes(namespace_patch),
        'preserved_agents': [
            {
                'agent': agent,
                'before_pane_id': preserved_before.get(agent),
                'after_pane_id': preserved_after.get(agent),
                'pane_identity_source': 'namespace_preserved_before_after',
                'changed': preserved_before.get(agent) != preserved_after.get(agent),
            }
            for agent in preserved_agents
        ],
        'created_panes': list(namespace_patch.get('created_panes') or ()),
        'removed_panes': list(namespace_patch.get('removed_panes') or ()),
        'removed_windows': list(namespace_patch.get('removed_windows') or ()),
        'reflowed_windows': list(namespace_patch.get('reflowed_windows') or ()),
        'reflow_errors': dict(namespace_patch.get('reflow_errors') or {}),
        'mounted_agents': list(runtime_mount.get('mounted_agents') or ()),
        'moved_agents_runtime': list(runtime_mount.get('moved_agents') or ()),
        'unloaded_agents': list(runtime_mount.get('unloaded_agents') or ()),
    }


def _agent_panes(value: object, *, source: str) -> list[dict[str, object]]:
    panes = dict(value or {})
    return [
        {
            'agent': str(agent),
            'pane_id': str(pane_id),
            'pane_identity_source': source,
        }
        for agent, pane_id in sorted(panes.items())
    ]


def _moved_agent_panes(namespace_patch: dict[str, object]) -> list[dict[str, object]]:
    panes = dict(namespace_patch.get('moved_agents') or {})
    windows = dict(namespace_patch.get('moved_agent_windows') or {})
    return [
        {
            'agent': str(agent),
            'pane_id': str(pane_id),
            'window_name': str(windows.get(agent) or ''),
            'pane_identity_source': 'namespace_moved_agents',
        }
        for agent, pane_id in sorted(panes.items())
    ]


__all__ = ['pane_identity_report', 'reload_apply_summary']
