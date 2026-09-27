from __future__ import annotations

from .materialize_topology import existing_topology_agent_panes


def snapshot_preserved_agent_panes(
    controller,
    context,
    *,
    topology_plan,
    agents: tuple[str, ...] | list[str],
) -> dict[str, str]:
    expected = {str(agent) for agent in tuple(agents or ())}
    if not expected:
        return {}
    panes = existing_topology_agent_panes(controller, context, topology_plan=topology_plan)
    return {agent: pane_id for agent, pane_id in panes.items() if agent in expected}


def assert_preserved_agent_panes(
    before: dict[str, str],
    after: dict[str, str],
    *,
    expected_agents: tuple[str, ...] | list[str] = (),
) -> None:
    changed = _preservation_changes(before, after, expected_agents=expected_agents)
    if changed:
        raise RuntimeError(f'preserved agent pane ids changed: {" ".join(changed)}')


def _preservation_changes(
    before: dict[str, str],
    after: dict[str, str],
    *,
    expected_agents: tuple[str, ...] | list[str],
) -> list[str]:
    expected = {str(agent) for agent in tuple(expected_agents or ())}
    missing_before = sorted(expected - set(before))
    missing_after = sorted((expected or set(before)) - set(after))
    missing = sorted(set(before) - set(after))
    changed = sorted(agent for agent in set(before) & set(after) if before[agent] != after[agent])
    return [
        *_format_detail('missing_before', missing_before),
        *_format_detail('missing_after', missing_after),
        *_format_detail('missing', missing),
        *_format_detail('changed', changed),
    ]


def _format_detail(name: str, values: list[str]) -> list[str]:
    return [f'{name}={",".join(values)}'] if values else []


__all__ = ['assert_preserved_agent_panes', 'snapshot_preserved_agent_panes']
