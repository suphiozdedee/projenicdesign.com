from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
try:
    import pwd
except ImportError:  # pragma: no cover - Windows compatibility
    pwd = None  # type: ignore[assignment]

from cli.services.tmux_start_layout import TmuxStartLayout
from terminal_runtime.env import default_shell as _default_shell_impl
from terminal_runtime.env import is_windows as _is_windows_impl
from terminal_runtime.env import is_wsl as _is_wsl_impl


def prepare_start_layout(
    context,
    *,
    config,
    targets: tuple[str, ...],
    layout_plan=None,
    tmux_backend=None,
    root_pane_id: str | None = None,
    window_name: str | None = None,
    inside_tmux_fn,
    prepare_tmux_start_layout_fn,
) -> TmuxStartLayout:
    if tmux_backend is None and not inside_tmux_fn():
        return TmuxStartLayout(cmd_pane_id=None, agent_panes={})
    if tmux_backend is not None and root_pane_id is None:
        return TmuxStartLayout(cmd_pane_id=None, agent_panes={})
    return prepare_tmux_start_layout_fn(
        context,
        config=config,
        targets=targets,
        layout_plan=layout_plan,
        tmux_backend=tmux_backend,
        root_pane_id=root_pane_id,
        window_name=window_name,
    )


def inside_tmux() -> bool:
    return bool((os.environ.get('TMUX') or os.environ.get('TMUX_PANE') or '').strip())


def session_root_pane(
    backend,
    session_name: str | None,
    *,
    workspace_window_name: str | None = None,
) -> str | None:
    if backend is None or not session_name:
        return None
    target = str(session_name)
    if str(workspace_window_name or '').strip():
        target = f'{session_name}:{str(workspace_window_name).strip()}'
    try:
        result = backend._tmux_run(  # type: ignore[attr-defined]
            ['list-panes', '-t', target, '-F', '#{pane_id}'],
            capture=True,
            check=True,
        )
    except Exception:
        return None
    pane_id = ((result.stdout or '').splitlines() or [''])[0].strip()
    return pane_id if pane_id.startswith('%') else None


def bootstrap_project_namespace_cmd_pane(
    *,
    pane_id: str,
    project_root: Path,
    project_id: str,
    tmux_socket_path: str | None,
    namespace_epoch: int | None,
    tmux_backend_factory,
    apply_ccb_pane_identity_fn,
    cmd_bootstrap_command_fn,
) -> str | None:
    pane_text = str(pane_id or '').strip()
    socket_path = str(tmux_socket_path or '').strip()
    if not pane_text.startswith('%') or not socket_path:
        return None
    try:
        backend = tmux_backend_factory(socket_path=socket_path)
    except TypeError:
        backend = tmux_backend_factory()
    respawn = getattr(backend, 'respawn_pane', None)
    if not callable(respawn):
        return None
    respawn(
        pane_text,
        cmd=cmd_bootstrap_command_fn(),
        cwd=str(project_root),
        remain_on_exit=False,
    )
    apply_ccb_pane_identity_fn(
        backend,
        pane_text,
        title='cmd',
        agent_label='cmd',
        project_id=project_id,
        is_cmd=True,
        slot_key='cmd',
        namespace_epoch=namespace_epoch,
        managed_by='ccbd',
    )
    return pane_text


def cmd_bootstrap_command() -> str:
    shell = _resolved_cmd_shell()
    argv = ['exec', shell, *_cmd_shell_login_flags(shell)]
    return ' '.join(shlex.quote(part) for part in argv)


def _resolved_cmd_shell() -> str:
    seen: set[str] = set()
    for candidate in (
        str(os.environ.get('CCB_CMD_SHELL') or '').strip(),
        str(os.environ.get('SHELL') or '').strip(),
        _passwd_login_shell(),
        _default_shell_impl(is_wsl_fn=_is_wsl_impl, is_windows_fn=_is_windows_impl)[0],
        'bash',
        'sh',
    ):
        normalized = str(candidate or '').strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        resolved = _resolve_shell_candidate(normalized)
        if resolved:
            return resolved
    return 'sh'


def _resolve_shell_candidate(candidate: str) -> str | None:
    if '/' in candidate:
        return candidate if Path(candidate).exists() else None
    return shutil.which(candidate)


def _passwd_login_shell() -> str:
    if pwd is None:
        return ''
    try:
        shell = str(pwd.getpwuid(os.getuid()).pw_shell or '').strip()
    except Exception:
        return ''
    return shell


def _cmd_shell_login_flags(shell: str) -> list[str]:
    shell_name = Path(shell).name.lower()
    if shell_name in {'bash', 'dash', 'fish', 'ksh', 'sh', 'zsh'}:
        return ['-l']
    return []
