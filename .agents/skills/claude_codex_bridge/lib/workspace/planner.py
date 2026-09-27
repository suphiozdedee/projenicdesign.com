from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

from agents.models import AgentSpec, WorkspaceMode
from project.resolver import ProjectContext
from storage.paths import PathLayout
from workspace.binding import WorkspaceBindingStore
from workspace.models import WorkspacePlan

_PLACEHOLDER_RE = re.compile(r'\{([a-z_]+)\}')
_ALLOWED_BRANCH_VARS = {'agent_name', 'project_slug', 'date'}
_DEFAULT_BRANCH_TEMPLATE = 'ccb/{agent_name}'


class WorkspacePlanner:
    def plan(self, agent_spec: AgentSpec, project_ctx: ProjectContext) -> WorkspacePlan:
        layout = PathLayout(project_ctx.project_root)
        if agent_spec.workspace_mode is WorkspaceMode.INPLACE:
            workspace_path = project_ctx.project_root
            binding_path = None
            unsafe_shared_workspace = True
            branch_name = None
            workspace_scope = 'inplace'
        elif agent_spec.workspace_path is not None:
            workspace_path = Path(agent_spec.workspace_path).expanduser()
            binding_path = None
            unsafe_shared_workspace = False
            branch_name = None
            workspace_scope = 'external'
        elif agent_spec.workspace_group is not None:
            binding_path = layout.workspace_group_binding_path(agent_spec.workspace_group)
            if binding_path.is_file():
                binding = WorkspaceBindingStore().load(binding_path)
                if binding.project_id != project_ctx.project_id:
                    raise ValueError('workspace group binding belongs to a different project')
                if Path(binding.target_project).resolve() != project_ctx.project_root:
                    raise ValueError('workspace group binding targets a different project root')
                workspace_path = Path(binding.workspace_path)
                branch_name = binding.branch_name
            else:
                workspace_path = layout.workspace_group_path(agent_spec.workspace_group)
                branch_name = f'ccb/group/{agent_spec.workspace_group}'
            unsafe_shared_workspace = False
            workspace_scope = 'group'
        else:
            workspace_path = layout.workspace_path(agent_spec.name, workspace_root=agent_spec.workspace_root)
            binding_path = layout.workspace_binding_path(agent_spec.name, workspace_root=agent_spec.workspace_root)
            unsafe_shared_workspace = False
            branch_name = self._render_branch_name(agent_spec, layout.project_slug)
            workspace_scope = 'agent'
            if agent_spec.workspace_mode is WorkspaceMode.COPY:
                branch_name = None

        return WorkspacePlan(
            project_id=project_ctx.project_id,
            project_root=project_ctx.project_root,
            project_slug=layout.project_slug,
            agent_name=agent_spec.name,
            workspace_mode=agent_spec.workspace_mode,
            workspace_path=workspace_path,
            binding_path=binding_path,
            source_root=project_ctx.project_root,
            branch_name=branch_name,
            branch_template=agent_spec.branch_template or _DEFAULT_BRANCH_TEMPLATE,
            unsafe_shared_workspace=unsafe_shared_workspace,
            workspace_scope=workspace_scope,
        )

    def plan_controller_worktree(
        self,
        project_ctx: ProjectContext,
        *,
        agent_name: str,
        workspace_path: Path,
        branch_name: str,
        base_commit: str,
    ) -> WorkspacePlan:
        branch = str(branch_name or '').strip()
        if not branch:
            raise ValueError('controller worktree branch_name cannot be empty')
        base = str(base_commit or '').strip()
        if not base:
            raise ValueError('controller worktree base_commit cannot be empty')
        return WorkspacePlan(
            project_id=project_ctx.project_id,
            project_root=project_ctx.project_root,
            project_slug=PathLayout(project_ctx.project_root).project_slug,
            agent_name=agent_name,
            workspace_mode=WorkspaceMode.GIT_WORKTREE,
            workspace_path=workspace_path,
            binding_path=None,
            source_root=project_ctx.project_root,
            branch_name=branch,
            branch_template=None,
            unsafe_shared_workspace=False,
            workspace_scope='controller',
            base_commit=base,
        )

    def _render_branch_name(self, agent_spec: AgentSpec, project_slug: str) -> str:
        template = agent_spec.branch_template or _DEFAULT_BRANCH_TEMPLATE
        variables = set(_PLACEHOLDER_RE.findall(template))
        unknown = sorted(variables - _ALLOWED_BRANCH_VARS)
        if unknown:
            raise ValueError(f'branch_template contains unsupported variables: {unknown}')
        rendered = template.format(
            agent_name=agent_spec.name,
            project_slug=project_slug,
            date=datetime.now(timezone.utc).strftime('%Y%m%d'),
        )
        branch_name = rendered.strip()
        if not branch_name:
            raise ValueError('branch_template rendered empty branch name')
        return branch_name
