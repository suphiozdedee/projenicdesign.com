from __future__ import annotations

from pathlib import Path

from agents.models import normalize_agent_name
from storage.atomic import atomic_write_text
from storage.paths import PathLayout

from .hashing import sha256_text
from .renderer import render_memory_bundle
from .seed import ensure_project_memory
from .sources import load_memory_sources
from .types import ProjectMemoryMaterialization, ProjectMemorySource, ProjectMemorySourceRef


def runtime_memory_bundle_path(project_root_or_layout, agent_name: str) -> Path:
    layout = _layout(project_root_or_layout)
    normalized_agent = normalize_agent_name(agent_name)
    return layout.runtime_memory_bundle_path(normalized_agent)


def materialize_runtime_memory_bundle(
    project_root,
    *,
    agent_name: str,
    provider: str,
    workspace_path: Path | None = None,
    now: str | None = None,
) -> ProjectMemoryMaterialization:
    warnings: list[str] = []
    try:
        layout = _layout(project_root)
        normalized_agent = normalize_agent_name(agent_name)
    except Exception as exc:
        return ProjectMemoryMaterialization(
            path=Path(''),
            written=False,
            unchanged=False,
            sha256='',
            sources=(),
            warnings=(f'invalid_project_memory_context: {exc}',),
        )

    ensure_result = ensure_project_memory(layout, now=now)
    if ensure_result.warning:
        warnings.append(ensure_result.warning)

    sources = load_memory_sources(layout, agent_name=normalized_agent, provider=provider)
    warnings.extend(source.warning for source in sources if source.warning)
    rendered = render_memory_bundle(
        project_root=layout.project_root,
        agent_name=normalized_agent,
        provider=provider,
        sources=sources,
        workspace_path=workspace_path,
    )
    target = runtime_memory_bundle_path(layout, normalized_agent)
    digest = sha256_text(rendered)
    current_digest = _path_sha256(target)
    if current_digest == digest:
        return ProjectMemoryMaterialization(
            path=target,
            written=False,
            unchanged=True,
            sha256=digest,
            sources=_source_refs(sources),
            warnings=tuple(warnings),
        )
    try:
        atomic_write_text(target, rendered)
    except OSError as exc:
        warnings.append(f'failed_to_write_runtime_memory_bundle: {exc}')
        return ProjectMemoryMaterialization(
            path=target,
            written=False,
            unchanged=False,
            sha256='',
            sources=_source_refs(sources),
            warnings=tuple(warnings),
        )
    return ProjectMemoryMaterialization(
        path=target,
        written=True,
        unchanged=False,
        sha256=digest,
        sources=_source_refs(sources),
        warnings=tuple(warnings),
    )


def _source_refs(sources: tuple[ProjectMemorySource, ...]) -> tuple[ProjectMemorySourceRef, ...]:
    refs: list[ProjectMemorySourceRef] = []
    for source in sources:
        refs.append(
            ProjectMemorySourceRef(
                kind=source.kind,
                path=source.path,
                exists=source.exists,
                sha256=sha256_text(source.content) if source.exists else '',
                warning=source.warning,
                filtered=source.filtered,
                filter_names=source.filter_names,
            )
        )
    return tuple(refs)


def _path_sha256(path: Path) -> str:
    try:
        return sha256_text(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return ''


def _layout(project_root_or_layout) -> PathLayout:
    if isinstance(project_root_or_layout, PathLayout):
        return project_root_or_layout
    return PathLayout(Path(project_root_or_layout))


__all__ = [
    'materialize_runtime_memory_bundle',
    'runtime_memory_bundle_path',
]
