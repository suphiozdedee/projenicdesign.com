from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(path: Path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl

        with target.open('a+', encoding='utf-8') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except ModuleNotFoundError:
        # Windows: fcntl is unavailable.  Use msvcrt byte-range locking as
        # a cross-process mutex.  The OS releases the lock when the handle is
        # closed (or the process exits), so there is no stale-lock problem.
        import msvcrt

        with target.open('a+b') as handle:
            # LK_LOCK blocks until the byte range is available, matching
            # fcntl.LOCK_EX semantics.  We lock the first byte as a mutex
            # token; the actual file content is irrelevant.
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
