from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath

_TAIL_SUFFIXES = {'.log', '.jsonl', '.txt', '.yaml', '.yml'}
_COPY_SUFFIXES = {'.json', '.pid'}
_PROVIDER_STATE_SUFFIXES = _TAIL_SUFFIXES | _COPY_SUFFIXES | {'.toml'}
_PROVIDER_STATE_SECRET_FILENAMES = {
    '.credentials.json',
    '.env',
    'auth.json',
    'google_accounts.json',
    'oauth_creds.json',
}
_PROVIDER_STATE_SECRET_DIRNAMES = {'.codeisland'}
_EXCLUDED_PROVIDER_STORAGE_CLASSES = {'secret', 'rebuildable_cache', 'startup_authority_bundle'}
_PROVIDER_STATE_HARD_EXCLUDED_SEGMENTS = (
    ('.tmp', 'plugins'),
    ('.local', 'share', 'claude', 'versions'),
    ('.npm', '_cacache'),
    ('.cache', 'node-gyp'),
    ('.cache', 'vscode-ripgrep'),
)


def project_root_sources(
    context,
    *,
    storage_payload: dict[str, object] | None = None,
) -> tuple[tuple[str, Path], ...]:
    storage_records = _storage_records_by_path(storage_payload)
    items: list[tuple[str, Path]] = [
        ('project-config', context.paths.config_path),
        ('ccbd-authority', context.paths.ccbd_lifecycle_path),
        ('ccbd-authority', context.paths.ccbd_lease_path),
        ('ccbd-authority', context.paths.ccbd_keeper_path),
        ('ccbd-authority', context.paths.ccbd_shutdown_intent_path),
        ('ccbd-authority', context.paths.ccbd_state_path),
        ('ccbd-authority', context.paths.ccbd_start_policy_path),
        ('ccbd-report', context.paths.ccbd_startup_report_path),
        ('ccbd-report', context.paths.ccbd_shutdown_report_path),
        ('ccbd-report', context.paths.ccbd_restore_report_path),
        ('ccbd-events', context.paths.ccbd_submissions_path),
        ('ccbd-events', context.paths.ccbd_messages_path),
        ('ccbd-events', context.paths.ccbd_attempts_path),
        ('ccbd-events', context.paths.ccbd_replies_path),
        ('ccbd-events', context.paths.ccbd_dead_letters_path),
        ('ccbd-events', context.paths.ccbd_supervision_path),
        ('ccbd-events', context.paths.ccbd_lifecycle_log_path),
        ('ccbd-events', context.paths.ccbd_tmux_cleanup_history_path),
        ('ccbd-log', context.paths.ccbd_dir / 'ccbd.stdout.log'),
        ('ccbd-log', context.paths.ccbd_dir / 'ccbd.stderr.log'),
        ('ccbd-log', context.paths.ccbd_dir / 'keeper.stdout.log'),
        ('ccbd-log', context.paths.ccbd_dir / 'keeper.stderr.log'),
    ]

    if (
        context.paths.runtime_state_placement.root_kind == 'relocated'
        or context.paths.runtime_root_ref_path.exists()
        or context.paths.runtime_root_marker_path.exists()
    ):
        items.extend(
            [
                ('runtime-root', context.paths.runtime_root_ref_path),
                ('runtime-root', context.paths.runtime_root_marker_path),
            ]
        )

    items.extend(iter_dir_files('ccbd-execution', context.paths.ccbd_executions_dir, suffixes={'.json'}))
    items.extend(iter_dir_files('ccbd-snapshot', context.paths.ccbd_snapshots_dir, suffixes={'.json'}))
    items.extend(iter_dir_files('ccbd-cursor', context.paths.ccbd_cursors_dir, suffixes={'.json'}))
    items.extend(iter_dir_files('ccbd-heartbeat', context.paths.ccbd_heartbeats_dir, suffixes={'.json'}))
    items.extend(
        iter_dir_files(
            'ccbd-maintenance-heartbeat',
            context.paths.ccbd_maintenance_heartbeat_dir,
            suffixes={'.json', '.jsonl'},
        )
    )
    items.extend(iter_dir_files('ccbd-text-artifact', context.paths.ccbd_text_artifacts_dir, suffixes={'.txt'}))
    items.extend(iter_dir_files('ccbd-health', context.paths.ccbd_provider_health_dir, suffixes={'.jsonl'}))
    items.extend(iter_dir_files('ccbd-mailbox', context.paths.ccbd_mailboxes_dir, suffixes={'.json', '.jsonl'}))
    items.extend(iter_dir_files('ccbd-lease', context.paths.ccbd_leases_dir, suffixes={'.json'}))
    items.extend(
        _agent_source_items(
            context,
            seen_sources={path for _, path in items},
            storage_records=storage_records,
        )
    )
    return tuple(items)


def iter_dir_files(category: str, root: Path, *, suffixes: set[str]) -> list[tuple[str, Path]]:
    if not root.exists() or not root.is_dir():
        return []
    files: list[tuple[str, Path]] = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        files.append((category, path))
    return files


def iter_provider_state_files(
    category: str,
    root: Path,
    *,
    storage_records: dict[str, dict[str, object]] | None = None,
) -> list[tuple[str, Path]]:
    if not root.exists() or not root.is_dir():
        return []
    files: list[tuple[str, Path]] = []
    for path in _walk_provider_state_files(root):
        if path.suffix.lower() not in _PROVIDER_STATE_SUFFIXES:
            continue
        if _provider_state_path_hard_excluded(path, root=root):
            continue
        storage_class = _storage_class_for_path(path, storage_records)
        if storage_class in _EXCLUDED_PROVIDER_STORAGE_CLASSES:
            continue
        if any(part.lower() in _PROVIDER_STATE_SECRET_DIRNAMES for part in path.parts):
            continue
        if path.name.lower() in _PROVIDER_STATE_SECRET_FILENAMES:
            continue
        files.append((category, path))
    return files


def _walk_provider_state_files(root: Path):
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for dirname in sorted(dirnames):
            candidate = current_path / dirname
            if candidate.is_symlink() or _provider_state_path_hard_excluded(candidate, root=root):
                continue
            safe_dirs.append(dirname)
        dirnames[:] = safe_dirs
        for filename in sorted(filenames):
            candidate = current_path / filename
            if candidate.is_symlink():
                continue
            if candidate.is_file():
                yield candidate


def _provider_state_path_hard_excluded(path: Path, *, root: Path) -> bool:
    try:
        parts = tuple(part.lower() for part in path.relative_to(root).parts)
    except Exception:
        parts = tuple(part.lower() for part in path.parts)
    if len(parts) >= 2 and parts[-2:] == ('.tmp', 'plugins.sha'):
        return True
    return any(_parts_contain(parts, segment) for segment in _PROVIDER_STATE_HARD_EXCLUDED_SEGMENTS)


def _parts_contain(parts: tuple[str, ...], segment: tuple[str, ...]) -> bool:
    if not segment or len(parts) < len(segment):
        return False
    limit = len(parts) - len(segment) + 1
    return any(parts[index : index + len(segment)] == segment for index in range(limit))


def iter_agent_dirs(context) -> tuple[Path, ...]:
    root = context.paths.agents_dir
    if not root.exists() or not root.is_dir():
        return ()
    return tuple(path for path in sorted(root.iterdir()) if path.is_dir())


def session_path_from_runtime(runtime_path: Path) -> Path | None:
    try:
        payload = json.loads(runtime_path.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ('session_file', 'session_ref'):
        candidate = payload.get(key)
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        path = Path(candidate).expanduser()
        if path.is_absolute() and path.exists():
            return path
    return None


def archive_path_for_source(context, source: Path) -> str:
    windows_source = PureWindowsPath(str(source))
    if windows_source.drive:
        anchors = {windows_source.anchor, windows_source.drive, windows_source.root, '/', '\\', ''}
        safe_parts = [part for part in windows_source.parts if part not in anchors]
        suffix = Path(*safe_parts[-4:]) if safe_parts else Path(source.name)
        return (Path('external') / suffix).as_posix()
    source_path = _resolve_source(source)
    try:
        relative = source_path.relative_to(_resolve_source(context.project.project_root))
        return (Path('project') / relative).as_posix()
    except Exception:
        pass
    try:
        relative = source_path.relative_to(_resolve_source(context.paths.runtime_state_root))
        return (Path('project') / '.ccb' / relative).as_posix()
    except Exception:
        anchors = {source_path.anchor, source_path.drive, source_path.root, '/', '\\', ''}
        safe_parts = [part for part in source_path.parts if part not in anchors]
        suffix = Path(*safe_parts[-4:]) if safe_parts else Path(source.name)
        return (Path('external') / suffix).as_posix()


def _agent_source_items(
    context,
    *,
    seen_sources: set[Path],
    storage_records: dict[str, dict[str, object]],
) -> list[tuple[str, Path]]:
    items: list[tuple[str, Path]] = []
    for agent_dir in iter_agent_dirs(context):
        agent_name = agent_dir.name
        for category, path in _agent_sources(context, agent_name, agent_dir, storage_records=storage_records):
            resolved = _resolve_source(path)
            if resolved in seen_sources:
                continue
            seen_sources.add(resolved)
            items.append((category, path))
    return items


def _resolve_source(path: Path) -> Path:
    try:
        return path.resolve()
    except Exception:
        return path.absolute()


def _agent_sources(
    context,
    agent_name: str,
    agent_dir: Path,
    *,
    storage_records: dict[str, dict[str, object]],
) -> tuple[tuple[str, Path], ...]:
    items: list[tuple[str, Path]] = [
        ('agent-authority', context.paths.agent_spec_path(agent_name)),
        ('agent-authority', context.paths.agent_runtime_path(agent_name)),
        ('agent-authority', context.paths.agent_restore_path(agent_name)),
        ('agent-events', context.paths.agent_jobs_path(agent_name)),
        ('agent-events', context.paths.agent_events_path(agent_name)),
        ('agent-workspace', context.paths.workspace_binding_path(agent_name)),
    ]
    items.extend(iter_dir_files('agent-log', context.paths.agent_logs_dir(agent_name), suffixes=_TAIL_SUFFIXES))
    items.extend(
        iter_dir_files(
            'agent-runtime',
            agent_dir / 'provider-runtime',
            suffixes=_TAIL_SUFFIXES | _COPY_SUFFIXES,
        )
    )
    items.extend(
        iter_provider_state_files(
            'agent-provider-state',
            agent_dir / 'provider-state',
            storage_records=storage_records,
        )
    )

    runtime_path = context.paths.agent_runtime_path(agent_name)
    if runtime_path.exists():
        session_path = session_path_from_runtime(runtime_path)
        if session_path is not None:
            items.append(('agent-session', session_path))
    return tuple(items)


def _storage_records_by_path(storage_payload: dict[str, object] | None) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    if not isinstance(storage_payload, dict):
        return records
    for raw_entry in storage_payload.get('entries') or ():
        if not isinstance(raw_entry, dict):
            continue
        raw_path = raw_entry.get('path')
        if not raw_path:
            continue
        try:
            path = _resolve_source(Path(str(raw_path)))
        except Exception:
            continue
        records[str(path)] = raw_entry
    return records


def _storage_class_for_path(path: Path, storage_records: dict[str, dict[str, object]] | None) -> str | None:
    if not storage_records:
        return None
    record = storage_records.get(str(_resolve_source(path)))
    if not record:
        return None
    return str(record.get('storage_class') or '').strip().lower() or None


__all__ = ['archive_path_for_source', 'iter_dir_files', 'project_root_sources', 'session_path_from_runtime']
