from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class AutoRunnerLockState:
    path: Path
    status: str
    pid: int | None = None
    reason: str | None = None
    raw: str = ''


def auto_runner_lock_path(project_root: str | Path) -> Path:
    return Path(project_root) / '.ccb' / 'runtime' / 'loops' / 'auto-runner.lock'


def read_auto_runner_lock(project_root: str | Path) -> AutoRunnerLockState:
    path = auto_runner_lock_path(project_root)
    try:
        raw = path.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return AutoRunnerLockState(path=path, status='absent', reason='lock_absent')
    first_line = raw.splitlines()[0].strip() if raw else ''
    try:
        pid = int(first_line)
    except ValueError:
        return AutoRunnerLockState(path=path, status='stale', reason='invalid_pid', raw=raw)
    if _pid_alive(pid):
        return AutoRunnerLockState(path=path, status='live', pid=pid, reason='pid_alive', raw=raw)
    return AutoRunnerLockState(path=path, status='stale', pid=pid, reason='pid_not_running', raw=raw)


class AutoRunnerLock:
    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self.path = auto_runner_lock_path(self.project_root)
        self._acquired = False
        self.existing_state: AutoRunnerLockState | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                state = read_auto_runner_lock(self.project_root)
                self.existing_state = state
                if state.status == 'live':
                    return False
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    continue
                continue
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(str(os.getpid()))
                handle.write('\n')
            self._acquired = True
            self.existing_state = None
            return True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._acquired = False


def active_auto_runner(project_root: str | Path) -> dict[str, object] | None:
    state = read_auto_runner_lock(project_root)
    if state.status != 'live':
        return None
    return {
        'status': 'already_active',
        'pid': state.pid,
        'lock_path': str(state.path),
        'reason': 'auto_runner_lock_live',
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = ['AutoRunnerLock', 'active_auto_runner', 'auto_runner_lock_path', 'read_auto_runner_lock']
