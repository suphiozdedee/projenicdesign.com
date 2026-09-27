from __future__ import annotations

from collections.abc import Mapping


def binding_line(agent) -> str:
    return (
        f'binding: status={agent["binding_status"]} runtime={agent["runtime_ref"]} session={agent["session_ref"]} '
        f'source={agent.get("binding_source")} workspace={agent["workspace_path"]} terminal={agent.get("terminal")} '
        f'socket={agent.get("tmux_socket_name")} socket_path={agent.get("tmux_socket_path")} '
        f'window={agent.get("tmux_window_name")} window_id={agent.get("tmux_window_id")} '
        f'pane={agent.get("pane_id")} active_pane={agent.get("active_pane_id")} '
        f'pane_state={agent.get("pane_state")} marker={agent.get("pane_title_marker")}'
    )


def herdr_surface_lines(value: object, *, prefix: str = 'herdr') -> list[str]:
    if not isinstance(value, Mapping) or value.get('backend_impl') != 'herdr':
        return []
    refs = value.get('evidence_refs') if isinstance(value.get('evidence_refs'), Mapping) else {}
    lines = [
        f'{prefix}_surface: '
        f'capability_status={value.get("capability_status")} '
        f'support_tier_projection={value.get("support_tier_projection")} '
        f'source={value.get("support_tier_projection_source")} '
        f'beta_gaps={_format_list(value.get("beta_gaps"))} '
        f'blocking_gaps={_format_list(value.get("blocking_gaps"))} '
        f'next_action={value.get("degraded_next_action")}'
    ]
    namespace_ref = refs.get('namespace_ref') if isinstance(refs, Mapping) else None
    if isinstance(namespace_ref, Mapping):
        lines.append(f'{prefix}_namespace_ref: {_format_mapping(namespace_ref)}')
    pane_ref = refs.get('pane_ref') if isinstance(refs, Mapping) else None
    if isinstance(pane_ref, Mapping):
        lines.append(f'{prefix}_pane_ref: {_format_mapping(pane_ref)}')
    return lines


def _format_mapping(value: Mapping[str, object]) -> str:
    return ','.join(f'{key}={value[key]}' for key in sorted(value))


def _format_list(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return ','.join(str(item) for item in value) or 'none'
    text = str(value or '').strip()
    return text or 'none'


__all__ = ['binding_line', 'herdr_surface_lines']
