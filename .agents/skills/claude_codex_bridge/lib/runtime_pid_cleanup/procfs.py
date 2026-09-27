from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .utils import coerce_pid


def read_pid_file(path: Path) -> int | None:
    try:
        return coerce_pid(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def read_proc_path(pid: int, entry: str) -> Path | None:
    try:
        return Path(os.readlink(f'/proc/{pid}/{entry}')).expanduser()
    except Exception:
        return None


def read_proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f'/proc/{pid}/cmdline').read_bytes()
    except Exception:
        return _read_process_cmdline_via_ps(pid)
    text = raw.replace(b'\x00', b' ').decode('utf-8', errors='ignore').strip()
    return text or _read_process_cmdline_via_ps(pid)


def list_process_cmdlines(
    *,
    proc_root: Path = Path('/proc'),
    current_pid: int | None = None,
    read_proc_cmdline_fn=read_proc_cmdline,
) -> dict[int, str]:
    current_pid = int(current_pid or os.getpid())
    if proc_root == Path('/proc'):
        process_table = _list_process_cmdlines_via_ps(current_pid=current_pid)
        if process_table:
            return process_table
    entries = _proc_entries(proc_root)
    if entries is not None:
        mapping: dict[int, str] = {}
        for entry in entries:
            pid = coerce_pid(entry.name)
            if pid is None or pid == current_pid:
                continue
            mapping[pid] = str(read_proc_cmdline_fn(pid) or '').strip()
        return mapping
    return _list_process_cmdlines_via_ps(current_pid=current_pid)


def remove_pid_files(paths: tuple[Path, ...]) -> None:
    for path in paths:
        if path.suffix != '.pid' and path.name != 'runtime-accelerator.json':
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except Exception:
            continue


def _proc_entries(proc_root: Path) -> list[Path] | None:
    try:
        if not proc_root.exists():
            return None
        return list(proc_root.iterdir())
    except Exception:
        return None


def _read_process_cmdline_via_ps(pid: int) -> str:
    try:
        result = subprocess.run(
            ['ps', '-p', str(int(pid)), '-o', 'command='],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ''
    if result.returncode != 0:
        return ''
    return str(result.stdout or '').strip()


def _list_process_cmdlines_via_ps(*, current_pid: int) -> dict[int, str]:
    try:
        result = subprocess.run(
            ['ps', '-ax', '-o', 'pid=,command='],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    mapping: dict[int, str] = {}
    for line in str(result.stdout or '').splitlines():
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        pid = coerce_pid(parts[0])
        if pid is None or pid == current_pid:
            continue
        mapping[pid] = parts[1].strip() if len(parts) > 1 else ''
    return mapping


__all__ = ['list_process_cmdlines', 'read_pid_file', 'read_proc_cmdline', 'read_proc_path', 'remove_pid_files']
