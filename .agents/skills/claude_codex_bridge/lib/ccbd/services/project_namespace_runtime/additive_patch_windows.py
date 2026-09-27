from __future__ import annotations

from dataclasses import dataclass, field
import shlex
from typing import Any

from agents.models import (
    AgentValidationError,
    layout_tool_alias_command,
    layout_tool_alias_label,
    normalize_agent_name,
    parse_layout_spec,
)
from terminal_runtime.placeholders import pane_placeholder_cmd
from terminal_runtime.tmux_theme import tmux_theme_profile
from project_command_trust import require_project_command_approval

from .backend import apply_pane_identity, create_window, respawn_pane, session_window_target, split_pane, window_root_pane
from .sidebar_helper import SIDEBAR_HELPER_ID_OPTION, sidebar_helper_fingerprint


@dataclass
class WindowPatchResult:
    created_windows: list[str] = field(default_factory=list)
    created_panes: list[str] = field(default_factory=list)
    agent_panes: dict[str, str] = field(default_factory=dict)
    sidebar_panes: dict[str, str] = field(default_factory=dict)
    removed_windows: list[str] = field(default_factory=list)
    removed_panes: list[str] = field(default_factory=list)
    removed_agents: dict[str, str] = field(default_factory=dict)
    moved_agents: dict[str, str] = field(default_factory=dict)
    moved_agent_windows: dict[str, str] = field(default_factory=dict)
    move_anchor_panes: dict[str, str] = field(default_factory=dict)
    reflowed_windows: list[str] = field(default_factory=list)
    reflow_errors: dict[str, str] = field(default_factory=dict)
    tool_panes: dict[str, str] = field(default_factory=dict)


def create_new_windows(
    controller,
    backend,
    *,
    current,
    old_topology,
    new_topology,
    result: WindowPatchResult | None = None,
    excluded_agents: tuple[str, ...] | set[str] = (),
    timeout_s: float | None,
) -> WindowPatchResult:
    result = result or WindowPatchResult()
    for window in _new_windows(old_topology, new_topology):
        _create_single_window(
            controller,
            backend,
            current=current,
            window=window,
            result=result,
            timeout_s=timeout_s,
            excluded_agents=excluded_agents,
        )
    return result


def _create_single_window(
    controller,
    backend,
    *,
    current,
    window,
    result: WindowPatchResult,
    timeout_s: float | None,
    excluded_agents: tuple[str, ...] | set[str],
) -> None:
    window_name = str(window.name)
    record = create_window(
        backend,
        session_name=current.tmux_session_name,
        window_name=window_name,
        project_root=controller._layout.project_root,
        select=False,
        timeout_s=timeout_s,
    )
    result.created_windows.append(window_name)
    root_pane = window_root_pane(
        backend,
        target_window=session_window_target(current.tmux_session_name, record.window_id or window_name),
        timeout_s=timeout_s,
    )
    _append_unique(result.created_panes, root_pane)
    user_root = _maybe_create_sidebar(controller, backend, current=current, window=window, root_pane=root_pane, result=result, timeout_s=timeout_s)
    result.move_anchor_panes[window_name] = user_root
    result.agent_panes.update(
        _materialize_new_window_agents(
            controller,
            backend,
            window=window,
            user_root=user_root,
            namespace_epoch=current.namespace_epoch,
            created_panes=result.created_panes,
            result=result,
            timeout_s=timeout_s,
            excluded_agents=excluded_agents,
        )
    )
    _materialize_new_tool_window(
        controller,
        backend,
        window=window,
        user_root=user_root,
        namespace_epoch=current.namespace_epoch,
        created_panes=result.created_panes,
        result=result,
    )


def _maybe_create_sidebar(
    controller,
    backend,
    *,
    current,
    window,
    root_pane: str,
    result: WindowPatchResult,
    timeout_s: float | None,
) -> str:
    sidebar = getattr(window, 'sidebar', None)
    if sidebar is None:
        return root_pane
    window_name = str(window.name)
    user_root = split_pane(
        backend,
        target=root_pane,
        direction='right',
        percent=_user_pane_percent_for_sidebar(getattr(sidebar, 'width', '15%')),
        project_root=controller._layout.project_root,
        timeout_s=timeout_s,
    )
    _append_unique(result.created_panes, user_root)
    _respawn_sidebar(backend, root_pane, getattr(sidebar, 'launch_args', ()), cwd=str(controller._layout.project_root), timeout_s=timeout_s)
    apply_pane_identity(
        backend,
        pane_id=root_pane,
        title='sidebar',
        agent_label='sidebar',
        project_id=controller._project_id,
        role='sidebar',
        slot_key=f'sidebar:{window_name}',
        window_name=window_name,
        sidebar_instance=window_name,
        namespace_epoch=current.namespace_epoch,
        managed_by='ccbd',
    )
    helper_identity = sidebar_helper_fingerprint()
    setter = getattr(backend, 'set_pane_user_option', None)
    if helper_identity and callable(setter):
        setter(root_pane, SIDEBAR_HELPER_ID_OPTION, helper_identity)
    result.sidebar_panes[window_name] = root_pane
    return user_root


def _new_windows(old_topology, new_topology) -> tuple[object, ...]:
    old_windows = {str(window.name) for window in tuple(getattr(old_topology, 'windows', ()) or ())}
    return tuple(window for window in tuple(getattr(new_topology, 'windows', ()) or ()) if str(window.name) not in old_windows)


def _materialize_new_window_agents(
    controller,
    backend,
    *,
    window,
    user_root: str,
    namespace_epoch: int,
    created_panes: list[str],
    result: WindowPatchResult,
    timeout_s: float | None,
    excluded_agents: tuple[str, ...] | set[str],
) -> dict[str, str]:
    if str(getattr(window, 'kind', '') or '') == 'tool':
        return {}
    excluded = {str(agent) for agent in tuple(excluded_agents or ())}
    agent_names = tuple(str(name) for name in getattr(window, 'agent_names', ()) or ())
    if excluded and all(name in excluded for name in agent_names):
        return {}
    if excluded and any(name in excluded for name in agent_names):
        return {}
    layout = parse_layout_spec(window.user_layout)
    tool_names = set(str(name) for name in tuple(getattr(window, 'tool_names', ()) or ()))
    style_index_by_agent = {name: index for index, name in enumerate(agent_names)}
    agent_panes: dict[str, str] = {}

    def assign_leaf(item: str, pane_id: str) -> None:
        if item == 'cmd':
            return
        item_tool = str(item or '').strip().lower()
        if item_tool in tool_names:
            _materialize_new_tool_pane(
                controller,
                backend,
                pane_id=pane_id,
                tool_name=item_tool,
                command=layout_tool_alias_command(item_tool),
                label=layout_tool_alias_label(item_tool),
                window_name=str(window.name),
                namespace_epoch=namespace_epoch,
                order_index=int(getattr(window, 'order', 0) or 0),
                created_panes=created_panes,
                result=result,
                project_command_field=False,
            )
            return
        _append_unique(created_panes, pane_id)
        try:
            agent_name = normalize_agent_name(item)
        except AgentValidationError:
            agent_name = item
        agent_panes[agent_name] = pane_id
        apply_pane_identity(
            backend,
            pane_id=pane_id,
            title=item,
            agent_label=agent_name,
            project_id=controller._project_id,
            order_index=style_index_by_agent.get(agent_name),
            role='agent',
            slot_key=agent_name,
            window_name=str(window.name),
            namespace_epoch=namespace_epoch,
            managed_by='ccbd',
        )

    _materialize_layout(
        controller,
        backend,
        parent_pane_id=user_root,
        node=layout,
        assign_leaf=assign_leaf,
        created_panes=created_panes,
        timeout_s=timeout_s,
    )
    return agent_panes


def _materialize_new_tool_window(
    controller,
    backend,
    *,
    window,
    user_root: str,
    namespace_epoch: int,
    created_panes: list[str],
    result: WindowPatchResult,
) -> None:
    if str(getattr(window, 'kind', '') or '') != 'tool':
        return
    command = str(getattr(window, 'command', '') or '').strip() or pane_placeholder_cmd()
    _materialize_new_tool_pane(
        controller,
        backend,
        pane_id=user_root,
        tool_name=str(window.name),
        command=command,
        label=str(getattr(window, 'label', None) or window.name),
        window_name=str(window.name),
        namespace_epoch=namespace_epoch,
        order_index=int(getattr(window, 'order', 0) or 0),
        created_panes=created_panes,
        result=result,
        project_command_field=True,
    )


def _materialize_new_tool_pane(
    controller,
    backend,
    *,
    pane_id: str,
    tool_name: str,
    command: str,
    label: str,
    window_name: str,
    namespace_epoch: int,
    order_index: int,
    created_panes: list[str],
    result: WindowPatchResult | None,
    project_command_field: bool,
) -> None:
    command = str(command or '').strip() or pane_placeholder_cmd()
    if project_command_field:
        require_project_command_approval(
            controller._layout.project_root,
            field_path=f'tool_windows.{str(tool_name).strip().lower()}.command',
            field_value=command,
        )
    respawn_pane(
        backend,
        pane_id=pane_id,
        command=command,
        cwd=str(controller._layout.project_root),
    )
    _append_unique(created_panes, pane_id)
    apply_pane_identity(
        backend,
        pane_id=pane_id,
        title=label,
        agent_label=label,
        project_id=controller._project_id,
        order_index=order_index,
        role='tool',
        slot_key=f'tool:{tool_name}',
        window_name=window_name,
        namespace_epoch=namespace_epoch,
        managed_by='ccbd',
    )
    if result is not None:
        result.tool_panes[str(tool_name)] = pane_id


def _materialize_layout(
    controller,
    backend,
    *,
    parent_pane_id: str,
    node: Any,
    assign_leaf,
    created_panes: list[str],
    timeout_s: float | None,
) -> None:
    if node.kind == 'leaf':
        assert node.leaf is not None
        assign_leaf(node.leaf.name, parent_pane_id)
        return
    assert node.left is not None
    assert node.right is not None
    new_pane_id = split_pane(
        backend,
        target=parent_pane_id,
        direction='right' if node.kind == 'horizontal' else 'bottom',
        percent=_right_pane_percent(node),
        project_root=controller._layout.project_root,
        timeout_s=timeout_s,
    )
    _append_unique(created_panes, new_pane_id)
    _materialize_layout(
        controller,
        backend,
        parent_pane_id=parent_pane_id,
        node=node.left,
        assign_leaf=assign_leaf,
        created_panes=created_panes,
        timeout_s=timeout_s,
    )
    _materialize_layout(
        controller,
        backend,
        parent_pane_id=new_pane_id,
        node=node.right,
        assign_leaf=assign_leaf,
        created_panes=created_panes,
        timeout_s=timeout_s,
    )


def _right_pane_percent(node) -> int:
    total = max(1, node.leaf_count)
    right_count = max(1, node.right.leaf_count)
    return max(1, min(99, round((right_count * 100) / total)))


def _user_pane_percent_for_sidebar(width: object) -> int:
    text = str(width or '').strip()
    if text.endswith('%'):
        try:
            sidebar_percent = int(text[:-1])
        except Exception:
            sidebar_percent = 15
        return max(10, min(99, 100 - sidebar_percent))
    return 85


def _respawn_sidebar(backend, pane_id: str, launch_args: tuple[str, ...], *, cwd: str, timeout_s: float | None) -> None:
    args = tuple(launch_args or ())
    command = ' '.join(shlex.quote(str(part)) for part in args) if args else pane_placeholder_cmd()
    command = f'CCB_SIDEBAR_THEME_PROFILE={shlex.quote(tmux_theme_profile())} {command}'
    respawn_pane(backend, pane_id=pane_id, command=command, cwd=cwd, timeout_s=timeout_s)


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


__all__ = ['WindowPatchResult', 'create_new_windows']
