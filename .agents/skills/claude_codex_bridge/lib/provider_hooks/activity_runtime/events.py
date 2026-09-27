from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from storage.atomic import atomic_write_json

SCHEMA_VERSION = 1

ACTIVITY_ACTIVE = 'active'
ACTIVITY_PENDING = 'pending'
ACTIVITY_IDLE = 'idle'
ACTIVITY_FAILED = 'failed'
ACTIVITY_STATES = frozenset({ACTIVITY_ACTIVE, ACTIVITY_PENDING, ACTIVITY_IDLE, ACTIVITY_FAILED})

_STATE_ALIASES = {
    'active': ACTIVITY_ACTIVE,
    'running': ACTIVITY_ACTIVE,
    'tool': ACTIVITY_ACTIVE,
    'working': ACTIVITY_ACTIVE,
    'thinking': ACTIVITY_ACTIVE,
    'pending': ACTIVITY_PENDING,
    'waiting': ACTIVITY_PENDING,
    'blocked': ACTIVITY_PENDING,
    'permission': ACTIVITY_PENDING,
    'idle': ACTIVITY_IDLE,
    'done': ACTIVITY_IDLE,
    'stop': ACTIVITY_IDLE,
    'stopped': ACTIVITY_IDLE,
    'failed': ACTIVITY_FAILED,
    'failure': ACTIVITY_FAILED,
    'error': ACTIVITY_FAILED,
    'errored': ACTIVITY_FAILED,
}


@dataclass(frozen=True)
class ProviderActivityEvidence:
    state: str
    source: str
    reason: str
    updated_at: str
    event_name: str | None = None
    provider_session_id: str | None = None
    provider_turn_id: str | None = None
    model: str | None = None
    diagnostics: dict[str, object] | None = None


def activity_path(runtime_dir: Path | str) -> Path:
    return Path(runtime_dir).expanduser() / 'activity.json'


def normalize_activity_state(value: object) -> str | None:
    token = ''.join(ch for ch in str(value or '').strip().lower() if ch.isalnum() or ch in {'_', '-'})
    if not token:
        return None
    return _STATE_ALIASES.get(token)


def write_activity(
    *,
    provider: str,
    project_id: str,
    agent_name: str,
    runtime_dir: Path | str,
    state: str,
    source: str,
    event_name: str | None = None,
    ccb_session_id: str | None = None,
    pane_id: str | None = None,
    workspace_path: str | Path | None = None,
    provider_session_id: str | None = None,
    provider_turn_id: str | None = None,
    model: str | None = None,
    diagnostics: dict[str, Any] | None = None,
    updated_at: str | None = None,
) -> Path:
    normalized_state = normalize_activity_state(state)
    if normalized_state is None:
        raise ValueError(f'unsupported provider activity state: {state!r}')
    runtime = Path(runtime_dir).expanduser()
    payload = {
        'schema_version': SCHEMA_VERSION,
        'record_type': 'provider_activity',
        'project_id': str(project_id or '').strip(),
        'agent_name': str(agent_name or '').strip(),
        'provider': str(provider or '').strip().lower(),
        'state': normalized_state,
        'source': str(source or '').strip() or 'provider_hook',
        'event_name': _optional_text(event_name),
        'ccb_session_id': _optional_text(ccb_session_id),
        'runtime_dir': str(runtime),
        'pane_id': _optional_text(pane_id),
        'workspace_path': _optional_path_text(workspace_path),
        'provider_session_id': _optional_text(provider_session_id),
        'provider_turn_id': _optional_text(provider_turn_id),
        'model': _optional_text(model),
        'updated_at': updated_at or _utc_now(),
        'diagnostics': _safe_diagnostics(diagnostics),
    }
    path = activity_path(runtime)
    if normalized_state == ACTIVITY_IDLE and _existing_failed_same_identity(path, payload):
        return path
    atomic_write_json(path, payload)
    return path


def load_activity(runtime_dir: Path | str) -> dict[str, Any] | None:
    path = activity_path(runtime_dir)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def read_activity_evidence(
    runtime_dir: Path | str,
    *,
    project_id: str,
    agent_name: str,
    provider: str,
    ccb_session_id: str | None = None,
    provider_session_id: str | None = None,
    pane_id: str | None = None,
    workspace_path: str | Path | None = None,
    now: str | None = None,
    max_future_skew_s: float = 30.0,
) -> ProviderActivityEvidence | None:
    payload = load_activity(runtime_dir)
    if payload is None:
        return None
    if not _matches_identity(
        payload,
        runtime_dir=runtime_dir,
        project_id=project_id,
        agent_name=agent_name,
        provider=provider,
        ccb_session_id=ccb_session_id,
        provider_session_id=provider_session_id,
        pane_id=pane_id,
        workspace_path=workspace_path,
    ):
        return None
    state = normalize_activity_state(payload.get('state'))
    if state is None:
        return None
    updated_at = str(payload.get('updated_at') or '').strip()
    if not _timestamp_usable(updated_at, now=now, max_future_skew_s=max_future_skew_s):
        return None
    source = str(payload.get('source') or '').strip() or 'provider_activity'
    reason = _reason(payload, state=state)
    diagnostics = payload.get('diagnostics')
    return ProviderActivityEvidence(
        state=state,
        source=source,
        reason=reason,
        updated_at=updated_at,
        event_name=_optional_text(payload.get('event_name')),
        provider_session_id=_optional_text(payload.get('provider_session_id')),
        provider_turn_id=_optional_text(payload.get('provider_turn_id')),
        model=_optional_text(payload.get('model')),
        diagnostics=diagnostics if isinstance(diagnostics, dict) else {},
    )


def _matches_identity(
    payload: dict[str, Any],
    *,
    runtime_dir: Path | str,
    project_id: str,
    agent_name: str,
    provider: str,
    ccb_session_id: str | None,
    provider_session_id: str | None,
    pane_id: str | None,
    workspace_path: str | Path | None,
) -> bool:
    if payload.get('schema_version') != SCHEMA_VERSION:
        return False
    if str(payload.get('record_type') or '').strip() != 'provider_activity':
        return False
    if str(payload.get('project_id') or '').strip() != str(project_id or '').strip():
        return False
    if str(payload.get('agent_name') or '').strip() != str(agent_name or '').strip():
        return False
    if str(payload.get('provider') or '').strip().lower() != str(provider or '').strip().lower():
        return False
    if _path_text(payload.get('runtime_dir')) != _path_text(runtime_dir):
        return False
    if not _optional_matches(payload.get('ccb_session_id'), ccb_session_id):
        return False
    if not _optional_matches(payload.get('pane_id'), pane_id):
        return False
    recorded_workspace = _optional_path_text(payload.get('workspace_path'))
    expected_workspace = _optional_path_text(workspace_path)
    if recorded_workspace and expected_workspace and _path_text(recorded_workspace) != _path_text(expected_workspace):
        return False
    return True


def _existing_failed_same_identity(path: Path, next_payload: dict[str, Any]) -> bool:
    try:
        current = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return False
    if not isinstance(current, dict):
        return False
    if normalize_activity_state(current.get('state')) != ACTIVITY_FAILED:
        return False
    for key in ('project_id', 'agent_name', 'provider', 'runtime_dir', 'ccb_session_id', 'provider_session_id', 'pane_id'):
        current_text = str(current.get(key) or '').strip()
        next_text = str(next_payload.get(key) or '').strip()
        if current_text and next_text and current_text != next_text:
            return False
    return True


def _timestamp_usable(updated_at: str, *, now: str | None, max_future_skew_s: float) -> bool:
    observed = _parse_timestamp(updated_at)
    if observed is None:
        return False
    if now is None:
        return True
    current = _parse_timestamp(now)
    if current is None:
        return False
    return (observed - current).total_seconds() <= max_future_skew_s


def _parse_timestamp(value: str) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = f'{text[:-1]}+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _optional_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def _optional_path_text(value: str | Path | object | None) -> str | None:
    text = str(value or '').strip()
    if not text:
        return None
    return str(Path(text).expanduser())


def _path_text(value: object) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    return str(Path(text).expanduser().resolve(strict=False))


def _optional_matches(recorded: object, expected: str | None) -> bool:
    recorded_text = str(recorded or '').strip()
    expected_text = str(expected or '').strip()
    return not recorded_text or not expected_text or recorded_text == expected_text


def _reason(payload: dict[str, Any], *, state: str) -> str:
    diagnostics = payload.get('diagnostics')
    if isinstance(diagnostics, dict):
        reason = str(diagnostics.get('reason') or '').strip()
        if reason:
            return reason
    event_name = str(payload.get('event_name') or '').strip()
    if event_name:
        return f'provider_{event_name}'
    return f'provider_activity_{state}'


def _safe_diagnostics(diagnostics: dict[str, Any] | None) -> dict[str, object]:
    result: dict[str, object] = {}
    if not isinstance(diagnostics, dict):
        return result
    for key, value in diagnostics.items():
        name = str(key or '').strip()
        if not name:
            continue
        lowered = name.lower()
        if any(secret in lowered for secret in ('key', 'token', 'secret', 'password')):
            continue
        if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
            result[name] = value
        elif value is not None:
            result[name] = str(value)[:300]
    return result


__all__ = [
    'ACTIVITY_FAILED',
    'ACTIVITY_IDLE',
    'ACTIVITY_PENDING',
    'ACTIVITY_ACTIVE',
    'ACTIVITY_STATES',
    'SCHEMA_VERSION',
    'ProviderActivityEvidence',
    'activity_path',
    'load_activity',
    'normalize_activity_state',
    'read_activity_evidence',
    'write_activity',
]
