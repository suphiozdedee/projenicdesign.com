from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from storage.atomic import atomic_write_json, atomic_write_text

from .plan_tasks import plan_task

_QUESTION_SCHEMA_VERSION = 1
_QUESTION_RECORD_TYPE = 'ccb_question_index'
_CANDIDATE_KIND = 'candidate_questions'
_USER_KIND = 'user_questions'
_RAW_KIND = 'raw_answer'
_NORMALIZED_KIND = 'normalized_answers'
_IMPORT_ACTIONS = {
    'candidate-import': _CANDIDATE_KIND,
    'user-batch-import': _USER_KIND,
    'answer-import': _RAW_KIND,
    'normalized-import': _NORMALIZED_KIND,
}
_DEST_NAMES = {
    _CANDIDATE_KIND: 'candidate-questions.jsonl',
    _NORMALIZED_KIND: 'normalized-answers.jsonl',
}
_NORMALIZED_SOURCES = frozenset({'user', 'default', 'deferred'})


def question_command(context, command) -> dict[str, object]:
    action = str(command.action or '').strip().lower()
    if action == 'status':
        return _question_status(context, command)
    if action in _IMPORT_ACTIONS:
        return _question_import(context, command, kind=_IMPORT_ACTIONS[action])
    raise ValueError(f'unsupported question action: {action}')


def question_refs(context, task_id: object) -> dict[str, object]:
    payload = _question_status(context, SimpleNamespace(action='status', task_id=task_id))
    return {
        'question_status': payload.get('question_status'),
        'task_id': payload.get('task_id'),
        'question_root': payload.get('question_root'),
        'artifact_count': payload.get('artifact_count'),
        'artifacts': payload.get('artifacts'),
        'latest': payload.get('latest'),
        'next_owner': _next_owner_from_status(payload),
    }


def _question_import(context, command, *, kind: str) -> dict[str, object]:
    task_payload = _task_payload(context, command.task_id)
    record = task_payload['task'] if isinstance(task_payload.get('task'), dict) else {}
    task_id = str(task_payload.get('task_id') or '')
    source_path = _safe_project_file(Path(context.project.project_root), command.file_path)
    text = _read_utf8_artifact(source_path)
    validation = _validate_import_text(kind=kind, text=text, source_path=source_path, task_id=task_id)
    question_root = _question_root(context, record)
    question_root.mkdir(parents=True, exist_ok=True)
    dest = question_root / _dest_name(kind=kind, source_path=source_path)
    atomic_write_text(dest, text)
    encoded = text.encode('utf-8')
    now = _utc_now()
    artifact = {
        'kind': kind,
        'path': str(dest.relative_to(context.project.project_root)),
        'source_path': str(source_path.relative_to(context.project.project_root)),
        'sha256': hashlib.sha256(encoded).hexdigest(),
        'bytes': len(encoded),
        'imported_at': now,
        'actor': _artifact_actor_metadata(context, command),
    }
    artifact.update(validation)
    index = _load_question_index(question_root, task_id=task_id)
    artifacts = dict(index.get('artifacts') or {})
    idempotent = isinstance(artifacts.get(kind), dict) and artifacts[kind].get('sha256') == artifact['sha256']
    artifacts[kind] = artifact
    imports = list(index.get('imports') or ())
    imports.append({
        'kind': kind,
        'path': artifact['path'],
        'sha256': artifact['sha256'],
        'imported_at': now,
        'source_path': artifact['source_path'],
    })
    index['artifacts'] = artifacts
    index['imports'] = imports
    index['updated_at'] = now
    atomic_write_json(_index_path(question_root), index)
    status_update = _maybe_update_task_for_questions(context, task_payload, kind=kind)
    return _payload(
        context,
        task_payload,
        action=str(command.action or ''),
        question_root=question_root,
        index=index,
        artifact=artifact,
        idempotent=idempotent,
        status_update=status_update,
    )


def _question_status(context, command) -> dict[str, object]:
    task_payload = _task_payload(context, command.task_id)
    record = task_payload['task'] if isinstance(task_payload.get('task'), dict) else {}
    question_root = _question_root(context, record)
    index = _load_question_index(question_root, task_id=str(task_payload.get('task_id') or ''))
    return _payload(
        context,
        task_payload,
        action='status',
        question_root=question_root,
        index=index,
    )


def _task_payload(context, task_id: object) -> dict[str, object]:
    return plan_task(context, SimpleNamespace(action='task-show', task_id=task_id))


def _maybe_update_task_for_questions(context, task_payload: dict[str, object], *, kind: str) -> dict[str, object]:
    current = str(task_payload.get('status') or '').strip().lower()
    task_id = str(task_payload.get('task_id') or '')
    if kind == _NORMALIZED_KIND:
        if current != 'needs_clarification':
            return {'status': 'skipped', 'reason': f'current_status_{current or "unknown"}'}
        updated = plan_task(
            context,
            SimpleNamespace(action='task-status', task_id=task_id, status='draft'),
        )
        return {
            'status': 'updated',
            'from': current,
            'to': updated.get('status'),
            'task_id': task_id,
            'reason': 'normalized_answers_imported',
        }
    if kind != _USER_KIND:
        return {'status': 'unchanged', 'reason': 'not_user_questions'}
    if current == 'needs_clarification':
        return {'status': 'unchanged', 'reason': 'already_needs_clarification'}
    if current != 'draft':
        return {'status': 'skipped', 'reason': f'current_status_{current or "unknown"}'}
    updated = plan_task(
        context,
        SimpleNamespace(action='task-status', task_id=task_id, status='needs_clarification'),
    )
    return {
        'status': 'updated',
        'from': current,
        'to': updated.get('status'),
        'task_id': task_id,
    }


def _payload(
    context,
    task_payload: dict[str, object],
    *,
    action: str,
    question_root: Path,
    index: dict[str, object],
    artifact: dict[str, object] | None = None,
    idempotent: bool | None = None,
    status_update: dict[str, object] | None = None,
) -> dict[str, object]:
    artifacts = index.get('artifacts') if isinstance(index.get('artifacts'), dict) else {}
    payload: dict[str, object] = {
        'schema_version': 1,
        'record_type': 'ccb_question_command',
        'question_status': 'ok',
        'action': action,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'task_id': task_payload.get('task_id'),
        'task_status': task_payload.get('status'),
        'question_root': str(question_root.relative_to(context.project.project_root)),
        'index_path': str(_index_path(question_root).relative_to(context.project.project_root)),
        'artifact_count': len(artifacts),
        'artifacts': artifacts,
        'latest': _latest_refs(artifacts),
    }
    if artifact is not None:
        payload['artifact'] = artifact
    if idempotent is not None:
        payload['idempotent'] = idempotent
    if status_update is not None:
        payload['status_update'] = status_update
        if status_update.get('status') == 'updated':
            payload['task_status'] = status_update.get('to')
    return payload


def _latest_refs(artifacts: object) -> dict[str, object]:
    if not isinstance(artifacts, dict):
        return {}
    refs: dict[str, object] = {}
    for kind, artifact in sorted(artifacts.items()):
        if not isinstance(artifact, dict):
            continue
        refs[str(kind)] = {
            'path': artifact.get('path'),
            'sha256': artifact.get('sha256'),
            'bytes': artifact.get('bytes'),
            'imported_at': artifact.get('imported_at'),
            'count': artifact.get('question_count') or artifact.get('answer_count'),
            'batch_id': artifact.get('batch_id'),
        }
    return refs


def _next_owner_from_status(payload: dict[str, object]) -> str:
    artifacts = payload.get('artifacts') if isinstance(payload.get('artifacts'), dict) else {}
    if _USER_KIND in artifacts and _NORMALIZED_KIND not in artifacts:
        return 'task_detailer'
    if _NORMALIZED_KIND in artifacts:
        return 'planner'
    if _CANDIDATE_KIND in artifacts:
        return 'clarification_broker'
    return 'planner'


def _validate_import_text(*, kind: str, text: str, source_path: Path, task_id: str) -> dict[str, object]:
    if kind == _CANDIDATE_KIND:
        rows = _read_jsonl_objects(text, label='candidate questions')
        _validate_unique_ids(rows, key='id', label='candidate question')
        for row in rows:
            _require_text(row, 'id', label='candidate question')
            _require_text(row, 'stage', label='candidate question')
            _require_text(row, 'question', label='candidate question')
            _require_text(row, 'why_blocking', label='candidate question')
            if not isinstance(row.get('defer_allowed'), bool):
                raise ValueError('candidate question field defer_allowed must be boolean')
            default = row.get('default_if_unanswered')
            if default is not None and not isinstance(default, str):
                raise ValueError('candidate question field default_if_unanswered must be string when present')
        return {
            'format': 'jsonl',
            'question_count': len(rows),
            'blocking_count': sum(1 for row in rows if str(row.get('why_blocking') or '').strip()),
            'defer_allowed_count': sum(1 for row in rows if bool(row.get('defer_allowed'))),
        }
    if kind == _NORMALIZED_KIND:
        rows = _read_jsonl_objects(text, label='normalized answers')
        _validate_unique_ids(rows, key='question_id', label='normalized answer')
        for row in rows:
            _require_text(row, 'question_id', label='normalized answer')
            _require_text(row, 'answer', label='normalized answer')
            _require_text(row, 'planner_note', label='normalized answer')
            source = str(row.get('source') or '').strip()
            if source not in _NORMALIZED_SOURCES:
                known = ', '.join(sorted(_NORMALIZED_SOURCES))
                raise ValueError(f'normalized answer source must be one of: {known}')
        return {
            'format': 'jsonl',
            'answer_count': len(rows),
            'sources': sorted({str(row.get('source') or '') for row in rows}),
        }
    if kind == _USER_KIND:
        if source_path.suffix.lower() == '.json':
            payload = _read_json_object_text(text, label='user question batch')
            if payload.get('schema') != 'ccb.workflow.user_questions/v1':
                raise ValueError('user question batch schema must be ccb.workflow.user_questions/v1')
            if str(payload.get('task_id') or '') != task_id:
                raise ValueError(f'user question batch task_id must match {task_id}')
            batch_id = _require_text(payload, 'batch_id', label='user question batch')
            questions = payload.get('questions')
            if not isinstance(questions, list):
                raise ValueError('user question batch questions must be a list')
            _validate_unique_ids(questions, key='id', label='user question')
            for row in questions:
                if not isinstance(row, dict):
                    raise ValueError('user question batch questions entries must be objects')
                _require_text(row, 'id', label='user question')
                _require_text(row, 'text', label='user question')
                _require_text(row, 'why', label='user question')
                if not isinstance(row.get('required'), bool):
                    raise ValueError('user question field required must be boolean')
            defaults = _optional_list(payload, 'defaults', label='user question batch')
            deferred = _optional_list(payload, 'deferred', label='user question batch')
            return {
                'format': 'json',
                'batch_id': batch_id,
                'question_count': len(questions),
                'required_count': sum(1 for row in questions if isinstance(row, dict) and bool(row.get('required'))),
                'default_count': len(defaults),
                'deferred_count': len(deferred),
            }
        return {'format': 'markdown', 'question_count': None, 'default_count': None, 'deferred_count': None}
    if kind == _RAW_KIND:
        if source_path.suffix.lower() == '.json':
            _read_json_value_text(text, label='raw answer')
            return {'format': 'json'}
        return {'format': 'markdown'}
    raise ValueError(f'unsupported question artifact kind: {kind}')


def _read_jsonl_objects(text: str, *, label: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f'{label} JSONL line {line_number} is invalid JSON') from exc
        if not isinstance(payload, dict):
            raise ValueError(f'{label} JSONL line {line_number} must be an object')
        rows.append(payload)
    if not rows:
        raise ValueError(f'{label} JSONL must contain at least one object')
    return rows


def _read_json_object_text(text: str, *, label: str) -> dict[str, object]:
    payload = _read_json_value_text(text, label=label)
    if not isinstance(payload, dict):
        raise ValueError(f'{label} must be a JSON object')
    return payload


def _read_json_value_text(text: str, *, label: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f'{label} is invalid JSON') from exc


def _validate_unique_ids(rows: Iterable[object], *, key: str, label: str) -> None:
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f'{label} entries must be objects')
        value = str(row.get(key) or '').strip()
        if not value:
            raise ValueError(f'{label} field {key} is required')
        if value in seen:
            raise ValueError(f'duplicate {label} id: {value}')
        seen.add(value)


def _require_text(payload: dict[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} field {key} is required')
    return value.strip()


def _optional_list(payload: dict[str, object], key: str, *, label: str) -> list[object]:
    value = payload.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f'{label} {key} must be a list when present')
    return value


def _question_root(context, record: dict[str, object]) -> Path:
    return Path(context.project.project_root) / str(record.get('task_root') or '') / 'questions'


def _index_path(question_root: Path) -> Path:
    return question_root / 'index.json'


def _load_question_index(question_root: Path, *, task_id: str) -> dict[str, object]:
    path = _index_path(question_root)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {
            'schema_version': _QUESTION_SCHEMA_VERSION,
            'record_type': _QUESTION_RECORD_TYPE,
            'task_id': task_id,
            'updated_at': None,
            'artifacts': {},
            'imports': [],
        }
    except json.JSONDecodeError as exc:
        raise ValueError(f'question index is invalid JSON: {path}') from exc
    if not isinstance(payload, dict) or payload.get('record_type') != _QUESTION_RECORD_TYPE:
        raise ValueError(f'question index is invalid: {path}')
    if str(payload.get('task_id') or '') != task_id:
        raise ValueError(f'question index task_id mismatch: {path}')
    if not isinstance(payload.get('artifacts'), dict):
        raise ValueError(f'question index artifacts must be an object: {path}')
    if not isinstance(payload.get('imports'), list):
        raise ValueError(f'question index imports must be a list: {path}')
    return payload


def _dest_name(*, kind: str, source_path: Path) -> str:
    fixed = _DEST_NAMES.get(kind)
    if fixed:
        return fixed
    suffix = source_path.suffix.lower()
    if suffix == '.json':
        ext = 'json'
    else:
        ext = 'md'
    if kind == _USER_KIND:
        return f'user-questions.{ext}'
    if kind == _RAW_KIND:
        return f'raw-answer.{ext}'
    raise ValueError(f'unsupported question artifact kind: {kind}')


def _safe_project_file(project_root: Path, value: object) -> Path:
    raw = str(value or '').strip()
    if not raw:
        raise ValueError('question import requires --file')
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root / path
    try:
        resolved = path.resolve(strict=True)
        root = project_root.resolve()
    except FileNotFoundError as exc:
        raise ValueError(f'question artifact file not found: {path}') from exc
    if resolved != root and root not in resolved.parents:
        raise ValueError(f'question artifact file must be inside project root: {resolved}')
    if not resolved.is_file():
        raise ValueError(f'question artifact path is not a file: {resolved}')
    return resolved


def _read_utf8_artifact(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError(f'question artifact must be UTF-8 text: {path}') from exc


def _artifact_actor_metadata(context, command, *, default_source: str = 'cli') -> dict[str, object]:
    source = _first_text(
        getattr(command, 'actor_source', None),
        os.environ.get('CCB_ARTIFACT_SOURCE'),
        default_source,
    )
    actor = _first_text(
        getattr(command, 'actor_agent', None),
        getattr(command, 'actor', None),
        os.environ.get('CCB_CALLER_ACTOR'),
        os.environ.get('CCB_ACTOR'),
        os.environ.get('CCB_AGENT_NAME'),
        _actor_from_runtime_dir(context),
    )
    role = _first_text(
        getattr(command, 'actor_role', None),
        os.environ.get('CCB_CALLER_ROLE'),
        os.environ.get('CCB_ACTOR_ROLE'),
    )
    job_id = _first_text(
        getattr(command, 'job_id', None),
        getattr(command, 'request_id', None),
        os.environ.get('CCB_JOB_ID'),
        os.environ.get('CCB_REQ_ID'),
        os.environ.get('CCB_REQUEST_ID'),
    )
    metadata: dict[str, object] = {
        'source': source,
        'actor': actor or 'user',
    }
    if role:
        metadata['role'] = role
    if job_id:
        metadata['job_id'] = job_id
    return metadata


def _actor_from_runtime_dir(context) -> str:
    raw = _first_text(os.environ.get('CCB_CALLER_RUNTIME_DIR'), os.environ.get('CODEX_RUNTIME_DIR'))
    if not raw:
        return ''
    try:
        runtime_dir = Path(raw).expanduser().resolve()
        agents_dir = Path(context.paths.agents_dir).expanduser().resolve()
        relative = runtime_dir.relative_to(agents_dir)
    except Exception:
        return ''
    return str(relative.parts[0]) if relative.parts else ''


def _first_text(*values: object) -> str:
    for value in values:
        text = str(value or '').strip()
        if text:
            return text
    return ''


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


__all__ = ['question_command', 'question_refs']
