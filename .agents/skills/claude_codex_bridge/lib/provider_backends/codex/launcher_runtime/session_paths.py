from __future__ import annotations

import json
from pathlib import Path

from provider_core.pathing import session_filename_for_agent
from provider_sessions.files import safe_write_session
from storage.locks import file_lock
from storage.path_helpers import runtime_project_anchor_from_path

from provider_backends.codex.comm_runtime.binding import (
    codex_session_meta_payload,
    is_codex_subagent_log,
)
from provider_backends.codex.comm_runtime.pathing import normalize_work_dir
from provider_backends.codex.session import CodexProjectSession
from provider_backends.codex.session_authority import remember_bound_session_authority, resume_authority_matches

from ..start_cmd import build_resume_start_cmd, extract_resume_session_id
from ..start_cmd_runtime.fields_runtime import resume_template_command


def load_resume_session_id(
    spec,
    runtime_dir: Path,
    profile=None,
    *,
    current_fingerprint: str | None = None,
    current_memory_fingerprint: str | None = None,
) -> str | None:
    session_path = preferred_session_path(spec, runtime_dir)
    if session_path is None:
        return None
    data = read_session_payload(session_path)
    if data is None:
        return None
    authority_matches = _provider_authority_matches(
        data,
        profile=profile,
        current_fingerprint=current_fingerprint,
        current_memory_fingerprint=current_memory_fingerprint,
    )
    if not authority_matches and not _legacy_namespace_authority_matches(
        data,
        runtime_dir=runtime_dir,
        current_fingerprint=current_fingerprint,
    ):
        return None
    if not _resume_session_binding_is_usable(data):
        return None
    repaired = _repair_invalid_native_fork_binding(data)
    if repaired is not None:
        repaired_id, repaired_path = repaired
        repaired_data = _persist_invalid_native_fork_repair(
            session_path,
            data,
            repaired_id=repaired_id,
            repaired_path=repaired_path,
        )
        if repaired_data is None:
            # A claimed native fork without matching parent evidence is not a
            # safe resume target.  Do not resume the blank/corrupt binding when
            # the repair could not be made durable.
            return None
        data = repaired_data
    descendant = _latest_linear_descendant(data, binding='current')
    if descendant is not None:
        descendant_id, descendant_path = descendant
        try:
            session = CodexProjectSession(session_file=session_path, data=dict(data))
            session.update_codex_log_binding(
                log_path=str(descendant_path),
                session_id=descendant_id,
                post_write_validate=descendant_path.is_file,
            )
            data = session.data
        except Exception:
            # Startup must retain the last durable binding if reconciliation
            # cannot be persisted atomically.
            pass
    return payload_resume_session_id(data)


def load_linked_continuation_session_id(
    spec,
    runtime_dir: Path,
    *,
    current_fingerprint: str,
) -> str | None:
    """Return a transcript that should seed a new native Codex fork."""
    session_path = preferred_session_path(spec, runtime_dir)
    if session_path is None:
        return None
    data = read_session_payload(session_path)
    if not isinstance(data, dict):
        return None
    if str(data.get('ccb_resume_compatibility') or '').strip() != 'linked_continuation':
        return None
    if str(data.get('codex_provider_authority_fingerprint') or '').strip() != str(current_fingerprint or '').strip():
        return None
    if str(data.get('codex_session_id') or '').strip() or str(data.get('codex_session_path') or '').strip():
        return None
    old_id = str(data.get('old_codex_session_id') or '').strip()
    old_path = _path_or_none(data.get('old_codex_session_path'))
    session_root = _path_or_none(data.get('codex_session_root'))
    if not old_id or old_path is None or session_root is None:
        return None
    if not old_path.is_file() or not _is_within(old_path, session_root):
        return None
    descendant = _latest_linear_descendant(data, binding='old')
    if descendant is not None:
        descendant_id, descendant_path = descendant
        updated = dict(data)
        updated['old_codex_session_id'] = descendant_id
        updated['old_codex_session_path'] = str(descendant_path)
        payload = json.dumps(updated, ensure_ascii=False, indent=2) + '\n'
        ok, _error = safe_write_session(session_path, payload)
        if ok:
            old_id = descendant_id
    return old_id


def _repair_invalid_native_fork_binding(data: dict[str, object]) -> tuple[str, Path] | None:
    if str(data.get('ccb_resume_compatibility') or '').strip() != 'native_fork_continuation':
        return None
    current_path = _path_or_none(data.get('codex_session_path'))
    old_id = str(data.get('old_codex_session_id') or '').strip()
    if current_path is None or not current_path.is_file() or not old_id:
        return None
    current_meta = codex_session_meta_payload(current_path)
    if isinstance(current_meta, dict) and str(current_meta.get('forked_from_id') or '').strip() == old_id:
        return None
    descendant = _latest_linear_descendant(data, binding='old')
    if descendant is not None:
        return descendant
    old_path = _path_or_none(data.get('old_codex_session_path'))
    root = _path_or_none(data.get('codex_session_root'))
    if old_path is not None and root is not None and old_path.is_file() and _is_within(old_path, root):
        return old_id, old_path
    return None


def _persist_invalid_native_fork_repair(
    session_path: Path,
    expected: dict[str, object],
    *,
    repaired_id: str,
    repaired_path: Path,
) -> dict[str, object] | None:
    lock_path = session_path.with_name(session_path.name + '.binding.lock')
    with file_lock(lock_path):
        persisted = read_session_payload(session_path)
        if persisted is None or _binding_identity(persisted) != _binding_identity(expected):
            return None
        if not repaired_path.is_file():
            return None
        updated = dict(persisted)
        rejected_id = str(updated.get('codex_session_id') or '').strip()
        rejected_path = str(updated.get('codex_session_path') or '').strip()
        updated['codex_session_id'] = repaired_id
        updated['codex_session_path'] = str(repaired_path)
        resume_cmd = build_resume_start_cmd(resume_template_command(updated), repaired_id)
        updated['start_cmd'] = resume_cmd
        updated['codex_start_cmd'] = resume_cmd
        updated['ccb_resume_compatibility'] = 'recovered_native_fork_mismatch'
        updated['ccb_continuity_status'] = 'recovered'
        updated['rejected_codex_session_id'] = rejected_id
        updated['rejected_codex_session_path'] = rejected_path
        updated['codex_binding_recovery_reason'] = 'native_fork_parent_mismatch'
        remember_bound_session_authority(updated)
        payload = json.dumps(updated, ensure_ascii=False, indent=2) + '\n'
        ok, _error = safe_write_session(session_path, payload)
        return updated if ok else None


def _binding_identity(data: dict[str, object]) -> tuple[str, str]:
    return (
        str(data.get('codex_session_path') or '').strip(),
        str(data.get('codex_session_id') or '').strip(),
    )


def _latest_linear_descendant(
    data: dict[str, object],
    *,
    binding: str,
) -> tuple[str, Path] | None:
    """Follow one unambiguous native fork chain inside the managed home.

    This repairs a session file that lagged behind while its bridge was down.
    Branching is deliberately fail-closed: CCB cannot infer which sibling the
    user intended to continue.
    """
    prefix = 'old_codex_' if binding == 'old' else 'codex_'
    base_id = str(data.get(prefix + 'session_id') or '').strip()
    base_path = _path_or_none(data.get(prefix + 'session_path'))
    session_root = _path_or_none(data.get('codex_session_root'))
    expected_cwd = _normalized_work_dir(data)
    if not base_id or base_path is None or session_root is None or expected_cwd is None:
        return None
    if not base_path.is_file() or not _is_within(base_path, session_root):
        return None

    children: dict[str, list[tuple[str, Path]]] = {}
    try:
        paths = sorted(session_root.glob('**/*.jsonl'))
    except OSError:
        return None
    for path in paths:
        if not path.is_file() or is_codex_subagent_log(path):
            continue
        meta = codex_session_meta_payload(path)
        if not isinstance(meta, dict):
            continue
        child_id = str(meta.get('session_id') or meta.get('id') or '').strip()
        parent_id = str(meta.get('forked_from_id') or '').strip()
        cwd = _normalize_cwd(meta.get('cwd'))
        if not child_id or not parent_id or cwd != expected_cwd:
            continue
        children.setdefault(parent_id, []).append((child_id, path))

    current_id = base_id
    current_path = base_path
    advanced = False
    visited = {base_id}
    while True:
        direct = children.get(current_id, [])
        if not direct:
            break
        if len(direct) != 1:
            return None
        next_id, next_path = direct[0]
        if next_id in visited:
            return None
        visited.add(next_id)
        current_id, current_path = next_id, next_path
        advanced = True
    return (current_id, current_path) if advanced else None


def _normalized_work_dir(data: dict[str, object]) -> str | None:
    return _normalize_cwd(data.get('work_dir') or data.get('workspace_path') or data.get('start_dir'))


def _normalize_cwd(value: object) -> str | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        return normalize_work_dir(Path(raw).expanduser())
    except Exception:
        return None


def agent_session_path(spec, runtime_dir: Path) -> Path | None:
    ccb_dir = find_project_ccb_dir(runtime_dir)
    if ccb_dir is None:
        return None
    return ccb_dir / session_filename_for_agent('codex', spec.name)


def find_project_ccb_dir(runtime_dir: Path) -> Path | None:
    current = Path(runtime_dir)
    for parent in (current, *current.parents):
        if parent.name == '.ccb':
            return parent
    return runtime_project_anchor_from_path(current)


def session_file_for_runtime_dir(runtime_dir: Path) -> Path | None:
    ccb_dir = find_project_ccb_dir(runtime_dir)
    if ccb_dir is None:
        return None
    try:
        agent_name = runtime_dir.parents[1].name
    except Exception:
        return None
    agent_name = str(agent_name or '').strip()
    if not agent_name:
        return None
    return ccb_dir / session_filename_for_agent('codex', agent_name)


def state_dir_for_runtime_dir(runtime_dir: Path) -> Path | None:
    current = Path(runtime_dir)
    normalized_provider = str(current.name or '').strip().lower()
    if not normalized_provider:
        return None
    parent = current.parent
    if parent.name != 'provider-runtime':
        return None
    agent_dir = parent.parent
    if not agent_dir.name:
        return None
    return agent_dir / 'provider-state' / normalized_provider


def preferred_session_path(spec, runtime_dir: Path) -> Path | None:
    candidates = (agent_session_path(spec, runtime_dir),)
    for session_path in candidates:
        if session_path is not None and session_path.is_file():
            return session_path
    return None


def read_session_payload(session_path: Path) -> dict | None:
    try:
        data = json.loads(session_path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def payload_resume_session_id(data: dict) -> str | None:
    session_id = str(data.get('codex_session_id') or '').strip()
    if session_id:
        return session_id
    start_cmd = str(data.get('codex_start_cmd') or data.get('start_cmd') or '').strip()
    if not start_cmd:
        return None
    return extract_resume_session_id(start_cmd)


def _provider_authority_matches(
    data: dict,
    *,
    profile,
    current_fingerprint: str | None,
    current_memory_fingerprint: str | None,
) -> bool:
    return resume_authority_matches(
        data,
        profile=profile,
        current_fingerprint=current_fingerprint,
        current_memory_fingerprint=current_memory_fingerprint,
    )


def _legacy_namespace_authority_matches(
    data: dict,
    *,
    runtime_dir: Path,
    current_fingerprint: str | None,
) -> bool:
    """Allow legacy records only when the active home already proves authority."""
    if str(data.get('codex_provider_authority_fingerprint') or '').strip():
        return False
    if str(data.get('codex_session_authority_fingerprint') or '').strip():
        return False
    fingerprint = str(current_fingerprint or '').strip()
    if not fingerprint:
        return False
    codex_home = _path_or_none(data.get('codex_home'))
    if codex_home is None:
        from .command_runtime.home import _extract_command_path

        for key in ('codex_start_cmd', 'start_cmd'):
            codex_home = _extract_command_path(str(data.get(key) or ''), 'CODEX_HOME')
            if codex_home is not None:
                break
    if codex_home is None:
        state_dir = state_dir_for_runtime_dir(runtime_dir)
        if state_dir is not None:
            codex_home = state_dir / 'home'
    if codex_home is None:
        return False
    marker_path = codex_home / '.ccb-session-namespace.json'
    try:
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
    except Exception:
        return False
    return (
        isinstance(marker, dict)
        and str(marker.get('provider_authority_fingerprint') or '').strip() == fingerprint
    )


def _resume_session_binding_is_usable(data: dict) -> bool:
    session_path = _path_or_none(data.get('codex_session_path'))
    if session_path is None:
        return True
    if not session_path.is_file():
        return False
    session_root = _path_or_none(data.get('codex_session_root'))
    if session_root is not None and not _is_within(session_path, session_root):
        return False
    return True


def _path_or_none(value: object) -> Path | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    if raw.startswith('\\\\wsl.localhost\\') or raw.startswith('\\\\wsl$\\'):
        parts = raw.split('\\')
        if len(parts) >= 4:
            raw = '/' + '/'.join(parts[4:])
    try:
        return Path(raw).expanduser()
    except Exception:
        return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).expanduser().resolve().relative_to(Path(root).expanduser().resolve())
        return True
    except Exception:
        return False


__all__ = [
    'load_linked_continuation_session_id',
    'load_resume_session_id',
    'session_file_for_runtime_dir',
    'state_dir_for_runtime_dir',
]
