from __future__ import annotations


def resolve_sidebar_click_target(
    view: dict,
    *,
    mouse_y: int,
    pane_top: int,
    pane_height: int,
) -> tuple[str, str] | None:
    relative_y = relative_coordinate(mouse_y, pane_top, pane_height)
    if relative_y <= 0 or relative_y >= max(1, pane_height - 1):
        return None
    row_index = relative_y - 1
    targets = sidebar_tree_targets(view)
    if row_index < 0 or row_index >= len(targets):
        return None
    return targets[row_index]


def sidebar_tree_targets(view: dict) -> list[tuple[str, str]]:
    windows = view.get('windows') if isinstance(view.get('windows'), list) else []
    agents = view.get('agents') if isinstance(view.get('agents'), list) else []
    targets: list[tuple[str, str]] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        window_name = str(window.get('name') or '').strip()
        if not window_name:
            continue
        targets.append(('window', window_name))
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            if str(agent.get('window') or '').strip() != window_name:
                continue
            agent_name = str(agent.get('name') or '').strip()
            if agent_name:
                targets.append(('agent', agent_name))
    return targets


def relative_coordinate(value: int, pane_start: int, pane_size: int) -> int:
    # tmux normally exposes pane-relative mouse coordinates for pane bindings.
    # Keep an absolute-coordinate fallback for older or unusual format contexts.
    if value >= pane_size and value >= pane_start:
        return value - pane_start
    return value


__all__ = ['relative_coordinate', 'resolve_sidebar_click_target', 'sidebar_tree_targets']
