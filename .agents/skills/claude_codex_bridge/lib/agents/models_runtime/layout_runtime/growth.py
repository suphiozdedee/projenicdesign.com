from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .nodes import LayoutLeaf, LayoutNode
from .ops import stack_vertical


DEFAULT_MAX_PANES_PER_WINDOW = 6


@dataclass(frozen=True)
class PaneGrowthWindowPlan:
    name: str
    index: int
    agent_names: tuple[str, ...]
    layout: LayoutNode

    @property
    def layout_spec(self) -> str:
        return self.layout.render()

    def to_record(self) -> dict[str, object]:
        return {
            'name': self.name,
            'index': self.index,
            'agent_names': list(self.agent_names),
            'layout_spec': self.layout_spec,
        }


def build_pane_growth_windows(
    pane_names: Iterable[str],
    *,
    window_prefix: str = 'layout',
    max_panes_per_window: int = DEFAULT_MAX_PANES_PER_WINDOW,
) -> tuple[PaneGrowthWindowPlan, ...]:
    max_count = _normalize_max_panes(max_panes_per_window)
    names = tuple(_normalize_names(pane_names))
    windows: list[PaneGrowthWindowPlan] = []
    for offset in range(0, len(names), max_count):
        index = len(windows) + 1
        page_names = names[offset : offset + max_count]
        windows.append(
            PaneGrowthWindowPlan(
                name=_page_window_name(window_prefix, index),
                index=index,
                agent_names=page_names,
                layout=build_pane_growth_layout(page_names),
            )
        )
    return tuple(windows)


def build_pane_growth_layout(pane_names: Iterable[str]) -> LayoutNode:
    names = tuple(_normalize_names(pane_names))
    if not names:
        raise ValueError('at least one pane is required for layout')
    if len(names) > DEFAULT_MAX_PANES_PER_WINDOW:
        raise ValueError('single pane-growth layout supports at most 6 panes')
    leaves = [
        LayoutNode(kind='leaf', leaf=LayoutLeaf(name=name))
        for name in names
    ]
    if len(leaves) == 1:
        return leaves[0]
    left = stack_vertical(leaves[0::2])
    right = stack_vertical(leaves[1::2])
    if left is None or right is None:
        raise ValueError('pane-growth layout requires both columns after two panes')
    return LayoutNode(kind='horizontal', left=left, right=right)


def _normalize_max_panes(value: int) -> int:
    try:
        count = int(value)
    except Exception as exc:
        raise ValueError('max panes per window must be an integer') from exc
    if count < 1 or count > DEFAULT_MAX_PANES_PER_WINDOW:
        raise ValueError('max panes per window must be between 1 and 6')
    return count


def _normalize_names(names: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(str(name or '').strip() for name in names if str(name or '').strip())
    if len(set(normalized)) != len(normalized):
        raise ValueError('pane names must be unique')
    return normalized


def _page_window_name(prefix: str, index: int) -> str:
    base = str(prefix or '').strip() or 'layout'
    return base if index == 1 else f'{base}-{index}'


__all__ = [
    'DEFAULT_MAX_PANES_PER_WINDOW',
    'PaneGrowthWindowPlan',
    'build_pane_growth_layout',
    'build_pane_growth_windows',
]
