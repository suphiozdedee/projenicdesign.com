from __future__ import annotations

from pathlib import Path

from project.discovery import load_workspace_binding
from storage.atomic import atomic_write_json, atomic_write_json_if_changed
from workspace.models import WorkspaceBinding, WorkspacePlan


class WorkspaceBindingStore:
    def load(self, path: Path) -> WorkspaceBinding:
        record = load_workspace_binding(path)
        if record.get('schema_version') != 2:
            raise ValueError('workspace binding schema_version must be 2')
        if record.get('record_type') != 'workspace_binding':
            raise ValueError('workspace binding record_type must be workspace_binding')
        from agents.models import WorkspaceMode

        return WorkspaceBinding(
            target_project=record['target_project'],
            project_id=record['project_id'],
            agent_name=record['agent_name'],
            workspace_mode=WorkspaceMode(record['workspace_mode']),
            workspace_path=record['workspace_path'],
            branch_name=record.get('branch_name'),
        )

    def save(self, plan: WorkspacePlan) -> Path | None:
        if plan.binding_path is None:
            return None
        binding = WorkspaceBinding(
            target_project=str(plan.project_root),
            project_id=plan.project_id,
            agent_name=plan.agent_name,
            workspace_mode=plan.workspace_mode,
            workspace_path=str(plan.workspace_path),
            branch_name=plan.branch_name,
        )
        atomic_write_json_if_changed(plan.binding_path, binding.to_record())
        return plan.binding_path

    def bind_controller_worktree(
        self,
        path: Path,
        *,
        target_project: Path,
        project_id: str,
        workspace_group: str,
        workspace_path: Path,
        branch_name: str,
    ) -> Path:
        from agents.models import WorkspaceMode

        binding = WorkspaceBinding(
            target_project=str(Path(target_project).resolve()),
            project_id=str(project_id),
            agent_name=str(workspace_group),
            workspace_mode=WorkspaceMode.GIT_WORKTREE,
            workspace_path=str(Path(workspace_path).resolve()),
            branch_name=str(branch_name),
        )
        record = binding.to_record()
        atomic_write_json(path, record)
        atomic_write_json(Path(workspace_path).resolve() / '.ccb-workspace.json', record)
        return path
