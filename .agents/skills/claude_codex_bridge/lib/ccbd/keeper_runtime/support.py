from __future__ import annotations

import errno
import os
from pathlib import Path


def try_acquire_keeper_lock(path: Path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = target.open('a+', encoding='utf-8')
    try:
        import fcntl  # type: ignore

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ModuleNotFoundError:
        # fcntl 不存在（Windows）：改用 msvcrt.locking 真实跨进程锁。
        # 绝不能返回"未加锁的 handle"——否则两个 keeper 都会认为自己持锁，
        # 造成双 keeper 并发、状态文件互相覆盖（见 2026-08-06-...-issue G2）。
        handle.close()
        return _try_acquire_windows_lock(target)
    except OSError as exc:
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return None
        raise
    return handle


def _try_acquire_windows_lock(path: Path):
    # msvcrt.locking 要求文件至少 1 字节且锁字节落在文件范围内：
    # 空文件先写入 1 个占位字节，再 seek 到 0 锁住第一字节。
    handle = path.open('a+b')
    try:
        if handle.tell() == 0 and path.stat().st_size == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
    except OSError:
        # open/write/stat 失败是真实 I/O 错误，作为异常传播，
        # 不能被误判为"锁被他人持有"（否则 keeper 会静默退出）。
        handle.close()
        raise
    try:
        import msvcrt  # type: ignore

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except ModuleNotFoundError:
        # 平台既无 fcntl 也无 msvcrt（罕见）：fail-closed。
        # 宁可让本 keeper 退出，也绝不与既有 keeper 并发。
        handle.close()
        return None
    except OSError as exc:
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return None
        raise
    return handle


def reap_child_processes(*, waitpid_fn=os.waitpid, os_module=os) -> tuple[int, ...]:
    if os_module.name == 'nt' or not hasattr(os_module, 'WNOHANG'):
        return ()
    reaped: list[int] = []
    while True:
        try:
            pid, _status = waitpid_fn(-1, os_module.WNOHANG)
        except ChildProcessError:
            break
        except OSError as exc:
            if exc.errno in {errno.ECHILD, errno.EINTR}:
                break
            break
        if pid <= 0:
            break
        reaped.append(int(pid))
    return tuple(reaped)


__all__ = ['reap_child_processes', 'try_acquire_keeper_lock']
