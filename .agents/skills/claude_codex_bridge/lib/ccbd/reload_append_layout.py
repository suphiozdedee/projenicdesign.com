from __future__ import annotations

from dataclasses import dataclass

from agents.models import parse_layout_spec


@dataclass(frozen=True)
class AppendAgentPlan:
    agent: str
    direction: str


def rightmost_leaf_append_plan(old_window, new_window) -> tuple[AppendAgentPlan, ...] | None:
    try:
        old_layout = parse_layout_spec(getattr(old_window, 'user_layout', ''))
        new_layout = parse_layout_spec(getattr(new_window, 'user_layout', ''))
    except Exception:
        return None
    plan = _rightmost_leaf_append_plan_for_nodes(old_layout, new_layout)
    if plan is not None:
        return plan
    return _trailing_sequence_append_plan(old_layout, new_layout)


def _rightmost_leaf_append_plan_for_nodes(old_node, new_node) -> tuple[AppendAgentPlan, ...] | None:
    if old_node.kind == 'leaf':
        return _expanded_leaf_append_plan(old_node, new_node)
    if old_node.kind != new_node.kind:
        return None
    assert old_node.left is not None
    assert old_node.right is not None
    assert new_node.left is not None
    assert new_node.right is not None
    if old_node.left.render() != new_node.left.render():
        return None
    return _rightmost_leaf_append_plan_for_nodes(old_node.right, new_node.right)


def _expanded_leaf_append_plan(anchor_node, new_node) -> tuple[AppendAgentPlan, ...] | None:
    if new_node.kind == 'leaf':
        return () if new_node.render() == anchor_node.render() else None
    assert new_node.left is not None
    assert new_node.right is not None
    left_plan = _expanded_leaf_append_plan(anchor_node, new_node.left)
    if left_plan is None:
        return None
    if new_node.right.kind != 'leaf':
        return None
    assert new_node.right.leaf is not None
    direction = 'right' if new_node.kind == 'horizontal' else 'bottom'
    return (*left_plan, AppendAgentPlan(agent=new_node.right.leaf.name, direction=direction))


def _trailing_sequence_append_plan(old_node, new_node) -> tuple[AppendAgentPlan, ...] | None:
    old_leaves = tuple(leaf.name for leaf in old_node.iter_leaves())
    new_leaves = tuple(leaf.name for leaf in new_node.iter_leaves())
    if not old_leaves or tuple(new_leaves[: len(old_leaves)]) != old_leaves:
        return None
    appended = new_leaves[len(old_leaves) :]
    if not appended:
        return ()
    old_kind = _sequence_kind(old_node)
    new_kind = _sequence_kind(new_node)
    if old_kind is None or new_kind is None or old_kind != new_kind:
        return None
    direction = 'right' if old_kind == 'horizontal' else 'bottom'
    return tuple(AppendAgentPlan(agent=agent, direction=direction) for agent in appended)


def _sequence_kind(node) -> str | None:
    if node.kind == 'leaf':
        return None
    if not _all_branch_kinds(node, node.kind):
        return None
    return node.kind


def _all_branch_kinds(node, kind: str) -> bool:
    if node.kind == 'leaf':
        return True
    if node.kind != kind:
        return False
    assert node.left is not None
    assert node.right is not None
    return _all_branch_kinds(node.left, kind) and _all_branch_kinds(node.right, kind)


__all__ = ['AppendAgentPlan', 'rightmost_leaf_append_plan']
