from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ccbd.system import parse_utc_timestamp, process_exists

from .records import KeeperState


def restart_backoff_active(*, state: KeeperState, now: str) -> bool:
    if state.restart_count <= 0 or state.last_failure_reason is None or state.last_restart_at is None:
        return False
    try:
        elapsed = (parse_utc_timestamp(now) - parse_utc_timestamp(state.last_restart_at)).total_seconds()
    except Exception:
        return False
    return elapsed < restart_backoff_seconds(state.restart_count)


def restart_backoff_seconds(restart_count: int) -> float:
    capped = min(max(1, int(restart_count)), 5)
    return min(8.0, 0.5 * float(2 ** (capped - 1)))


def compute_project_id(project_root: Path) -> str:
    from project.ids import compute_project_id as _compute_project_id

    return _compute_project_id(project_root)


def keeper_state_is_running(
    state: KeeperState | None,
    *,
    process_exists_fn=process_exists,
    expected_project_id: str | None = None,
    project_root: Path | None = None,
    process_cmdline_fn=None,
    require_cmdline_match: bool = False,
) -> bool:
    if state is None:
        return False
    if state.state != 'running':
        return False
    if expected_project_id is not None and state.project_id != expected_project_id:
        return False
    if not process_exists_fn(state.keeper_pid):
        return False
    if project_root is None:
        return True
    reader = process_cmdline_fn or read_process_cmdline
    cmdline = reader(state.keeper_pid)
    if cmdline is None:
        return True
    return keeper_cmdline_matches_project(cmdline, project_root)


def read_process_cmdline(pid: int) -> tuple[str, ...] | None:
    proc_root = Path('/proc')
    if not proc_root.exists():
        return None
    if pid <= 0:
        return ()
    try:
        raw = (proc_root / str(pid) / 'cmdline').read_bytes()
    except FileNotFoundError:
        return ()
    except OSError:
        return ()
    return tuple(part.decode('utf-8', errors='replace') for part in raw.split(b'\0') if part)


def keeper_cmdline_matches_project(cmdline: Iterable[str], project_root: Path) -> bool:
    args = tuple(str(arg) for arg in cmdline if str(arg))
    if not args:
        return False
    if not any(_is_keeper_entrypoint_arg(arg) for arg in args):
        return False
    project_arg = _project_arg_value(args)
    if project_arg is None:
        return False
    return _normalized_path(project_arg) == _normalized_path(project_root)


def _is_keeper_entrypoint_arg(value: str) -> bool:
    normalized = value.replace('\\', '/')
    return normalized == 'ccbd.keeper_main' or normalized.endswith('/ccbd/keeper_main.py')


def _project_arg_value(args: tuple[str, ...]) -> str | None:
    for index, arg in enumerate(args):
        if arg == '--project' and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith('--project='):
            return arg.split('=', 1)[1]
    return None


def _normalized_path(value: str | Path) -> str:
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except Exception:
        return str(Path(value).expanduser())


__all__ = [
    'compute_project_id',
    'keeper_cmdline_matches_project',
    'keeper_state_is_running',
    'read_process_cmdline',
    'restart_backoff_active',
    'restart_backoff_seconds',
]
