from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path

from cli.models import ParsedAskCommand, ParsedCommand
from project.discovery import ProjectDiscoveryError
from project.discovery import project_ccb_dir
from project.ids import compute_project_id
from project.resolver import ProjectContext, ProjectResolver, bootstrap_project
from storage.paths import PathLayout


@dataclass(frozen=True)
class CliContext:
    command: ParsedCommand
    cwd: Path
    project: ProjectContext
    paths: PathLayout


class CliContextBuilder:
    def __init__(self, resolver: ProjectResolver | None = None):
        self._resolver = resolver or ProjectResolver()

    def build(
        self,
        command: ParsedCommand,
        *,
        cwd: Path | None = None,
        bootstrap_if_missing: bool = False,
    ) -> CliContext:
        current = Path(cwd or Path.cwd()).expanduser()
        explicit_project = Path(command.project).expanduser() if getattr(command, 'project', None) else None
        caller_project = None
        if isinstance(command, ParsedAskCommand) and explicit_project is None:
            caller_project = _caller_project_root()
            if caller_project is not None:
                explicit_project = caller_project
        try:
            project = self._resolver.resolve(
                current,
                explicit_project=explicit_project,
                allow_ancestor_anchor=not bootstrap_if_missing,
            )
        except ProjectDiscoveryError:
            if not bootstrap_if_missing:
                raise
            bootstrap_root = explicit_project or current
            if not bootstrap_root.exists() or not bootstrap_root.is_dir():
                raise
            project = bootstrap_project(bootstrap_root)
        if caller_project is not None:
            project = replace(project, source='caller-runtime')
        return CliContext(
            command=command,
            cwd=current,
            project=project,
            paths=PathLayout(project.project_root),
        )


def _caller_project_root() -> Path | None:
    raw = str(os.environ.get('CCB_CALLER_PROJECT_ROOT') or '').strip()
    if not raw:
        return None
    project_root = _resolve_path(Path(raw))
    if not project_ccb_dir(project_root).is_dir():
        return None
    expected_project_id = str(os.environ.get('CCB_CALLER_PROJECT_ID') or '').strip()
    if expected_project_id and expected_project_id != compute_project_id(project_root):
        return None
    return project_root


def _resolve_path(path: Path) -> Path:
    current = Path(path).expanduser()
    try:
        return current.resolve()
    except Exception:
        return current.absolute()
