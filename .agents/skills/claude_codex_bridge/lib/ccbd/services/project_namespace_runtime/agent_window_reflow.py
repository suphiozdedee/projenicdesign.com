from __future__ import annotations

from dataclasses import dataclass

from agents.models import parse_layout_spec
from ccbd.reload_additive_agents import window_agent_names, window_map

@dataclass(frozen=True)
class _ObservedPane:
    pane_id: str
    pane_index: int
    pane_left: int
    pane_top: int
    pane_width: int
    pane_height: int
    role: str
    slot: str
    window_name: str
    sidebar_instance: str


@dataclass(frozen=True)
class _LayoutLeaf:
    pane_id: str
    width: int
    height: int
    left: int
    top: int

    def render(self, leaf_id: int) -> str:
        return f'{self.width}x{self.height},{self.left},{self.top},{leaf_id}'


@dataclass(frozen=True)
class _LayoutBranch:
    width: int
    height: int
    left: int
    top: int
    delimiter: str
    children: tuple[object, ...]

    def render(self, leaf_ids: list[int]) -> str:
        open_char, close_char = ('{', '}') if self.delimiter == 'horizontal' else ('[', ']')
        rendered = ','.join(_render_node(child, leaf_ids) for child in self.children)
        return f'{self.width}x{self.height},{self.left},{self.top}{open_char}{rendered}{close_char}'


def reflow_agent_window_fixed(
    backend,
    *,
    session_name: str,
    window_target: str,
    topology_plan,
    window_name: str,
    timeout_s: float | None,
    prefer_topology_layout: bool = False,
) -> tuple[bool, str | None]:
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return False, 'tmux backend does not support fixed-layout reflow'
    topology_window = _topology_window(topology_plan, window_name)
    agent_names = window_agent_names(topology_window) if topology_window is not None else ()
    user_layout = str(getattr(topology_window, 'user_layout', '') or '').strip()
    if not agent_names or (not user_layout and len(agent_names) > 6):
        return False, None
    panes, error = _observed_panes(runner, window_target=window_target, timeout_s=timeout_s)
    if error is not None:
        return False, error
    if not panes:
        return False, None
    total_width = max(pane.pane_left + pane.pane_width for pane in panes)
    total_height = max(pane.pane_top + pane.pane_height for pane in panes)
    if total_width <= 0 or total_height <= 0:
        return False, None
    topology_layout = (
        _build_topology_layout(
            panes,
            topology_window=topology_window,
            window_name=window_name,
            total_width=total_width,
            total_height=total_height,
        )
        if prefer_topology_layout
        else None
    )
    if topology_layout is not None:
        layout_root, desired_order = topology_layout
    else:
        sidebar_panes, agent_panes = _classify_panes(panes, agent_names=agent_names, window_name=window_name)
        if len(agent_panes) != len(agent_names) or len(sidebar_panes) > 1:
            return False, None
        managed_count = len(sidebar_panes) + len(agent_panes)
        if managed_count != len(panes):
            return False, None
        layout_root, desired_order = _build_fixed_layout(
            sidebar=sidebar_panes[0] if sidebar_panes else None,
            agent_panes=agent_panes,
            total_width=total_width,
            total_height=total_height,
        )
    if layout_root is None:
        return False, None
    body = _render_layout(layout_root)
    layout = f'{_layout_checksum(body):04x},{body}'
    completed = runner(['select-layout', '-t', window_target, layout], check=False, capture=True, timeout=timeout_s)
    if int(getattr(completed, 'returncode', 1) or 0) != 0:
        detail = str(getattr(completed, 'stderr', '') or getattr(completed, 'stdout', '') or '').strip()
        return False, detail or 'fixed select-layout failed'
    swap_error = _apply_visual_order(
        runner,
        current_order=[pane.pane_id for pane in panes],
        desired_order=desired_order,
        timeout_s=timeout_s,
    )
    if swap_error is not None:
        return False, swap_error
    return True, None


def _topology_window(topology_plan, window_name: str):
    windows = window_map(topology_plan)
    return windows.get(str(window_name))


def _observed_panes(runner, *, window_target: str, timeout_s: float | None) -> tuple[tuple[_ObservedPane, ...], str | None]:
    completed = runner(
        [
            'list-panes',
            '-t',
            window_target,
            '-F',
            '#{pane_id}\t#{pane_index}\t#{pane_left}\t#{pane_top}\t#{pane_width}\t#{pane_height}\t#{@ccb_role}\t#{@ccb_slot}\t#{@ccb_window}\t#{@ccb_sidebar_instance}',
        ],
        check=False,
        capture=True,
        timeout=timeout_s,
    )
    if int(getattr(completed, 'returncode', 1) or 0) != 0:
        detail = str(getattr(completed, 'stderr', '') or getattr(completed, 'stdout', '') or '').strip()
        return (), detail or 'list-panes failed'
    panes: list[_ObservedPane] = []
    for line in str(getattr(completed, 'stdout', '') or '').splitlines():
        parts = line.split('\t')
        if len(parts) < 10:
            return (), None
        try:
            panes.append(
                _ObservedPane(
                    pane_id=parts[0],
                    pane_index=int(parts[1]),
                    pane_left=int(parts[2]),
                    pane_top=int(parts[3]),
                    pane_width=int(parts[4]),
                    pane_height=int(parts[5]),
                    role=parts[6],
                    slot=parts[7],
                    window_name=parts[8],
                    sidebar_instance=parts[9],
                )
            )
        except ValueError:
            return (), None
    return tuple(sorted(panes, key=lambda pane: pane.pane_index)), None


def _classify_panes(
    panes: tuple[_ObservedPane, ...],
    *,
    agent_names: tuple[str, ...],
    window_name: str,
) -> tuple[tuple[_ObservedPane, ...], tuple[_ObservedPane, ...]]:
    agent_set = set(agent_names)
    sidebar_panes: list[_ObservedPane] = []
    agent_by_name: dict[str, _ObservedPane] = {}
    for pane in panes:
        if pane.role == 'sidebar' and (pane.sidebar_instance == window_name or pane.slot == f'sidebar:{window_name}'):
            sidebar_panes.append(pane)
            continue
        if pane.role == 'agent' and pane.window_name == window_name and pane.slot in agent_set:
            agent_by_name[pane.slot] = pane
    return tuple(sidebar_panes), tuple(agent_by_name[name] for name in agent_names if name in agent_by_name)


def _build_topology_layout(
    panes: tuple[_ObservedPane, ...],
    *,
    topology_window,
    window_name: str,
    total_width: int,
    total_height: int,
) -> tuple[object, list[str]] | None:
    user_layout = str(getattr(topology_window, 'user_layout', '') or '').strip()
    if not user_layout:
        return None
    try:
        layout_spec = parse_layout_spec(user_layout)
    except ValueError:
        return None
    leaf_names = tuple(str(leaf.name) for leaf in layout_spec.iter_leaves())
    if not leaf_names or len(set(leaf_names)) != len(leaf_names):
        return None
    sidebar_panes, pane_by_leaf = _classify_topology_panes(
        panes,
        leaf_names=leaf_names,
        window_name=window_name,
    )
    if len(sidebar_panes) > 1 or set(pane_by_leaf) != set(leaf_names):
        return None
    if len(sidebar_panes) + len(pane_by_leaf) != len(panes):
        return None

    sidebar = sidebar_panes[0] if sidebar_panes else None
    sidebar_width = sidebar.pane_width if sidebar is not None else 0
    user_width = total_width - sidebar_width - (1 if sidebar is not None else 0)
    if user_width <= 0:
        return None
    sidebar_position = str(getattr(getattr(topology_window, 'sidebar', None), 'position', 'left') or 'left')
    user_left = (
        0
        if sidebar is not None and sidebar_position == 'right'
        else sidebar_width + (1 if sidebar is not None else 0)
    )
    user = _layout_from_spec(
        layout_spec,
        pane_by_leaf=pane_by_leaf,
        width=user_width,
        height=total_height,
        left=user_left,
        top=0,
    )
    if user is None:
        return None
    desired_user_order = [pane_by_leaf[name].pane_id for name in leaf_names]
    if sidebar is None:
        return user, desired_user_order

    sidebar_left = user_width + 1 if sidebar_position == 'right' else 0
    sidebar_leaf = _LayoutLeaf(sidebar.pane_id, sidebar_width, total_height, sidebar_left, 0)
    if sidebar_position == 'right':
        children = (user, sidebar_leaf)
        desired_order = [*desired_user_order, sidebar.pane_id]
    else:
        children = (sidebar_leaf, user)
        desired_order = [sidebar.pane_id, *desired_user_order]
    return (
        _LayoutBranch(
            width=total_width,
            height=total_height,
            left=0,
            top=0,
            delimiter='horizontal',
            children=children,
        ),
        desired_order,
    )


def _classify_topology_panes(
    panes: tuple[_ObservedPane, ...],
    *,
    leaf_names: tuple[str, ...],
    window_name: str,
) -> tuple[tuple[_ObservedPane, ...], dict[str, _ObservedPane]]:
    expected = set(leaf_names)
    sidebar_panes: list[_ObservedPane] = []
    pane_by_leaf: dict[str, _ObservedPane] = {}
    for pane in panes:
        if pane.role == 'sidebar' and (pane.sidebar_instance == window_name or pane.slot == f'sidebar:{window_name}'):
            sidebar_panes.append(pane)
            continue
        leaf_name = pane.slot.removeprefix('tool:') if pane.role == 'tool' else pane.slot
        if pane.window_name == window_name and leaf_name in expected and pane.role in {'agent', 'tool'}:
            pane_by_leaf[leaf_name] = pane
    return tuple(sidebar_panes), pane_by_leaf


def _layout_from_spec(node, *, pane_by_leaf: dict[str, _ObservedPane], width: int, height: int, left: int, top: int):
    if node.kind == 'leaf':
        pane = pane_by_leaf.get(str(node.leaf.name))
        return _LayoutLeaf(pane.pane_id, width, height, left, top) if pane is not None else None
    assert node.left is not None and node.right is not None
    if node.kind == 'horizontal':
        left_size, right_size = _split_sizes(node, width)
        if left_size <= 0 or right_size <= 0:
            return None
        left_child = _layout_from_spec(
            node.left,
            pane_by_leaf=pane_by_leaf,
            width=left_size,
            height=height,
            left=left,
            top=top,
        )
        right_child = _layout_from_spec(
            node.right,
            pane_by_leaf=pane_by_leaf,
            width=right_size,
            height=height,
            left=left + left_size + 1,
            top=top,
        )
    else:
        left_size, right_size = _split_sizes(node, height)
        if left_size <= 0 or right_size <= 0:
            return None
        left_child = _layout_from_spec(
            node.left,
            pane_by_leaf=pane_by_leaf,
            width=width,
            height=left_size,
            left=left,
            top=top,
        )
        right_child = _layout_from_spec(
            node.right,
            pane_by_leaf=pane_by_leaf,
            width=width,
            height=right_size,
            left=left,
            top=top + left_size + 1,
        )
    if left_child is None or right_child is None:
        return None
    return _LayoutBranch(
        width=width,
        height=height,
        left=left,
        top=top,
        delimiter=node.kind,
        children=(left_child, right_child),
    )


def _split_sizes(node, total_size: int) -> tuple[int, int]:
    available = total_size - 1
    if available < 2:
        return 0, 0
    right_percent = _specified_percent(node.right)
    left_percent = _specified_percent(node.left)
    if right_percent is not None:
        percent = max(1, min(99, right_percent))
    elif left_percent is not None:
        percent = max(1, min(99, 100 - left_percent))
    else:
        percent = round((node.right.leaf_count * 100) / max(1, node.leaf_count))
    right_size = max(1, min(available - 1, round((available * percent) / 100)))
    return available - right_size, right_size


def _specified_percent(node) -> int | None:
    if node.kind == 'leaf':
        return node.leaf.percent
    for leaf in node.iter_leaves():
        if leaf.percent is not None:
            return leaf.percent
    return None


def _build_fixed_layout(
    *,
    sidebar: _ObservedPane | None,
    agent_panes: tuple[_ObservedPane, ...],
    total_width: int,
    total_height: int,
) -> tuple[object | None, list[str]]:
    sidebar_width = sidebar.pane_width if sidebar is not None else 0
    user_left = sidebar_width + 1 if sidebar is not None else 0
    user_width = total_width - user_left
    if user_width <= 0:
        return None, []
    user = _agent_layout(agent_panes, width=user_width, height=total_height, left=user_left, top=0)
    if user is None:
        return None, []
    if sidebar is None:
        return user, [pane.pane_id for pane in _desired_agent_visual_order(agent_panes)]
    sidebar_leaf = _LayoutLeaf(sidebar.pane_id, sidebar_width, total_height, 0, 0)
    root = _LayoutBranch(
        width=total_width,
        height=total_height,
        left=0,
        top=0,
        delimiter='horizontal',
        children=(sidebar_leaf, user),
    )
    return root, [sidebar.pane_id, *[pane.pane_id for pane in _desired_agent_visual_order(agent_panes)]]


def _agent_layout(
    agent_panes: tuple[_ObservedPane, ...],
    *,
    width: int,
    height: int,
    left: int,
    top: int,
) -> object | None:
    visual = _desired_agent_visual_order(agent_panes)
    if len(visual) == 1:
        return _LayoutLeaf(visual[0].pane_id, width, height, left, top)
    left_panes = agent_panes[0::2]
    right_panes = agent_panes[1::2]
    if not right_panes:
        return _vertical_stack(left_panes, width=width, height=height, left=left, top=top)
    left_width = (width - 1) // 2
    right_width = width - left_width - 1
    if left_width <= 0 or right_width <= 0:
        return None
    left_col = _vertical_stack(left_panes, width=left_width, height=height, left=left, top=top)
    right_col = _vertical_stack(right_panes, width=right_width, height=height, left=left + left_width + 1, top=top)
    if left_col is None or right_col is None:
        return None
    return _LayoutBranch(width=width, height=height, left=left, top=top, delimiter='horizontal', children=(left_col, right_col))


def _vertical_stack(
    panes: tuple[_ObservedPane, ...],
    *,
    width: int,
    height: int,
    left: int,
    top: int,
) -> object | None:
    if not panes:
        return None
    if len(panes) == 1:
        return _LayoutLeaf(panes[0].pane_id, width, height, left, top)
    available = height - (len(panes) - 1)
    if available < len(panes):
        return None
    base = available // len(panes)
    remainder = available % len(panes)
    children = []
    cursor = top
    for index, pane in enumerate(panes):
        pane_height = base + (1 if index < remainder else 0)
        children.append(_LayoutLeaf(pane.pane_id, width, pane_height, left, cursor))
        cursor += pane_height + 1
    return _LayoutBranch(width=width, height=height, left=left, top=top, delimiter='vertical', children=tuple(children))


def _desired_agent_visual_order(agent_panes: tuple[_ObservedPane, ...]) -> tuple[_ObservedPane, ...]:
    return (*agent_panes[0::2], *agent_panes[1::2])


def _render_layout(root: object) -> str:
    return _render_node(root, list(range(10000)))


def _render_node(node: object, leaf_ids: list[int]) -> str:
    if isinstance(node, _LayoutLeaf):
        return node.render(leaf_ids.pop(0))
    if isinstance(node, _LayoutBranch):
        return node.render(leaf_ids)
    raise TypeError(f'unsupported layout node: {type(node).__name__}')


def _layout_checksum(body: str) -> int:
    checksum = 0
    for char in body:
        checksum = ((checksum >> 1) + ((checksum & 1) << 15) + ord(char)) & 0xFFFF
    return checksum


def _apply_visual_order(runner, *, current_order: list[str], desired_order: list[str], timeout_s: float | None) -> str | None:
    if sorted(current_order) != sorted(desired_order):
        return 'fixed layout pane order mismatch'
    order = list(current_order)
    for index, desired in enumerate(desired_order):
        if order[index] == desired:
            continue
        swap_index = order.index(desired)
        source = order[index]
        completed = runner(['swap-pane', '-s', source, '-t', desired], check=False, capture=True, timeout=timeout_s)
        if int(getattr(completed, 'returncode', 1) or 0) != 0:
            detail = str(getattr(completed, 'stderr', '') or getattr(completed, 'stdout', '') or '').strip()
            return detail or 'swap-pane failed'
        order[index], order[swap_index] = order[swap_index], order[index]
    return None


__all__ = ['reflow_agent_window_fixed']
