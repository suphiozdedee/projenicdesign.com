from __future__ import annotations

from collections.abc import Iterable, Mapping


def agent_panes_from_record(value: Mapping[str, object]) -> dict[str, str]:
    panes: dict[str, str] = {}
    for agent, pane in dict(value).items():
        agent_name = str(agent or '').strip()
        pane_id = str(pane or '').strip()
        if agent_name:
            panes[agent_name] = pane_id
    return panes


def agent_names(value: Mapping[str, object] | Iterable[object]) -> tuple[str, ...]:
    values = value.keys() if isinstance(value, Mapping) else value
    names: list[str] = []
    for item in tuple(values or ()):
        name = str(item or '').strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def runtime_snapshots(
    registry,
    agents: Iterable[str],
) -> dict[str, dict[str, object] | None]:
    return {
        agent: runtime_record(registry.get(agent) if registry is not None else None)
        for agent in agent_names(tuple(agents))
    }


def runtime_record(runtime) -> dict[str, object] | None:
    if runtime is None:
        return None
    to_record = getattr(runtime, 'to_record', None)
    if callable(to_record):
        return dict(to_record())
    return dict(getattr(runtime, '__dict__', {}) or {})


def runtime_guard_agents(
    registry,
    requested_agents: Iterable[str],
    preserved_agents: Iterable[str],
) -> tuple[str, ...]:
    requested = set(agent_names(requested_agents))
    guarded = list(agent_names(preserved_agents))
    if registry is not None:
        for runtime in registry.list_all():
            name = str(getattr(runtime, 'agent_name', '') or '').strip()
            if name and name not in requested and name not in guarded:
                guarded.append(name)
    return tuple(guarded)


def changed_agents(
    before: dict[str, dict[str, object] | None],
    after: dict[str, dict[str, object] | None],
) -> tuple[str, ...]:
    return tuple(
        agent
        for agent in sorted(set(before) | set(after))
        if before.get(agent) != after.get(agent)
    )


def summary_started(summary, *, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if summary is None:
        return tuple(fallback)
    started = getattr(summary, 'started', None)
    if started is None and isinstance(summary, Mapping):
        started = summary.get('started')
    if started is None:
        return tuple(fallback)
    return agent_names(started)


def summary_record(summary) -> dict[str, object] | None:
    if summary is None:
        return None
    to_record = getattr(summary, 'to_record', None)
    if callable(to_record):
        return dict(to_record())
    if isinstance(summary, Mapping):
        return dict(summary)
    return dict(getattr(summary, '__dict__', {}) or {})


def clean_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def valid_pane_id(value: object) -> bool:
    return str(value or '').strip().startswith('%')


__all__ = [
    'agent_names',
    'agent_panes_from_record',
    'changed_agents',
    'clean_text',
    'optional_int',
    'runtime_snapshots',
    'runtime_guard_agents',
    'summary_record',
    'summary_started',
    'valid_pane_id',
]
