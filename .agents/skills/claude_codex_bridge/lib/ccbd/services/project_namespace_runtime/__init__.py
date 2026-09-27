from __future__ import annotations

from .additive_patch import (
    NamespacePatchApplyResult,
    apply_reload_patch,
    assert_preserved_agent_panes,
    snapshot_preserved_agent_panes,
)
from .controller import ProjectNamespaceController
from .models import ProjectNamespace, ProjectNamespaceDestroySummary
from .topology_plan import NamespaceTopologyPlan, NamespaceWindowPlan, SidebarPanePlan, build_namespace_topology_plan

__all__ = [
    'NamespacePatchApplyResult',
    'NamespaceTopologyPlan',
    'NamespaceWindowPlan',
    'ProjectNamespace',
    'ProjectNamespaceController',
    'ProjectNamespaceDestroySummary',
    'SidebarPanePlan',
    'apply_reload_patch',
    'assert_preserved_agent_panes',
    'build_namespace_topology_plan',
    'snapshot_preserved_agent_panes',
]
