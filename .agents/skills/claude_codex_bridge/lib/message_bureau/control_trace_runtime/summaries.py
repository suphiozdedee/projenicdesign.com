from __future__ import annotations

from message_bureau.reply_metadata import (
    reply_heartbeat_silence_seconds,
    reply_last_progress_at,
    reply_notice,
    reply_notice_kind,
)


def submission_summary(submission) -> dict[str, object]:
    return {
        'submission_id': submission.submission_id,
        'from_actor': submission.from_actor,
        'target_scope': submission.target_scope,
        'task_id': submission.task_id,
        'job_ids': list(submission.job_ids),
        'created_at': submission.created_at,
        'updated_at': submission.updated_at,
    }


def message_summary(message) -> dict[str, object]:
    return {
        'message_id': message.message_id,
        'origin_message_id': message.origin_message_id,
        'submission_id': message.submission_id,
        'from_actor': message.from_actor,
        'target_scope': message.target_scope,
        'target_agents': list(message.target_agents),
        'message_class': message.message_class,
        'message_state': message.message_state.value,
        'priority': message.priority,
        'reply_mode': message.reply_policy.get('mode'),
        'expected_reply_count': message.reply_policy.get('expected_reply_count'),
        'silence_on_success': bool(message.reply_policy.get('silence_on_success')),
        'retry_mode': message.retry_policy.get('mode'),
        'created_at': message.created_at,
        'updated_at': message.updated_at,
    }


def attempt_summary(attempt) -> dict[str, object]:
    return {
        'attempt_id': attempt.attempt_id,
        'message_id': attempt.message_id,
        'agent_name': attempt.agent_name,
        'provider': attempt.provider,
        'job_id': attempt.job_id,
        'retry_index': attempt.retry_index,
        'attempt_state': attempt.attempt_state.value,
        'health_snapshot_ref': attempt.health_snapshot_ref,
        'started_at': attempt.started_at,
        'updated_at': attempt.updated_at,
    }


def reply_summary(reply) -> dict[str, object]:
    artifact = _reply_artifact(reply)
    decision_diagnostics = _decision_diagnostics(reply)
    artifact_bytes = _artifact_bytes(artifact)
    terminal_status = reply.terminal_status.value
    artifact_reply_forced = bool(decision_diagnostics.get('artifact_reply_forced'))
    empty_reply_artifact = artifact is not None and artifact_bytes == 0
    return {
        'reply_id': reply.reply_id,
        'message_id': reply.message_id,
        'attempt_id': reply.attempt_id,
        'agent_name': reply.agent_name,
        'terminal_status': terminal_status,
        'reply': reply.reply,
        'reply_preview': preview_text(reply.reply),
        'reply_size': len(reply.reply or ''),
        'reply_artifact': artifact,
        'reply_artifact_path': artifact.get('path') if artifact is not None else None,
        'reply_artifact_bytes': artifact_bytes,
        'reply_artifact_sha256': artifact.get('sha256') if artifact is not None else None,
        'artifact_reply_forced': artifact_reply_forced,
        'empty_reply_artifact': empty_reply_artifact,
        'no_captured_reply': bool(artifact_reply_forced and empty_reply_artifact and terminal_status != 'completed'),
        'notice': reply_notice(reply),
        'notice_kind': reply_notice_kind(reply),
        'last_progress_at': reply_last_progress_at(reply),
        'heartbeat_silence_seconds': reply_heartbeat_silence_seconds(reply),
        'reason': reply.diagnostics.get('reason'),
        'status': reply.diagnostics.get('status'),
        'completion_mode': decision_diagnostics.get('mode'),
        'total_secs': decision_diagnostics.get('total_secs'),
        'anchor_seen': decision_diagnostics.get('anchor_seen'),
        'captured_reply_chars': decision_diagnostics.get('reply_chars'),
        'silence_on_success': bool(reply.diagnostics.get('silence_on_success')),
        'provider_turn_ref': reply.diagnostics.get('provider_turn_ref'),
        'finished_at': reply.finished_at,
    }


def event_summary(service, event) -> dict[str, object]:
    mailbox = service._mailbox_store.load(event.agent_name)
    return {
        'inbound_event_id': event.inbound_event_id,
        'agent_name': event.agent_name,
        'event_type': event.event_type.value,
        'message_id': event.message_id,
        'attempt_id': event.attempt_id,
        'payload_ref': event.payload_ref,
        'priority': event.priority,
        'status': event.status.value,
        'mailbox_state': mailbox.mailbox_state.value if mailbox is not None else None,
        'mailbox_active': bool(mailbox is not None and mailbox.active_inbound_event_id == event.inbound_event_id),
        'created_at': event.created_at,
        'started_at': event.started_at,
        'finished_at': event.finished_at,
    }


def job_summary(service, job_id: str, *, hint_agent: str | None = None) -> dict[str, object] | None:
    job = None
    if hint_agent:
        job = service._job_store.get_latest(hint_agent, job_id)
    if job is None:
        for agent_name in sorted(service._config.agents):
            if agent_name == hint_agent:
                continue
            job = service._job_store.get_latest(agent_name, job_id)
            if job is not None:
                break
    if job is None:
        return None
    summary = {
        'job_id': job.job_id,
        'agent_name': job.agent_name,
        'provider': job.provider,
        'status': job.status.value,
        'submission_id': job.submission_id,
        'created_at': job.created_at,
        'updated_at': job.updated_at,
    }
    _add_kimi_terminal_fields(summary, job)
    return summary


def _add_kimi_terminal_fields(summary: dict[str, object], job) -> None:
    terminal = job.terminal_decision if isinstance(job.terminal_decision, dict) else {}
    reason = str(terminal.get('reason') or '')
    provider = str(getattr(job, 'provider', '') or '').strip().lower()
    if provider != 'kimi' and not reason.startswith('kimi_'):
        return

    diagnostics = terminal.get('diagnostics') if isinstance(terminal.get('diagnostics'), dict) else {}
    _set_present(summary, 'terminal_reason', reason)
    _set_present(summary, 'terminal_confidence', terminal.get('confidence'))
    for key in ('reply_chars', 'total_secs', 'artifact_reply_forced', 'receipt_class'):
        _set_present(summary, key, diagnostics.get(key))


def _set_present(target: dict[str, object], key: str, value: object) -> None:
    if value is None or value == '':
        return
    target[key] = value


def preview_text(value: str, *, limit: int = 120) -> str:
    text = str(value or '').replace('\r', '').replace('\n', '\\n').strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + '...'


def _reply_artifact(reply) -> dict[str, object] | None:
    artifact = getattr(reply, 'reply_artifact', None)
    return dict(artifact) if isinstance(artifact, dict) else None


def _decision_diagnostics(reply) -> dict[str, object]:
    diagnostics = getattr(reply, 'diagnostics', None)
    if not isinstance(diagnostics, dict):
        return {}
    payload = diagnostics.get('decision_diagnostics')
    return dict(payload) if isinstance(payload, dict) else {}


def _artifact_bytes(artifact: dict[str, object] | None) -> int | None:
    if artifact is None:
        return None
    try:
        return int(artifact.get('bytes') or 0)
    except (TypeError, ValueError):
        return None


__all__ = [
    'attempt_summary',
    'event_summary',
    'job_summary',
    'message_summary',
    'reply_summary',
    'submission_summary',
]
