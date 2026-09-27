from __future__ import annotations

import json
import os
from pathlib import Path

from provider_runtime.helper_manifest import load_helper_manifest
from storage.paths import PathLayout

from .procfs import list_process_cmdlines, read_pid_file, read_proc_cmdline
from .utils import coerce_pid, resolved_runtime_roots


def collect_pid_candidates(agent_dir: Path, *, runtime, fallback_to_agent_dir: bool) -> dict[int, list[Path]]:
    candidates: dict[int, list[Path]] = {}
    if runtime is not None:
        runtime_pid = coerce_pid(getattr(runtime, 'runtime_pid', None) or getattr(runtime, 'pid', None))
        if runtime_pid is not None:
            candidates.setdefault(runtime_pid, []).append(agent_dir / 'runtime.json')
    for root in resolved_runtime_roots(agent_dir, runtime=runtime, fallback_to_agent_dir=fallback_to_agent_dir):
        for pid_path in sorted(root.rglob('*.pid')):
            pid = read_pid_file(pid_path)
            if pid is None:
                continue
            candidates.setdefault(pid, []).append(pid_path)
    helper_path = agent_dir / 'helper.json'
    helper = _load_helper_manifest_best_effort(helper_path)
    if helper is not None:
        candidates.setdefault(helper.leader_pid, []).append(helper_path)
    return candidates


def collect_project_process_candidates(
    project_root: Path,
    *,
    proc_root: Path = Path('/proc'),
    read_proc_cmdline_fn=read_proc_cmdline,
    list_process_cmdlines_fn=list_process_cmdlines,
    current_pid: int | None = None,
) -> dict[int, list[Path]]:
    current_pid = int(current_pid or os.getpid())
    layout = PathLayout(project_root)
    ccb_root = layout.ccb_dir
    markers = _project_runtime_markers(project_root, ccb_root=ccb_root, layout=layout)
    if not markers:
        return {}
    candidates: dict[int, list[Path]] = {}
    process_cmdlines = list_process_cmdlines_fn(
        proc_root=proc_root,
        current_pid=current_pid,
        read_proc_cmdline_fn=read_proc_cmdline_fn,
    )
    for pid, cmdline in process_cmdlines.items():
        pid = coerce_pid(pid)
        if pid is None or pid == current_pid:
            continue
        cmdline = str(cmdline or '').strip()
        matched_markers = tuple(marker for marker in markers if str(marker) in cmdline)
        control_plane_marker = _control_plane_marker(project_root, cmdline=cmdline, layout=layout)
        if control_plane_marker is not None and control_plane_marker not in matched_markers:
            matched_markers = (*matched_markers, control_plane_marker)
        if not matched_markers:
            continue
        candidates.setdefault(pid, []).extend(matched_markers)
    from runtime_accelerator.config import accelerator_socket_path
    from runtime_accelerator.ownership import legacy_marker_path, legacy_runtime_accelerator_pids

    socket_path = accelerator_socket_path(project_root)
    if socket_path is not None:
        marker = legacy_marker_path(project_root)
        for pid in legacy_runtime_accelerator_pids(
            project_root,
            socket_path=socket_path,
            process_cmdlines=process_cmdlines,
        ):
            candidates.setdefault(pid, []).append(marker)
    return candidates


__all__ = ['collect_pid_candidates', 'collect_project_process_candidates']


def _load_helper_manifest_best_effort(path: Path):
    try:
        return load_helper_manifest(path)
    except Exception:
        return None


def _project_runtime_markers(project_root: Path, *, ccb_root: Path, layout: PathLayout | None = None) -> tuple[Path, ...]:
    layout = layout or PathLayout(project_root)
    markers: list[Path] = [ccb_root]
    if layout.runtime_state_placement.root_kind == 'relocated':
        for path in (layout.runtime_state_root / 'agents', layout.runtime_state_root / 'ccbd'):
            if path not in markers:
                markers.append(path)
    return tuple(markers)


def collect_project_authority_pid_candidates(project_root: Path) -> dict[int, list[Path]]:
    layout = PathLayout(project_root)
    candidates: dict[int, list[Path]] = {}
    from runtime_accelerator.ownership import owner_manifest_path

    for path, keys in (
        (layout.ccbd_lease_path, ('ccbd_pid', 'keeper_pid')),
        (layout.ccbd_keeper_path, ('keeper_pid',)),
        (layout.ccbd_lifecycle_path, ('owner_pid', 'keeper_pid')),
        (owner_manifest_path(project_root), ('pid',)),
    ):
        payload = _load_json_object(path)
        if payload is None:
            continue
        for key in keys:
            pid = coerce_pid(payload.get(key))
            if pid is None:
                continue
            candidates.setdefault(pid, []).append(path)
    return candidates


def _load_json_object(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _control_plane_marker(project_root: Path, *, cmdline: str, layout: PathLayout) -> Path | None:
    if not cmdline:
        return None
    if 'ccbd/main.py' not in cmdline and 'ccbd/keeper_main.py' not in cmdline:
        return None
    if not _cmdline_has_project_arg(cmdline, project_root):
        return None
    return layout.ccbd_dir


def _cmdline_has_project_arg(cmdline: str, project_root: Path) -> bool:
    expected = _resolved_project_text(project_root)
    tokens = cmdline.split()
    for index, token in enumerate(tokens):
        if token == '--project' and index + 1 < len(tokens):
            if _resolved_project_text(tokens[index + 1]) == expected:
                return True
        if token.startswith('--project='):
            if _resolved_project_text(token.split('=', 1)[1]) == expected:
                return True
    return False


def _resolved_project_text(value) -> str:
    try:
        return str(Path(value).expanduser().resolve())
    except Exception:
        return str(Path(value).expanduser().absolute())
