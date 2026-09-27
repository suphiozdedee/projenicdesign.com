from __future__ import annotations

import os


def _is_windows() -> bool:
    return os.name == 'nt'


def process_exists(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if _is_windows():
        return _windows_process_exists(int(pid))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def _windows_process_exists(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        synchronize = 0x00100000
        try:
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
        except (AttributeError, TypeError):
            pass
        handle = kernel32.OpenProcess(synchronize, False, int(pid))
        if not handle:
            return False
        try:
            return True
        finally:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
    except Exception:
        return False


__all__ = ['process_exists']
