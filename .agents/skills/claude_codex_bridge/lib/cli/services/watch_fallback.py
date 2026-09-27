from __future__ import annotations

from completion.snapshot_store import CompletionSnapshotStore
from jobs.store import JobEventStore, JobStore
from message_bureau import CallbackEdgeStore
from message_bureau.store import AttemptStore, ReplyStore
from storage.paths import PathLayout
from storage.text_artifacts import read_text_artifact

_TERMINAL_JOB_STATUSES = frozenset({'completed', 'cancelled', 'failed', 'incomplete'})


def load_persisted_terminal_watch_payload(context, target: str, *, cursor: int = 0) -> dict | None:
    job_id = str(target or '').strip()
    if not job_id.startswith('job_'):
        return None

    layout = _context_layout(context)
    snapshot = _load_completion_snapshot(layout, job_id)
    latest_decision = getattr(snapshot, 'latest_decision', None)
    agent_name = str(getattr(snapshot, 'agent_name', '') or '').strip()
    attempt = AttemptStore(layout).get_latest_by_job_id(job_id)
    if not agent_name and attempt is not None:
        agent_name = str(attempt.agent_name or '').strip()
    if not agent_name:
        return None

    job = JobStore(layout).get_latest(agent_name, job_id)
    if job is None:
        return None
    if latest_decision is None:
        latest_decision = getattr(job, 'terminal_decision', None)
    if latest_decision is None or not bool(_decision_field(latest_decision, 'terminal', False)):
        return None
    target_kind = getattr(getattr(job, 'target_kind', None), 'value', None) or 'agent'
    target_name = str(getattr(job, 'target_name', None) or agent_name).strip() or agent_name
    provider = getattr(job, 'provider', None)
    provider_instance = getattr(job, 'provider_instance', None)
    status = _resolved_terminal_status(job, latest_decision=latest_decision)
    if status not in _TERMINAL_JOB_STATUSES:
        return None
    visible_reply, visible_source = _persisted_visible_reply(layout, job, latest_decision=latest_decision)
    if visible_source == 'callback_delegated_pending':
        return None
    chain_evidence = _persisted_chain_evidence(layout, job)

    next_cursor, filtered_events = _read_job_events(
        layout,
        target_kind=target_kind,
        target_name=target_name,
        job_id=job_id,
        cursor=cursor,
    )
    return {
        'job_id': job_id,
        'agent_name': agent_name,
        'target_kind': target_kind,
        'target_name': target_name,
        'provider': provider,
        'provider_instance': provider_instance,
        'cursor': next_cursor,
        'generation': None,
        'terminal': True,
        'status': status,
        'reply': visible_reply,
        'visible_reply_source': visible_source,
        'chain_evidence': chain_evidence,
        'events': filtered_events,
    }


def persisted_delegated_callback_pending(context, target: str) -> bool:
    job_id = str(target or '').strip()
    if not job_id.startswith('job_'):
        return False
    layout = _context_layout(context)
    snapshot = _load_completion_snapshot(layout, job_id)
    agent_name = str(getattr(snapshot, 'agent_name', '') or '').strip()
    attempt = AttemptStore(layout).get_latest_by_job_id(job_id)
    if not agent_name and attempt is not None:
        agent_name = str(attempt.agent_name or '').strip()
    if not agent_name:
        return False
    job = JobStore(layout).get_latest(agent_name, job_id)
    terminal = getattr(job, 'terminal_decision', None)
    if not isinstance(terminal, dict) or not bool(
        terminal.get('delegated') or terminal.get('chain_edge_id')
    ):
        return False
    return _latest_reply_for_job_message(layout, job) is None


def _load_completion_snapshot(layout: PathLayout, job_id: str):
    try:
        return CompletionSnapshotStore(layout).load(job_id)
    except (KeyError, TypeError, ValueError):
        # Older runtime snapshots can predate fields required by the current
        # model. Durable Job/Attempt/Reply records still carry callback
        # authority, so a legacy snapshot must not crash auto-run recovery.
        return None


def _context_layout(context) -> PathLayout:
    layout = getattr(context, 'paths', None)
    if isinstance(layout, PathLayout):
        return layout
    project = getattr(context, 'project', None)
    project_root = getattr(project, 'project_root', None)
    if project_root is None:
        raise ValueError('context project root is required for persisted watch fallback')
    return PathLayout(project_root)


def _resolved_terminal_status(job, *, latest_decision) -> str:
    job_status = str(getattr(getattr(job, 'status', None), 'value', '') or '').strip().lower()
    if job_status in _TERMINAL_JOB_STATUSES:
        return job_status
    status = _decision_field(latest_decision, 'status', '')
    return str(getattr(status, 'value', status) or '').strip().lower()


def _persisted_visible_reply(layout: PathLayout, job, *, latest_decision) -> tuple[str, str]:
    terminal = getattr(job, 'terminal_decision', None)
    if isinstance(terminal, dict) and bool(terminal.get('delegated') or terminal.get('chain_edge_id')):
        reply = _latest_reply_for_job_message(layout, job)
        if reply is not None:
            return reply.reply, 'message_bureau_reply'
        return '', 'callback_delegated_pending'
    return str(_decision_field(latest_decision, 'reply', '') or ''), 'snapshot'


def _decision_field(decision, key: str, default=None):
    if isinstance(decision, dict):
        return decision.get(key, default)
    return getattr(decision, key, default)


def _latest_reply_for_job_message(layout: PathLayout, job):
    if job is None:
        return None
    attempt = AttemptStore(layout).get_latest_by_job_id(job.job_id)
    if attempt is None:
        return None
    replies = ReplyStore(layout).list_message(attempt.message_id)
    if not replies:
        return None
    return sorted(replies, key=lambda item: (item.finished_at, item.reply_id))[-1]


def _persisted_chain_evidence(layout: PathLayout, job) -> list[dict[str, object]]:
    if job is None:
        return []
    attempt = AttemptStore(layout).get_latest_by_job_id(job.job_id)
    if attempt is None:
        return []
    latest_by_id = {}
    for edge in CallbackEdgeStore(layout).list_all():
        if edge.parent_message_id == attempt.message_id:
            latest_by_id[edge.edge_id] = edge
    records = []
    reply_store = ReplyStore(layout)
    job_store = JobStore(layout)
    for edge in sorted(latest_by_id.values(), key=lambda item: (item.created_at, item.edge_id)):
        child_agent = str(edge.diagnostics.get('child_agent') or '').strip().lower()
        child_job = job_store.get_latest(child_agent, edge.child_job_id) if child_agent else None
        reply = reply_store.get_latest(edge.child_reply_id) if edge.child_reply_id else None
        if reply is None:
            replies = reply_store.list_message(edge.child_message_id)
            reply = sorted(replies, key=lambda item: (item.finished_at, item.reply_id))[-1] if replies else None
        reply_text, reply_artifact_valid = _chain_reply_text(layout, reply)
        records.append(
            {
                'edge_id': edge.edge_id,
                'parent_job_id': edge.parent_job_id,
                'child_job_id': edge.child_job_id,
                'child_agent': child_agent,
                'state': edge.state.value,
                'child_status': (
                    getattr(getattr(child_job, 'status', None), 'value', None)
                    or edge.child_status
                ),
                'reply': reply_text,
                'reply_artifact': (
                    dict(reply.reply_artifact)
                    if reply is not None and isinstance(reply.reply_artifact, dict)
                    else None
                ),
                'reply_artifact_valid': reply_artifact_valid,
                'review_workspace_path': edge.diagnostics.get('review_workspace_path'),
                'review_tree_digest': edge.diagnostics.get('review_tree_digest'),
                'created_at': edge.created_at,
                'updated_at': edge.updated_at,
            }
        )
    return records


def _chain_reply_text(layout: PathLayout, reply) -> tuple[str, bool | None]:
    if reply is None:
        return '', None
    artifact = getattr(reply, 'reply_artifact', None)
    if not isinstance(artifact, dict):
        return str(getattr(reply, 'reply', '') or ''), None
    try:
        return read_text_artifact(layout, artifact), True
    except Exception:
        return '', False


def _read_job_events(
    layout: PathLayout,
    *,
    target_kind: str,
    target_name: str,
    job_id: str,
    cursor: int,
) -> tuple[int, list[dict]]:
    try:
        next_cursor, events = JobEventStore(layout).read_since_target(target_kind, target_name, cursor)
    except Exception:
        return cursor, []
    return next_cursor, [event.to_record() for event in events if event.job_id == job_id]


__all__ = [
    'load_persisted_terminal_watch_payload',
    'persisted_delegated_callback_pending',
]
