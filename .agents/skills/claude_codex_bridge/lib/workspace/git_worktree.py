from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import stat
import subprocess

from project.discovery import WORKSPACE_BINDING_FILENAME


@dataclass(frozen=True)
class WorkspaceBindingAuthority:
    target_project: Path
    project_id: str
    workspace_path: Path
    branch_name: str
    agent_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'target_project', _normalize_path(self.target_project))
        object.__setattr__(self, 'workspace_path', _normalize_path(self.workspace_path))


def can_use_git_worktree(repo_root: Path) -> bool:
    result = _git(repo_root, ['rev-parse', '--show-toplevel'])
    return result.returncode == 0


def has_missing_registered_worktree(repo_root: Path, workspace_path: Path) -> bool:
    target = _normalize_path(workspace_path)
    if target.exists():
        return False
    return any(path == target for path in list_registered_worktrees(repo_root))


def prune_missing_worktrees_under(repo_root: Path, workspaces_root: Path) -> bool:
    missing = tuple(
        path
        for path in list_registered_worktrees(repo_root)
        if _path_within(path, workspaces_root) and not path.exists()
    )
    if not missing:
        return False
    _run_git(repo_root, ['worktree', 'prune'], error='failed to prune stale git worktrees')
    return True


def unregister_worktrees_under(repo_root: Path, workspaces_root: Path) -> None:
    existing = tuple(
        path
        for path in list_registered_worktrees(repo_root)
        if _path_within(path, workspaces_root) and path.exists()
    )
    for path in existing:
        _run_git(
            repo_root,
            ['worktree', 'remove', '--force', str(path)],
            error=f'failed to remove git worktree {path}',
        )
    prune_missing_worktrees_under(repo_root, workspaces_root)


def branch_exists(repo_root: Path, branch_name: str) -> bool:
    result = _git(repo_root, ['show-ref', '--verify', '--quiet', f'refs/heads/{branch_name}'])
    return result.returncode == 0


def branch_is_merged_into_head(repo_root: Path, branch_name: str) -> bool | None:
    if not branch_exists(repo_root, branch_name):
        return None
    result = _git(repo_root, ['merge-base', '--is-ancestor', branch_name, 'HEAD'])
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(_detail(result) or f'failed to inspect merge state for {branch_name}')


def delete_branch(repo_root: Path, branch_name: str) -> bool:
    if not branch_exists(repo_root, branch_name):
        return False
    _run_git(repo_root, ['branch', '-d', branch_name], error=f'failed to delete merged branch {branch_name}')
    return True


def list_registered_worktrees(repo_root: Path) -> tuple[Path, ...]:
    if not can_use_git_worktree(repo_root):
        return ()
    result = _git(repo_root, ['worktree', 'list', '--porcelain'])
    if result.returncode != 0:
        raise RuntimeError(_detail(result) or 'failed to list git worktrees')

    worktrees: list[Path] = []
    for raw_line in (result.stdout or '').splitlines():
        line = raw_line.strip()
        if not line.startswith('worktree '):
            continue
        worktrees.append(_normalize_path(Path(line[len('worktree ') :])))
    return tuple(worktrees)


def is_registered_worktree(repo_root: Path, workspace_path: Path) -> bool:
    target = _normalize_path(workspace_path)
    return any(path == target for path in list_registered_worktrees(repo_root))


def is_git_workspace_root(workspace_path: Path) -> bool:
    git_dir = workspace_path / '.git'
    return git_dir.exists()


def workspace_is_dirty(
    workspace_path: Path,
    *,
    binding_authority: WorkspaceBindingAuthority | None = None,
) -> bool | None:
    target = _normalize_path(workspace_path)
    if not target.exists() or not is_git_workspace_root(target):
        return None
    result = subprocess.run(
        ['git', '-C', str(target), 'status', '--porcelain=v1', '-z', '--untracked-files=all'],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = bytes(result.stderr or result.stdout or b'').decode('utf-8', errors='replace').strip()
        raise RuntimeError(detail or f'failed to inspect workspace status: {target}')
    records = tuple(record for record in bytes(result.stdout or b'').split(b'\0') if record)
    marker_record = f'?? {WORKSPACE_BINDING_FILENAME}'.encode('utf-8')
    marker = target / WORKSPACE_BINDING_FILENAME
    if (marker.is_symlink() or marker.exists()) and marker_record not in records:
        return True
    for record in records:
        if (
            record == marker_record
            and binding_authority is not None
            and _binding_marker_is_owned(target, binding_authority)
        ):
            continue
        return True
    return False


def remove_registered_worktree(repo_root: Path, workspace_path: Path) -> bool:
    target = _normalize_path(workspace_path)
    if is_registered_worktree(repo_root, target):
        if target.exists():
            _run_git(repo_root, ['worktree', 'remove', '--force', str(target)], error=f'failed to remove git worktree {target}')
        else:
            prune_missing_worktrees_under(repo_root, target.parent)
        return True
    return False


def remove_clean_registered_worktree(
    repo_root: Path,
    workspace_path: Path,
    *,
    binding_authority: WorkspaceBindingAuthority | None = None,
) -> bool:
    target = _normalize_path(workspace_path)
    if not is_registered_worktree(repo_root, target):
        return False
    if not target.exists():
        raise RuntimeError(f'registered worktree is missing: {target}')
    dirty = workspace_is_dirty(target, binding_authority=binding_authority)
    if dirty is not False:
        raise RuntimeError(f'refusing to remove dirty or unreadable worktree: {target}')
    marker = target / WORKSPACE_BINDING_FILENAME
    if marker.is_symlink() or marker.exists():
        if (
            binding_authority is None
            or not _binding_marker_is_owned(target, binding_authority)
            or not _binding_marker_is_untracked(target)
        ):
            raise RuntimeError(f'refusing to remove worktree with tracked or unowned workspace binding: {target}')
        marker.unlink()
    dirty_after_marker_removal = workspace_is_dirty(target, binding_authority=binding_authority)
    if dirty_after_marker_removal is not False:
        raise RuntimeError(f'refusing to remove worktree changed during retirement: {target}')
    _run_git(
        repo_root,
        ['worktree', 'remove', str(target)],
        error=f'failed to remove clean git worktree {target}',
    )
    return True


def _binding_marker_is_owned(target: Path, authority: WorkspaceBindingAuthority) -> bool:
    if target != authority.workspace_path:
        return False
    marker = target / WORKSPACE_BINDING_FILENAME
    try:
        marker_stat = marker.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(marker_stat.st_mode) or marker.is_symlink():
        return False
    try:
        record = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    if record.get('schema_version') != 2 or record.get('record_type') != 'workspace_binding':
        return False
    if record.get('workspace_mode') != 'git-worktree':
        return False
    if str(record.get('project_id') or '') != authority.project_id:
        return False
    if str(record.get('branch_name') or '') != authority.branch_name:
        return False
    record_agent_name = str(record.get('agent_name') or '').strip()
    if not record_agent_name:
        return False
    if authority.agent_name is not None and record_agent_name != authority.agent_name:
        return False
    target_project_text = str(record.get('target_project') or '').strip()
    workspace_path_text = str(record.get('workspace_path') or '').strip()
    if not target_project_text or not workspace_path_text:
        return False
    if (
        not Path(target_project_text).expanduser().is_absolute()
        or not Path(workspace_path_text).expanduser().is_absolute()
    ):
        return False
    try:
        target_project = _normalize_path(Path(target_project_text))
        workspace_path = _normalize_path(Path(workspace_path_text))
    except (OSError, RuntimeError, ValueError):
        return False
    return target_project == authority.target_project and workspace_path == authority.workspace_path


def _binding_marker_is_untracked(target: Path) -> bool:
    result = subprocess.run(
        [
            'git',
            '-C',
            str(target),
            'status',
            '--porcelain=v1',
            '-z',
            '--untracked-files=all',
            '--',
            WORKSPACE_BINDING_FILENAME,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return False
    records = tuple(record for record in bytes(result.stdout or b'').split(b'\0') if record)
    return records == (f'?? {WORKSPACE_BINDING_FILENAME}'.encode('utf-8'),)


def delete_owned_branch(repo_root: Path, branch_name: str, *, expected_commit: str) -> bool:
    if not branch_exists(repo_root, branch_name):
        return False
    observed = _git(repo_root, ['rev-parse', '--verify', f'{branch_name}^{{commit}}'])
    if observed.returncode != 0:
        raise RuntimeError(_detail(observed) or f'failed to resolve branch {branch_name}')
    observed_commit = (observed.stdout or '').strip()
    if observed_commit != expected_commit:
        raise RuntimeError(
            f'owned branch authority drift for {branch_name}: '
            f'expected {expected_commit}, observed {observed_commit}'
        )
    _run_git(
        repo_root,
        ['branch', '-D', branch_name],
        error=f'failed to delete owned branch {branch_name}',
    )
    return True


def _path_within(path: Path, parent: Path) -> bool:
    normalized_path = _normalize_path(path)
    normalized_parent = _normalize_path(parent)
    try:
        normalized_path.relative_to(normalized_parent)
    except ValueError:
        return False
    return True


def _normalize_path(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['git', '-C', str(repo_root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or '').strip()


def _run_git(repo_root: Path, args: list[str], *, error: str) -> None:
    result = _git(repo_root, args)
    if result.returncode != 0:
        raise RuntimeError(f'{error}: {_detail(result)}')


__all__ = [
    'WorkspaceBindingAuthority',
    'branch_exists',
    'branch_is_merged_into_head',
    'can_use_git_worktree',
    'delete_branch',
    'delete_owned_branch',
    'has_missing_registered_worktree',
    'is_git_workspace_root',
    'is_registered_worktree',
    'list_registered_worktrees',
    'prune_missing_worktrees_under',
    'remove_registered_worktree',
    'remove_clean_registered_worktree',
    'unregister_worktrees_under',
    'workspace_is_dirty',
]
