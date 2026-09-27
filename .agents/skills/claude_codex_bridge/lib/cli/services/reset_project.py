from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile

from agents.config_loader import load_project_config
from cli.context import CliContext
from cli.models import ParsedKillCommand, ParsedStartCommand
from cli.services.daemon import shutdown_daemon
from cli.services.kill import kill_project
from cli.services.tmux_ui import set_tmux_ui_active
from ccbd.services.project_namespace import ProjectNamespaceController
from project.ids import compute_project_id
from project.identity_store import PROJECT_IDENTITY_FILENAME
from project.resolver import ProjectContext
from provider_core.pathing import session_filename_for_agent
from storage.path_helpers import RUNTIME_ROOT_REF_FILENAME
from storage.paths import PathLayout
from workspace.git_worktree import unregister_worktrees_under
from workspace.reconcile import format_workspace_blockers, prepare_reset_workspaces


@dataclass(frozen=True)
class ResetProjectSummary:
    project_root: str
    project_id: str
    preserved_config: bool
    reset_performed: bool
    preserved_provider_histories: int = 0
    preserved_session_files: int = 0
    preserved_user_files: int = 0


@dataclass(frozen=True)
class _PreservationItem:
    source: Path
    staged: Path
    category: str


def reset_project_state(project_root: Path, *, context: CliContext | None = None) -> ResetProjectSummary:
    root = _resolve_path(project_root)
    layout = PathLayout(root)
    project_id = compute_project_id(root)
    preserved_config_bytes = _read_optional_bytes(layout.config_path)
    preservation_specs = _preservation_specs(root, layout)
    reset_performed = False
    preserved_counts: dict[str, int] = {
        'provider_state': 0,
        'session_file': 0,
        'user_file': 0,
    }

    if layout.ccb_dir.exists():
        reset_performed = True
        preflight = prepare_reset_workspaces(root, apply=False)
        if preflight.blockers:
            raise RuntimeError(format_workspace_blockers('ccb -n', preflight.blockers))
        _stop_project_runtime(context or _build_reset_context(root))
        prepare_reset_workspaces(root, apply=True)
        _unregister_project_worktrees(root, layout)
        layout.ensure_runtime_state_root()
        staged_root, staged_items = _stage_preserved_paths(preservation_specs)
        preserved_counts = _preserved_counts(staged_items)
        cleanup_staging = False
        try:
            _clear_runtime_state(layout)
            _clear_anchor_contents(
                layout.ccb_dir,
                preserve_runtime_root_ref=(
                    layout.runtime_state_placement.root_kind == 'relocated'
                    and layout.runtime_marker_status == 'ok'
                ),
            )
            if preserved_config_bytes is not None:
                layout.ccb_dir.mkdir(parents=True, exist_ok=True)
                layout.config_path.write_bytes(preserved_config_bytes)
            _restore_preserved_paths(staged_items)
            cleanup_staging = True
        except Exception as reset_exc:
            try:
                _restore_preserved_paths(staged_items)
                cleanup_staging = True
            except Exception as restore_exc:
                staged_hint = f' preserved reset data remains staged at {staged_root}.' if staged_root else ''
                raise RuntimeError(
                    f'{reset_exc}; failed to restore preserved reset data: {restore_exc}.{staged_hint}'
                ) from reset_exc
            raise
        finally:
            if cleanup_staging and staged_root is not None:
                shutil.rmtree(staged_root, ignore_errors=True)

    return ResetProjectSummary(
        project_root=str(root),
        project_id=project_id,
        preserved_config=preserved_config_bytes is not None,
        reset_performed=reset_performed,
        preserved_provider_histories=preserved_counts['provider_state'],
        preserved_session_files=preserved_counts['session_file'],
        preserved_user_files=preserved_counts['user_file'],
    )


def _build_reset_context(project_root: Path) -> CliContext:
    root = _resolve_path(project_root)
    project_id = compute_project_id(root)
    command = ParsedStartCommand(
        project=str(root),
        agent_names=(),
        restore=True,
        auto_permission=True,
        reset_context=True,
    )
    project = ProjectContext(
        cwd=root,
        project_root=root,
        config_dir=root / '.ccb',
        project_id=project_id,
        source='reset',
    )
    return CliContext(
        command=command,
        cwd=root,
        project=project,
        paths=PathLayout(root),
    )


def _stop_project_runtime(context: CliContext) -> None:
    cleanup_errors: list[str] = []
    try:
        kill_project(
            context,
            ParsedKillCommand(
                project=str(context.project.project_root),
                force=True,
            ),
        )
        set_tmux_ui_active(False)
        return
    except Exception as exc:
        cleanup_errors.append(f'kill_project: {exc}')

    daemon_stopped = False
    try:
        shutdown_daemon(context, force=True)
        daemon_stopped = True
    except Exception as exc:
        cleanup_errors.append(f'shutdown_daemon: {exc}')

    namespace_destroyed = False
    try:
        ProjectNamespaceController(context.paths, context.project.project_id).destroy(
            reason='reset',
            force=True,
        )
        namespace_destroyed = True
    except Exception as exc:
        cleanup_errors.append(f'namespace_destroy: {exc}')

    set_tmux_ui_active(False)
    if daemon_stopped or namespace_destroyed:
        return
    details = '; '.join(cleanup_errors) if cleanup_errors else 'unknown cleanup failure'
    raise RuntimeError(
        'failed to stop project runtime before rebuilding `.ccb`; '
        f'{details}. Run `ccb kill -f` from the project root and retry `ccb -n`.'
    )


def _clear_runtime_state(layout: PathLayout) -> None:
    for path in (layout.ccbd_dir, layout.agents_dir):
        _remove_path(path)


def _clear_anchor_contents(ccb_dir: Path, *, preserve_runtime_root_ref: bool) -> None:
    if ccb_dir.is_symlink() or ccb_dir.is_file():
        ccb_dir.unlink()
        ccb_dir.mkdir(parents=True, exist_ok=True)
        return
    if not ccb_dir.is_dir():
        return
    for child in tuple(ccb_dir.iterdir()):
        if child.name in {
            'ccb.config',
            PROJECT_IDENTITY_FILENAME,
        }:
            continue
        if preserve_runtime_root_ref and child.name == RUNTIME_ROOT_REF_FILENAME:
            continue
        _remove_path(child)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)


def _unregister_project_worktrees(project_root: Path, layout: PathLayout) -> None:
    unregister_worktrees_under(project_root, layout.workspaces_dir)


def _preservation_specs(project_root: Path, layout: PathLayout) -> tuple[tuple[Path, str], ...]:
    specs: list[tuple[Path, str]] = [
        (layout.project_memory_path, 'user_file'),
        (layout.ccb_dir / 'history', 'user_file'),
    ]
    for agent in _configured_agent_specs(project_root):
        specs.append((layout.agent_private_memory_path(agent.name), 'user_file'))
        try:
            specs.append((layout.ccb_dir / session_filename_for_agent(agent.provider, agent.name), 'session_file'))
        except RuntimeError:
            pass
        for provider_state in _provider_state_candidates(layout, agent.name, agent.provider):
            specs.append((provider_state, 'provider_state'))
    return _dedupe_specs(specs)


def _configured_agent_specs(project_root: Path) -> tuple[object, ...]:
    try:
        config = load_project_config(project_root).config
    except Exception:
        return ()
    return tuple(config.agents.values())


def _provider_state_candidates(layout: PathLayout, agent_name: str, provider: str) -> tuple[Path, ...]:
    effective = layout.agent_provider_state_dir(agent_name, provider)
    anchor = layout.agent_anchor_dir(agent_name) / 'provider-state' / str(provider or '').strip().lower()
    if anchor == effective:
        return (effective,)
    return (effective, anchor)


def _dedupe_specs(specs: list[tuple[Path, str]]) -> tuple[tuple[Path, str], ...]:
    deduped: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for path, category in specs:
        if path in seen:
            continue
        seen.add(path)
        deduped.append((path, category))
    return tuple(deduped)


def _stage_preserved_paths(specs: tuple[tuple[Path, str], ...]) -> tuple[Path | None, tuple[_PreservationItem, ...]]:
    items: list[_PreservationItem] = []
    staging_root: Path | None = None
    for source, category in specs:
        if not _path_exists_or_symlink(source):
            continue
        if staging_root is None:
            staging_root = Path(tempfile.mkdtemp(prefix='ccb-reset-preserve-'))
            try:
                staging_root.chmod(0o700)
            except OSError:
                pass
        staged = staging_root / f'{len(items):04d}-{category}'
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(staged))
        items.append(_PreservationItem(source=source, staged=staged, category=category))
    return staging_root, tuple(items)


def _restore_preserved_paths(items: tuple[_PreservationItem, ...]) -> None:
    for item in items:
        if not _path_exists_or_symlink(item.staged):
            continue
        if _path_exists_or_symlink(item.source):
            _remove_path(item.source)
        item.source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item.staged), str(item.source))


def _preserved_counts(items: tuple[_PreservationItem, ...]) -> dict[str, int]:
    counts = {
        'provider_state': 0,
        'session_file': 0,
        'user_file': 0,
    }
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
    return counts


def _path_exists_or_symlink(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _read_optional_bytes(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    return path.read_bytes()


def _resolve_path(path: Path) -> Path:
    candidate = Path(path).expanduser()
    try:
        return candidate.resolve()
    except Exception:
        return candidate.absolute()


__all__ = ['ResetProjectSummary', 'reset_project_state']
