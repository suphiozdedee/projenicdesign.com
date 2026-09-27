from __future__ import annotations

import shlex

from ccbd.api_models import JobRecord
from completion.models import CompletionSourceKind
from provider_core.protocol import request_anchor_for_job
from provider_execution.base import ProviderRuntimeContext, ProviderSubmission
from provider_execution.common import error_submission, send_prompt_to_runtime_target

from ..comm import AgyPaneReader, agy_pane_ready_for_input
from ..native_log import agy_home_from_start_cmd
from ..protocol import wrap_agy_prompt
from ..session import load_project_session
from .helpers import resolve_work_dir


_PANE_LINES_DEFAULT = 2000
_AUTO_PERMISSION_FLAG = '--dangerously-skip-permissions'


def start_submission(
    job: JobRecord,
    *,
    context: ProviderRuntimeContext | None,
    now: str,
    provider: str,
) -> ProviderSubmission:
    work_dir = resolve_work_dir(job, context)
    if work_dir is None:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.SESSION_EVENT_LOG,
            reason='runtime_unavailable',
            error='work_dir_missing',
        )

    instance = (job.agent_name or '').strip().lower() or None
    session = None
    load_error: str | None = None
    try:
        session = load_project_session(work_dir, instance=instance)
        if session is None and instance is not None:
            session = load_project_session(work_dir)
    except Exception as exc:
        load_error = f'load_session_failed:{exc!r}'

    if session is None:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.SESSION_EVENT_LOG,
            reason='runtime_unavailable',
            error=load_error or 'agy_session_file_missing',
        )

    pane_id = session.pane_id
    if not pane_id:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.SESSION_EVENT_LOG,
            reason='pane_unavailable',
            error='pane_id_missing_in_session',
        )

    try:
        backend = session.backend()
    except Exception as exc:
        backend = None
        backend_error = f'backend_resolve_failed:{exc!r}'
    else:
        backend_error = None

    if backend is None:
        return error_submission(
            job,
            provider=provider,
            now=now,
            source_kind=CompletionSourceKind.SESSION_EVENT_LOG,
            reason='backend_unavailable',
            error=backend_error or 'terminal_backend_unavailable',
        )

    req_id = request_anchor_for_job(job.job_id)
    prompt = wrap_agy_prompt(job.request.body or '', req_id)

    reader = AgyPaneReader(backend=backend, pane_id=pane_id, lines=_PANE_LINES_DEFAULT)
    initial_content = reader.snapshot()
    prompt_deferred_until_ready = not agy_pane_ready_for_input(initial_content)
    session_data = getattr(session, 'data', {})
    ccb_auto_permission = bool(
        session_data.get('agy_auto_permission') if isinstance(session_data, dict) else False
    )
    trust_confirmation_allowed = ccb_auto_permission and _trust_confirmation_allowed(session.start_cmd)
    trust_authority = (
        'ccb_auto_permission_and_start_cmd_dangerously_skip_permissions'
        if trust_confirmation_allowed
        else 'none'
    )

    send_error: str | None = None
    prompt_sent = False
    if not prompt_deferred_until_ready:
        try:
            send_prompt_to_runtime_target(backend, pane_id, prompt)
            prompt_sent = True
        except Exception as exc:
            send_error = f'send_text_failed:{exc!r}'

    diagnostics: dict[str, object] = {
        'provider': provider,
        'mode': 'native_transcript_log',
        'pane_id': pane_id,
        'req_id': req_id,
        'task_id': job.request.task_id,
        'workspace_path': str(work_dir),
        'agy_project_trust_confirmation_allowed': trust_confirmation_allowed,
        'agy_project_trust_authority': trust_authority,
    }
    if send_error:
        diagnostics['send_error'] = send_error
    if prompt_deferred_until_ready:
        diagnostics['prompt_deferred_until_ready'] = True

    return ProviderSubmission(
        job_id=job.job_id,
        agent_name=job.agent_name,
        provider=provider,
        accepted_at=now,
        ready_at=now,
        source_kind=CompletionSourceKind.SESSION_EVENT_LOG,
        reply='',
        diagnostics=diagnostics,
        runtime_state={
            'mode': 'native_transcript_log',
            'reader': reader,
            'backend': backend,
            'pane_id': pane_id,
            'req_id': req_id,
            'request_anchor': req_id,
            'work_dir': str(work_dir),
            'runtime_dir': str(session.runtime_dir),
            'agy_home': str(agy_home_from_start_cmd(session.start_cmd) or ''),
            'pane_lines': _PANE_LINES_DEFAULT,
            'started_at': now,
            'last_hash': None,
            'last_change_at': now,
            'last_poll_at': now,
            'prompt_sent': prompt_sent,
            'pending_prompt': prompt,
            'prompt_deferred_until_ready': prompt_deferred_until_ready,
            'agy_project_trust_confirmation_allowed': trust_confirmation_allowed,
            'agy_project_trust_authority': trust_authority,
            'send_error': send_error,
            'snapshot_errors': 0,
            'next_seq': 1,
            'anchor_emitted': False,
            'reply_buffer': '',
            'last_reply_signature': '',
            'turn_boundary_ref': '',
            'session_path': '',
        },
    )


def _trust_confirmation_allowed(start_cmd: str) -> bool:
    try:
        parts = shlex.split(str(start_cmd or ''))
    except ValueError:
        return False
    return _AUTO_PERMISSION_FLAG in parts


__all__ = ['start_submission']
