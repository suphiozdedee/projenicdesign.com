from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import os
import re
import tempfile
from typing import Any, Literal

try:
    import pwd
except ImportError:  # pragma: no cover - unavailable on non-POSIX runtimes
    pwd = None

from agents.models import normalize_agent_name
from ccbd.api_models import TargetKind


TARGET_SEGMENT_PATTERN = re.compile(r'[^a-z0-9._-]+')
UNIX_SOCKET_SAFE_BYTES = 100
_WSL_MOUNTED_DRIVE_RE = re.compile(r'^/mnt/([A-Za-z])(?:/|$)')
RUNTIME_ROOT_MARKER_FILENAME = 'runtime-root.json'
RUNTIME_ROOT_REF_FILENAME = 'runtime-root-ref.json'
RUNTIME_ROOT_RECORD_TYPE = 'ccb_runtime_root'
RUNTIME_ROOT_REF_RECORD_TYPE = 'ccb_runtime_root_ref'


@dataclass(frozen=True)
class SocketPlacement:
    preferred_path: Path
    effective_path: Path
    root_kind: Literal['project', 'runtime']
    fallback_reason: str | None = None
    filesystem_hint: str | None = None


@dataclass(frozen=True)
class RuntimeStatePlacement:
    anchor_path: Path
    effective_path: Path
    root_kind: Literal['project', 'relocated']
    relocation_reason: str | None = None
    filesystem_hint: str | None = None


def normalized_segment(value: str, *, label: str) -> str:
    normalized = TARGET_SEGMENT_PATTERN.sub(
        '-',
        str(value or '').strip().lower(),
    ).strip('-.')
    if not normalized:
        raise ValueError(f'{label} cannot be empty')
    return normalized


def target_segment(target_kind: TargetKind | str, target_name: str) -> str:
    kind = TargetKind(target_kind)
    raw_name = str(target_name or '').strip()
    if kind is TargetKind.AGENT:
        return normalize_agent_name(raw_name)
    return normalized_segment(raw_name, label='target_name')


def unix_socket_path_is_safe(path: Path) -> bool:
    return len(os.fsencode(str(path))) <= UNIX_SOCKET_SAFE_BYTES


def runtime_socket_root() -> Path:
    candidates = runtime_socket_root_candidates()
    for candidate in candidates:
        if pathname_unix_socket_supported(candidate):
            return candidate
    if candidates:
        return candidates[0]
    return Path('/tmp').expanduser() / 'ccb-runtime'


def runtime_socket_root_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    xdg_runtime_dir = str(os.environ.get('XDG_RUNTIME_DIR') or '').strip()
    if xdg_runtime_dir:
        candidates.append(Path(xdg_runtime_dir).expanduser() / 'ccb-runtime')
    candidates.append(Path('/tmp').expanduser() / 'ccb-runtime')
    candidates.append(Path(tempfile.gettempdir()).expanduser() / 'ccb-runtime')
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def runtime_state_root_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    runtime_state_home = _absolute_path_from_env('CCB_RUNTIME_STATE_HOME')
    if runtime_state_home is not None:
        candidates.append(runtime_state_home)
    xdg_state_home = str(os.environ.get('XDG_STATE_HOME') or '').strip()
    xdg_state_root = _absolute_path_from_value(xdg_state_home)
    if xdg_state_root is not None:
        candidates.append(xdg_state_root / 'ccb' / 'projects')
    candidates.append(_account_home_dir() / '.local' / 'state' / 'ccb' / 'projects')
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def runtime_state_base_root() -> Path:
    candidates = runtime_state_root_candidates()
    for candidate in candidates:
        if pathname_runtime_state_supported(candidate):
            return candidate
    if candidates:
        return candidates[0]
    return _account_home_dir() / '.local' / 'state' / 'ccb' / 'projects'


def runtime_state_root_for_project(project_id: str) -> Path:
    normalized = str(project_id or '').strip()
    if not normalized:
        raise ValueError('project_id cannot be empty')
    return runtime_state_base_root() / normalized


def is_wsl() -> bool:
    if os.environ.get('WSL_INTEROP') or os.environ.get('WSL_DISTRO_NAME'):
        return True
    proc_version = Path('/proc/version')
    try:
        return 'microsoft' in proc_version.read_text(encoding='utf-8', errors='ignore').lower()
    except Exception:
        return False


def socket_filesystem_hint(path: Path) -> str | None:
    normalized = str(Path(path).expanduser()).replace('\\', '/')
    if is_wsl() and _WSL_MOUNTED_DRIVE_RE.match(normalized):
        return 'wsl_drvfs'
    return None


def pathname_unix_socket_supported(path: Path) -> bool:
    return socket_filesystem_hint(path) != 'wsl_drvfs'


def pathname_runtime_state_supported(path: Path) -> bool:
    return socket_filesystem_hint(path) != 'wsl_drvfs'


def choose_runtime_state_placement(
    *,
    project_root: Path,
    project_id: str,
    anchor_path: Path,
) -> RuntimeStatePlacement:
    del project_root
    anchor = Path(anchor_path).expanduser()
    filesystem_hint = socket_filesystem_hint(anchor)
    runtime_state_home = _absolute_path_from_env('CCB_RUNTIME_STATE_HOME')
    if runtime_state_home is not None:
        return RuntimeStatePlacement(
            anchor_path=anchor,
            effective_path=runtime_state_home / project_id,
            root_kind='relocated',
            relocation_reason='runtime_state_home',
            filesystem_hint=filesystem_hint,
        )
    ref_root = runtime_state_root_from_anchor_ref(anchor, project_id=project_id)
    if ref_root is not None:
        return RuntimeStatePlacement(
            anchor_path=anchor,
            effective_path=ref_root,
            root_kind='relocated',
            relocation_reason='runtime_root_ref',
            filesystem_hint=filesystem_hint,
        )
    if filesystem_hint == 'wsl_drvfs':
        return RuntimeStatePlacement(
            anchor_path=anchor,
            effective_path=runtime_state_root_for_project(project_id),
            root_kind='relocated',
            relocation_reason='wsl_drvfs',
            filesystem_hint=filesystem_hint,
        )
    return RuntimeStatePlacement(
        anchor_path=anchor,
        effective_path=anchor,
        root_kind='project',
        relocation_reason=None,
        filesystem_hint=filesystem_hint,
    )


def choose_socket_placement(
    *,
    preferred_path: Path,
    project_socket_key: str,
    preferred_root_kind: Literal['project', 'runtime'] = 'project',
) -> SocketPlacement:
    preferred = Path(preferred_path).expanduser()
    if not unix_socket_path_is_safe(preferred):
        return _runtime_socket_placement(
            preferred_path=preferred,
            project_socket_key=project_socket_key,
            fallback_reason='path_too_long',
            filesystem_hint=socket_filesystem_hint(preferred),
        )
    filesystem_hint = socket_filesystem_hint(preferred)
    if not pathname_unix_socket_supported(preferred):
        return _runtime_socket_placement(
            preferred_path=preferred,
            project_socket_key=project_socket_key,
            fallback_reason='unsupported_filesystem',
            filesystem_hint=filesystem_hint,
        )
    return SocketPlacement(
        preferred_path=preferred,
        effective_path=preferred,
        root_kind=preferred_root_kind,
        fallback_reason=None,
        filesystem_hint=filesystem_hint,
    )


def socket_placement_payload(placement: SocketPlacement, *, prefix: str = '') -> dict[str, Any]:
    field_prefix = f'{prefix}_' if prefix else ''
    return {
        f'{field_prefix}preferred_socket_path': _display_path(placement.preferred_path),
        f'{field_prefix}effective_socket_path': _display_path(placement.effective_path),
        f'{field_prefix}socket_root_kind': placement.root_kind,
        f'{field_prefix}socket_fallback_reason': placement.fallback_reason,
        f'{field_prefix}socket_filesystem_hint': placement.filesystem_hint,
    }


def runtime_state_placement_payload(placement: RuntimeStatePlacement) -> dict[str, Any]:
    return {
        'project_anchor_path': _display_path(placement.anchor_path),
        'runtime_state_root': _display_path(placement.effective_path),
        'runtime_root_kind': placement.root_kind,
        'runtime_relocation_reason': placement.relocation_reason,
        'runtime_filesystem_hint': placement.filesystem_hint,
    }


def _display_path(path: Path) -> str:
    return Path(path).as_posix()


def runtime_root_marker_path(runtime_state_root: Path) -> Path:
    return Path(runtime_state_root).expanduser() / RUNTIME_ROOT_MARKER_FILENAME


def runtime_root_ref_path(anchor_path: Path) -> Path:
    return Path(anchor_path).expanduser() / RUNTIME_ROOT_REF_FILENAME


def runtime_state_root_from_anchor_ref(anchor_path: Path, *, project_id: str | None = None) -> Path | None:
    payload = read_runtime_root_ref_payload(anchor_path, project_id=project_id)
    if not payload:
        return None
    return Path(str(payload['runtime_state_root'])).expanduser()


def runtime_state_root_from_anchor(anchor_path: Path, *, project_id: str | None = None) -> Path:
    ref_root = runtime_state_root_from_anchor_ref(anchor_path, project_id=project_id)
    return ref_root if ref_root is not None else Path(anchor_path).expanduser()


def runtime_project_anchor_from_path(path: Path) -> Path | None:
    marker_path = find_runtime_root_marker_path(path)
    if marker_path is None:
        return None
    payload = read_runtime_root_marker_payload(marker_path)
    if not payload:
        return None
    return Path(str(payload['anchor_path'])).expanduser()


def runtime_project_root_from_path(path: Path) -> Path | None:
    marker_path = find_runtime_root_marker_path(path)
    if marker_path is None:
        return None
    payload = read_runtime_root_marker_payload(marker_path)
    if not payload:
        return None
    return Path(str(payload['project_root'])).expanduser()


def find_runtime_root_marker_path(path: Path) -> Path | None:
    current = Path(path).expanduser()
    for candidate in (current, *current.parents):
        marker = candidate / RUNTIME_ROOT_MARKER_FILENAME
        try:
            if marker.is_file():
                return marker
        except Exception:
            continue
    return None


def _runtime_socket_placement(
    *,
    preferred_path: Path,
    project_socket_key: str,
    fallback_reason: str,
    filesystem_hint: str | None,
) -> SocketPlacement:
    stem = preferred_path.stem
    effective_root = runtime_socket_root()
    return SocketPlacement(
        preferred_path=preferred_path,
        effective_path=effective_root / f'{stem}-{project_socket_key}.sock',
        root_kind='runtime',
        fallback_reason=fallback_reason,
        filesystem_hint=filesystem_hint,
    )


def read_runtime_root_ref_payload(anchor_path: Path, *, project_id: str | None = None) -> dict[str, Any]:
    payload = _read_json_object(runtime_root_ref_path(anchor_path))
    if not payload:
        return {}
    if str(payload.get('record_type') or '').strip() != RUNTIME_ROOT_REF_RECORD_TYPE:
        return {}
    recorded_project_id = str(payload.get('project_id') or '').strip()
    if not recorded_project_id:
        return {}
    if project_id is not None and recorded_project_id != str(project_id).strip():
        return {}
    runtime_state_root = _absolute_path_from_value(payload.get('runtime_state_root'))
    if runtime_state_root is None:
        return {}
    normalized = dict(payload)
    normalized['project_id'] = recorded_project_id
    normalized['runtime_state_root'] = str(runtime_state_root)
    return normalized


def read_runtime_root_marker_payload(marker_path: Path) -> dict[str, Any]:
    path = Path(marker_path).expanduser()
    payload = _read_json_object(path)
    if not payload:
        return {}
    if str(payload.get('record_type') or '').strip() != RUNTIME_ROOT_RECORD_TYPE:
        return {}
    project_id = str(payload.get('project_id') or '').strip()
    if not project_id:
        return {}
    runtime_root = _absolute_path_from_value(payload.get('runtime_root_path'))
    if runtime_root is None or runtime_root != path.parent:
        return {}
    project_root = _absolute_path_from_value(payload.get('project_root'))
    anchor_path = _absolute_path_from_value(payload.get('anchor_path'))
    if project_root is None or anchor_path is None:
        return {}
    if anchor_path.name != '.ccb':
        return {}
    if anchor_path != project_root / '.ccb':
        return {}
    normalized = dict(payload)
    normalized['project_id'] = project_id
    normalized['project_root'] = str(project_root)
    normalized['anchor_path'] = str(anchor_path)
    normalized['runtime_root_path'] = str(runtime_root)
    return normalized


def _absolute_path_from_env(env_name: str) -> Path | None:
    return _absolute_path_from_value(os.environ.get(env_name))


def _absolute_path_from_value(raw: object) -> Path | None:
    text = str(raw or '').strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        return None
    return path


def _account_home_dir() -> Path:
    try:
        home = str(pwd.getpwuid(os.getuid()).pw_dir or '').strip() if pwd is not None else ''
    except Exception:
        home = ''
    if home:
        return Path(home).expanduser()
    return Path.home().expanduser()


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    'RUNTIME_ROOT_MARKER_FILENAME',
    'RUNTIME_ROOT_REF_FILENAME',
    'RuntimeStatePlacement',
    'SocketPlacement',
    'TARGET_SEGMENT_PATTERN',
    'UNIX_SOCKET_SAFE_BYTES',
    'choose_socket_placement',
    'choose_runtime_state_placement',
    'find_runtime_root_marker_path',
    'is_wsl',
    'normalized_segment',
    'pathname_runtime_state_supported',
    'pathname_unix_socket_supported',
    'read_runtime_root_marker_payload',
    'read_runtime_root_ref_payload',
    'runtime_project_anchor_from_path',
    'runtime_project_root_from_path',
    'RUNTIME_ROOT_RECORD_TYPE',
    'RUNTIME_ROOT_REF_RECORD_TYPE',
    'runtime_root_marker_path',
    'runtime_root_ref_path',
    'runtime_socket_root',
    'runtime_socket_root_candidates',
    'runtime_state_base_root',
    'runtime_state_placement_payload',
    'runtime_state_root_candidates',
    'runtime_state_root_for_project',
    'runtime_state_root_from_anchor',
    'runtime_state_root_from_anchor_ref',
    'socket_placement_payload',
    'socket_filesystem_hint',
    'target_segment',
    'unix_socket_path_is_safe',
]
