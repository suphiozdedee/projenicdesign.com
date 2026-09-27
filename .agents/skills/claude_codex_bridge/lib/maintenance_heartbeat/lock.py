from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import fcntl  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised on native Windows
    fcntl = None

try:
    import msvcrt  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised on POSIX
    msvcrt = None


class MaintenanceHeartbeatLockBusy(RuntimeError):
    pass


class MaintenanceHeartbeatLock:
    def __init__(self, path: Path, *, payload: dict[str, Any]) -> None:
        self._path = Path(path)
        self._payload = dict(payload)
        self._handle = None

    def __enter__(self) -> 'MaintenanceHeartbeatLock':
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open('a+', encoding='utf-8')
        try:
            _lock_handle(handle)
        except BlockingIOError as exc:
            handle.close()
            raise MaintenanceHeartbeatLockBusy('maintenance heartbeat tick is already running') from exc
        except OSError as exc:
            handle.close()
            raise MaintenanceHeartbeatLockBusy('maintenance heartbeat tick is already running') from exc
        self._handle = handle
        self._write_state({'held': True, **self._payload})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            release_payload = dict(self._payload)
            released_at = release_payload.pop('released_at', None)
            self._write_state({'held': False, **release_payload, 'released_at': released_at})
            _unlock_handle(handle)
        finally:
            handle.close()
            self._handle = None

    def _write_state(self, payload: dict[str, Any]) -> None:
        handle = self._handle
        if handle is None:
            return
        handle.seek(0)
        handle.truncate(0)
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def _lock_handle(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is None:
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock_handle(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is None:
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


__all__ = ['MaintenanceHeartbeatLock', 'MaintenanceHeartbeatLockBusy']
