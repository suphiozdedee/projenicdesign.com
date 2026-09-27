from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Any


# Runtime snapshot/usage field semantics align with Paseo at pinned commit
# b599d38a772f621e0001abfb90a769de11c8cd8b. See mobile/THIRD_PARTY_NOTICES.md.
_TAIL_MAX_BYTES = 2 * 1024 * 1024
_TAIL_MAX_LINES = 2_000


@dataclass(frozen=True)
class ProviderSessionUsage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    context_window_max_tokens: int | None = None
    context_window_used_tokens: int | None = None
    scope: str = 'current_session_tail'

    def to_record(self) -> dict[str, object]:
        return {
            'input_tokens': self.input_tokens,
            'cached_input_tokens': self.cached_input_tokens,
            'output_tokens': self.output_tokens,
            'reasoning_output_tokens': self.reasoning_output_tokens,
            'total_tokens': self.total_tokens,
            'context_window_max_tokens': self.context_window_max_tokens,
            'context_window_used_tokens': self.context_window_used_tokens,
            'scope': self.scope,
        }


@dataclass(frozen=True)
class ProviderRuntimeSnapshot:
    provider: str
    session_id: str | None = None
    active_model: str | None = None
    active_thinking: str | None = None
    usage: ProviderSessionUsage | None = None
    source: str = 'unavailable'
    source_revision: str | None = None

    def to_record(self) -> dict[str, object]:
        return {
            'provider': self.provider,
            'session_id': self.session_id,
            'active_model': self.active_model,
            'active_thinking': self.active_thinking,
            'usage': self.usage.to_record() if self.usage is not None else None,
            'source': self.source,
            'source_revision': self.source_revision,
        }


def read_provider_runtime_snapshot(
    provider: object,
    session_path: Path | str | None,
    *,
    fallback_session_id: object = None,
    project_root: Path | str | None = None,
    agent: object = None,
    max_bytes: int = _TAIL_MAX_BYTES,
    max_lines: int = _TAIL_MAX_LINES,
) -> ProviderRuntimeSnapshot:
    provider_name = str(provider or '').strip().lower()
    fallback = _text(fallback_session_id)
    if provider_name not in {'codex', 'claude'}:
        return ProviderRuntimeSnapshot(provider=provider_name, session_id=fallback)
    path = resolve_provider_session_path(
        provider_name,
        session_path,
        project_root=project_root,
        agent=agent,
    )
    if path is None:
        return ProviderRuntimeSnapshot(provider=provider_name, session_id=fallback)
    try:
        stat = path.stat()
    except OSError:
        return ProviderRuntimeSnapshot(provider=provider_name, session_id=fallback)
    return _cached_provider_runtime_snapshot(
        provider_name,
        str(path),
        int(stat.st_mtime_ns),
        int(stat.st_size),
        fallback,
        max(1, int(max_bytes)),
        max(1, int(max_lines)),
    )


def resolve_provider_session_path(
    provider: object,
    session_path: Path | str | None,
    *,
    project_root: Path | str | None = None,
    agent: object = None,
) -> Path | None:
    path = _regular_file(session_path)
    if path is not None:
        return path
    if str(provider or '').strip().lower() != 'claude':
        return None
    root = _path(project_root)
    agent_name = _text(agent)
    if root is None or agent_name is None:
        return None
    try:
        from provider_backends.claude.session_runtime.pathing import (
            find_project_session_file,
            read_json,
        )

        session_file = find_project_session_file(root, agent_name)
    except Exception:
        return None
    if session_file is None:
        return None
    try:
        data = _mapping(read_json(session_file))
    except Exception:
        return None
    explicit = _regular_file(data.get('claude_session_path'))
    if explicit is not None:
        return explicit
    projects_root = _path(data.get('claude_projects_root'))
    work_dir = _path(
        data.get('work_dir')
        or data.get('workspace_path')
        or data.get('project_root')
        or root
    )
    if projects_root is None or work_dir is None:
        return None
    try:
        from provider_backends.claude.comm import ClaudeLogReader

        discovered = ClaudeLogReader(
            root=projects_root,
            work_dir=work_dir,
            use_sessions_index=False,
        ).current_session_path()
    except Exception:
        return None
    return _regular_file(discovered)


@lru_cache(maxsize=128)
def _cached_provider_runtime_snapshot(
    provider_name: str,
    path_text: str,
    mtime_ns: int,
    size: int,
    fallback_session_id: str | None,
    max_bytes: int,
    max_lines: int,
) -> ProviderRuntimeSnapshot:
    path = Path(path_text)
    entries = _read_tail_entries(path, max_bytes=max_bytes, max_lines=max_lines)
    revision = f'{mtime_ns}:{size}'
    if provider_name == 'codex':
        return _codex_snapshot(
            entries,
            fallback_session_id=fallback_session_id,
            source_revision=revision,
        )
    return _claude_snapshot(
        entries,
        fallback_session_id=fallback_session_id,
        source_revision=revision,
    )


def _codex_snapshot(
    entries: list[dict[str, Any]],
    *,
    fallback_session_id: str | None,
    source_revision: str | None,
) -> ProviderRuntimeSnapshot:
    session_id = fallback_session_id
    model = None
    thinking = None
    latest_usage: dict[str, Any] | None = None
    context_window = None
    for entry in entries:
        entry_type = _text(entry.get('type'))
        payload = _mapping(entry.get('payload'))
        if entry_type == 'session_meta':
            session_id = _text(payload.get('id')) or _text(payload.get('session_id')) or session_id
            context_window = _integer(payload.get('context_window')) or context_window
        elif entry_type == 'turn_context':
            model = _text(payload.get('model')) or model
            thinking = _text(payload.get('effort')) or thinking
            context_window = _integer(payload.get('context_window')) or context_window
        elif entry_type == 'event_msg' and _text(payload.get('type')) == 'token_count':
            info = _mapping(payload.get('info'))
            candidate = _mapping(info.get('total_token_usage'))
            if candidate:
                latest_usage = candidate
            context_window = _integer(info.get('model_context_window')) or context_window
    usage = None
    if latest_usage is not None or context_window is not None:
        total = _integer((latest_usage or {}).get('total_tokens'))
        usage = ProviderSessionUsage(
            input_tokens=_integer((latest_usage or {}).get('input_tokens')),
            cached_input_tokens=_integer((latest_usage or {}).get('cached_input_tokens')),
            output_tokens=_integer((latest_usage or {}).get('output_tokens')),
            reasoning_output_tokens=_integer((latest_usage or {}).get('reasoning_output_tokens')),
            total_tokens=total,
            context_window_max_tokens=context_window,
            context_window_used_tokens=total,
        )
    return ProviderRuntimeSnapshot(
        provider='codex',
        session_id=session_id,
        active_model=model,
        active_thinking=thinking,
        usage=usage,
        source='provider_native/codex',
        source_revision=source_revision,
    )


def _claude_snapshot(
    entries: list[dict[str, Any]],
    *,
    fallback_session_id: str | None,
    source_revision: str | None,
) -> ProviderRuntimeSnapshot:
    session_id = fallback_session_id
    model = None
    # Claude repeats the same message while streaming. Keep only the latest
    # usage record for each message identity before summing the bounded tail.
    usage_by_message: dict[str, dict[str, Any]] = {}
    latest_usage: dict[str, Any] | None = None
    for index, entry in enumerate(entries):
        entry_type = _text(entry.get('type'))
        session_id = (
            _text(entry.get('sessionId'))
            or _text(entry.get('session_id'))
            or session_id
        )
        if entry_type != 'assistant':
            continue
        message = _mapping(entry.get('message'))
        model = _text(message.get('model')) or model
        candidate = _mapping(message.get('usage'))
        if not candidate:
            continue
        identity = (
            _text(message.get('id'))
            or _text(entry.get('uuid'))
            or _text(entry.get('messageId'))
            or f'row-{index}'
        )
        usage_by_message[identity] = candidate
        latest_usage = candidate
    usage = _sum_claude_usage(usage_by_message.values(), latest_usage=latest_usage)
    return ProviderRuntimeSnapshot(
        provider='claude',
        session_id=session_id,
        active_model=model,
        usage=usage,
        source='provider_native/claude',
        source_revision=source_revision,
    )


def _sum_claude_usage(
    records,
    *,
    latest_usage: dict[str, Any] | None,
) -> ProviderSessionUsage | None:
    rows = tuple(records)
    if not rows:
        return None
    input_tokens = sum(_integer(row.get('input_tokens')) or 0 for row in rows)
    cached = sum(
        (_integer(row.get('cache_read_input_tokens')) or 0)
        + (_integer(row.get('cache_creation_input_tokens')) or 0)
        for row in rows
    )
    output_tokens = sum(_integer(row.get('output_tokens')) or 0 for row in rows)
    total = input_tokens + cached + output_tokens
    latest = latest_usage or {}
    context_used = (
        (_integer(latest.get('input_tokens')) or 0)
        + (_integer(latest.get('cache_read_input_tokens')) or 0)
        + (_integer(latest.get('cache_creation_input_tokens')) or 0)
        + (_integer(latest.get('output_tokens')) or 0)
    )
    return ProviderSessionUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output_tokens,
        total_tokens=total,
        context_window_used_tokens=context_used or None,
    )


def _read_tail_entries(path: Path, *, max_bytes: int, max_lines: int) -> list[dict[str, Any]]:
    try:
        size = path.stat().st_size
        with path.open('rb') as handle:
            start = max(0, size - max(1, int(max_bytes)))
            handle.seek(start)
            data = handle.read(max(1, int(max_bytes)))
    except OSError:
        return []
    lines = data.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    result: list[dict[str, Any]] = []
    for raw in lines[-max(1, int(max_lines)) :]:
        try:
            value = json.loads(raw.decode('utf-8', errors='replace'))
        except (UnicodeDecodeError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _regular_file(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def _path(value: object) -> Path | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return Path(text).expanduser()
    except (OSError, TypeError, ValueError):
        return None


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def _integer(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


__all__ = [
    'ProviderRuntimeSnapshot',
    'ProviderSessionUsage',
    'read_provider_runtime_snapshot',
    'resolve_provider_session_path',
]
