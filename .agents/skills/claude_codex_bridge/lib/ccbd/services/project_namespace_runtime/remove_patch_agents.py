from __future__ import annotations

from agents.config_loader import load_project_config
from ccbd.reload_additive_agents import window_agent_names, window_map

from .agent_window_reflow import reflow_agent_window_fixed
from .backend import find_window, kill_pane, kill_window, reflow_window, session_window_target
from .materialize_topology import sync_topology_sidebar_widths


def remove_agent_panes(
    controller,
    backend,
    *,
    old_topology,
    new_topology,
    existing_agent_panes: dict[str, str],
    current,
    result,
    timeout_s: float | None,
    excluded_agents: tuple[str, ...] | set[str] = (),
) -> None:
    old_windows = window_map(old_topology)
    new_windows = window_map(new_topology)
    removed_windows = set(old_windows) - set(new_windows)
    excluded = {str(agent) for agent in tuple(excluded_agents or ())}
    for window_name, old_window in old_windows.items():
        if window_name in removed_windows:
            if str(getattr(old_window, 'kind', '') or '') == 'tool':
                continue
            _remove_window_agents(
                backend,
                window_name=window_name,
                agents=tuple(agent for agent in window_agent_names(old_window) if agent not in excluded),
                existing_agent_panes=existing_agent_panes,
                current=current,
                result=result,
                timeout_s=timeout_s,
            )
            _kill_window(backend, current=current, window_name=window_name, result=result, timeout_s=timeout_s)
            continue
        new_window = new_windows.get(window_name)
        if new_window is None:
            continue
        new_agents = set(window_agent_names(new_window))
        removed_agents = tuple(agent for agent in window_agent_names(old_window) if agent not in new_agents and agent not in excluded)
        _remove_window_agents(
            backend,
            window_name=window_name,
            agents=removed_agents,
            existing_agent_panes=existing_agent_panes,
            current=current,
            result=result,
            timeout_s=timeout_s,
        )
        if removed_agents:
            reflow_window_after_agent_change(
                controller,
                backend,
                current=current,
                topology_plan=new_topology,
                window_name=window_name,
                result=result,
                timeout_s=timeout_s,
                prefer_topology_layout=_window_is_static_config_owned(
                    controller,
                    topology_plan=new_topology,
                    window_name=window_name,
                ),
            )


def _remove_window_agents(
    backend,
    *,
    window_name: str,
    agents: tuple[str, ...],
    existing_agent_panes: dict[str, str],
    current,
    result,
    timeout_s: float | None,
) -> None:
    del current
    for agent_name in agents:
        pane_id = existing_agent_panes.get(agent_name)
        if not pane_id:
            raise RuntimeError(f'pane missing for removed agent {agent_name!r}')
        kill_pane(backend, pane_id=pane_id, timeout_s=timeout_s)
        _append_unique(result.removed_panes, pane_id)
        result.removed_agents[agent_name] = pane_id


def _kill_window(backend, *, current, window_name: str, result, timeout_s: float | None) -> None:
    if find_window(backend, session_name=current.tmux_session_name, window_name=window_name, timeout_s=timeout_s) is None:
        _append_unique(result.removed_windows, window_name)
        return
    _select_workspace_before_window_removal(backend, current=current, removed_window_name=window_name, timeout_s=timeout_s)
    kill_window(
        backend,
        target=session_window_target(current.tmux_session_name, window_name),
        timeout_s=timeout_s,
    )
    _append_unique(result.removed_windows, window_name)


def _select_workspace_before_window_removal(backend, *, current, removed_window_name: str, timeout_s: float | None) -> None:
    workspace_name = str(getattr(current, 'workspace_window_name', '') or '').strip()
    if not workspace_name or workspace_name == str(removed_window_name or '').strip():
        return
    if find_window(backend, session_name=current.tmux_session_name, window_name=workspace_name, timeout_s=timeout_s) is None:
        return
    workspace_ref = str(getattr(current, 'workspace_window_id', '') or '').strip() or workspace_name
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return
    try:
        runner(
            ['select-window', '-t', session_window_target(current.tmux_session_name, workspace_ref)],
            check=False,
            capture=True,
            timeout=timeout_s,
        )
    except Exception:
        return


def reflow_window_after_agent_change(
    controller,
    backend,
    *,
    current,
    topology_plan,
    window_name: str,
    result,
    timeout_s: float | None,
    prefer_topology_layout: bool = False,
) -> None:
    target = _reflow_target(backend, current=current, topology_plan=topology_plan, window_name=window_name, timeout_s=timeout_s)
    runner = getattr(backend, '_tmux_run', None)
    is_mux_backend = callable(getattr(backend, 'capabilities', None)) and not callable(runner)
    if is_mux_backend:
        try:
            reflow_window(
                backend,
                session_name=current.tmux_session_name,
                window_name=window_name,
                target=target,
                timeout_s=timeout_s,
                prefer_topology_layout=prefer_topology_layout,
            )
        except Exception as exc:
            result.reflow_errors[window_name] = str(exc)
            raise
        _append_unique(result.reflowed_windows, window_name)
        _sync_sidebar_widths(controller, backend, current=current, topology_plan=topology_plan, window_name=window_name, result=result, timeout_s=timeout_s)
        return
    if not callable(runner):
        result.reflow_errors[window_name] = 'tmux backend does not support select-layout'
        return
    fixed_applied, fixed_error = reflow_agent_window_fixed(
        backend,
        session_name=current.tmux_session_name,
        window_target=target,
        topology_plan=topology_plan,
        window_name=window_name,
        timeout_s=timeout_s,
        prefer_topology_layout=prefer_topology_layout,
    )
    if fixed_error is not None:
        result.reflow_errors[window_name] = fixed_error
        return
    if fixed_applied:
        _append_unique(result.reflowed_windows, window_name)
        _sync_sidebar_widths(controller, backend, current=current, topology_plan=topology_plan, window_name=window_name, result=result, timeout_s=timeout_s)
        return
    try:
        reflow_window(backend, session_name=current.tmux_session_name, window_name=window_name, target=target, timeout_s=timeout_s)
    except Exception as exc:
        result.reflow_errors[window_name] = str(exc)
        return
    _append_unique(result.reflowed_windows, window_name)
    _sync_sidebar_widths(controller, backend, current=current, topology_plan=topology_plan, window_name=window_name, result=result, timeout_s=timeout_s)


def _sync_sidebar_widths(
    controller,
    backend,
    *,
    current,
    topology_plan,
    window_name: str,
    result,
    timeout_s: float | None,
) -> None:
    try:
        sync_topology_sidebar_widths(
            controller,
            backend,
            session_name=current.tmux_session_name,
            topology_plan=topology_plan,
            timeout_s=timeout_s,
            namespace_epoch=current.namespace_epoch,
        )
    except Exception as exc:
        result.reflow_errors[window_name] = f'sidebar_width_sync_failed: {exc}'


def _reflow_target(backend, *, current, topology_plan, window_name: str, timeout_s: float | None) -> str:
    session_name = current.tmux_session_name
    if find_window(backend, session_name=session_name, window_name=window_name, timeout_s=timeout_s) is not None:
        return session_window_target(session_name, window_name)
    if _is_entry_window(topology_plan, window_name):
        workspace_ref = str(getattr(current, 'workspace_window_id', '') or '').strip()
        if not workspace_ref:
            workspace_ref = str(getattr(current, 'workspace_window_name', '') or '').strip()
        if workspace_ref:
            return session_window_target(session_name, workspace_ref)
    return session_window_target(session_name, window_name)


def _is_entry_window(topology_plan, window_name: str) -> bool:
    target = str(window_name or '').strip()
    if not target:
        return False
    entry = str(getattr(topology_plan, 'entry_window', '') or '').strip()
    if entry and target == entry:
        return True
    windows = tuple(getattr(topology_plan, 'windows', ()) or ())
    first = str(getattr(windows[0], 'name', '') or '').strip() if windows else ''
    return bool(first and target == first)


def _window_is_static_config_owned(controller, *, topology_plan, window_name: str) -> bool:
    try:
        configured = set(
            load_project_config(
                controller._layout.project_root,
                include_loop_overlays=False,
            ).config.agents
        )
    except Exception:
        return False
    window = window_map(topology_plan).get(str(window_name))
    names = window_agent_names(window) if window is not None else ()
    return bool(names) and all(name in configured for name in names)


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


__all__ = ['remove_agent_panes', 'reflow_window_after_agent_change']
