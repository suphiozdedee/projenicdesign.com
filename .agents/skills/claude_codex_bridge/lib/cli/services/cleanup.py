from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import time

from cli.services.daemon import inspect_daemon
from provider_backends.claude.launcher_runtime.legacy_binary_cache import detach_legacy_claude_binary_cache
from provider_execution.state_store import ExecutionStateStore
from storage.locks import file_lock
from .legacy_provider_cache import cleanup_orphaned_legacy_provider_caches


_PENDING_JOB_STATUSES = {'accepted', 'queued', 'running'}
_SAFE_GEMINI_CACHE_RELS = (
    Path('.npm') / '_cacache',
    Path('.cache') / 'node-gyp',
    Path('.cache') / 'vscode-ripgrep',
)
_SAFE_CLAUDE_CACHE_RELS = (
    Path('.cache') / 'claude',
    Path('.npm') / '_logs',
    Path('.claude') / 'cache',
    Path('.claude') / 'telemetry',
    Path('.claude') / 'paste-cache',
    Path('.claude') / 'plugins' / 'marketplaces',
)
_PANE_CRASH_LOG_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_PANE_CRASH_LOG_MAX_KEEP_PER_RUNTIME = 50


@dataclass(frozen=True)
class CleanupAction:
    provider: str
    kind: str
    path: str
    bytes_removed: int
    reason: str


@dataclass(frozen=True)
class CleanupSkipped:
    provider: str
    path: str
    reason: str


@dataclass(frozen=True)
class CleanupSummary:
    project_root: str
    project_id: str
    status: str
    deleted_bytes: int
    deleted_count: int
    skipped_count: int
    actions: tuple[CleanupAction, ...] = ()
    skipped: tuple[CleanupSkipped, ...] = ()


def cleanup_project_storage(context, command) -> CleanupSummary:
    with file_lock(context.paths.ccbd_dir / 'startup.lock'):
        _require_stopped_backend(context)
        _require_no_pending_jobs(context)
        actions: list[CleanupAction] = []
        skipped: list[CleanupSkipped] = []
        _detach_legacy_claude_cache_links(context.paths, actions=actions)
        _cleanup_claude_version_caches(context.paths, actions=actions, skipped=skipped)
        _cleanup_claude_rebuildable_caches(context.paths, actions=actions, skipped=skipped)
        _cleanup_gemini_rebuildable_caches(context.paths, actions=actions, skipped=skipped)
        _cleanup_legacy_project_provider_caches(context.paths, actions=actions, skipped=skipped)
        if bool(getattr(command, 'legacy_provider_caches', False)):
            _cleanup_orphaned_legacy_provider_caches(context.paths, actions=actions, skipped=skipped)
        _cleanup_pane_crash_logs(context.paths, actions=actions, skipped=skipped)
        return _cleanup_summary(
            context,
            actions=actions,
            skipped=skipped,
        )


def cleanup_current_project_legacy_provider_caches(
    context,
    *,
    measure_bytes: bool = False,
) -> CleanupSummary:
    """Clean only the stopped current project's retired Provider cache."""

    with file_lock(context.paths.ccbd_dir / 'startup.lock'):
        _require_stopped_backend(context)
        _require_no_pending_jobs(context)
        actions: list[CleanupAction] = []
        skipped: list[CleanupSkipped] = []
        _detach_legacy_claude_cache_links(context.paths, actions=actions)
        _cleanup_legacy_project_provider_caches(
            context.paths,
            actions=actions,
            skipped=skipped,
            measure_bytes=measure_bytes,
        )
        return _cleanup_summary(
            context,
            actions=actions,
            skipped=skipped,
        )


def current_project_legacy_provider_cache_present(layout) -> bool:
    for root in (layout.external_provider_cache_root, layout.shared_cache_dir):
        for provider in ('claude', 'gemini'):
            candidate = root / provider
            if candidate.exists() or candidate.is_symlink():
                return True
    return False


def _cleanup_summary(
    context,
    *,
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
) -> CleanupSummary:
    return CleanupSummary(
        project_root=str(context.project.project_root),
        project_id=context.project.project_id,
        status='ok',
        deleted_bytes=sum(item.bytes_removed for item in actions),
        deleted_count=len(actions),
        skipped_count=len(skipped),
        actions=tuple(actions),
        skipped=tuple(skipped),
    )


def _require_stopped_backend(context) -> None:
    _manager, _guard, inspection = inspect_daemon(context)
    phase = str(getattr(inspection, 'phase', '') or '').strip()
    desired_state = str(getattr(inspection, 'desired_state', '') or '').strip()
    if getattr(inspection, 'pid_alive', False) or getattr(inspection, 'socket_connectable', False):
        raise RuntimeError('ccb cleanup requires stopped ccbd; run `ccb kill` first')
    if phase not in {'', 'unmounted', 'failed'}:
        raise RuntimeError(f'ccb cleanup requires stopped ccbd; current phase={phase}')
    if desired_state and desired_state != 'stopped':
        raise RuntimeError(f'ccb cleanup requires stopped ccbd; desired_state={desired_state}')


def _require_no_pending_jobs(context) -> None:
    execution_summary = ExecutionStateStore(context.paths).summary()
    active_execution_count = int(execution_summary.get('active_execution_count') or 0)
    pending_items_count = int(execution_summary.get('pending_items_count') or 0)
    terminal_pending_count = int(execution_summary.get('terminal_pending_count') or 0)
    pending_job_count = _pending_job_count(context.paths)
    if active_execution_count or pending_items_count or terminal_pending_count or pending_job_count:
        raise RuntimeError(
            'ccb cleanup refused: pending ask jobs exist; wait for completion or run `ccb kill` after terminalization'
        )


def _pending_job_count(layout) -> int:
    roots = [layout.agents_dir, layout.ccbd_dir / 'targets']
    count = 0
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob('jobs.jsonl')):
            count += _pending_job_count_in_file(path)
    return count


def _pending_job_count_in_file(path: Path) -> int:
    latest_by_job: dict[str, str] = {}
    unreadable_or_malformed_count = 0
    try:
        handle = path.open('r', encoding='utf-8')
    except OSError:
        return 1
    with handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                unreadable_or_malformed_count += 1
                continue
            if not isinstance(record, dict):
                unreadable_or_malformed_count += 1
                continue
            job_id = str(record.get('job_id') or '').strip()
            if not job_id:
                continue
            latest_by_job[job_id] = str(record.get('status') or '').strip().lower()
    return (
        sum(1 for status in latest_by_job.values() if status in _PENDING_JOB_STATUSES)
        + unreadable_or_malformed_count
    )


def _cleanup_claude_version_caches(layout, *, actions: list[CleanupAction], skipped: list[CleanupSkipped]) -> None:
    agents_dir = layout.agents_dir
    if agents_dir.exists():
        for home in sorted(agents_dir.glob('*/provider-state/claude/home')):
            active_name = _current_claude_version_name(home)
            versions_dir = home / '.local' / 'share' / 'claude' / 'versions'
            _cleanup_one_claude_versions_dir(
                versions_dir,
                active_version_names={active_name} if active_name else set(),
                actions=actions,
                skipped=skipped,
            )


def _cleanup_one_claude_versions_dir(
    versions_dir: Path,
    *,
    active_version_names: set[str],
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
) -> None:
    if versions_dir.is_symlink():
        skipped.append(
            CleanupSkipped(
                provider='claude',
                path=str(versions_dir),
                reason='versions_dir_is_symlink',
            )
        )
        return
    if not versions_dir.is_dir():
        return
    version_paths = _claude_version_paths(versions_dir)
    if not version_paths:
        return
    if not active_version_names:
        skipped.append(
            CleanupSkipped(
                provider='claude',
                path=str(versions_dir),
                reason='current_version_symlink_unresolved',
            )
        )
        return
    _prune_claude_versions(
        versions_dir,
        version_paths,
        active_version_names=active_version_names,
        provider='claude',
        reason='old_claude_version_cache',
        actions=actions,
        skipped=skipped,
    )


def _prune_claude_versions(
    versions_dir: Path,
    version_paths: list[Path],
    *,
    active_version_names: set[str],
    provider: str,
    reason: str,
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
) -> None:
    keep = _claude_version_keep_paths(version_paths, active_version_names=active_version_names)
    for path in version_paths:
        if path in keep:
            continue
        _remove_tree(
            path,
            root=versions_dir,
            provider=provider,
            kind='version_cache',
            reason=reason,
            actions=actions,
            skipped=skipped,
        )


def _claude_version_keep_paths(version_paths: list[Path], *, active_version_names: set[str]) -> set[Path]:
    keep = {path for path in version_paths if path.name in active_version_names}
    if not active_version_names:
        return keep
    rollback = _newest_version_path(path for path in version_paths if path not in keep)
    if rollback is not None:
        keep.add(rollback)
    return keep


def _current_claude_version_name(home: Path) -> str | None:
    link = home / '.local' / 'bin' / 'claude'
    try:
        target = link.resolve(strict=True)
    except Exception:
        return None
    versions_dir = home / '.local' / 'share' / 'claude' / 'versions'
    if not _is_within(target, versions_dir):
        return None
    try:
        relative = target.relative_to(versions_dir.resolve(strict=False))
    except Exception:
        return None
    if not relative.parts:
        return None
    return relative.parts[0]


def _claude_version_paths(versions_dir: Path) -> list[Path]:
    try:
        entries = sorted(versions_dir.iterdir(), key=lambda path: (_version_key(path.name), _safe_mtime(path), path.name))
    except OSError:
        return []
    return [
        path
        for path in entries
        if _looks_like_claude_version_name(path.name)
        and (path.is_file() or path.is_dir())
        and not path.is_symlink()
        and _is_within(path, versions_dir)
    ]


def _looks_like_claude_version_name(value: str) -> bool:
    if not value or not value[0].isdigit():
        return False
    return all(item.isalnum() or item in {'.', '_', '-'} for item in value)


def _newest_version_path(paths) -> Path | None:
    candidates = list(paths)
    if not candidates:
        return None
    return max(candidates, key=lambda path: (_version_key(path.name), _safe_mtime(path), path.name))


def _version_key(value: str) -> tuple[tuple[int, object], ...]:
    parts: list[tuple[int, object]] = []
    for item in value.replace('-', '.').split('.'):
        if item.isdigit():
            parts.append((1, int(item)))
        else:
            parts.append((0, item))
    return tuple(parts)


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _cleanup_claude_rebuildable_caches(layout, *, actions: list[CleanupAction], skipped: list[CleanupSkipped]) -> None:
    agents_dir = layout.agents_dir
    if not agents_dir.exists():
        return
    for home in sorted(agents_dir.glob('*/provider-state/claude/home')):
        if not home.is_dir() or home.is_symlink():
            continue
        for relative in _SAFE_CLAUDE_CACHE_RELS:
            path = home / relative
            if not path.exists():
                continue
            _remove_tree(
                path,
                root=home,
                provider='claude',
                kind='tool_cache',
                reason='rebuildable_claude_cache',
                actions=actions,
                skipped=skipped,
            )


def _cleanup_gemini_rebuildable_caches(layout, *, actions: list[CleanupAction], skipped: list[CleanupSkipped]) -> None:
    agents_dir = layout.agents_dir
    if agents_dir.exists():
        for home in sorted(agents_dir.glob('*/provider-state/gemini/home')):
            if not home.is_dir() or home.is_symlink():
                continue
            for relative in _SAFE_GEMINI_CACHE_RELS:
                path = home / relative
                if not path.exists():
                    continue
                _remove_tree(
                    path,
                    root=home,
                    provider='gemini',
                    kind='tool_cache',
                    reason='rebuildable_gemini_cache',
                    actions=actions,
                    skipped=skipped,
                )


def _detach_legacy_claude_cache_links(
    layout,
    *,
    actions: list[CleanupAction],
) -> None:
    agents_dir = layout.agents_dir
    if not agents_dir.exists():
        return
    cache_roots = (
        layout.provider_external_cache_dir('claude'),
        layout.shared_cache_dir / 'claude',
    )
    for home in sorted(agents_dir.glob('*/provider-state/claude/home')):
        result = detach_legacy_claude_binary_cache(home, cache_roots=cache_roots)
        if result.get('status') != 'ok':
            continue
        actions.append(
            CleanupAction(
                provider='claude',
                kind='legacy_cache_link',
                path=str(result.get('versions_dir') or ''),
                bytes_removed=0,
                reason='legacy_claude_binary_cache_detached',
            )
        )


def _cleanup_legacy_project_provider_caches(
    layout,
    *,
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
    measure_bytes: bool = True,
) -> None:
    roots = (
        layout.external_provider_cache_root,
        layout.shared_cache_dir,
    )
    for root in roots:
        for provider in ('claude', 'gemini'):
            cache_dir = root / provider
            if not cache_dir.exists() and not cache_dir.is_symlink():
                continue
            if provider == 'claude' and _managed_claude_cache_references(layout, cache_dir):
                skipped.append(
                    CleanupSkipped(
                        provider='claude',
                        path=str(cache_dir),
                        reason='legacy_cache_still_referenced',
                    )
                )
                continue
            _remove_tree(
                cache_dir,
                root=root,
                provider=provider,
                kind='legacy_project_cache',
                reason='legacy_project_provider_cache',
                actions=actions,
                skipped=skipped,
                measure_bytes=measure_bytes,
            )
        _remove_empty_dirs(root, stop_at=root.parent)
    _remove_empty_dirs(
        layout.external_provider_cache_root.parent,
        stop_at=layout.external_provider_cache_root.parent.parent,
    )


def _cleanup_orphaned_legacy_provider_caches(
    layout,
    *,
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
) -> None:
    projects_root = layout.external_provider_cache_root.parent.parent
    sweep = cleanup_orphaned_legacy_provider_caches(
        projects_root,
        measure_bytes=True,
    )
    actions.extend(
        CleanupAction(
            provider=item.provider,
            kind='legacy_project_cache',
            path=item.path,
            bytes_removed=item.bytes_removed,
            reason=item.reason,
        )
        for item in sweep.removals
    )
    skipped.extend(
        CleanupSkipped(
            provider=item.provider,
            path=item.path,
            reason=item.reason,
        )
        for item in sweep.preserved
    )


def _managed_claude_cache_references(layout, cache_root: Path) -> bool:
    versions_root = cache_root / 'versions'
    agents_dir = layout.agents_dir
    if not agents_dir.exists():
        return False
    for home in sorted(agents_dir.glob('*/provider-state/claude/home')):
        versions_dir = home / '.local' / 'share' / 'claude' / 'versions'
        if not versions_dir.is_symlink():
            continue
        try:
            if versions_dir.resolve(strict=False) == versions_root.resolve(strict=False):
                return True
        except Exception:
            continue
    return False


def _remove_empty_dirs(path: Path, *, stop_at: Path) -> None:
    current = path
    stop = stop_at.resolve(strict=False)
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _cleanup_pane_crash_logs(layout, *, actions: list[CleanupAction], skipped: list[CleanupSkipped]) -> None:
    agents_dir = layout.agents_dir
    if not agents_dir.exists():
        return
    now = time.time()
    for runtime_dir in sorted(agents_dir.glob('*/provider-runtime/*')):
        if not runtime_dir.is_dir() or runtime_dir.is_symlink():
            continue
        logs = sorted(
            (path for path in runtime_dir.glob('pane-crash-*.log') if path.is_file() and not path.is_symlink()),
            key=lambda path: (_safe_mtime(path), path.name),
            reverse=True,
        )
        for index, path in enumerate(logs):
            age = now - _safe_mtime(path)
            if index < _PANE_CRASH_LOG_MAX_KEEP_PER_RUNTIME and age < _PANE_CRASH_LOG_MAX_AGE_SECONDS:
                continue
            _remove_tree(
                path,
                root=runtime_dir,
                provider=runtime_dir.name,
                kind='crash_log',
                reason='old_pane_crash_log',
                actions=actions,
                skipped=skipped,
            )
            reason_path = path.with_suffix('.reason.json')
            if reason_path.is_file() and not reason_path.is_symlink():
                _remove_tree(
                    reason_path,
                    root=runtime_dir,
                    provider=runtime_dir.name,
                    kind='crash_reason',
                    reason='old_pane_crash_reason',
                    actions=actions,
                    skipped=skipped,
                )


def _remove_tree(
    path: Path,
    *,
    root: Path,
    provider: str,
    kind: str,
    reason: str,
    actions: list[CleanupAction],
    skipped: list[CleanupSkipped],
    measure_bytes: bool = True,
) -> None:
    if path.is_symlink():
        skipped.append(CleanupSkipped(provider=provider, path=str(path), reason='symlink_not_removed'))
        return
    if not _is_within(path, root):
        skipped.append(CleanupSkipped(provider=provider, path=str(path), reason='path_out_of_bounds'))
        return
    size = _tree_size(path) if measure_bytes else 0
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        skipped.append(CleanupSkipped(provider=provider, path=str(path), reason='remove_failed'))
        return
    actions.append(
        CleanupAction(
            provider=provider,
            kind=kind,
            path=str(path),
            bytes_removed=size,
            reason=reason,
        )
    )


def _tree_size(path: Path) -> int:
    total = 0
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_file() or path.is_symlink():
        return _lstat_size(path)
    for child in path.rglob('*'):
        total += _lstat_size(child)
    return total


def _lstat_size(path: Path) -> int:
    try:
        return int(path.lstat().st_size)
    except OSError:
        return 0


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except Exception:
        return False


__all__ = [
    'CleanupAction',
    'CleanupSkipped',
    'CleanupSummary',
    'cleanup_current_project_legacy_provider_caches',
    'cleanup_project_storage',
    'current_project_legacy_provider_cache_present',
]
