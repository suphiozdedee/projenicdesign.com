from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time
from typing import Mapping, TextIO

from cli.context import CliContextBuilder
from cli.models import ParsedCleanupCommand
from cli.services.cleanup import (
    cleanup_current_project_legacy_provider_caches,
    current_project_legacy_provider_cache_present,
)
from cli.services.legacy_provider_cache import cleanup_orphaned_legacy_provider_caches
from project.discovery import find_nearest_project_anchor
from runtime_env.source_home import current_provider_source_home
from storage.atomic import atomic_write_json
from storage.paths import PathLayout, legacy_provider_projects_root
from ui_text.i18n import detect_language


STATE_SCHEMA_VERSION = 1
MIGRATION_ID = 'retire-project-provider-cache-v1'
STATE_FILE_NAME = 'provider-cache-cleanup.json'
LOCK_FILE_NAME = 'provider-cache-cleanup.lock'
LOCK_STALE_SECONDS = 60 * 60
MAX_STATE_ITEMS = 200


@dataclass(frozen=True)
class PostUpdateCacheCleanupSummary:
    status: str
    removed_count: int = 0
    deferred_project_roots: tuple[str, ...] = ()
    preserved_count: int = 0
    errors: tuple[str, ...] = ()
    state_path: str = ''


def provider_cache_cleanup_state_path(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    source_env = os.environ if env is None else env
    raw_state_home = str(source_env.get('XDG_STATE_HOME') or '').strip()
    state_home = Path(raw_state_home).expanduser() if raw_state_home else None
    if state_home is None or not state_home.is_absolute():
        source_home = Path(home).expanduser() if home is not None else current_provider_source_home()
        state_home = source_home / '.local' / 'state'
    return state_home / 'ccb' / STATE_FILE_NAME


def run_post_update_provider_cache_cleanup(
    *,
    from_version: str = 'unknown',
    to_version: str = 'unknown',
    cwd: Path | None = None,
    stdout: TextIO | None = None,
    projects_root: Path | None = None,
    state_path: Path | None = None,
) -> PostUpdateCacheCleanupSummary:
    output = stdout or sys.stdout
    target_state = Path(state_path or provider_cache_cleanup_state_path()).expanduser()
    lock_path = target_state.with_name(LOCK_FILE_NAME)
    if not _acquire_lock(lock_path):
        _print_locked(output)
        return PostUpdateCacheCleanupSummary(
            status='locked',
            state_path=str(target_state),
        )

    try:
        return _run_cleanup_locked(
            from_version=from_version,
            to_version=to_version,
            cwd=Path(cwd or Path.cwd()).expanduser(),
            stdout=output,
            projects_root=Path(projects_root or legacy_provider_projects_root()).expanduser(),
            state_path=target_state,
        )
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        _print_failure(output, error)
        summary = PostUpdateCacheCleanupSummary(
            status='error',
            errors=(error,),
            state_path=str(target_state),
        )
        _write_state_best_effort(
            target_state,
            _state_payload(
                summary,
                from_version=from_version,
                to_version=to_version,
                preserved=(),
            ),
        )
        return summary
    finally:
        _release_lock(lock_path)


def _run_cleanup_locked(
    *,
    from_version: str,
    to_version: str,
    cwd: Path,
    stdout: TextIO,
    projects_root: Path,
    state_path: Path,
) -> PostUpdateCacheCleanupSummary:
    removed_count = 0
    deferred_roots: set[str] = set()
    preserved: list[dict[str, str]] = []
    errors: list[str] = []

    current_anchor = _nearest_project_anchor(cwd)
    if current_anchor is not None:
        layout = PathLayout(current_anchor)
        if current_project_legacy_provider_cache_present(layout):
            try:
                command = ParsedCleanupCommand(project=str(current_anchor))
                context = CliContextBuilder().build(command, cwd=current_anchor)
                current_summary = cleanup_current_project_legacy_provider_caches(
                    context,
                    measure_bytes=False,
                )
                removed_count += sum(
                    1
                    for item in current_summary.actions
                    if item.kind == 'legacy_project_cache'
                )
                if current_summary.skipped:
                    deferred_roots.add(str(current_anchor))
                preserved.extend(
                    {
                        'provider': item.provider,
                        'path': item.path,
                        'reason': item.reason,
                        'project_root': str(current_anchor),
                    }
                    for item in current_summary.skipped
                )
            except RuntimeError as exc:
                deferred_roots.add(str(current_anchor))
                preserved.append(
                    {
                        'provider': '',
                        'path': str(layout.external_provider_cache_root),
                        'reason': f'current_project_deferred: {exc}',
                        'project_root': str(current_anchor),
                    }
                )
            except Exception as exc:
                deferred_roots.add(str(current_anchor))
                errors.append(f'current project {current_anchor}: {type(exc).__name__}: {exc}')

    sweep = cleanup_orphaned_legacy_provider_caches(
        projects_root,
        measure_bytes=False,
    )
    removed_count += sweep.removed_count
    for item in sweep.preserved:
        if item.reason == 'project_root_exists' and item.project_root:
            deferred_roots.add(item.project_root)
        else:
            preserved.append(
                {
                    'provider': item.provider,
                    'path': item.path,
                    'reason': item.reason,
                    'project_root': item.project_root,
                }
            )

    status = 'complete'
    if deferred_roots or preserved or errors:
        status = 'partial'
    summary = PostUpdateCacheCleanupSummary(
        status=status,
        removed_count=removed_count,
        deferred_project_roots=tuple(sorted(deferred_roots)),
        preserved_count=len(preserved),
        errors=tuple(errors),
        state_path=str(state_path),
    )
    state_error = _write_state_best_effort(
        state_path,
        _state_payload(
            summary,
            from_version=from_version,
            to_version=to_version,
            preserved=tuple(preserved),
        ),
    )
    if state_error:
        errors.append(state_error)
        summary = PostUpdateCacheCleanupSummary(
            status='partial',
            removed_count=removed_count,
            deferred_project_roots=tuple(sorted(deferred_roots)),
            preserved_count=len(preserved),
            errors=tuple(errors),
            state_path=str(state_path),
        )
    _print_summary(stdout, summary)
    return summary


def _state_payload(
    summary: PostUpdateCacheCleanupSummary,
    *,
    from_version: str,
    to_version: str,
    preserved: tuple[dict[str, str], ...],
) -> dict[str, object]:
    deferred = list(summary.deferred_project_roots)
    kept = list(preserved)
    return {
        'schema_version': STATE_SCHEMA_VERSION,
        'migration_id': MIGRATION_ID,
        'updated_at': _utc_now(),
        'from_version': str(from_version or 'unknown'),
        'to_version': str(to_version or 'unknown'),
        'status': summary.status,
        'removed_cache_count': summary.removed_count,
        'deferred_project_roots': deferred[:MAX_STATE_ITEMS],
        'deferred_project_roots_truncated': max(0, len(deferred) - MAX_STATE_ITEMS),
        'preserved': kept[:MAX_STATE_ITEMS],
        'preserved_truncated': max(0, len(kept) - MAX_STATE_ITEMS),
        'errors': list(summary.errors)[:MAX_STATE_ITEMS],
    }


def _write_state_best_effort(path: Path, payload: dict[str, object]) -> str | None:
    try:
        atomic_write_json(path, payload)
    except Exception as exc:
        return f'state write failed: {type(exc).__name__}: {exc}'
    return None


def _nearest_project_anchor(cwd: Path) -> Path | None:
    try:
        return find_nearest_project_anchor(cwd)
    except Exception:
        return None


def _acquire_lock(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if attempt == 0 and _lock_is_stale(path):
                try:
                    path.unlink()
                except OSError:
                    return False
                continue
            return False
        try:
            os.write(descriptor, f'{os.getpid()} {_utc_now()}\n'.encode('utf-8'))
        finally:
            os.close(descriptor)
        return True
    return False


def _lock_is_stale(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    try:
        raw_pid = path.read_text(encoding='utf-8', errors='replace').split(maxsplit=1)[0]
        pid = int(raw_pid)
    except (OSError, ValueError, IndexError):
        pid = 0
    if pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except (OverflowError, OSError):
            pass
        else:
            return False
    return stat.st_mtime + LOCK_STALE_SECONDS < time.time()


def _release_lock(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _print_summary(stdout: TextIO, summary: PostUpdateCacheCleanupSummary) -> None:
    lang = detect_language()
    if summary.removed_count:
        if lang == 'zh':
            print(f'✅ 更新后缓存迁移：已安全清理 {summary.removed_count} 个旧项目 Provider 缓存。', file=stdout)
        else:
            print(
                f'✅ Post-update cache migration safely removed {summary.removed_count} '
                'legacy project Provider cache(s).',
                file=stdout,
            )
    elif not summary.deferred_project_roots and not summary.preserved_count and not summary.errors:
        message = '✅ 更新后缓存迁移：没有需要清理的旧项目缓存。' if lang == 'zh' else (
            '✅ Post-update cache migration: no legacy project cache needs cleanup.'
        )
        print(message, file=stdout)
    if summary.deferred_project_roots:
        if lang == 'zh':
            print(
                f'ℹ️  已保留 {len(summary.deferred_project_roots)} 个仍有关联项目的缓存；'
                '将在对应项目下次成功执行 `ccb kill` 后清理。',
                file=stdout,
            )
        else:
            print(
                f'ℹ️  Preserved cache for {len(summary.deferred_project_roots)} existing project(s); '
                'cleanup will retry after the next successful `ccb kill` in each project.',
                file=stdout,
            )
    if summary.preserved_count or summary.errors:
        if lang == 'zh':
            print(
                f'⚠️  {summary.preserved_count} 个无法安全确认的缓存已保留'
                f'（错误 {len(summary.errors)} 个）。',
                file=stdout,
            )
        else:
            print(
                f'⚠️  Preserved {summary.preserved_count} cache(s) that could not be safely verified '
                f'({len(summary.errors)} error(s)).',
                file=stdout,
            )


def _print_locked(stdout: TextIO) -> None:
    message = (
        'ℹ️  另一个 CCB 更新窗口正在执行旧缓存迁移，本窗口已跳过。'
        if detect_language() == 'zh'
        else 'ℹ️  Another CCB update window is migrating legacy caches; this window skipped it.'
    )
    print(message, file=stdout)


def _print_failure(stdout: TextIO, error: str) -> None:
    message = (
        f'⚠️  更新后旧缓存迁移失败，核心更新不受影响：{error}'
        if detect_language() == 'zh'
        else f'⚠️  Post-update legacy cache migration failed; the core update is unaffected: {error}'
    )
    print(message, file=stdout)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


__all__ = [
    'PostUpdateCacheCleanupSummary',
    'provider_cache_cleanup_state_path',
    'run_post_update_provider_cache_cleanup',
]
