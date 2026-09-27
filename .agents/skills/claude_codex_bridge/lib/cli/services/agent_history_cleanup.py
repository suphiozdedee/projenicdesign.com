from __future__ import annotations

import json
import re
import stat
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from storage.locks import file_lock
from storage.paths import PathLayout
from storage_classification import summarize_storage

DEFAULT_HISTORY_RETENTION_DAYS = 30
ALLOWED_HISTORY_RETENTION_DAYS = frozenset({7, 30, 90})

_CONTROL_SESSION_RE = re.compile(r'^\.(?P<provider>[^-]+)-(?P<agent>.+)-session$')


def scan_agent_history(
    context_or_layout,
    *,
    retention_days: int = DEFAULT_HISTORY_RETENTION_DAYS,
    agent: str | None = None,
    now: datetime | float | None = None,
) -> dict[str, object]:
    layout = _layout_from_context(context_or_layout)
    state = _collect_history_state(
        layout,
        retention_days=_validated_retention_days(retention_days),
        agent=_normalized_agent(agent),
        now_epoch=_now_epoch(now),
    )
    return _public_scan_payload(state)


def cleanup_agent_history(
    context_or_layout,
    *,
    retention_days: int = DEFAULT_HISTORY_RETENTION_DAYS,
    agent: str | None = None,
    now: datetime | float | None = None,
) -> dict[str, object]:
    layout = _layout_from_context(context_or_layout)
    retention = _validated_retention_days(retention_days)
    selected_agent = _normalized_agent(agent)
    now_epoch = _now_epoch(now)
    deleted: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []

    # Use the lifecycle guard so project startup/recovery cannot replace provider
    # homes while the cleanup snapshot is being validated and applied. Running
    # providers do not take this lock, so current bindings, the retention window,
    # and the newest-transcript fallback remain the deletion safety boundary.
    with file_lock(layout.ccbd_dir / 'startup.lock'):
        state = _collect_history_state(
            layout,
            retention_days=retention,
            agent=selected_agent,
            now_epoch=now_epoch,
        )
        protected = _protected_session_references(layout)
        for candidate in state['candidates']:
            path = Path(str(candidate['path']))
            agent_name = str(candidate['agent'])
            provider = str(candidate['provider'])
            if _matches_protected_reference(path, protected.get(agent_name, {})):
                skipped.append(_skip(candidate, 'current_session_binding'))
                continue
            try:
                metadata = path.lstat()
            except OSError:
                skipped.append(_skip(candidate, 'path_missing'))
                continue
            if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
                skipped.append(_skip(candidate, 'not_regular_file'))
                continue
            if metadata.st_mtime >= float(state['cutoff_epoch']):
                skipped.append(_skip(candidate, 'inside_retention_window'))
                continue
            if not _is_managed_transcript_path(layout, path, agent=agent_name, provider=provider):
                skipped.append(_skip(candidate, 'path_outside_managed_transcript_allowlist'))
                continue
            try:
                path.unlink()
            except OSError as exc:
                skipped.append(_skip(candidate, f'delete_failed:{type(exc).__name__}'))
                continue
            deleted.append(
                {
                    'agent': agent_name,
                    'provider': provider,
                    'path': str(path),
                    'bytes_removed': int(metadata.st_size),
                    'modified_at': _iso_from_epoch(metadata.st_mtime),
                }
            )

        refreshed = _collect_history_state(
            layout,
            retention_days=retention,
            agent=selected_agent,
            now_epoch=now_epoch,
        )

    return {
        'schema_version': 1,
        'status': 'ok',
        'retention_days': retention,
        'agent': selected_agent or 'all',
        'deleted_count': len(deleted),
        'deleted_bytes': sum(int(item['bytes_removed']) for item in deleted),
        'skipped_count': len(skipped),
        'deleted': deleted,
        'skipped': skipped,
        'scan': _public_scan_payload(refreshed),
    }


def _collect_history_state(
    layout: PathLayout,
    *,
    retention_days: int,
    agent: str | None,
    now_epoch: float,
) -> dict[str, object]:
    cutoff_epoch = now_epoch - retention_days * 24 * 60 * 60
    storage = summarize_storage(layout)
    protected = _protected_session_references(layout)
    known_agents = _known_agent_names(layout, storage)
    if agent is not None and agent not in known_agents:
        raise ValueError(f'unknown agent: {agent}')

    transcripts: list[dict[str, object]] = []
    for raw in storage.get('entries', []):
        if not isinstance(raw, dict):
            continue
        agent_name = str(raw.get('agent') or '').strip().lower()
        provider = str(raw.get('provider') or '').strip().lower()
        path = Path(str(raw.get('path') or ''))
        if not agent_name or not provider:
            continue
        if not _is_managed_transcript_path(layout, path, agent=agent_name, provider=provider):
            continue
        metadata = _regular_file_stat(path)
        if metadata is None:
            continue
        transcripts.append(
            {
                'agent': agent_name,
                'provider': provider,
                'path': str(path),
                'size_bytes': int(metadata.st_size),
                'mtime': float(metadata.st_mtime),
                'modified_at': _iso_from_epoch(metadata.st_mtime),
            }
        )

    newest_paths: set[str] = set()
    by_binding: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for item in transcripts:
        by_binding[(str(item['agent']), str(item['provider']))].append(item)
    for items in by_binding.values():
        newest = max(items, key=lambda item: (float(item['mtime']), str(item['path'])))
        newest_paths.add(str(newest['path']))

    candidates: list[dict[str, object]] = []
    row_items: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in transcripts:
        path = Path(str(item['path']))
        agent_name = str(item['agent'])
        reason = None
        if _matches_protected_reference(path, protected.get(agent_name, {})):
            reason = 'current'
        elif float(item['mtime']) >= cutoff_epoch:
            reason = 'recent'
        elif str(item['path']) in newest_paths:
            reason = 'latest'
        else:
            reason = 'candidate'
            if agent is None or agent_name == agent:
                candidates.append(item)
        row_items[agent_name].append({**item, 'retention_state': reason})

    by_agent_storage = storage.get('by_agent') if isinstance(storage.get('by_agent'), dict) else {}
    rows = []
    for agent_name in sorted(known_agents):
        items = row_items.get(agent_name, [])
        candidate_items = [item for item in items if item['retention_state'] == 'candidate']
        recent_items = [item for item in items if item['retention_state'] == 'recent']
        current_items = [item for item in items if item['retention_state'] in {'current', 'latest'}]
        storage_row = by_agent_storage.get(agent_name) if isinstance(by_agent_storage, dict) else None
        rows.append(
            {
                'agent': agent_name,
                'providers': sorted({str(item['provider']) for item in items}),
                'status': _agent_mount_status(layout, agent_name),
                'total_storage_bytes': int(storage_row.get('bytes') or 0) if isinstance(storage_row, dict) else 0,
                'history_bytes': sum(int(item['size_bytes']) for item in items),
                'history_count': len(items),
                'candidate_bytes': sum(int(item['size_bytes']) for item in candidate_items),
                'candidate_count': len(candidate_items),
                'recent_bytes': sum(int(item['size_bytes']) for item in recent_items),
                'recent_count': len(recent_items),
                'protected_bytes': sum(int(item['size_bytes']) for item in current_items),
                'protected_count': len(current_items),
                'oldest_at': _iso_from_epoch(min(float(item['mtime']) for item in items)) if items else None,
                'selected': agent is None or agent_name == agent,
            }
        )

    selected_rows = [row for row in rows if bool(row['selected'])]
    return {
        'layout': layout,
        'retention_days': retention_days,
        'agent': agent,
        'generated_at': _iso_from_epoch(now_epoch),
        'cutoff_at': _iso_from_epoch(cutoff_epoch),
        'cutoff_epoch': cutoff_epoch,
        'total_storage_bytes': int(storage.get('total_bytes') or 0),
        'history_bytes': sum(int(row['history_bytes']) for row in selected_rows),
        'history_count': sum(int(row['history_count']) for row in selected_rows),
        'candidate_bytes': sum(int(item['size_bytes']) for item in candidates),
        'candidate_count': len(candidates),
        'protected_agent_count': sum(1 for row in selected_rows if int(row['protected_count']) > 0),
        'rows': rows,
        'candidates': candidates,
    }


def _public_scan_payload(state: dict[str, object]) -> dict[str, object]:
    return {
        'schema_version': 1,
        'status': 'ok',
        'generated_at': state['generated_at'],
        'retention_days': state['retention_days'],
        'cutoff_at': state['cutoff_at'],
        'agent': state['agent'] or 'all',
        'total_storage_bytes': state['total_storage_bytes'],
        'history_bytes': state['history_bytes'],
        'history_count': state['history_count'],
        'candidate_bytes': state['candidate_bytes'],
        'candidate_count': state['candidate_count'],
        'protected_agent_count': state['protected_agent_count'],
        'agents': state['rows'],
    }


def _known_agent_names(layout: PathLayout, storage: dict[str, object]) -> set[str]:
    names: set[str] = set()
    if layout.agents_dir.is_dir():
        for path in layout.agents_dir.iterdir():
            if path.is_dir() and not path.is_symlink():
                names.add(path.name.strip().lower())
    by_agent = storage.get('by_agent')
    if isinstance(by_agent, dict):
        names.update(str(name).strip().lower() for name in by_agent if str(name).strip())
    return names


def _protected_session_references(layout: PathLayout) -> dict[str, dict[str, set[object]]]:
    protected: dict[str, dict[str, set[object]]] = defaultdict(lambda: {'paths': set(), 'ids': set()})
    roots = {layout.ccb_dir, layout.runtime_state_root}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.glob('.*-session'):
            payload = _read_json_mapping(path)
            if not payload:
                continue
            match = _CONTROL_SESSION_RE.match(path.name)
            agent = str(payload.get('agent_name') or (match.group('agent') if match else '')).strip().lower()
            if not agent:
                continue
            _collect_payload_references(payload, protected[agent])

    if layout.agents_dir.is_dir():
        for runtime_path in layout.agents_dir.glob('*/runtime.json'):
            agent = runtime_path.parent.name.strip().lower()
            payload = _read_json_mapping(runtime_path)
            if not payload:
                continue
            for key in ('session_id', 'session_ref'):
                value = str(payload.get(key) or '').strip()
                if len(value) >= 6:
                    protected[agent]['ids'].add(value)
            session_file = Path(str(payload.get('session_file') or '')).expanduser()
            if session_file.is_file():
                _collect_payload_references(_read_json_mapping(session_file), protected[agent])
    return protected


def _collect_payload_references(payload: dict[str, object], target: dict[str, set[object]]) -> None:
    for raw_key, raw_value in payload.items():
        key = str(raw_key).strip().lower()
        if key.startswith('old_'):
            continue
        value = str(raw_value or '').strip()
        if not value:
            continue
        if key.endswith(('_session_path', '_resume_session_path')):
            target['paths'].add(_lexical_path(value))
        elif key.endswith('_session_id') and key != 'ccb_session_id' and len(value) >= 6:
            target['ids'].add(value)


def _matches_protected_reference(path: Path, references: dict[str, set[object]]) -> bool:
    candidate = _lexical_path(path)
    for raw in references.get('paths', set()):
        protected = Path(raw)
        if candidate == protected:
            return True
        try:
            candidate.relative_to(protected)
            return True
        except ValueError:
            continue
    rendered = str(candidate)
    return any(str(session_id) in rendered for session_id in references.get('ids', set()))


def _is_managed_transcript_path(layout: PathLayout, path: Path, *, agent: str, provider: str) -> bool:
    home = layout.agent_provider_state_dir(agent, provider) / 'home'
    candidate = _lexical_path(path)
    home_path = _lexical_path(home)
    try:
        relative = candidate.relative_to(home_path)
    except ValueError:
        return False
    parts = relative.parts
    if not parts:
        return False
    name = parts[-1]
    suffix = candidate.suffix.lower()

    if provider == 'codex':
        return parts[0] == 'sessions' and suffix == '.jsonl' and name.startswith('rollout-')
    if provider == 'claude':
        return len(parts) >= 3 and parts[:2] == ('.claude', 'projects') and suffix == '.jsonl'
    if provider == 'gemini':
        return (
            len(parts) >= 5
            and parts[:2] == ('.gemini', 'tmp')
            and parts[-2] == 'chats'
            and name.startswith('session-')
            and suffix == '.json'
        )
    if provider == 'droid':
        under_sessions = parts[0] == 'sessions' or (len(parts) >= 2 and parts[:2] == ('.factory', 'sessions'))
        return under_sessions and suffix == '.jsonl'
    if provider == 'kimi':
        return 'sessions' in parts and name == 'wire.jsonl'
    if provider == 'grok':
        return len(parts) >= 3 and parts[:2] == ('.grok', 'sessions') and name == 'updates.jsonl'
    if provider == 'deepseek':
        return '.deepcode' in parts and 'projects' in parts and suffix == '.jsonl'
    if provider == 'agy':
        return '.system_generated' in parts and 'logs' in parts and name.startswith('transcript') and suffix == '.jsonl'
    return False


def _agent_mount_status(layout: PathLayout, agent: str) -> str:
    payload = _read_json_mapping(layout.agents_dir / agent / 'runtime.json')
    if not payload:
        return 'unmounted'
    desired = str(payload.get('desired_state') or '').strip().lower()
    pane_state = str(payload.get('pane_state') or '').strip().lower()
    state = str(payload.get('state') or '').strip().lower()
    if desired == 'mounted' and (pane_state == 'alive' or state in {'idle', 'busy', 'starting'}):
        return 'mounted'
    return 'unmounted'


def _regular_file_stat(path: Path):
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        return None
    return metadata


def _read_json_mapping(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _layout_from_context(context_or_layout) -> PathLayout:
    if isinstance(context_or_layout, PathLayout):
        return context_or_layout
    layout = getattr(context_or_layout, 'paths', None)
    if isinstance(layout, PathLayout):
        return layout
    project = getattr(context_or_layout, 'project', None)
    project_root = getattr(project, 'project_root', None)
    if project_root is None:
        raise ValueError('project root is required for agent history cleanup')
    return PathLayout(Path(project_root))


def _validated_retention_days(value: object) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('retention_days must be one of: 7, 30, 90') from exc
    if days not in ALLOWED_HISTORY_RETENTION_DAYS:
        raise ValueError('retention_days must be one of: 7, 30, 90')
    return days


def _normalized_agent(value: object) -> str | None:
    text = str(value or '').strip().lower()
    if not text or text == 'all':
        return None
    return text


def _now_epoch(value: datetime | float | None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return current.timestamp()
    return float(value)


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def _lexical_path(value: Path | str) -> Path:
    return Path(value).expanduser().absolute()


def _skip(candidate: dict[str, object], reason: str) -> dict[str, object]:
    return {
        'agent': str(candidate.get('agent') or ''),
        'provider': str(candidate.get('provider') or ''),
        'path': str(candidate.get('path') or ''),
        'reason': reason,
    }


__all__ = [
    'ALLOWED_HISTORY_RETENTION_DAYS',
    'DEFAULT_HISTORY_RETENTION_DAYS',
    'cleanup_agent_history',
    'scan_agent_history',
]
