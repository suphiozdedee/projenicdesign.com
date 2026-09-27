from __future__ import annotations

from pathlib import Path

from agents.models import normalize_agent_name
from storage.paths import PathLayout

from .seed import project_memory_path
from .filters import filter_memory_source
from .policy import (
    SOURCE_AGENT_PRIVATE,
    SOURCE_CCB_SHARED,
    SOURCE_PROVIDER_NATIVE_PROJECT,
    filters_for_source,
    should_include_source,
)
from .types import ProjectMemorySource

_PROVIDER_NATIVE_FILES = {
    'claude': 'CLAUDE.md',
    'codex': 'AGENTS.md',
    'opencode': 'AGENTS.md',
    'gemini': 'GEMINI.md',
}


def agent_private_memory_path(project_root_or_layout, agent_name: str) -> Path:
    layout = _layout(project_root_or_layout)
    return layout.agent_private_memory_path(agent_name)


def provider_native_memory_path(project_root_or_layout, provider: str) -> Path | None:
    layout = _layout(project_root_or_layout)
    filename = _PROVIDER_NATIVE_FILES.get(str(provider or '').strip().lower())
    if not filename:
        return None
    return layout.project_root / filename


def load_memory_sources(
    project_root_or_layout,
    *,
    agent_name: str,
    provider: str,
    extra_sources: tuple[ProjectMemorySource, ...] = (),
    include_missing: bool = True,
    include_provider_native_project: bool | None = None,
) -> tuple[ProjectMemorySource, ...]:
    layout = _layout(project_root_or_layout)
    sources: list[ProjectMemorySource] = []
    sources.extend(_filter_sources(extra_sources, provider=provider))
    sources.append(
        _read_source(
            kind=SOURCE_CCB_SHARED,
            title='CCB Shared Project Memory',
            path=project_memory_path(layout),
            include_missing=include_missing,
        )
    )

    include_native = (
        should_include_source(provider, SOURCE_PROVIDER_NATIVE_PROJECT)
        if include_provider_native_project is None
        else include_provider_native_project
    )
    provider_path = provider_native_memory_path(layout, provider) if include_native else None
    if provider_path is not None:
        provider_source = _read_source(
            kind=SOURCE_PROVIDER_NATIVE_PROJECT,
            title='Provider-Native Project Memory',
            path=provider_path,
            include_missing=include_missing,
        )
        if provider_source is not None:
            sources.append(provider_source)

    sources.extend(_role_memory_sources(layout.project_root, agent_name=agent_name))

    sources.append(
        _read_source(
            kind=SOURCE_AGENT_PRIVATE,
            title='Agent Private Memory',
            path=agent_private_memory_path(layout, agent_name),
            include_missing=include_missing,
        )
    )
    return tuple(source for source in sources if source is not None)


def _filter_sources(sources: tuple[ProjectMemorySource, ...], *, provider: str) -> tuple[ProjectMemorySource, ...]:
    return tuple(
        filter_memory_source(source, filter_names=filters_for_source(provider, source.kind))
        for source in sources
    )


def _role_memory_sources(project_root: Path, *, agent_name: str) -> tuple[ProjectMemorySource, ...]:
    try:
        from rolepacks.runtime_lookup import project_role_memory_sources

        return tuple(
            source
            for source in project_role_memory_sources(project_root, agent_name)
            if isinstance(source, ProjectMemorySource)
        )
    except Exception:
        return ()


def _layout(project_root_or_layout) -> PathLayout:
    if isinstance(project_root_or_layout, PathLayout):
        return project_root_or_layout
    return PathLayout(Path(project_root_or_layout))


def read_memory_source(
    *,
    kind: str,
    title: str,
    path: Path,
    include_missing: bool = True,
) -> ProjectMemorySource | None:
    return _read_source(kind=kind, title=title, path=path, include_missing=include_missing)


def _read_source(*, kind: str, title: str, path: Path, include_missing: bool) -> ProjectMemorySource | None:
    source_path = Path(path)
    if not source_path.is_file():
        if not include_missing:
            return None
        return ProjectMemorySource(kind=kind, title=title, path=source_path, content='', exists=False)
    try:
        content = source_path.read_text(encoding='utf-8')
    except OSError as exc:
        return ProjectMemorySource(
            kind=kind,
            title=title,
            path=source_path,
            content='',
            exists=True,
            warning=f'failed_to_read_memory_source: {exc}',
        )
    return ProjectMemorySource(kind=kind, title=title, path=source_path, content=content, exists=True)


__all__ = [
    'agent_private_memory_path',
    'load_memory_sources',
    'provider_native_memory_path',
    'read_memory_source',
]
