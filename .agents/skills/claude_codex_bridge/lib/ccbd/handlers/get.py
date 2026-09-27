from __future__ import annotations

from completion.models import CompletionConfidence, CompletionDecision, CompletionStatus
from ccbd.services.dispatcher_runtime.visible_reply import visible_reply_for_job


def _resolve_requested_job(dispatcher, payload: dict):
    job_id = payload.get('job_id')
    agent_name = payload.get('agent_name')
    if job_id:
        return dispatcher.get(str(job_id))
    if agent_name:
        return dispatcher.latest_for_agent(str(agent_name))
    raise ValueError('get requires job_id or agent_name')


def _terminal_decision_from_job(job) -> CompletionDecision | None:
    record = job.terminal_decision
    if not isinstance(record, dict):
        return None
    try:
        return CompletionDecision(
            terminal=bool(record['terminal']),
            status=CompletionStatus(record['status']),
            reason=record.get('reason'),
            confidence=CompletionConfidence(record['confidence']) if record.get('confidence') is not None else None,
            reply=record.get('reply', ''),
            anchor_seen=bool(record.get('anchor_seen', False)),
            reply_started=bool(record.get('reply_started', False)),
            reply_stable=bool(record.get('reply_stable', False)),
            provider_turn_ref=record.get('provider_turn_ref'),
            source_cursor=None,
            finished_at=record.get('finished_at'),
            diagnostics=dict(record.get('diagnostics') or {}),
        )
    except Exception:
        return None


def _build_result_payload(dispatcher, job, snapshot) -> dict:
    latest_decision = _terminal_decision_from_job(job) or (snapshot.latest_decision if snapshot is not None else None)
    visible = visible_reply_for_job(dispatcher, job, snapshot)
    return {
        'job_id': job.job_id,
        'agent_name': job.agent_name,
        'target_kind': job.target_kind.value,
        'target_name': job.target_name,
        'provider_instance': job.provider_instance,
        'provider': job.provider,
        'status': job.status.value,
        'job': job.to_record(),
        'snapshot': snapshot.to_record() if snapshot else None,
        'reply': visible.reply,
        'completion_reason': visible.reason,
        'completion_confidence': (
            visible.confidence
            if visible.confidence is not None
            else (latest_decision.confidence.value if latest_decision and latest_decision.confidence else None)
        ),
        'updated_at': visible.updated_at or (snapshot.updated_at if snapshot else job.updated_at),
        'visible_reply_source': visible.source,
        'visible_reply_id': visible.reply_id,
        'message_id': visible.message_id,
    }


def _append_generation(result: dict, health_monitor) -> dict:
    if health_monitor is None:
        return result
    inspection = health_monitor.daemon_health()
    result = dict(result)
    result['generation'] = inspection.generation
    return result


def build_get_handler(dispatcher, *, health_monitor=None):
    def handle(payload: dict) -> dict:
        job = _resolve_requested_job(dispatcher, payload)
        if job is None:
            raise ValueError('job not found')
        snapshot = dispatcher.get_snapshot(job.job_id)
        result = _build_result_payload(dispatcher, job, snapshot)
        return _append_generation(result, health_monitor)

    return handle
