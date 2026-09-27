from __future__ import annotations

from pathlib import Path


def prepared_state(launcher, runtime_dir: Path) -> dict:
    if launcher.prepare_runtime is None:
        return {}
    return dict(launcher.prepare_runtime(runtime_dir) or {})


def _is_herdr_launch(
    *,
    namespace_backend_impl: str | None = None,
    assigned_pane_ref: object = None,
) -> bool:
    """判断当前启动是否为 herdr 后端。

    优先 ``namespace_backend_impl``，其次 ``assigned_pane_ref['backend_impl']``。
    """
    if str(namespace_backend_impl or '').strip() == 'herdr':
        return True
    if isinstance(assigned_pane_ref, dict):
        return str(assigned_pane_ref.get('backend_impl') or '').strip() == 'herdr'
    return False


def create_tmux_backend(backend_factory, tmux_socket_path: str | None):
    """创建 tmux 后端实例。

    当 ``tmux_socket_path`` 为 None 时，调用 ``backend_factory()``；
    否则尝试 ``backend_factory(socket_path=tmux_socket_path)``，
    失败时 fallback 到 ``backend_factory()``。
    """
    if tmux_socket_path is None:
        return backend_factory()
    try:
        return backend_factory(socket_path=tmux_socket_path)
    except TypeError:
        return backend_factory()


# 向后兼容别名
tmux_backend = create_tmux_backend


def run_cwd(
    launcher,
    *,
    command,
    spec,
    plan,
    runtime_dir: Path,
    launch_session_id: str,
) -> Path:
    workspace_path = Path(plan.workspace_path)
    if launcher.resolve_run_cwd is None:
        return workspace_path
    resolved = launcher.resolve_run_cwd(
        command,
        spec,
        plan,
        runtime_dir,
        launch_session_id,
    )
    if resolved is None:
        return workspace_path
    return Path(resolved)


__all__ = [
    '_is_herdr_launch',
    'create_tmux_backend',
    'prepared_state',
    'run_cwd',
    'tmux_backend',  # backward compat alias
]
