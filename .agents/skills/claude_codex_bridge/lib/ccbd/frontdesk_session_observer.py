from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re

from cli.services.role_output_import import frontdesk_intake_missing_fields
from storage.atomic import atomic_write_json

from .frontdesk_handler import build_frontdesk_forward_planner_handler


_REQUEST_ID_RE = re.compile(
    r'(?mi)^\s*CCB_REQ_ID\s*:\s*`?([A-Za-z0-9][A-Za-z0-9_-]{0,79})`?\s*$'
)


def observe_frontdesk_session(app) -> dict[str, object] | None:
    runtime = _frontdesk_runtime(app)
    if runtime is None:
        return None
    if str(getattr(runtime, 'provider', '') or '').strip().lower() != 'codex':
        return None
    session_path = _codex_session_path(runtime)
    if session_path is None:
        return _record_state(
            app,
            {
                'status': 'blocked',
                'reason': 'frontdesk_codex_session_path_missing',
                'agent_name': 'frontdesk',
            },
        )
    latest = _latest_task_complete(session_path)
    if latest is None:
        return None
    turn_id = str(latest.get('turn_id') or '').strip()
    if not turn_id:
        return None
    state = _load_state(app)
    if str(state.get('last_observed_turn_id') or state.get('last_turn_id') or state.get('turn_id') or '') == turn_id:
        return None
    reply = str(latest.get('last_agent_message') or '')
    request = _request_context_for_turn(session_path, turn_id)
    request_id = str(request['request_id'])
    reply = _normalize_intake_request_id(reply, request_id)
    missing = frontdesk_intake_missing_fields(reply)
    if missing:
        if _has_successful_handoff(state):
            return _record_state(
                app,
                {
                    'status': state.get('status'),
                    'reason': state.get('reason', ''),
                    'agent_name': 'frontdesk',
                    'last_turn_id': state.get('last_turn_id') or state.get('turn_id'),
                    'turn_id': state.get('turn_id') or state.get('last_turn_id'),
                    'last_observed_turn_id': turn_id,
                    'session_path': state.get('session_path'),
                    'frontdesk_intake': state.get('frontdesk_intake'),
                    'last_ignored': {
                        'turn_id': turn_id,
                        'reason': 'frontdesk_reply_not_intake_evidence',
                        'missing_fields': missing,
                    },
                },
            )
        return _record_state(
            app,
            {
                'status': 'ignored',
                'reason': 'frontdesk_reply_not_intake_evidence',
                'agent_name': 'frontdesk',
                'last_turn_id': turn_id,
                'turn_id': turn_id,
                'last_observed_turn_id': turn_id,
                'missing_fields': missing,
            },
        )
    handler = build_frontdesk_forward_planner_handler(
        app.dispatcher,
        start_auto_runner=getattr(app, 'frontdesk_observer_start_auto_runner', None),
    )
    payload = handler(
        {
            'intake_base64': base64.b64encode(reply.encode('utf-8')).decode('ascii'),
            'request_id': request_id,
            'source_job_id': request.get('source_job_id'),
            'json_output': True,
        }
    )
    status = 'ok' if str(payload.get('frontdesk_intake_status') or '') == 'ok' else 'blocked'
    return _record_state(
        app,
        {
            'status': status,
            'reason': str(payload.get('reason') or ''),
            'agent_name': 'frontdesk',
            'last_turn_id': turn_id,
            'turn_id': turn_id,
            'last_observed_turn_id': turn_id,
            'session_path': str(session_path),
            'request_id': request_id,
            'request_id_source': request['source'],
            'source_job_id': request.get('source_job_id'),
            'user_message_sha256': request.get('user_message_sha256'),
            'frontdesk_intake': _compact_intake_payload(payload),
        },
    )


def _frontdesk_runtime(app):
    registry = getattr(app, 'registry', None)
    get = getattr(registry, 'get', None)
    if not callable(get):
        return None
    try:
        return get('frontdesk')
    except Exception:
        return None


def _codex_session_path(runtime) -> Path | None:
    session_file = _optional_path(getattr(runtime, 'session_file', None))
    if session_file is None:
        return None
    try:
        payload = json.loads(session_file.read_text(encoding='utf-8'))
    except Exception:
        return None
    session_path = _optional_path(payload.get('codex_session_path'))
    if session_path is not None and session_path.is_file():
        return session_path
    return None


def _latest_task_complete(session_path: Path) -> dict[str, object] | None:
    latest: dict[str, object] | None = None
    try:
        lines = session_path.read_text(encoding='utf-8').splitlines()
    except Exception:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except Exception:
            continue
        if str(record.get('type') or '') != 'event_msg':
            continue
        payload = record.get('payload')
        if not isinstance(payload, dict):
            continue
        if str(payload.get('type') or '') != 'task_complete':
            continue
        latest = payload
    return latest


def _request_context_for_turn(session_path: Path, turn_id: str) -> dict[str, object]:
    user_messages: list[str] = []
    try:
        lines = session_path.read_text(encoding='utf-8').splitlines()
    except Exception:
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except Exception:
            continue
        if str(record.get('type') or '') != 'response_item':
            continue
        payload = record.get('payload')
        if not isinstance(payload, dict) or str(payload.get('type') or '') != 'message':
            continue
        if str(payload.get('role') or '') != 'user':
            continue
        metadata = payload.get('internal_chat_message_metadata_passthrough')
        if not isinstance(metadata, dict) or str(metadata.get('turn_id') or '') != turn_id:
            continue
        content = payload.get('content')
        if not isinstance(content, list):
            continue
        parts = []
        for item in content:
            if not isinstance(item, dict) or str(item.get('type') or '') != 'input_text':
                continue
            text = str(item.get('text') or '')
            if text:
                parts.append(text)
        if parts:
            user_messages.append('\n'.join(parts))

    user_message = '\n'.join(user_messages)
    request_ids: list[str] = []
    for message in user_messages:
        match = re.match(
            r'(?is)^\s*CCB_REQ_ID\s*:\s*`?([A-Za-z0-9][A-Za-z0-9_-]{0,79})`?(?:\s|$)',
            message,
        )
        if match:
            request_ids.append(match.group(1))
    if request_ids:
        request_id = request_ids[-1]
        source = 'current_user_turn'
    else:
        normalized_turn_id = re.sub(r'[^A-Za-z0-9_-]+', '-', turn_id).strip('-_')
        request_id = f'frontdesk-{normalized_turn_id}'
        if len(request_id) > 80:
            request_id = f'frontdesk-{hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:16]}'
        source = 'codex_turn_id'
    return {
        'request_id': request_id,
        'source': source,
        'source_job_id': request_id if source == 'current_user_turn' and request_id.startswith('job_') else None,
        'user_message_sha256': hashlib.sha256(user_message.encode('utf-8')).hexdigest() if user_message else None,
    }


def _normalize_intake_request_id(reply: str, request_id: str) -> str:
    without_ids = _REQUEST_ID_RE.sub('', reply).strip()
    if not without_ids:
        return f'CCB_REQ_ID: {request_id}'
    lines = without_ids.splitlines()
    insert_at = 1 if lines else 0
    lines[insert_at:insert_at] = ['', f'CCB_REQ_ID: {request_id}']
    return '\n'.join(lines).strip()


def _state_path(app) -> Path:
    return Path(app.paths.project_root) / '.ccb' / 'runtime' / 'frontdesk-session-observer' / 'state.json'


def _load_state(app) -> dict[str, object]:
    try:
        payload = json.loads(_state_path(app).read_text(encoding='utf-8'))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _has_successful_handoff(state: dict[str, object]) -> bool:
    intake = state.get('frontdesk_intake')
    if not isinstance(intake, dict):
        return False
    return str(state.get('status') or '') == 'ok' and str(intake.get('frontdesk_intake_status') or '') == 'ok'


def _record_state(app, payload: dict[str, object]) -> dict[str, object]:
    record = {
        'schema_version': 1,
        'record_type': 'ccb_frontdesk_session_observer',
        'recorded_at': app.clock() if callable(getattr(app, 'clock', None)) else None,
        **payload,
    }
    atomic_write_json(_state_path(app), record)
    return record


def _compact_intake_payload(payload: dict[str, object]) -> dict[str, object]:
    return {
        'frontdesk_intake_status': payload.get('frontdesk_intake_status'),
        'action': payload.get('action'),
        'reason': payload.get('reason'),
        'plan_slug': payload.get('plan_slug'),
        'request_id': payload.get('request_id'),
        'activation_id': payload.get('activation_id'),
        'activation_path': payload.get('activation_path'),
        'planner_job_id': payload.get('planner_job_id'),
        'silence': payload.get('silence'),
    }


def _optional_path(value) -> Path | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return Path(text).expanduser()
    except Exception:
        return None


__all__ = ['observe_frontdesk_session']
