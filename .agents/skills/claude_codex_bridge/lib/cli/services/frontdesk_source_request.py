from __future__ import annotations

from collections.abc import Mapping
import hashlib

from storage.text_artifacts import read_text_artifact


def resolve_frontdesk_source_request(context, *, source_job_id: str, job) -> dict[str, object]:
    source_job_id = str(source_job_id or '').strip()
    agent_name = str(_field(job, 'agent_name') or '')
    request = _field(job, 'request')
    if request is None:
        return {
            'status': 'blocked',
            'reason': 'frontdesk_source_job_request_missing',
            'source_job_id': source_job_id,
            'agent_name': agent_name,
        }
    identity = {
        'source_job_id': source_job_id,
        'agent_name': agent_name,
        'project_id': str(_field(request, 'project_id') or ''),
        'to_agent': str(_field(request, 'to_agent') or ''),
        'from_actor': str(_field(request, 'from_actor') or ''),
        'message_type': str(_field(request, 'message_type') or ''),
    }
    if (
        not source_job_id
        or identity['agent_name'] != 'frontdesk'
        or identity['project_id'] != str(context.project.project_id)
        or identity['to_agent'] != 'frontdesk'
        or identity['message_type'] != 'ask'
    ):
        return {
            'status': 'blocked',
            'reason': 'frontdesk_source_job_identity_mismatch',
            **identity,
        }
    artifact = _field(request, 'body_artifact')
    if artifact is not None and not isinstance(artifact, Mapping):
        return {
            'status': 'blocked',
            'reason': 'frontdesk_source_request_artifact_invalid',
            'error': 'body_artifact must be an object',
            **identity,
        }
    try:
        body = (
            read_text_artifact(context.paths, dict(artifact))
            if artifact
            else str(_field(request, 'body') or '')
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return {
            'status': 'blocked',
            'reason': 'frontdesk_source_request_artifact_invalid',
            'error': str(exc),
            **identity,
        }
    body = strip_ccb_reply_guidance(body)
    if not body.strip():
        return {
            'status': 'blocked',
            'reason': 'frontdesk_source_request_empty',
            **identity,
        }
    data = body.encode('utf-8')
    return {
        'status': 'ok',
        'text': body,
        'bytes': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
        'preview': body.strip()[:400],
        'body_artifact': compact_text_artifact(artifact),
        **identity,
    }


def strip_ccb_reply_guidance(body: str) -> str:
    text = str(body or '')
    indexes = [
        index
        for marker in ('\n\nCCB_REPLY_MODE:', '\n\nCCB reply guidance:')
        if (index := text.find(marker)) >= 0
    ]
    return text[: min(indexes)] if indexes else text


def compact_text_artifact(value) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        key: value[key]
        for key in ('schema_version', 'kind', 'artifact_id', 'path', 'bytes', 'sha256', 'encoding')
        if key in value
    }


def _field(value, name: str):
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


__all__ = ['resolve_frontdesk_source_request']
