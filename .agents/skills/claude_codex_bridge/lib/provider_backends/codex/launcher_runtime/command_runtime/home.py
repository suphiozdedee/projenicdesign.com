from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import time

from provider_backends.codex.session_authority import (
    current_memory_projection_fingerprint,
    current_provider_authority_fingerprint,
    has_resume_candidate,
    stored_provider_authority_fingerprint,
    stored_session_authority_fingerprint,
)
from provider_backends.codex.start_cmd import strip_resume_start_cmd
from provider_backends.session_authority import rebind_provider_session_data
from provider_sessions.files import safe_write_session
from provider_core.inherited_skills import materialize_required_control_skills
from provider_profiles.codex_home_config import (
    codex_provider_authority_fingerprint,
    materialize_codex_home_config,
    repair_codex_activity_hooks,
)

from .diagnostics import ensure_codex_diagnostic_log_filter

from ..session_paths import read_session_payload, session_file_for_runtime_dir, state_dir_for_runtime_dir


_ENV_ASSIGNMENT_RE = re.compile(
    r"(?:(?:^|[;\s])export\s+|(?:^|[;\s]))(?P<name>[A-Z0-9_]+)=(?P<value>'[^']*'|\"[^\"]*\"|[^;\s]+)"
)
_SESSION_NAMESPACE_MARKER = '.ccb-session-namespace.json'


@dataclass(frozen=True)
class CodexHomeLayout:
    codex_home: Path
    session_root: Path


def resolve_codex_home_layout(runtime_dir: Path, profile) -> CodexHomeLayout:
    explicit_runtime_home = _profile_runtime_home(profile)
    if explicit_runtime_home is not None:
        return CodexHomeLayout(
            codex_home=explicit_runtime_home,
            session_root=explicit_runtime_home / 'sessions',
        )

    existing = _existing_layout(runtime_dir)
    if existing is not None:
        return existing

    isolated_home = _managed_isolated_home(runtime_dir)
    return CodexHomeLayout(
        codex_home=isolated_home,
        session_root=isolated_home / 'sessions',
    )


def prepare_codex_home_overrides(
    runtime_dir: Path,
    profile,
    *,
    refresh_home: bool = False,
    project_root: Path | None = None,
    agent_name: str | None = None,
    workspace_path: Path | None = None,
    memory_projection_event_path: Path | None = None,
    memory_projection_marker_path: Path | None = None,
    enforce_session_namespace: bool = True,
) -> dict[str, str]:
    layout = resolve_codex_home_layout(runtime_dir, profile)
    layout.codex_home.mkdir(parents=True, exist_ok=True)
    layout.session_root.mkdir(parents=True, exist_ok=True)
    if refresh_home:
        _prepare_managed_home(
            _system_codex_home(),
            layout.codex_home,
            profile=profile,
            runtime_dir=runtime_dir,
            project_root=project_root,
            agent_name=agent_name,
            workspace_path=workspace_path,
            memory_projection_event_path=memory_projection_event_path,
            memory_projection_marker_path=memory_projection_marker_path,
        )
    if enforce_session_namespace:
        _ensure_session_namespace_authority(runtime_dir, layout.codex_home, layout.session_root, profile=profile)
    if not refresh_home:
        repair_codex_activity_hooks(
            layout.codex_home,
            project_root=project_root,
            agent_name=agent_name,
            runtime_dir=runtime_dir,
            workspace_path=workspace_path,
        )
    materialize_required_control_skills(
        provider='codex',
        target_dir=layout.codex_home / 'skills',
    )

    overrides = {
        'CODEX_HOME': str(layout.codex_home),
        'CODEX_SESSION_ROOT': str(layout.session_root),
        # Some Codex builds consult this independently from CODEX_HOME.
        # Pin it as well so SQLite state never falls back to the caller's home.
        'CODEX_SQLITE_HOME': str(layout.codex_home),
    }
    ensure_codex_diagnostic_log_filter(layout.codex_home, runtime_dir=runtime_dir)

    if "WSL_DISTRO_NAME" in os.environ:
        # We are running inside WSL. The target executable might be a Windows binary (via interop).
        # Set USERPROFILE to the same isolated path and instruct WSLENV to automatically translate paths.
        overrides['USERPROFILE'] = str(layout.codex_home)
        wslenv_additions = "CODEX_HOME/p:CODEX_SESSION_ROOT/p:CODEX_SQLITE_HOME/p:USERPROFILE/p"
        existing_wslenv = os.environ.get("WSLENV", "")
        if existing_wslenv:
            overrides['WSLENV'] = f"{wslenv_additions}:{existing_wslenv}"
        else:
            overrides['WSLENV'] = wslenv_additions

    return overrides


def _profile_runtime_home(profile) -> Path | None:
    runtime_home = getattr(profile, 'runtime_home', None) if profile is not None else None
    if not runtime_home:
        return None
    return Path(runtime_home).expanduser()


def _existing_layout(runtime_dir: Path) -> CodexHomeLayout | None:
    session_file = session_file_for_runtime_dir(runtime_dir)
    if session_file is None or not session_file.is_file():
        return None
    data = read_session_payload(session_file)
    if not isinstance(data, dict):
        return None
    return _layout_from_payload(data)


def _layout_from_payload(data: dict[str, object]) -> CodexHomeLayout | None:
    codex_home = _path_or_none(data.get('codex_home'))
    session_root = _path_or_none(data.get('codex_session_root'))
    if session_root is None:
        session_root = _session_root_from_commands(data)
    if session_root is None and codex_home is not None:
        session_root = codex_home / 'sessions'
    if session_root is None:
        session_root = _session_root_from_log_path(data.get('codex_session_path'))
    if session_root is None:
        return None
    if codex_home is None:
        codex_home = _codex_home_from_commands(data)
    if codex_home is None:
        codex_home = _legacy_root_to_home(session_root)
    _migrate_legacy_session_root(session_root, codex_home / 'sessions')
    return CodexHomeLayout(codex_home=codex_home, session_root=codex_home / 'sessions')


def _session_root_from_commands(data: dict[str, object]) -> Path | None:
    commands = (
        str(data.get('codex_start_cmd') or '').strip(),
        str(data.get('start_cmd') or '').strip(),
    )
    for command in commands:
        session_root = _extract_command_path(command, 'CODEX_SESSION_ROOT')
        if session_root is not None:
            return session_root
        codex_home = _extract_command_path(command, 'CODEX_HOME')
        if codex_home is not None:
            return codex_home / 'sessions'
    return None


def _codex_home_from_commands(data: dict[str, object]) -> Path | None:
    commands = (
        str(data.get('codex_start_cmd') or '').strip(),
        str(data.get('start_cmd') or '').strip(),
    )
    for command in commands:
        codex_home = _extract_command_path(command, 'CODEX_HOME')
        if codex_home is not None:
            return codex_home
    return None


def _extract_command_path(command: str, env_name: str) -> Path | None:
    if not command:
        return None
    for match in _ENV_ASSIGNMENT_RE.finditer(command):
        if match.group('name') != env_name:
            continue
        return _path_or_none(_unquote_env_value(match.group('value')))
    return None


def _unquote_env_value(value: str) -> str:
    text = str(value or '').strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _session_root_from_log_path(value: object) -> Path | None:
    log_path = _path_or_none(value)
    if log_path is None:
        return None
    for parent in (log_path.parent, *log_path.parents):
        if parent.name == 'sessions':
            return parent
    return None


def _path_or_none(value: object) -> Path | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser()
    except Exception:
        return None


def _managed_state_dir(runtime_dir: Path) -> Path:
    derived = state_dir_for_runtime_dir(runtime_dir)
    if derived is not None:
        return derived
    return Path(runtime_dir).expanduser() / 'codex-state'


def _managed_isolated_home(runtime_dir: Path) -> Path:
    return _managed_state_dir(runtime_dir) / 'home'
def _legacy_root_to_home(session_root: Path) -> Path:
    normalized_root = Path(session_root).expanduser()
    if normalized_root.name == 'sessions':
        parent = normalized_root.parent
        if parent.name == 'home':
            return parent
        return parent / 'home'
    return normalized_root / 'home'


def _migrate_legacy_session_root(source_root: Path, target_root: Path) -> None:
    normalized_source = Path(source_root).expanduser()
    normalized_target = Path(target_root).expanduser()
    if normalized_source == normalized_target:
        normalized_target.mkdir(parents=True, exist_ok=True)
        return
    if normalized_source.name != 'sessions':
        normalized_target.mkdir(parents=True, exist_ok=True)
        return
    if normalized_target.exists():
        return
    normalized_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(normalized_source), str(normalized_target))
    except Exception:
        normalized_target.mkdir(parents=True, exist_ok=True)


def _system_codex_home() -> Path:
    return Path(os.environ.get('CODEX_HOME') or (Path.home() / '.codex')).expanduser()


def _prepare_managed_home(
    source_home: Path,
    target_home: Path,
    *,
    profile,
    runtime_dir: Path,
    project_root: Path | None,
    agent_name: str | None,
    workspace_path: Path | None,
    memory_projection_event_path: Path | None,
    memory_projection_marker_path: Path | None,
) -> None:
    materialize_codex_home_config(
        target_home,
        profile=profile,
        source_home=source_home,
        project_root=project_root,
        agent_name=agent_name,
        runtime_dir=runtime_dir,
        workspace_path=workspace_path,
        memory_projection_event_path=memory_projection_event_path,
        memory_projection_marker_path=memory_projection_marker_path,
    )


def _ensure_session_namespace_authority(runtime_dir: Path, codex_home: Path, session_root: Path, *, profile) -> None:
    current_fingerprint = current_provider_authority_fingerprint(profile, runtime_dir=runtime_dir)
    legacy_fingerprint = str(codex_provider_authority_fingerprint(profile) or '').strip()
    legacy_layout_compatible = _legacy_layout_is_managed(
        runtime_dir,
        codex_home=codex_home,
        session_root=session_root,
        profile=profile,
    )
    memory_fingerprint = current_memory_projection_fingerprint(runtime_dir)
    marker_path = codex_home / _SESSION_NAMESPACE_MARKER
    stored_marker = _read_session_namespace_marker(marker_path)
    session_file = session_file_for_runtime_dir(runtime_dir)
    session_data = read_session_payload(session_file) if session_file is not None and session_file.is_file() else {}
    marker_fingerprint = (
        str(stored_marker.get('provider_authority_fingerprint') or '').strip()
        if stored_marker is not None
        else ''
    )
    if stored_marker is not None and legacy_layout_compatible:
        _recover_v855_legacy_archive(
            codex_home=codex_home,
            session_root=session_root,
            session_file=session_file,
            session_data=session_data,
            current_fingerprint=current_fingerprint,
            legacy_fingerprint=legacy_fingerprint,
            archived_fingerprint=marker_fingerprint,
            restore_binding=marker_fingerprint == current_fingerprint,
        )
        session_data = read_session_payload(session_file) if session_file is not None and session_file.is_file() else {}
    if _session_namespace_requires_reset(
        stored_marker=stored_marker,
        current_fingerprint=current_fingerprint,
        current_memory_fingerprint=memory_fingerprint,
        session_data=session_data,
        legacy_fingerprint=legacy_fingerprint,
        legacy_layout_compatible=legacy_layout_compatible,
        namespace_has_entries=_directory_has_entries(session_root),
    ):
        if legacy_layout_compatible:
            _link_project_session_binding(
                session_file,
                codex_home=codex_home,
                session_root=session_root,
                current_fingerprint=current_fingerprint,
            )
        else:
            _archive_session_root(
                codex_home,
                session_root,
                label=(
                    _marker_label(stored_marker)
                    or stored_provider_authority_fingerprint(session_data)
                ),
            )
            _scrub_project_session_binding(
                session_file,
                codex_home=codex_home,
                session_root=session_root,
            )
    elif stored_marker is None and legacy_layout_compatible:
        _adopt_legacy_session_authority(
            session_file,
            codex_home=codex_home,
            session_root=session_root,
            current_fingerprint=current_fingerprint,
        )
    _write_session_namespace_marker(marker_path, current_fingerprint, memory_fingerprint=memory_fingerprint)


def _session_namespace_marker_exists(codex_home: Path) -> bool:
    return (Path(codex_home) / _SESSION_NAMESPACE_MARKER).is_file()


def _read_session_namespace_marker(marker_path: Path) -> dict[str, str] | None:
    try:
        data = json.loads(marker_path.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {
        'provider_authority_fingerprint': str(data.get('provider_authority_fingerprint') or '').strip(),
        'memory_projection_sha256': str(data.get('memory_projection_sha256') or '').strip(),
    }


def _session_namespace_requires_reset(
    *,
    stored_marker: dict[str, str] | None,
    current_fingerprint: str,
    current_memory_fingerprint: str,
    session_data: dict[str, object],
    legacy_fingerprint: str = '',
    legacy_layout_compatible: bool = True,
    namespace_has_entries: bool = False,
) -> bool:
    del current_memory_fingerprint
    stored_session_fingerprint = stored_provider_authority_fingerprint(session_data)
    stored_binding_fingerprint = stored_session_authority_fingerprint(session_data)
    if stored_marker is not None:
        marker_fingerprint = str(stored_marker.get('provider_authority_fingerprint') or '').strip()
        if marker_fingerprint == current_fingerprint:
            return False
        # A crash can leave the marker one write behind a fully rebound session.
        # Current HMAC-bound session evidence is sufficient to heal that marker.
        if (
            legacy_layout_compatible
            and stored_session_fingerprint == current_fingerprint
            and (
                not has_resume_candidate(session_data)
                or stored_binding_fingerprint == current_fingerprint
            )
        ):
            return False
        return True
    if not legacy_layout_compatible:
        return bool(
            namespace_has_entries
            or has_resume_candidate(session_data)
            or stored_session_fingerprint
            or stored_binding_fingerprint
        )
    compatible = {value for value in (current_fingerprint, legacy_fingerprint) if value}
    recorded = {value for value in (stored_session_fingerprint, stored_binding_fingerprint) if value}
    if not recorded:
        return False
    return not recorded.issubset(compatible)


def _legacy_layout_is_managed(
    runtime_dir: Path,
    *,
    codex_home: Path,
    session_root: Path,
    profile,
) -> bool:
    explicit_home = _profile_runtime_home(profile)
    expected_home = explicit_home if explicit_home is not None else _managed_isolated_home(runtime_dir)
    try:
        normalized_home = Path(codex_home).expanduser().resolve()
        normalized_expected = expected_home.expanduser().resolve()
        normalized_sessions = Path(session_root).expanduser().resolve()
    except OSError:
        return False
    return (
        normalized_home == normalized_expected
        and normalized_sessions == (normalized_home / 'sessions').resolve()
    )


def _directory_has_entries(path: Path) -> bool:
    try:
        return next(Path(path).iterdir(), None) is not None
    except OSError:
        return False


def _adopt_legacy_session_authority(
    session_file: Path | None,
    *,
    codex_home: Path,
    session_root: Path,
    current_fingerprint: str,
) -> None:
    if session_file is None or not session_file.is_file() or not current_fingerprint:
        return
    data = read_session_payload(session_file)
    if not isinstance(data, dict):
        return
    data['codex_home'] = str(codex_home)
    data['codex_session_root'] = str(session_root)
    data['codex_provider_authority_fingerprint'] = current_fingerprint
    if has_resume_candidate(data) and _session_binding_path_is_usable(data, session_root):
        data['codex_session_authority_fingerprint'] = current_fingerprint
    ok, error = safe_write_session(session_file, json.dumps(data, ensure_ascii=False, indent=2))
    if not ok:
        raise RuntimeError(error or f'failed to adopt legacy Codex session authority: {session_file}')


def _recover_v855_legacy_archive(
    *,
    codex_home: Path,
    session_root: Path,
    session_file: Path | None,
    session_data: dict[str, object],
    current_fingerprint: str,
    legacy_fingerprint: str,
    archived_fingerprint: str = '',
    restore_binding: bool = True,
) -> bool:
    if session_file is None or not session_file.is_file() or not current_fingerprint:
        return False
    old_path = _path_or_none(session_data.get('old_codex_session_path'))
    old_id = str(session_data.get('old_codex_session_id') or '').strip()
    if old_path is None or not old_id:
        return False
    relative_old_path = _relative_session_path(old_path, session_root)
    if relative_old_path is None:
        return False
    labels = {'global'}
    if legacy_fingerprint:
        labels.add(legacy_fingerprint)
    archive_root = codex_home / 'archived-sessions'
    candidates = []
    if archive_root.is_dir():
        candidates = sorted(
            (
                path
                for path in archive_root.iterdir()
                if path.is_dir()
                and not path.is_symlink()
                and any(path.name.endswith(f'-{label}') for label in labels)
                and (path / relative_old_path).is_file()
            ),
            key=lambda path: path.name,
            reverse=True,
        )
    if not candidates:
        return False
    _merge_legacy_archive(candidates[0], session_root)
    restored_old_path = session_root / relative_old_path
    if not restored_old_path.is_file():
        return False

    data = read_session_payload(session_file)
    if not isinstance(data, dict):
        return False
    data['codex_home'] = str(codex_home)
    data['codex_session_root'] = str(session_root)
    current_id = str(data.get('codex_session_id') or '').strip()
    if not current_id:
        data['codex_session_id'] = old_id
        data['codex_session_path'] = str(restored_old_path)
    if restore_binding:
        data['codex_provider_authority_fingerprint'] = current_fingerprint
        data['codex_session_authority_fingerprint'] = current_fingerprint
    elif archived_fingerprint:
        data.setdefault('codex_provider_authority_fingerprint', archived_fingerprint)
        data.setdefault('codex_session_authority_fingerprint', archived_fingerprint)
    ok, error = safe_write_session(session_file, json.dumps(data, ensure_ascii=False, indent=2))
    if not ok:
        raise RuntimeError(error or f'failed to restore archived Codex session binding: {session_file}')
    return True


def _merge_legacy_archive(archive_dir: Path, session_root: Path) -> None:
    session_root.mkdir(parents=True, exist_ok=True)
    for source in sorted(archive_dir.rglob('*')):
        if source.is_symlink() or not source.is_file():
            continue
        relative = source.relative_to(archive_dir)
        target = session_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            continue
        shutil.move(str(source), str(target))
    directories = sorted(
        (path for path in archive_dir.rglob('*') if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        archive_dir.rmdir()
    except OSError:
        pass


def _session_binding_path_is_usable(data: dict[str, object], session_root: Path) -> bool:
    session_path = _path_or_none(data.get('codex_session_path'))
    if session_path is None:
        return True
    return session_path.is_file() and _relative_session_path(session_path, session_root) is not None


def _relative_session_path(path: Path, session_root: Path) -> Path | None:
    try:
        return Path(path).expanduser().resolve().relative_to(
            Path(session_root).expanduser().resolve()
        )
    except (OSError, ValueError):
        return None


def _marker_label(stored_marker: dict[str, str] | None) -> str:
    if not stored_marker:
        return ''
    return (
        str(stored_marker.get('provider_authority_fingerprint') or '').strip()
        or str(stored_marker.get('memory_projection_sha256') or '').strip()
    )


def _archive_session_root(codex_home: Path, session_root: Path, *, label: str) -> None:
    normalized_root = Path(session_root).expanduser()
    if not normalized_root.exists():
        normalized_root.mkdir(parents=True, exist_ok=True)
        return
    try:
        has_entries = next(normalized_root.iterdir(), None) is not None
    except Exception:
        has_entries = False
    if not has_entries:
        normalized_root.mkdir(parents=True, exist_ok=True)
        return
    archive_parent = codex_home / 'archived-sessions'
    archive_parent.mkdir(parents=True, exist_ok=True)
    archive_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{_archive_label(label)}"
    archive_path = archive_parent / archive_name
    try:
        shutil.move(str(normalized_root), str(archive_path))
    except Exception:
        pass
    normalized_root.mkdir(parents=True, exist_ok=True)


def _archive_label(label: str) -> str:
    text = str(label or '').strip().lower()
    if not text:
        return 'global'
    return re.sub(r'[^a-z0-9._-]+', '-', text)[:32] or 'global'


def _scrub_project_session_binding(
    session_file: Path | None,
    *,
    codex_home: Path | None = None,
    session_root: Path | None = None,
) -> None:
    if session_file is None or not session_file.is_file():
        return
    data = read_session_payload(session_file)
    if not isinstance(data, dict):
        return
    old_id = str(data.get('codex_session_id') or '').strip()
    old_path = str(data.get('codex_session_path') or '').strip()
    changed = False
    if codex_home is not None and not str(data.get('codex_home') or '').strip():
        data['codex_home'] = str(codex_home)
        changed = True
    if session_root is not None and not str(data.get('codex_session_root') or '').strip():
        data['codex_session_root'] = str(session_root)
        changed = True
    if old_id and data.get('old_codex_session_id') != old_id:
        data['old_codex_session_id'] = old_id
        changed = True
    if old_path and data.get('old_codex_session_path') != old_path:
        data['old_codex_session_path'] = old_path
        changed = True
    if old_id or old_path:
        data['old_updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        changed = True
    for key in ('codex_session_id', 'codex_session_path', 'codex_session_authority_fingerprint'):
        if key in data:
            data.pop(key, None)
            changed = True
    for key in ('start_cmd', 'codex_start_cmd'):
        stripped = strip_resume_start_cmd(data.get(key))
        current = str(data.get(key) or '').strip()
        if stripped and stripped != current:
            data[key] = stripped
            changed = True
    if not changed:
        return
    ok, error = safe_write_session(session_file, json.dumps(data, ensure_ascii=False, indent=2))
    if not ok:
        raise RuntimeError(error or f'failed to rewrite session file: {session_file}')


def _link_project_session_binding(
    session_file: Path | None,
    *,
    codex_home: Path,
    session_root: Path,
    current_fingerprint: str,
) -> None:
    """Start a new authority generation without hiding Agent-owned transcripts."""
    if session_file is None or not session_file.is_file():
        return
    data = read_session_payload(session_file)
    if not isinstance(data, dict):
        return

    old_id = str(data.get('codex_session_id') or '').strip()
    old_path = _path_or_none(data.get('codex_session_path'))
    if old_path is not None and _relative_session_path(old_path, session_root) is None:
        data.pop('codex_session_path', None)
        old_path = None

    rebind_provider_session_data(
        data,
        'codex',
        current_fingerprint,
        native_resume_compatible=False,
    )
    data['codex_home'] = str(codex_home)
    data['codex_session_root'] = str(session_root)
    if old_id:
        data['old_codex_session_id'] = old_id
    if old_path is not None:
        data['old_codex_session_path'] = str(old_path)
    if old_id or old_path is not None:
        data['old_updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    for key in ('start_cmd', 'codex_start_cmd'):
        stripped = strip_resume_start_cmd(data.get(key))
        current = str(data.get(key) or '').strip()
        if stripped and stripped != current:
            data[key] = stripped

    ok, error = safe_write_session(
        session_file,
        json.dumps(data, ensure_ascii=False, indent=2),
    )
    if not ok:
        raise RuntimeError(error or f'failed to link Codex session continuity: {session_file}')


def _write_session_namespace_marker(marker_path: Path, fingerprint: str, *, memory_fingerprint: str = '') -> None:
    payload = {
        'provider': 'codex',
        'provider_authority_fingerprint': str(fingerprint or '').strip(),
        'memory_projection_sha256': str(memory_fingerprint or '').strip(),
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'version': 1,
    }
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


__all__ = ['CodexHomeLayout', 'prepare_codex_home_overrides', 'resolve_codex_home_layout']
