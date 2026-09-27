from __future__ import annotations

import os
from pathlib import Path


def pid_matches_project(
    pid: int,
    *,
    project_root: Path,
    hint_paths: tuple[Path, ...],
    read_proc_path_fn,
    read_proc_cmdline_fn,
    path_within_fn,
    os_name: str = os.name,
) -> bool:
    accelerator_hints = tuple(
        path for path in hint_paths if path.name in {'runtime-accelerator.json', 'runtime-accelerator.legacy'}
    )
    if accelerator_hints:
        from runtime_accelerator.ownership import (
            runtime_accelerator_pid_matches_legacy,
            runtime_accelerator_pid_matches_owner,
        )

        return all(
            runtime_accelerator_pid_matches_owner(
                pid,
                project_root=project_root,
                manifest_path=path,
            )
            if path.name == 'runtime-accelerator.json'
            else runtime_accelerator_pid_matches_legacy(pid, project_root=project_root)
            for path in accelerator_hints
        )
    if os_name == 'nt':
        return True
    normalized_hints = normalize_hint_roots(project_root, hint_paths=hint_paths)
    cwd_path = read_proc_path_fn(pid, 'cwd')
    if cwd_path is not None:
        for root in normalized_hints:
            if path_within_fn(cwd_path, root):
                return True
    cmdline = read_proc_cmdline_fn(pid)
    if cmdline:
        for candidate in (*normalized_hints, *hint_paths):
            text = str(candidate).strip()
            if text and text in cmdline:
                return True
    return False


def normalize_hint_roots(project_root: Path, *, hint_paths: tuple[Path, ...]) -> list[Path]:
    normalized: list[Path] = []
    for candidate in (project_root, *(path.parent for path in hint_paths)):
        try:
            resolved = candidate.expanduser().resolve()
        except Exception:
            resolved = candidate.expanduser().absolute()
        if resolved not in normalized:
            normalized.append(resolved)
    return normalized


def path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except Exception:
        return False
    return True


__all__ = ['normalize_hint_roots', 'path_within', 'pid_matches_project']
