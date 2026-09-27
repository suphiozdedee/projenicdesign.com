from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from project_memory import (
    ensure_project_memory,
    load_memory_sources,
    read_memory_source,
    render_memory_bundle,
)
from project_memory.hashing import sha256_text
from project_memory.policy import SOURCE_PROVIDER_USER_MEMORY
from project_memory.types import ProjectMemorySource
from storage.atomic import atomic_write_text


def memory_projection_result(
    *,
    status: str,
    reason: str,
    path: Path,
    sha256: str = '',
    source_count: int = 0,
    warnings: list[str] | tuple[str, ...] = (),
    error_detail: str = '',
) -> dict[str, object]:
    return {
        'status': status,
        'reason': reason,
        'path': str(path),
        'sha256': sha256,
        'source_count': source_count,
        'warnings': tuple(str(item) for item in warnings if str(item)),
        'error_detail': str(error_detail or ''),
    }


def record_memory_projection_event(
    result: dict[str, object],
    *,
    provider: str,
    event_path: Path | None,
    marker_path: Path | None,
    agent_name: str | None,
) -> None:
    if event_path is None or marker_path is None or not agent_name:
        return
    provider_name = str(provider or '').strip()
    if not provider_name:
        return
    status = str(result.get('status') or 'unknown')
    reason = str(result.get('reason') or '')
    signature = {
        'status': status,
        'reason': reason,
        'path': str(result.get('path') or ''),
        'sha256': str(result.get('sha256') or ''),
        'warnings': list(result.get('warnings') or ()),
    }
    marker = Path(marker_path)
    if same_memory_projection_signature(marker, signature):
        return
    event = {
        'record_type': 'agent_event',
        'event_type': f'{provider_name}_memory_projection_{status}',
        'provider': provider_name,
        'agent_name': agent_name,
        'status': status,
        'reason': reason,
        'projection_path': signature['path'],
        'sha256': signature['sha256'],
        'source_count': int(result.get('source_count') or 0),
        'warnings': signature['warnings'],
        'error_detail': str(result.get('error_detail') or ''),
        'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    write_projection_event_and_marker(event, signature, event_path=event_path, marker_path=marker)


def write_projection_event_and_marker(
    event: dict[str, object],
    signature: dict[str, object],
    *,
    event_path: Path,
    marker_path: Path,
) -> None:
    try:
        target = Path(event_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        marker = Path(marker_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(signature, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
    except OSError:
        return


def materialize_provider_memory_file(
    *,
    project_root: Path,
    agent_name: str,
    provider: str,
    target: Path,
    provider_memory_path: Path,
    provider_memory_title: str,
    workspace_path: Path | None,
) -> dict[str, object]:
    root = Path(project_root).expanduser()
    try:
        warnings, sources = _provider_memory_sources(
            root,
            agent_name=agent_name,
            provider=provider,
            provider_memory_path=provider_memory_path,
            provider_memory_title=provider_memory_title,
        )
        rendered = render_memory_bundle(
            project_root=root,
            agent_name=agent_name,
            provider=provider,
            sources=sources,
            workspace_path=workspace_path,
        )
        digest = sha256_text(rendered)
        if text_file_sha256(target) == digest:
            return memory_projection_result(
                status='skipped',
                reason='unchanged',
                path=target,
                sha256=digest,
                source_count=len(sources),
                warnings=warnings,
            )
        atomic_write_text(target, rendered)
        return memory_projection_result(
            status='ok',
            reason='written',
            path=target,
            sha256=digest,
            source_count=len(sources),
            warnings=warnings,
        )
    except Exception as exc:
        return memory_projection_result(
            status='failed',
            reason=type(exc).__name__,
            path=target,
            error_detail=str(exc),
        )


def _provider_memory_sources(
    project_root: Path,
    *,
    agent_name: str,
    provider: str,
    provider_memory_path: Path,
    provider_memory_title: str,
) -> tuple[list[str], tuple[ProjectMemorySource, ...]]:
    warnings: list[str] = []
    ensure_result = ensure_project_memory(project_root)
    if ensure_result.warning:
        warnings.append(ensure_result.warning)
    extra_sources = tuple(
        source
        for source in (
            read_memory_source(
                kind=SOURCE_PROVIDER_USER_MEMORY,
                title=provider_memory_title,
                path=provider_memory_path,
                include_missing=False,
            ),
        )
        if source is not None
    )
    sources = load_memory_sources(
        project_root,
        agent_name=agent_name,
        provider=provider,
        extra_sources=extra_sources,
    )
    warnings.extend(source.warning for source in sources if source.warning)
    return warnings, sources


def same_memory_projection_signature(path: Path, payload: dict[str, object]) -> bool:
    try:
        existing = json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return False
    if not isinstance(existing, dict):
        return False
    if existing == payload:
        return True
    if payload.get('status') == 'skipped' and payload.get('reason') == 'unchanged':
        return (
            bool(payload.get('sha256'))
            and existing.get('path') == payload.get('path')
            and existing.get('sha256') == payload.get('sha256')
            and existing.get('warnings') == payload.get('warnings')
        )
    if payload.get('status') == 'skipped':
        return (
            existing.get('reason') == payload.get('reason')
            and existing.get('path') == payload.get('path')
            and existing.get('sha256') == payload.get('sha256')
            and existing.get('warnings') == payload.get('warnings')
        )
    return False


def text_file_sha256(path: Path) -> str:
    try:
        return sha256_text(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return ''


__all__ = [
    'memory_projection_result',
    'materialize_provider_memory_file',
    'record_memory_projection_event',
    'same_memory_projection_signature',
    'text_file_sha256',
    'write_projection_event_and_marker',
]
