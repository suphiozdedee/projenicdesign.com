from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from cli.models import ParsedAskCommand
from jobs.store import JobStore
from message_bureau import RetryLineageError, authoritative_retry_successor
from storage.atomic import atomic_write_json
from storage.locks import file_lock
from storage.paths import PathLayout
from storage.text_artifacts import read_text_artifact

from .ask import submit_ask
from .planner_feedback import (
    frontdesk_status_envelope,
    parse_planner_feedback_reply,
    planner_feedback_digest,
    validate_planner_feedback_authority,
)
from .planner_feedback_apply import apply_planner_feedback
from .task_set_closure import (
    evaluate_current_task_set_closures,
    settle_task_set_closure_feedback,
)
from .watch_fallback import load_persisted_terminal_watch_payload


RUNTIME_SCHEMA = 'ccb.plan.task_set_feedback_runtime.v1'
TRANSPORT_SCHEMA = 'ccb.plan.task_set_closure_transport.v1'
_TERMINAL_FAILURES = {'cancelled', 'failed', 'incomplete', 'timed_out'}


def advance_task_set_feedback_runtime(context, services=None) -> dict[str, object] | None:
    """Advance at most one revision-fenced task-set feedback transport."""
    deps = _deps(services)
    discovered = deps.discover(context, plan_task_fn=deps.plan_task)
    failures = [item for item in discovered.get('evaluated', ()) if item.get('status') == 'system_failure']
    if failures:
        return _payload(context, 'blocked', 'task_set_closure_system_failure', failures=failures)
    pending = discovered.get('pending')
    if not isinstance(pending, list) or not pending:
        return None
    return _advance_intent(context, dict(pending[0]), deps)


def _advance_intent(context, intent: dict[str, object], deps) -> dict[str, object]:
    task_set_id = _text(intent.get('task_set_id'), 'task_set_id')
    revision = _positive_int(intent.get('task_set_revision'), 'task_set_revision')
    intent_id = _text(intent.get('intent_id'), 'intent_id')
    task_set_path = Path(_text(intent.get('task_set_path'), 'task_set_path'))
    task_set = _read_json(task_set_path)
    if task_set.get('task_set_id') != task_set_id or task_set.get('task_set_revision') != revision:
        raise RuntimeError('task_set_feedback_stale_revision')
    if task_set.get('state') != 'closure_pending':
        raise RuntimeError('task_set_feedback_authority_not_closure_pending')
    closure_ref = task_set.get('closure') if isinstance(task_set.get('closure'), dict) else {}
    closure_path = _canonical_closure_path(
        context,
        task_set_path,
        _text(closure_ref.get('path'), 'closure.path'),
    )
    closure = _read_json(closure_path)
    if (
        closure.get('task_set_id') != task_set_id
        or closure.get('task_set_revision') != revision
        or closure.get('closure_digest') != intent.get('closure_digest')
        or closure.get('ordered_terminal_evidence_digest')
        != intent.get('ordered_terminal_evidence_digest')
    ):
        raise RuntimeError('task_set_feedback_closure_authority_mismatch')

    runtime_root = Path(context.project.project_root) / '.ccb/runtime/task-sets' / task_set_id
    runtime_root.mkdir(parents=True, exist_ok=True)
    state_path = runtime_root / f'feedback-r{revision}.json'
    with file_lock(runtime_root / 'feedback.lock'):
        state = _read_json(state_path) if state_path.is_file() else {}
        if state:
            _validate_state(state, intent=intent)
        else:
            planner_message = _planner_message(
                closure,
                intent,
                _closure_reference(context, closure_path, closure),
            )
            state = {
                'schema': RUNTIME_SCHEMA,
                'schema_version': 1,
                'task_set_id': task_set_id,
                'task_set_revision': revision,
                'closure_intent_id': intent_id,
                'closure_digest': closure['closure_digest'],
                'terminal_evidence_digest': closure['ordered_terminal_evidence_digest'],
                'stage': 'planner_prepared',
                'planner': _prepared_transport(
                    target='planner',
                    purpose='planner_backfill',
                    task_id=f'task-set-feedback-{intent_id}',
                    message=planner_message,
                    silent=True,
                ),
                'frontdesk': None,
                'backfill_import': None,
            }
            atomic_write_json(state_path, state)

        if state['stage'] in {'planner_prepared', 'planner_pending'}:
            result = _advance_transport(context, state['planner'], deps)
            state['planner'] = result
            if result['status'] == 'pending':
                state['stage'] = 'planner_pending'
                atomic_write_json(state_path, state)
                return _pending_payload(context, state, action='task_set_planner_backfill_pending')
            if result['status'] != 'completed':
                return _fail(context, state, state_path, 'task_set_planner_backfill_failed')
            state['stage'] = 'planner_terminal'
            atomic_write_json(state_path, state)

        if state['stage'] == 'planner_terminal':
            proposal = deps.parse_planner_feedback(str(state['planner'].get('reply') or ''))
            expected_plan_revision = deps.resolve_plan_revision(context, task_set, closure)
            evidence_refs = [str(closure_path.relative_to(context.project.project_root))]
            deps.validate_planner_feedback(
                proposal,
                mode='task_set_closure',
                expected_plan_revision=expected_plan_revision,
                task_or_task_set_id=task_set_id,
                task_or_task_set_revision=revision,
                closure_evidence_digest=str(closure['ordered_terminal_evidence_digest']),
                aggregate_result=str(closure['aggregate_result']),
                evidence_refs=evidence_refs,
            )
            planner_transport = state['planner']
            authority = {
                'task_set_id': task_set_id,
                'task_set_revision': revision,
                'closure_intent_id': intent_id,
                'closure_digest': closure['closure_digest'],
                'ordered_terminal_evidence_digest': closure['ordered_terminal_evidence_digest'],
                'expected_plan_revision': expected_plan_revision,
                'planner_job_id': planner_transport['effective_job_id'],
                'planner_source_job_id': planner_transport['source_job_id'],
                'planner_effective_job_id': planner_transport['effective_job_id'],
                'planner_retry_lineage': planner_transport['retry_lineage'],
                'planner_feedback_digest': deps.planner_feedback_digest(proposal),
                'plan_slug': task_set['plan_slug'],
            }
            imported = deps.apply_planner_feedback(context, proposal, authority)
            if not isinstance(imported, dict) or imported.get('status') != 'imported':
                raise RuntimeError('planner_feedback_apply_did_not_return_imported_authority')
            state['backfill_import'] = {**authority, **imported}
            if proposal.frontdesk_notification_required:
                message = _frontdesk_message(deps.frontdesk_status_envelope(proposal))
                state['frontdesk'] = _prepared_transport(
                    target='frontdesk',
                    purpose='frontdesk_status',
                    task_id=f'task-set-status-{intent_id}',
                    message=message,
                    silent=False,
                )
                state['stage'] = 'frontdesk_prepared'
            else:
                state['stage'] = 'closed'
                state['notification'] = {'status': 'notification_not_required'}
                state['runtime_digest'] = _runtime_digest(state)
            atomic_write_json(state_path, state)

        if state['stage'] in {'frontdesk_prepared', 'frontdesk_pending'}:
            result = _advance_transport(context, state['frontdesk'], deps)
            state['frontdesk'] = result
            if result['status'] == 'pending':
                state['stage'] = 'frontdesk_pending'
                atomic_write_json(state_path, state)
                return _pending_payload(context, state, action='task_set_frontdesk_status_pending')
            if result['status'] != 'completed':
                return _fail(context, state, state_path, 'task_set_frontdesk_status_failed')
            state['stage'] = 'closed'
            state['notification'] = {
                'status': 'delivered',
                'job_id': result['job_id'],
            }
            state['runtime_digest'] = _runtime_digest(state)
            atomic_write_json(state_path, state)

        if state['stage'] == 'failed':
            return _payload(
                context,
                'blocked',
                str(state.get('failure_reason') or 'task_set_feedback_failed'),
                task_set_id=task_set_id,
                task_set_revision=revision,
                runtime_state_path=str(state_path),
            )
        deps.settle_feedback(
            context,
            task_set_id=task_set_id,
            task_set_revision=revision,
            intent_id=intent_id,
            ordered_terminal_evidence_digest=str(closure['ordered_terminal_evidence_digest']),
            transport_ref={
                'runtime_state_path': str(state_path),
                'planner_job_id': state['planner'].get('effective_job_id'),
                'frontdesk_job_id': (state.get('frontdesk') or {}).get('effective_job_id'),
                'planner_backfill_path': (state.get('backfill_import') or {}).get('planner_backfill_path'),
                'planner_feedback_digest': (state.get('backfill_import') or {}).get('planner_feedback_digest'),
                'notification_status': (state.get('notification') or {}).get('status'),
                'backfill_digest': (state.get('backfill_import') or {}).get('backfill_digest'),
                'planner_source_job_id': state['planner'].get('source_job_id'),
                'planner_effective_job_id': state['planner'].get('effective_job_id'),
                'planner_retry_lineage': state['planner'].get('retry_lineage'),
                'frontdesk_source_job_id': (state.get('frontdesk') or {}).get('source_job_id'),
                'frontdesk_effective_job_id': (state.get('frontdesk') or {}).get('effective_job_id'),
                'frontdesk_retry_lineage': (state.get('frontdesk') or {}).get('retry_lineage'),
            },
        )
        return _payload(
            context,
            'ok',
            'task_set_feedback_closed',
            task_set_id=task_set_id,
            task_set_revision=revision,
            planner_job_id=state['planner'].get('effective_job_id'),
            frontdesk_job_id=(state.get('frontdesk') or {}).get('effective_job_id'),
            runtime_state_path=str(state_path),
        )


def _advance_transport(context, transport: dict[str, object], deps) -> dict[str, object]:
    message = str(transport.get('message') or '')
    message_sha256 = hashlib.sha256(message.encode('utf-8')).hexdigest()
    if not message or transport.get('message_sha256') != message_sha256:
        raise RuntimeError('task_set_feedback_transport_message_digest_mismatch')
    job_id = str(transport.get('job_id') or '')
    if job_id:
        recovered = deps.find_transport_job(
            context,
            target=str(transport['target']),
            task_id=str(transport['task_id']),
            message_sha256=message_sha256,
            message=message,
        )
        if recovered != job_id:
            raise RuntimeError('task_set_feedback_bound_job_authority_mismatch')
    if not job_id:
        recovered = deps.find_transport_job(
            context,
            target=str(transport['target']),
            task_id=str(transport['task_id']),
            message_sha256=message_sha256,
            message=message,
        )
        if recovered:
            job_id = recovered
        else:
            summary = deps.submit_ask(
                context,
                ParsedAskCommand(
                    project=None,
                    target=str(transport['target']),
                    sender='system',
                    message=message,
                    task_id=str(transport['task_id']),
                    compact=True,
                    silence=bool(transport['silent']),
                ),
            )
            jobs = [job for job in summary.jobs if str(job.get('agent_name') or job.get('target_name') or '') == transport['target']]
            if len(jobs) != 1 or not str(jobs[0].get('job_id') or ''):
                raise RuntimeError('task_set_feedback_submission_not_single_job')
            job_id = str(jobs[0]['job_id'])
        transport = {
            **transport,
            'job_id': job_id,
            'source_job_id': job_id,
            'effective_job_id': job_id,
            'retry_lineage': [],
        }
    source_job_id = str(transport.get('source_job_id') or job_id)
    current_job_id = str(transport.get('effective_job_id') or job_id)
    lineage = list(transport.get('retry_lineage') or [])
    seen = {source_job_id}
    for _depth in range(8):
        if current_job_id in seen and current_job_id != source_job_id:
            raise RuntimeError('task_set_feedback_retry_lineage_cycle')
        seen.add(current_job_id)
        terminal = deps.terminal_watch(context, current_job_id)
        if terminal is None:
            return {
                **transport, 'source_job_id': source_job_id,
                'effective_job_id': current_job_id, 'retry_lineage': lineage,
                'status': 'pending',
            }
        status = str(terminal.get('status') or '').lower()
        if status == 'completed':
            return {
                **transport, 'source_job_id': source_job_id,
                'effective_job_id': current_job_id, 'retry_lineage': lineage,
                'status': 'completed', 'reply': str(terminal.get('reply') or ''),
            }
        if status not in _TERMINAL_FAILURES:
            raise RuntimeError(f'task_set_feedback_unknown_terminal_status:{status or "missing"}')
        successor = deps.retry_successor(
            context,
            source_job_id=current_job_id,
            target=str(transport['target']),
            task_id=str(transport['task_id']),
            message=str(transport['message']),
            message_sha256=str(transport['message_sha256']),
        )
        if successor is None:
            return {
                **transport, 'source_job_id': source_job_id,
                'effective_job_id': current_job_id, 'retry_lineage': lineage,
                'status': status, 'reply': str(terminal.get('reply') or ''),
            }
        successor_job_id = str(successor['retry_successor_job_id'])
        if successor_job_id in seen:
            raise RuntimeError('task_set_feedback_retry_lineage_cycle')
        lineage.append(successor)
        current_job_id = successor_job_id
    raise RuntimeError('task_set_feedback_retry_lineage_depth_exceeded')


def _find_transport_job(context, *, target: str, task_id: str, message_sha256: str, message: str) -> str | None:
    latest = {}
    layout = context.paths if isinstance(getattr(context, 'paths', None), PathLayout) else PathLayout(
        context.project.project_root
    )
    for job in JobStore(layout).list_agent(target):
        if str(job.provider_options.get('retry_source_job_id') or ''):
            continue
        if str(job.request.task_id or '') == task_id:
            latest[job.job_id] = job
    matches = []
    for job in latest.values():
        body_artifact = job.request.body_artifact if isinstance(job.request.body_artifact, dict) else {}
        body_matches = str(job.request.body or '').startswith(message)
        artifact_matches = False
        if body_artifact:
            artifact_body = read_text_artifact(layout, body_artifact)
            artifact_matches = (
                artifact_body.startswith(message)
                and hashlib.sha256(message.encode('utf-8')).hexdigest() == message_sha256
            )
        if body_matches or artifact_matches:
            matches.append(job.job_id)
        else:
            raise RuntimeError('task_set_feedback_persisted_job_authority_mismatch')
    if len(matches) > 1:
        raise RuntimeError('task_set_feedback_duplicate_persisted_jobs')
    return matches[0] if matches else None


def _prepared_transport(*, target: str, purpose: str, task_id: str, message: str, silent: bool) -> dict[str, object]:
    return {
        'target': target,
        'purpose': purpose,
        'task_id': task_id,
        'message': message,
        'message_sha256': hashlib.sha256(message.encode('utf-8')).hexdigest(),
        'silent': silent,
        'job_id': None,
        'source_job_id': None,
        'effective_job_id': None,
        'retry_lineage': [],
        'status': 'prepared',
    }


def _retry_successor_job(
    context,
    *,
    source_job_id: str,
    target: str,
    task_id: str,
    message: str,
    message_sha256: str,
) -> dict[str, object] | None:
    layout = context.paths if isinstance(getattr(context, 'paths', None), PathLayout) else PathLayout(
        context.project.project_root
    )
    try:
        edge = authoritative_retry_successor(layout, source_job_id)
    except RetryLineageError as exc:
        raise RuntimeError(f'task_set_feedback_retry_lineage_authority_invalid: {exc}') from exc
    if edge is None:
        return None
    job = JobStore(layout).get_latest(target, edge.retry_successor_job_id)
    if job is None:
        raise RuntimeError('task_set_feedback_retry_successor_job_missing')
    body = str(job.request.body or '')
    if job.request.body_artifact:
        body = read_text_artifact(layout, job.request.body_artifact)
    if (
        job.request.to_agent != target
        or job.request.task_id != task_id
        or body != message
        or hashlib.sha256(body.encode('utf-8')).hexdigest() != message_sha256
    ):
        raise RuntimeError('task_set_feedback_retry_successor_authority_mismatch')
    option_source = str(job.provider_options.get('retry_source_job_id') or '')
    if option_source and option_source != source_job_id:
        raise RuntimeError('task_set_feedback_retry_successor_provider_option_mismatch')
    return edge.to_record()


def _planner_message(
    closure: dict[str, object],
    intent: dict[str, object],
    closure_ref: dict[str, object],
) -> str:
    envelope = {
        'schema': TRANSPORT_SCHEMA,
        'closure': closure,
        'closure_ref': closure_ref,
        'closure_intent': {
            key: intent[key]
            for key in (
                'intent_id', 'task_set_id', 'task_set_revision',
                'ordered_terminal_evidence_digest', 'closure_digest',
            )
        },
    }
    return '**task-set-closure.json**\n```json\n' + _canonical_json(envelope) + '\n```'


def _closure_reference(context, closure_path: Path, closure: dict[str, object]) -> dict[str, object]:
    root = Path(context.project.project_root).resolve()
    resolved = closure_path.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError('task_set_feedback_closure_ref_outside_project')
    return {
        'path': str(resolved.relative_to(root)),
        'closure_digest': closure['closure_digest'],
        'ordered_terminal_evidence_digest': closure['ordered_terminal_evidence_digest'],
    }


def _canonical_closure_path(context, task_set_path: Path, value: str) -> Path:
    root = Path(context.project.project_root).resolve()
    expected = task_set_path.parent / 'closure.json'
    raw = Path(value)
    if raw.is_absolute() or raw.as_posix() != value:
        raise RuntimeError('task_set_feedback_closure_ref_path_invalid')
    candidate = Path(context.project.project_root) / raw
    if candidate != expected:
        raise RuntimeError('task_set_feedback_closure_ref_path_mismatch')
    current = Path(context.project.project_root)
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError('task_set_feedback_closure_ref_symlink_forbidden')
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError('task_set_feedback_closure_ref_outside_project')
    return candidate


def _frontdesk_message(envelope: dict[str, object]) -> str:
    return '**frontdesk-status.json**\n```json\n' + _canonical_json(envelope) + '\n```'


def _resolve_plan_revision(_context, task_set: dict[str, object], _closure: dict[str, object]) -> str:
    authority = task_set.get('plan_revision') if isinstance(task_set.get('plan_revision'), dict) else {}
    digest = str(authority.get('digest') or '')
    if not digest.startswith('sha256:') or len(digest) != 71:
        raise RuntimeError('task_set_feedback_plan_revision_digest_invalid')
    return digest


def _validate_state(state: dict[str, object], *, intent: dict[str, object]) -> None:
    if state.get('schema') != RUNTIME_SCHEMA:
        raise RuntimeError('task_set_feedback_runtime_schema_mismatch')
    expected = {
        'task_set_id': intent.get('task_set_id'),
        'task_set_revision': intent.get('task_set_revision'),
        'closure_intent_id': intent.get('intent_id'),
        'closure_digest': intent.get('closure_digest'),
        'terminal_evidence_digest': intent.get('ordered_terminal_evidence_digest'),
    }
    actual = {key: state.get(key) for key in expected}
    if actual != expected:
        raise RuntimeError('task_set_feedback_runtime_authority_mismatch')
    if state.get('stage') not in {
        'planner_prepared', 'planner_pending', 'planner_terminal',
        'frontdesk_prepared', 'frontdesk_pending', 'closed', 'failed',
    }:
        raise RuntimeError('task_set_feedback_runtime_stage_invalid')
    if not isinstance(state.get('planner'), dict):
        raise RuntimeError('task_set_feedback_runtime_planner_transport_missing')


def _pending_payload(context, state: dict[str, object], *, action: str) -> dict[str, object]:
    transport = state['planner'] if state['stage'] == 'planner_pending' else state['frontdesk']
    effective_job_id = transport.get('effective_job_id') or transport['job_id']
    return _payload(
        context,
        'pending',
        action,
        task_set_id=state['task_set_id'],
        task_set_revision=state['task_set_revision'],
        pending_job_ids=[effective_job_id],
        ask={'target': transport['target'], 'job_id': effective_job_id, 'status': 'running'},
    )


def _fail(context, state: dict[str, object], state_path: Path, reason: str) -> dict[str, object]:
    state['stage'] = 'failed'
    state['failure_reason'] = reason
    atomic_write_json(state_path, state)
    return _payload(
        context,
        'blocked',
        reason,
        task_set_id=state['task_set_id'],
        task_set_revision=state['task_set_revision'],
        runtime_state_path=str(state_path),
    )


def _payload(context, status: str, action: str, **extra) -> dict[str, object]:
    return {
        'schema_version': 1,
        'record_type': 'ccb_task_set_feedback_runtime',
        'loop_runner_status': status,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': action,
        **extra,
    }


def _deps(services):
    services = services or SimpleNamespace()
    from .plan_tasks import plan_task

    return SimpleNamespace(
        discover=getattr(services, 'discover_task_set_closures', evaluate_current_task_set_closures),
        plan_task=getattr(services, 'plan_task', plan_task),
        submit_ask=getattr(services, 'submit_ask', submit_ask),
        terminal_watch=getattr(services, 'persisted_terminal_watch', load_persisted_terminal_watch_payload),
        find_transport_job=getattr(services, 'find_task_set_transport_job', _find_transport_job),
        retry_successor=getattr(services, 'find_task_set_retry_successor', _retry_successor_job),
        parse_planner_feedback=getattr(services, 'parse_planner_feedback', parse_planner_feedback_reply),
        validate_planner_feedback=getattr(services, 'validate_planner_feedback', validate_planner_feedback_authority),
        planner_feedback_digest=getattr(services, 'planner_feedback_digest', planner_feedback_digest),
        frontdesk_status_envelope=getattr(services, 'frontdesk_status_envelope', frontdesk_status_envelope),
        resolve_plan_revision=getattr(services, 'resolve_plan_revision', _resolve_plan_revision),
        apply_planner_feedback=getattr(services, 'apply_planner_feedback', apply_planner_feedback),
        settle_feedback=getattr(
            services,
            'settle_task_set_feedback',
            settle_task_set_closure_feedback,
        ),
    )


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'task_set_feedback_authority_unreadable:{path}') from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f'task_set_feedback_authority_not_object:{path}')
    return payload


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _runtime_digest(state: dict[str, object]) -> str:
    payload = {key: value for key, value in state.items() if key != 'runtime_digest'}
    return 'sha256:' + hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _text(value: object, field: str) -> str:
    text = str(value or '').strip()
    if not text:
        raise RuntimeError(f'task_set_feedback_{field}_missing')
    return text


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(f'task_set_feedback_{field}_invalid')
    return value


__all__ = ['advance_task_set_feedback_runtime']
