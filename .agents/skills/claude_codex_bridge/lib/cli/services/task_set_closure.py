from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Callable

from storage.atomic import atomic_write_json
from storage.locks import file_lock
from storage.paths import PathLayout
from storage.text_artifacts import read_text_artifact
from jobs.store import JobStore
from message_bureau import RetryLineageError, authoritative_retry_successor
from completion.snapshot_store import CompletionSnapshotStore

from .planner_feedback_apply import plan_revision_authority, validate_planner_backfill_record
from .planner_feedback import parse_planner_feedback_reply, planner_feedback_digest
from .plan_tasks import detail_ready_stop_contract_authority


TASK_SET_SCHEMA = 'ccb.plan.task_set.v1'
CLOSURE_SCHEMA = 'ccb.plan.task_set_closure.v1'
INTENT_SCHEMA = 'ccb.plan.task_set_closure_intent.v1'
INTENT_STORE_SCHEMA = 'ccb.plan.task_set_closure_intent_store.v1'
_SEGMENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
_TERMINAL_RESULTS = {
    'done': 'pass',
    'detail_ready': 'pass',
    'partial': 'partial',
    'replan_required': 'replan_required',
    'blocked': 'blocked',
}
_TASK_SET_REQUIRED_KEYS = {
    'schema', 'schema_version', 'task_set_id', 'task_set_revision', 'project_id',
    'plan_slug', 'source_task_id', 'source_request', 'planner_job', 'plan_revision',
    'children', 'ordered_required_children', 'state', 'aggregate_result', 'closure',
    'created_at', 'updated_at',
}
_CLOSURE_KEYS = {
    'schema', 'schema_version', 'task_set_id', 'task_set_revision', 'source_request',
    'planner_job', 'expected_plan_revision', 'ordered_children',
    'ordered_terminal_evidence_digest', 'status', 'aggregate_result', 'reason',
    'created_at', 'closure_digest',
}
_INTENT_REQUIRED_KEYS = {
    'schema', 'schema_version', 'intent_id', 'task_set_id', 'task_set_revision',
    'ordered_terminal_evidence_digest', 'closure_digest', 'status', 'created_at',
}


def create_task_set_authority(
    context,
    *,
    plan_slug: str,
    source_task_id: str,
    source_request: dict[str, object],
    planner_job: dict[str, object],
    children: list[dict[str, object]],
    plan_task_fn: Callable = None,
    task_set_id: str | None = None,
) -> dict[str, object]:
    plan_task_fn = _plan_task_fn(plan_task_fn)
    plan_slug = _segment(plan_slug, field='plan_slug')
    source_task_id = _segment(source_task_id, field='source_task_id')
    normalized_source = _source_request(context, source_request)
    normalized_planner = _planner_job(planner_job)
    task_set_id = _segment(
        task_set_id or _new_task_set_id(plan_slug, source_task_id, normalized_planner['job_id']),
        field='task_set_id',
    )
    child_records = _resolve_children(
        context,
        children,
        plan_slug=plan_slug,
        source_task_id=source_task_id,
        plan_task_fn=plan_task_fn,
    )
    source_task = _show_task(context, source_task_id, plan_task_fn=plan_task_fn)
    if str(source_task.get('plan_slug') or '') != plan_slug:
        raise ValueError('task-set source task is outside the owning plan')
    plan_revision = _plan_revision(Path(context.project.project_root), plan_slug)
    root = _task_set_root(context, plan_slug, task_set_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'task-set.json'
    with file_lock(root / 'task-set.lock'):
        existing = _read_json(path)
        identity = {
            'task_set_id': task_set_id,
            'source_task_id': source_task_id,
            'source_request': normalized_source,
            'planner_job': normalized_planner,
            'plan_revision': plan_revision,
            'children': child_records,
        }
        if existing:
            _validate_task_set(existing)
            observed = {key: existing.get(key) for key in identity}
            if observed != identity or existing.get('task_set_revision') != 1:
                raise ValueError('task-set creation conflicts with existing authority')
            record = existing
            target_state = str(record.get('state') or 'running')
            if target_state == 'binding':
                target_state = 'running'
        else:
            now = _now()
            record = {
                'schema': TASK_SET_SCHEMA,
                'schema_version': 1,
                'task_set_id': task_set_id,
                'task_set_revision': 1,
                'project_id': context.project.project_id,
                'plan_slug': plan_slug,
                **identity,
                'ordered_required_children': [
                    child['task_id'] for child in child_records if child['required']
                ],
                'state': 'binding',
                'aggregate_result': None,
                'closure': None,
                'created_at': now,
                'updated_at': now,
            }
            atomic_write_json(path, record)
            target_state = 'running'
        parent_binding = plan_task_fn(
            context,
            SimpleNamespace(
                action='task-bind-task-set',
                task_id=source_task_id,
                task_set_id=task_set_id,
                task_set_revision=1,
                binding_role='parent',
                expected_task_revision=source_task['task_revision'],
            ),
        )
        child_bindings = _bind_children(
            context,
            task_set_id=task_set_id,
            task_set_revision=1,
            children=child_records,
            plan_task_fn=plan_task_fn,
        )
        record['state'] = target_state
        record['updated_at'] = _now()
        atomic_write_json(path, record)
    return {
        'status': 'ok',
        'task_set': record,
        'task_set_path': str(path),
        'parent_transition': parent_binding,
        'child_bindings': child_bindings,
        'idempotent': bool(existing),
    }


def revise_task_set_authority(
    context,
    *,
    task_set_id: str,
    expected_revision: int,
    children: list[dict[str, object]],
    plan_task_fn: Callable = None,
) -> dict[str, object]:
    plan_task_fn = _plan_task_fn(plan_task_fn)
    task_set_id = _segment(task_set_id, field='task_set_id')
    record, path = _find_task_set(context, task_set_id)
    root = path.parent
    with file_lock(root / 'task-set.lock'):
        record = _read_json(path)
        _validate_task_set(record)
        current_revision = _positive_int(record.get('task_set_revision'), field='task_set_revision')
        if current_revision != expected_revision:
            raise ValueError(
                f'stale task-set revision: expected {expected_revision}, current {current_revision}'
            )
        new_revision = current_revision + 1
        new_children = _resolve_children(
            context,
            children,
            plan_slug=str(record['plan_slug']),
            source_task_id=str(record['source_task_id']),
            plan_task_fn=plan_task_fn,
        )
        old_children = {
            str(child['task_id']): child
            for child in record['children']
            if isinstance(child, dict)
        }
        closure_path = root / 'closure.json'
        if closure_path.is_file():
            archive_path = root / f'closure-r{current_revision}.json'
            if archive_path.is_file():
                if archive_path.read_bytes() != closure_path.read_bytes():
                    raise ValueError('task-set closure revision archive conflicts with current closure')
                closure_path.unlink()
            else:
                closure_path.replace(archive_path)
        record['state'] = 'revising'
        record['pending_revision'] = new_revision
        record['pending_children'] = new_children
        record['updated_at'] = _now()
        atomic_write_json(path, record)
        _stale_open_intents(context, task_set_id, current_revision=current_revision)
        new_ids = {str(child['task_id']) for child in new_children}
        for task_id, child in old_children.items():
            if task_id in new_ids:
                continue
            plan_task_fn(
                context,
                SimpleNamespace(
                    action='task-unbind-task-set',
                    task_id=task_id,
                    task_set_id=task_set_id,
                    task_set_revision=current_revision,
                ),
            )
        source_task = _show_task(context, str(record['source_task_id']), plan_task_fn=plan_task_fn)
        plan_task_fn(
            context,
            SimpleNamespace(
                action='task-bind-task-set',
                task_id=record['source_task_id'],
                task_set_id=task_set_id,
                task_set_revision=new_revision,
                binding_role='parent',
                expected_task_revision=source_task['task_revision'],
            ),
        )
        _bind_children(
            context,
            task_set_id=task_set_id,
            task_set_revision=new_revision,
            children=new_children,
            plan_task_fn=plan_task_fn,
        )
        record['task_set_revision'] = new_revision
        record['children'] = new_children
        record['ordered_required_children'] = [
            child['task_id'] for child in new_children if child['required']
        ]
        record['state'] = 'running'
        record['aggregate_result'] = None
        record['closure'] = None
        record.pop('pending_revision', None)
        record.pop('pending_children', None)
        record['updated_at'] = _now()
        atomic_write_json(path, record)
    return {'status': 'ok', 'task_set': record, 'task_set_path': str(path)}


def evaluate_task_set_closure(
    context,
    *,
    task_set_id: str,
    expected_revision: int | None = None,
    plan_task_fn: Callable = None,
) -> dict[str, object]:
    plan_task_fn = _plan_task_fn(plan_task_fn)
    task_set_id = _segment(task_set_id, field='task_set_id')
    record, path = _find_task_set(context, task_set_id)
    root = path.parent
    with file_lock(root / 'task-set.lock'):
        record = _read_json(path)
        _validate_task_set(record)
        revision = _positive_int(record.get('task_set_revision'), field='task_set_revision')
        if expected_revision is not None and expected_revision != revision:
            raise ValueError(
                f'stale task-set revision: expected {expected_revision}, current {revision}'
            )
        if record.get('state') == 'revising':
            return _pending(record, reason='task_set_revision_in_progress')
        evidence: list[dict[str, object]] = []
        pending: list[str] = []
        failures: list[dict[str, object]] = []
        for child in record['children']:
            if not isinstance(child, dict) or not child.get('required'):
                continue
            item = _child_evidence(
                context,
                child,
                plan_slug=str(record['plan_slug']),
                plan_task_fn=plan_task_fn,
            )
            evidence.append(item)
            if item['evidence_status'] == 'pending':
                pending.append(str(item['task_id']))
            elif item['evidence_status'] == 'system_failure':
                failures.append(item)
        if pending:
            return _pending(record, reason='required_children_not_terminal', child_task_ids=pending)
        evidence_digest = _digest({'task_set_revision': revision, 'children': evidence})
        closure_path = root / 'closure.json'
        if failures:
            closure = _closure_record(
                record,
                evidence=evidence,
                evidence_digest=evidence_digest,
                aggregate_result=None,
                status='system_failure',
                reason='required_child_authority_incomplete',
            )
            atomic_write_json(closure_path, closure)
            record['state'] = 'system_failure'
            record['aggregate_result'] = None
            record['closure'] = _closure_ref(context, closure_path, closure)
            record['updated_at'] = _now()
            atomic_write_json(path, record)
            return {
                'status': 'system_failure',
                'reason': 'required_child_authority_incomplete',
                'task_set': record,
                'closure': closure,
                'failures': failures,
                'planner_intent_created': False,
            }
        aggregate_result, reason = _aggregate_required_results(evidence)
        existing_closure = _read_json(closure_path)
        if existing_closure:
            _validate_closure(existing_closure)
        if existing_closure and existing_closure.get('status') != 'system_failure':
            if (
                existing_closure.get('task_set_revision') != revision
                or existing_closure.get('ordered_terminal_evidence_digest') != evidence_digest
            ):
                raise ValueError('task-set closure conflicts with existing terminal evidence digest')
            closure = existing_closure
            closure_idempotent = True
        else:
            closure = _closure_record(
                record,
                evidence=evidence,
                evidence_digest=evidence_digest,
                aggregate_result=aggregate_result,
                status='closure_pending',
                reason=reason,
            )
            atomic_write_json(closure_path, closure)
            closure_idempotent = False
        intent, intent_idempotent = _ensure_closure_intent(context, record, closure)
        record['state'] = 'closure_pending'
        record['aggregate_result'] = aggregate_result
        record['closure'] = _closure_ref(context, closure_path, closure)
        record['updated_at'] = _now()
        atomic_write_json(path, record)
        return {
            'status': 'closure_pending',
            'task_set': record,
            'closure': closure,
            'closure_intent': intent,
            'idempotent': closure_idempotent and intent_idempotent,
            'planner_intent_created': not intent_idempotent,
        }


def find_pending_task_set_closures(context) -> dict[str, object]:
    runtime_root = Path(context.project.project_root) / '.ccb' / 'runtime' / 'task-sets'
    pending: list[dict[str, object]] = []
    stale: list[dict[str, object]] = []
    for path in sorted(runtime_root.glob('*/closure-intents.json')):
        store = _read_json(path)
        if store.get('schema') != INTENT_STORE_SCHEMA:
            raise ValueError(f'invalid task-set closure intent store: {path}')
        task_set_id = str(store.get('task_set_id') or '')
        record, task_set_path = _find_task_set(context, task_set_id)
        revision = record.get('task_set_revision')
        for raw in store.get('intents') or ():
            if not isinstance(raw, dict):
                raise ValueError('task-set closure intent must be an object')
            _validate_intent(raw)
            item = {**raw, 'task_set_path': str(task_set_path), 'intent_store_path': str(path)}
            if raw.get('status') == 'stale':
                stale.append(item)
                continue
            if raw.get('status') != 'pending_planner_backfill':
                continue
            if raw.get('task_set_revision') == revision and record.get('state') == 'closure_pending':
                pending.append(item)
            else:
                stale.append(item)
    return {
        'status': 'ok',
        'pending_count': len(pending),
        'pending': pending,
        'stale_count': len(stale),
        'stale': stale,
    }


def evaluate_current_task_set_closures(
    context,
    *,
    plan_task_fn: Callable = None,
) -> dict[str, object]:
    """Evaluate current task sets in stable path order, then expose closure intents."""
    plans_root = Path(context.project.project_root) / 'docs' / 'plantree' / 'plans'
    evaluated: list[dict[str, object]] = []
    for path in sorted(plans_root.glob('*/task-sets/*/task-set.json')):
        record = _read_json(path)
        _validate_task_set(record)
        state = str(record.get('state') or '')
        if state not in {'running', 'closure_pending', 'system_failure'}:
            continue
        result = evaluate_task_set_closure(
            context,
            task_set_id=str(record['task_set_id']),
            expected_revision=int(record['task_set_revision']),
            plan_task_fn=plan_task_fn,
        )
        evaluated.append(
            {
                'task_set_id': record['task_set_id'],
                'task_set_revision': record['task_set_revision'],
                'status': result.get('status'),
                'reason': result.get('reason'),
            }
        )
    discovered = find_pending_task_set_closures(context)
    return {**discovered, 'evaluated': evaluated}


def settle_task_set_closure_feedback(
    context,
    *,
    task_set_id: str,
    task_set_revision: int,
    intent_id: str,
    ordered_terminal_evidence_digest: str,
    transport_ref: dict[str, object],
    plan_task_fn: Callable = None,
) -> dict[str, object]:
    task_set_id = _segment(task_set_id, field='task_set_id')
    task_set_revision = _positive_int(task_set_revision, field='task_set_revision')
    intent_id = _segment(intent_id, field='intent_id')
    plan_task_fn = _plan_task_fn(plan_task_fn)
    task_set, task_set_path = _find_task_set(context, task_set_id)
    if task_set.get('task_set_revision') != task_set_revision or task_set.get('state') not in {'closure_pending', 'closed', 'partial', 'replan_required', 'blocked'}:
        raise ValueError('task-set closure settlement task-set authority mismatch')
    closure_path = task_set_path.parent / 'closure.json'
    closure = _read_json(closure_path)
    if closure.get('ordered_terminal_evidence_digest') != ordered_terminal_evidence_digest:
        raise ValueError('task-set closure settlement evidence mismatch')
    path = _runtime_task_set_root(context, task_set_id) / 'closure-intents.json'
    intent_store = _read_json(path)
    intent_matches = [
        item for item in intent_store.get('intents') or ()
        if isinstance(item, dict) and item.get('intent_id') == intent_id
    ]
    if len(intent_matches) != 1:
        raise ValueError('task-set closure intent not found or ambiguous')
    _validate_intent(intent_matches[0])
    if any(
        intent_matches[0].get(key) != expected
        for key, expected in {
            'task_set_revision': task_set_revision,
            'ordered_terminal_evidence_digest': ordered_terminal_evidence_digest,
            'closure_digest': closure.get('closure_digest'),
        }.items()
    ):
        raise ValueError('task-set closure feedback settlement authority mismatch')
    normalized_ref = _validate_feedback_transport(
        context,
        transport_ref,
        task_set=task_set,
        closure=closure,
        intent=intent_matches[0],
    )
    settlement_path = task_set_path.parent / f'closure-settlement-r{task_set_revision}.json'
    settlement = {
        'schema': 'ccb.plan.task_set_closure_settlement.v2',
        'schema_version': 2,
        'task_set_id': task_set_id,
        'task_set_revision': task_set_revision,
        'intent_id': intent_id,
        'aggregate_result': closure['aggregate_result'],
        'closure_digest': closure['closure_digest'],
        'ordered_terminal_evidence_digest': ordered_terminal_evidence_digest,
        'transport_ref': normalized_ref,
    }
    settlement['settlement_digest'] = _digest(settlement)
    with file_lock(task_set_path.parent / f'closure-settlement-r{task_set_revision}.lock'):
        with file_lock(task_set_path.parent / 'task-set.lock'):
            locked_task_set = _read_json(task_set_path)
            _validate_task_set(locked_task_set)
            locked_closure = _read_json(closure_path)
            _validate_closure(locked_closure)
            with file_lock(path.with_suffix('.lock')):
                locked_intents = _read_json(path)
            if (
                locked_task_set != task_set
                or locked_closure != closure
                or locked_intents != intent_store
            ):
                raise ValueError('task-set closure settlement authority changed during validation')
            existing_settlement = _read_json(settlement_path)
            if existing_settlement and existing_settlement != settlement:
                raise ValueError('task-set closure settlement transaction conflict')
            if not existing_settlement:
                atomic_write_json(settlement_path, settlement)
            from .plan_tasks import settle_task_set_parent
            settle_task_set_parent(
                context,
                task_id=str(locked_task_set['source_task_id']),
                task_set_id=task_set_id,
                task_set_revision=task_set_revision,
                aggregate_result=str(locked_closure['aggregate_result']),
                closure_digest=str(locked_closure['closure_digest']),
                planner_feedback_digest=str(normalized_ref['planner_feedback_digest']),
            )
            target_state = {
                'pass': 'closed', 'partial': 'partial',
                'replan_required': 'replan_required', 'blocked': 'blocked',
            }[str(locked_closure['aggregate_result'])]
            if locked_task_set.get('state') == 'closure_pending':
                locked_task_set['state'] = target_state
                locked_task_set['aggregate_result'] = locked_closure['aggregate_result']
                locked_task_set['updated_at'] = _now()
                atomic_write_json(task_set_path, locked_task_set)
            elif locked_task_set.get('state') != target_state:
                raise ValueError('task-set closure settlement state conflict')

    with file_lock(path.with_suffix('.lock')):
        store = _read_json(path)
        if store.get('schema') != INTENT_STORE_SCHEMA or store.get('task_set_id') != task_set_id:
            raise ValueError('task-set closure intent store authority mismatch')
        matches = [
            item for item in store.get('intents') or ()
            if isinstance(item, dict) and item.get('intent_id') == intent_id
        ]
        if len(matches) != 1:
            raise ValueError('task-set closure intent not found or ambiguous')
        intent = matches[0]
        _validate_intent(intent)
        expected = {
            'task_set_revision': task_set_revision,
            'ordered_terminal_evidence_digest': ordered_terminal_evidence_digest,
        }
        if any(intent.get(key) != value for key, value in expected.items()):
            raise ValueError('task-set closure feedback settlement authority mismatch')
        if intent.get('status') == 'feedback_closed':
            if intent.get('transport_ref') != normalized_ref:
                raise ValueError('task-set closure feedback settlement conflicts with persisted transport')
            return {'status': 'feedback_closed', 'intent': intent, 'idempotent': True}
        if intent.get('status') != 'pending_planner_backfill':
            raise ValueError('task-set closure intent is not pending feedback')
        intent['status'] = 'feedback_closed'
        intent['transport_ref'] = normalized_ref
        intent['feedback_closed_at'] = _now()
        atomic_write_json(path, store)
        return {'status': 'feedback_closed', 'intent': intent, 'idempotent': False}


def _validate_feedback_transport(context, value, *, task_set, closure, intent) -> dict[str, object]:
    allowed = {
        'runtime_state_path', 'planner_job_id', 'frontdesk_job_id',
        'planner_backfill_path', 'planner_feedback_digest', 'notification_status',
        'backfill_digest',
        'planner_source_job_id', 'planner_effective_job_id', 'planner_retry_lineage',
        'frontdesk_source_job_id', 'frontdesk_effective_job_id', 'frontdesk_retry_lineage',
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('task-set closure transport fields invalid')
    required = {
        'runtime_state_path', 'planner_job_id', 'planner_backfill_path',
        'planner_feedback_digest', 'notification_status', 'backfill_digest',
        'planner_source_job_id', 'planner_effective_job_id', 'planner_retry_lineage',
    }
    lineage_fields = {'planner_retry_lineage'}
    if (
        any(key not in value for key in required)
        or any(not value.get(key) for key in required - lineage_fields)
        or not isinstance(value.get('planner_retry_lineage'), list)
    ):
        raise ValueError('task-set closure transport authority incomplete')
    root = Path(context.project.project_root).resolve()
    runtime_path = _exact_authority_path(
        root,
        value['runtime_state_path'],
        root / '.ccb/runtime/task-sets' / str(task_set['task_set_id'])
        / f'feedback-r{task_set["task_set_revision"]}.json',
        label='task-set feedback runtime',
    )
    runtime = _read_json(runtime_path)
    _validate_closed_runtime(runtime, task_set=task_set, closure=closure, intent=intent)
    backfill_path = _exact_authority_path(
        root,
        value['planner_backfill_path'],
        root / 'docs/plantree/plans' / str(task_set['plan_slug']) / 'task-sets'
        / str(task_set['task_set_id']) / f'planner-backfill-r{task_set["task_set_revision"]}.json',
        label='planner backfill',
    )
    backfill = validate_planner_backfill_record(
        context, backfill_path, task_set=task_set, closure=closure
    )
    authority = backfill['authority']
    proposal = backfill['proposal']
    if (
        authority.get('planner_job_id') != value['planner_job_id']
        or authority.get('planner_source_job_id') != value['planner_source_job_id']
        or authority.get('planner_effective_job_id') != value['planner_effective_job_id']
        or authority.get('planner_retry_lineage') != value['planner_retry_lineage']
        or authority.get('planner_feedback_digest') != value['planner_feedback_digest']
        or backfill.get('backfill_digest') != value['backfill_digest']
    ):
        raise ValueError('task-set closure imported backfill authority mismatch')
    planner = runtime['planner']
    _validate_retry_lineage(context, planner)
    if planner['message'] != _expected_planner_message(closure, intent, task_set):
        raise ValueError('task-set closure Planner message authority mismatch')
    _validate_completed_job(context, planner, expected_target='planner')
    try:
        persisted_proposal = parse_planner_feedback_reply(str(planner['reply']))
    except ValueError as exc:
        raise ValueError('task-set closure Planner reply authority invalid') from exc
    if (
        json.loads(json.dumps(persisted_proposal.to_record(), sort_keys=True)) != proposal
        or planner_feedback_digest(persisted_proposal) != authority['planner_feedback_digest']
    ):
        raise ValueError('task-set closure Planner feedback identity mismatch')
    notification = str(value['notification_status'])
    if notification == 'delivered':
        job_id = str(value.get('frontdesk_job_id') or '')
        if proposal.get('frontdesk_notification_required') is not True:
            raise ValueError('task-set closure unexpected Frontdesk delivery')
        if not job_id or runtime['frontdesk'].get('effective_job_id') != job_id:
            raise ValueError('task-set closure Frontdesk delivery is incomplete')
        if runtime['frontdesk']['message'] != _expected_frontdesk_message(proposal, authority['planner_feedback_digest']):
            raise ValueError('task-set closure Frontdesk message authority mismatch')
        if (
            runtime['frontdesk'].get('source_job_id') != value.get('frontdesk_source_job_id')
            or runtime['frontdesk'].get('effective_job_id') != value.get('frontdesk_effective_job_id')
            or runtime['frontdesk'].get('retry_lineage') != value.get('frontdesk_retry_lineage')
        ):
            raise ValueError('task-set closure Frontdesk retry authority mismatch')
        _validate_retry_lineage(context, runtime['frontdesk'])
        _validate_completed_job(context, runtime['frontdesk'], expected_target='frontdesk')
    elif (
        notification != 'notification_not_required'
        or value.get('frontdesk_job_id')
        or proposal.get('frontdesk_notification_required') is not False
    ):
        raise ValueError('task-set closure notification authority invalid')
    return {key: value.get(key) for key in sorted(allowed) if key in value and value.get(key) is not None}


def _validate_closed_runtime(runtime, *, task_set, closure, intent) -> None:
    expected_fields = {
        'schema', 'schema_version', 'task_set_id', 'task_set_revision',
        'closure_intent_id', 'closure_digest', 'terminal_evidence_digest',
        'stage', 'planner', 'frontdesk', 'backfill_import', 'notification',
        'runtime_digest',
    }
    if (
        set(runtime) != expected_fields
        or runtime.get('schema') != 'ccb.plan.task_set_feedback_runtime.v1'
        or runtime.get('schema_version') != 1
        or runtime.get('stage') != 'closed'
    ):
        raise ValueError('task-set closure runtime schema or fields invalid')
    runtime_digest = 'sha256:' + hashlib.sha256(json.dumps(
        {key: value for key, value in runtime.items() if key != 'runtime_digest'},
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')).hexdigest()
    if runtime.get('runtime_digest') != runtime_digest:
        raise ValueError('task-set closure runtime digest invalid')
    expected = {
        'task_set_id': task_set['task_set_id'],
        'task_set_revision': task_set['task_set_revision'],
        'closure_intent_id': intent['intent_id'],
        'closure_digest': closure['closure_digest'],
        'terminal_evidence_digest': closure['ordered_terminal_evidence_digest'],
    }
    if any(runtime.get(key) != expected_value for key, expected_value in expected.items()):
        raise ValueError('task-set closure runtime identity mismatch')
    _validate_transport_shape(runtime.get('planner'), purpose='planner_backfill')
    if (
        runtime['planner'].get('target') != 'planner'
        or runtime['planner'].get('task_id') != f'task-set-feedback-{intent["intent_id"]}'
        or runtime['planner'].get('silent') is not True
    ):
        raise ValueError('task-set closure Planner transport identity mismatch')
    if runtime.get('frontdesk') is not None:
        _validate_transport_shape(runtime.get('frontdesk'), purpose='frontdesk_status')
        if (
            runtime['frontdesk'].get('target') != 'frontdesk'
            or runtime['frontdesk'].get('task_id') != f'task-set-status-{intent["intent_id"]}'
            or runtime['frontdesk'].get('silent') is not False
        ):
            raise ValueError('task-set closure Frontdesk transport identity mismatch')
    backfill_fields = {
        'task_set_id', 'task_set_revision', 'closure_intent_id', 'closure_digest',
        'ordered_terminal_evidence_digest', 'expected_plan_revision', 'planner_job_id',
        'planner_source_job_id', 'planner_effective_job_id', 'planner_retry_lineage',
        'planner_feedback_digest', 'plan_slug', 'status', 'idempotent',
        'planner_backfill_path', 'transaction_path', 'target_plan_revision',
        'backfill_digest',
    }
    if (
        not isinstance(runtime.get('backfill_import'), dict)
        or set(runtime['backfill_import']) != backfill_fields
        or not isinstance(runtime.get('notification'), dict)
    ):
        raise ValueError('task-set closure runtime import authority invalid')


def _validate_transport_shape(value, *, purpose) -> None:
    fields = {
        'target', 'purpose', 'task_id', 'message', 'message_sha256',
        'silent', 'job_id', 'source_job_id', 'effective_job_id', 'retry_lineage',
        'status', 'reply',
    }
    if not isinstance(value, dict) or set(value) != fields or value.get('purpose') != purpose:
        raise ValueError('task-set closure transport schema or fields invalid')
    message = str(value.get('message') or '')
    if not message or value.get('message_sha256') != hashlib.sha256(message.encode('utf-8')).hexdigest():
        raise ValueError('task-set closure transport message digest invalid')
    if value.get('status') != 'completed' or not str(value.get('job_id') or ''):
        raise ValueError('task-set closure transport is not completed')


def _validate_completed_job(context, transport, *, expected_target) -> None:
    if transport.get('target') != expected_target:
        raise ValueError('task-set closure transport target mismatch')
    layout = getattr(context, 'paths', None)
    if not isinstance(layout, PathLayout):
        layout = PathLayout(context.project.project_root)
    job = JobStore(layout).get_latest(expected_target, str(transport['effective_job_id']))
    if (
        job is None
        or job.status.value != 'completed'
        or not isinstance(job.terminal_decision, dict)
        or str(job.terminal_decision.get('status') or '') != 'completed'
        or job.terminal_decision.get('terminal') is not True
    ):
        raise ValueError('task-set closure persisted job is not terminal completed')
    if job.request.to_agent != expected_target or job.request.task_id != transport['task_id']:
        raise ValueError('task-set closure persisted job identity mismatch')
    body = str(job.request.body or '')
    if job.request.body_artifact:
        body = read_text_artifact(layout, job.request.body_artifact)
    if body != transport['message'] or hashlib.sha256(body.encode('utf-8')).hexdigest() != transport['message_sha256']:
        raise ValueError('task-set closure persisted job message mismatch')
    persisted_reply = _persisted_terminal_reply(layout, job)
    if persisted_reply != str(transport.get('reply') or ''):
        raise ValueError('task-set closure persisted job reply mismatch')


def _persisted_terminal_reply(layout: PathLayout, job) -> str:
    snapshot = CompletionSnapshotStore(layout).load(job.job_id)
    if snapshot is not None:
        decision = snapshot.latest_decision
        if (
            snapshot.agent_name != job.agent_name
            or decision.terminal is not True
            or decision.status.value != 'completed'
        ):
            raise ValueError('task-set closure persisted completion snapshot mismatch')
        reply = str(decision.reply or '')
        diagnostics = decision.diagnostics
    else:
        decision = job.terminal_decision
        reply = str(decision.get('reply') or '')
        diagnostics = decision.get('diagnostics') if isinstance(decision.get('diagnostics'), dict) else {}
    artifact = diagnostics.get('reply_artifact') if isinstance(diagnostics, dict) else None
    if isinstance(artifact, dict):
        reply = read_text_artifact(layout, artifact)
    return reply


def _validate_retry_lineage(context, transport) -> None:
    source = str(transport.get('source_job_id') or '')
    effective = str(transport.get('effective_job_id') or '')
    lineage = transport.get('retry_lineage')
    if not source or not effective or not isinstance(lineage, list) or transport.get('job_id') != source:
        raise ValueError('task-set closure retry lineage fields invalid')
    current = source
    seen = {source}
    layout = PathLayout(context.project.project_root)
    store = JobStore(layout)
    for edge in lineage:
        edge_fields = {
            'message_id', 'source_attempt_id', 'successor_attempt_id',
            'retry_source_job_id', 'retry_successor_job_id', 'retry_index',
        }
        if not isinstance(edge, dict) or set(edge) != edge_fields:
            raise ValueError('task-set closure retry lineage edge invalid')
        successor = str(edge['retry_successor_job_id'])
        if edge['retry_source_job_id'] != current or successor in seen:
            raise ValueError('task-set closure retry lineage chain invalid')
        source_job = store.get_latest(str(transport['target']), current)
        if (
            source_job is None
            or source_job.status.value not in {'failed', 'incomplete', 'cancelled'}
            or source_job.terminal_decision is None
        ):
            raise ValueError('task-set closure retry source is not terminal non-success')
        try:
            authority = authoritative_retry_successor(layout, current)
        except RetryLineageError as exc:
            raise ValueError(f'task-set closure retry lineage authority invalid: {exc}') from exc
        if authority is None or authority.to_record() != edge:
            raise ValueError('task-set closure retry successor authority mismatch')
        job = store.get_latest(str(transport['target']), successor)
        if job is None:
            raise ValueError('task-set closure retry successor job missing')
        body = str(job.request.body or '')
        if job.request.body_artifact:
            body = read_text_artifact(layout, job.request.body_artifact)
        if (
            job.request.to_agent != transport['target']
            or job.request.task_id != transport['task_id']
            or body != transport['message']
            or hashlib.sha256(body.encode('utf-8')).hexdigest() != transport['message_sha256']
        ):
            raise ValueError('task-set closure retry successor request mismatch')
        option_source = str(job.provider_options.get('retry_source_job_id') or '')
        if option_source and option_source != current:
            raise ValueError('task-set closure retry successor provider option mismatch')
        current = successor
        seen.add(successor)
    if current != effective or (not lineage and source != effective):
        raise ValueError('task-set closure retry effective job mismatch')


def _exact_authority_path(root: Path, value, expected: Path, *, label: str) -> Path:
    raw = Path(str(value or ''))
    candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if candidate != expected.resolve() or (candidate != root and root not in candidate.parents):
        raise ValueError(f'{label} path authority mismatch')
    return candidate


def _expected_planner_message(closure, intent, task_set) -> str:
    closure_ref = {
        'path': (
            f'docs/plantree/plans/{task_set["plan_slug"]}/task-sets/'
            f'{intent["task_set_id"]}/closure.json'
        ),
        'closure_digest': closure['closure_digest'],
        'ordered_terminal_evidence_digest': closure['ordered_terminal_evidence_digest'],
    }
    envelope = {
        'schema': 'ccb.plan.task_set_closure_transport.v1',
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
    body = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return '**task-set-closure.json**\n```json\n' + body + '\n```'


def _expected_frontdesk_message(proposal, feedback_digest) -> str:
    envelope = dict(proposal['frontdesk_status'])
    envelope['planner_feedback_digest'] = feedback_digest
    body = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return '**frontdesk-status.json**\n```json\n' + body + '\n```'


def _resolve_children(
    context,
    children: list[dict[str, object]],
    *,
    plan_slug: str,
    source_task_id: str,
    plan_task_fn: Callable,
) -> list[dict[str, object]]:
    if not children:
        raise ValueError('task set requires at least one child')
    resolved: list[dict[str, object]] = []
    seen: set[str] = set()
    for order, raw in enumerate(children):
        if not isinstance(raw, dict):
            raise ValueError(f'task-set child {order} must be an object')
        task_id = _segment(raw.get('task_id'), field=f'children[{order}].task_id')
        if task_id == source_task_id or task_id in seen:
            raise ValueError(f'task-set child identity is invalid or duplicate: {task_id}')
        required = raw.get('required', True)
        if not isinstance(required, bool):
            raise ValueError(f'task-set child required must be boolean: {task_id}')
        task = _show_task(context, task_id, plan_task_fn=plan_task_fn)
        if str(task.get('plan_slug') or '') != plan_slug:
            raise ValueError(f'task-set child is outside the owning plan: {task_id}')
        resolved.append(
            {
                'task_id': task_id,
                'task_revision': _positive_int(task.get('task_revision'), field='task_revision'),
                'required': required,
                'order': order,
            }
        )
        seen.add(task_id)
    if not any(child['required'] for child in resolved):
        raise ValueError('task set requires at least one required child')
    return resolved


def _bind_children(
    context,
    *,
    task_set_id: str,
    task_set_revision: int,
    children: list[dict[str, object]],
    plan_task_fn: Callable,
) -> list[dict[str, object]]:
    bindings = []
    for child in children:
        bindings.append(
            plan_task_fn(
                context,
                SimpleNamespace(
                    action='task-bind-task-set',
                    task_id=child['task_id'],
                    task_set_id=task_set_id,
                    task_set_revision=task_set_revision,
                    binding_role='child',
                    required=child['required'],
                    order=child['order'],
                    expected_task_revision=child['task_revision'],
                ),
            )
        )
    return bindings


def _child_evidence(context, child: dict[str, object], *, plan_slug: str, plan_task_fn: Callable) -> dict[str, object]:
    task_id = str(child['task_id'])
    task = _show_task(context, task_id, plan_task_fn=plan_task_fn)
    status = str(task.get('status') or '')
    base = {
        'task_id': task_id,
        'task_revision': task.get('task_revision'),
        'required': True,
        'status': status,
    }
    if str(task.get('plan_slug') or '') != plan_slug:
        return {**base, 'evidence_status': 'system_failure', 'reason': 'child_plan_drift'}
    if task.get('task_revision') != child.get('task_revision'):
        return {**base, 'evidence_status': 'system_failure', 'reason': 'stale_child_revision'}
    result = _TERMINAL_RESULTS.get(status)
    if result is None:
        return {**base, 'evidence_status': 'pending', 'reason': f'child_status_{status or "missing"}'}
    try:
        authority = _terminal_authority(context, task, status=status, result=result)
    except ValueError as exc:
        return {
            **base,
            'result': result,
            'evidence_status': 'system_failure',
            'reason': str(exc),
        }
    return {
        **base,
        'result': result,
        'evidence_status': 'terminal',
        'authority': authority,
        'evidence_digest': _digest({'task_id': task_id, 'task_revision': task['task_revision'], **authority}),
    }


def _terminal_authority(
    context,
    task: dict[str, object],
    *,
    status: str,
    result: str,
) -> dict[str, object]:
    if status == 'detail_ready':
        authority = detail_ready_stop_contract_authority(
            task,
            project_root=Path(context.project.project_root),
        )
        if authority is None:
            raise ValueError('terminal_child_detail_ready_authority_missing')
        return {
            'artifact_kind': 'detail_ready_stop_contract',
            'authority_digest': authority['authority_digest'],
            'basis_digest': authority['basis_digest'],
            'release': {'status': 'not_applicable_no_execution'},
            'cleanup': {'status': 'not_applicable_no_execution'},
        }
    artifacts = task.get('artifacts') if isinstance(task.get('artifacts'), dict) else {}
    last_round = task.get('last_round') if isinstance(task.get('last_round'), dict) else None
    if last_round is None:
        kind = 'blocker_evidence' if result == 'blocked' else 'macro_adjustment_request'
        artifact = artifacts.get(kind) if isinstance(artifacts, dict) else None
        if not isinstance(artifact, dict):
            raise ValueError('terminal_child_evidence_missing')
        artifact_digest = _verified_artifact_digest(context, artifact)
        return {
            'artifact_kind': kind,
            'artifact_digest': artifact_digest,
            'release': {'status': 'not_applicable_no_execution'},
            'cleanup': {'status': 'not_applicable_no_execution'},
        }
    if str(last_round.get('result') or '') != result:
        raise ValueError('terminal_child_round_result_mismatch')
    artifact = artifacts.get('round_summary') if isinstance(artifacts, dict) else None
    if not isinstance(artifact, dict):
        raise ValueError('terminal_child_round_summary_missing')
    artifact_digest = _verified_artifact_digest(context, artifact)
    loop_id = _segment(last_round.get('loop_id'), field='loop_id')
    round_path = Path(context.project.project_root) / '.ccb' / 'runtime' / 'loops' / loop_id / 'round.json'
    round_record = _read_json(round_path)
    if not round_record:
        raise ValueError('terminal_child_round_authority_missing')
    if str(round_record.get('task_id') or '') not in {'', str(task['task_id'])}:
        raise ValueError('terminal_child_round_task_mismatch')
    round_result = str(
        round_record.get('round_result')
        or _mapping(round_record.get('result')).get('value')
        or _mapping(round_record.get('authority_import')).get('round_result')
        or ''
    )
    if round_result and round_result != result:
        raise ValueError('terminal_child_round_record_result_mismatch')
    topology = _mapping(round_record.get('topology'))
    release = _mapping(round_record.get('release')) or _mapping(topology.get('release'))
    if (
        release.get('loop_topology_status') != 'released'
        or int(release.get('retained_count') or 0) != 0
        or int(release.get('release_incomplete_count') or 0) != 0
    ):
        raise ValueError('terminal_child_release_incomplete')
    cleanup_record = _mapping(round_record.get('cleanup'))
    if round_record.get('dispatch_source') == 'multi_workgroup_scheduler' or cleanup_record:
        if str(_mapping(cleanup_record.get('result')).get('status') or '') != 'complete':
            raise ValueError('terminal_child_cleanup_incomplete')
        cleanup = {'status': 'complete'}
    else:
        cleanup = {'status': 'not_applicable_single_round'}
    return {
        'artifact_kind': 'round_summary',
        'artifact_digest': artifact_digest,
        'loop_id': loop_id,
        'round_path': str(round_path.relative_to(context.project.project_root)),
        'round_digest': _file_digest(round_path),
        'release': {
            'status': 'released',
            'released_count': release.get('released_count'),
            'retained_count': release.get('retained_count'),
            'release_incomplete_count': release.get('release_incomplete_count', 0),
        },
        'cleanup': cleanup,
    }


def _aggregate_required_results(evidence: list[dict[str, object]]) -> tuple[str, str]:
    results = [str(item['result']) for item in evidence]
    if 'replan_required' in results:
        return 'replan_required', 'one_or_more_required_children_require_replan'
    if 'partial' in results:
        return 'partial', 'one_or_more_required_children_are_partial'
    if 'pass' in results and 'blocked' in results:
        return 'partial', 'required_children_include_pass_and_blocked'
    if results and all(result == 'blocked' for result in results):
        return 'blocked', 'all_required_children_are_blocked'
    if results and all(result == 'pass' for result in results):
        return 'pass', 'all_required_children_passed'
    raise ValueError(f'unsupported task-set aggregate results: {results}')


def _closure_record(
    record: dict[str, object],
    *,
    evidence: list[dict[str, object]],
    evidence_digest: str,
    aggregate_result: str | None,
    status: str,
    reason: str,
) -> dict[str, object]:
    payload = {
        'schema': CLOSURE_SCHEMA,
        'schema_version': 1,
        'task_set_id': record['task_set_id'],
        'task_set_revision': record['task_set_revision'],
        'source_request': record['source_request'],
        'planner_job': record['planner_job'],
        'expected_plan_revision': record['plan_revision']['digest'],
        'ordered_children': evidence,
        'ordered_terminal_evidence_digest': evidence_digest,
        'status': status,
        'aggregate_result': aggregate_result,
        'reason': reason,
        'created_at': _now(),
    }
    payload['closure_digest'] = _digest(payload)
    return payload


def _ensure_closure_intent(
    context,
    record: dict[str, object],
    closure: dict[str, object],
) -> tuple[dict[str, object], bool]:
    runtime_root = _runtime_task_set_root(context, str(record['task_set_id']))
    runtime_root.mkdir(parents=True, exist_ok=True)
    path = runtime_root / 'closure-intents.json'
    store = _read_json(path) or {
        'schema': INTENT_STORE_SCHEMA,
        'schema_version': 1,
        'task_set_id': record['task_set_id'],
        'intents': [],
    }
    if store.get('schema') != INTENT_STORE_SCHEMA or store.get('task_set_id') != record['task_set_id']:
        raise ValueError('task-set closure intent store authority mismatch')
    revision = record['task_set_revision']
    evidence_digest = closure['ordered_terminal_evidence_digest']
    for intent in store['intents']:
        if not isinstance(intent, dict):
            raise ValueError('task-set closure intent must be an object')
        _validate_intent(intent)
        if intent.get('task_set_revision') != revision:
            continue
        if intent.get('ordered_terminal_evidence_digest') != evidence_digest:
            raise ValueError('task-set closure intent conflicts with terminal evidence digest')
        return intent, True
    identity = {
        'task_set_id': record['task_set_id'],
        'task_set_revision': revision,
        'ordered_terminal_evidence_digest': evidence_digest,
    }
    intent = {
        'schema': INTENT_SCHEMA,
        'schema_version': 1,
        'intent_id': 'tsi-' + _digest(identity).split(':', 1)[1][:20],
        **identity,
        'closure_digest': closure['closure_digest'],
        'status': 'pending_planner_backfill',
        'created_at': _now(),
    }
    store['intents'].append(intent)
    atomic_write_json(path, store)
    return intent, False


def _stale_open_intents(context, task_set_id: str, *, current_revision: int) -> None:
    path = _runtime_task_set_root(context, task_set_id) / 'closure-intents.json'
    store = _read_json(path)
    if not store:
        return
    changed = False
    for intent in store.get('intents') or ():
        if (
            isinstance(intent, dict)
            and intent.get('task_set_revision') == current_revision
            and intent.get('status') == 'pending_planner_backfill'
        ):
            intent['status'] = 'stale'
            intent['stale_reason'] = 'task_set_revision_advanced'
            intent['stale_at'] = _now()
            changed = True
    if changed:
        atomic_write_json(path, store)


def _pending(record: dict[str, object], *, reason: str, child_task_ids: list[str] | None = None) -> dict[str, object]:
    return {
        'status': 'pending',
        'reason': reason,
        'task_set_id': record['task_set_id'],
        'task_set_revision': record['task_set_revision'],
        'child_task_ids': child_task_ids or [],
        'planner_intent_created': False,
    }


def _source_request(context, value: dict[str, object]) -> dict[str, object]:
    source_job_id = _segment(value.get('source_job_id'), field='source_request.source_job_id')
    digest = str(value.get('sha256') or '').strip()
    size = value.get('bytes')
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('task-set source request sha256 must be lowercase hex')
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError('task-set source request bytes must be a non-negative integer')
    result = {'source_job_id': source_job_id, 'sha256': digest, 'bytes': size}
    artifact = value.get('body_artifact') if isinstance(value.get('body_artifact'), dict) else None
    if artifact:
        result['body_artifact'] = _normalize_source_artifact(
            context, artifact, expected_bytes=size, expected_sha256=digest
        )
    return result


def _normalize_source_artifact(context, artifact, *, expected_bytes, expected_sha256):
    required = {'kind', 'path', 'bytes', 'sha256'}
    if not required <= set(artifact) or artifact.get('kind') != 'ask-request':
        raise ValueError('task-set source request artifact metadata invalid')
    size = artifact.get('bytes')
    digest = str(artifact.get('sha256') or '')
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or not re.fullmatch(r'[0-9a-f]{64}', digest)
        or size != expected_bytes
        or digest != expected_sha256
    ):
        raise ValueError('task-set source request artifact size or digest mismatch')
    root = Path(context.project.project_root)
    raw = Path(str(artifact.get('path') or ''))
    candidate = raw if raw.is_absolute() else root / raw
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError('task-set source request artifact outside project') from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('task-set source request artifact symlink forbidden')
    expected_parent = root / '.ccb/ccbd/artifacts/text/ask-request'
    if candidate.parent != expected_parent or '..' in raw.parts:
        raise ValueError('task-set source request artifact path invalid')
    try:
        data = candidate.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError('task-set source request artifact missing') from exc
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('task-set source request artifact content mismatch')
    return {
        'kind': 'ask-request',
        'path': relative.as_posix(),
        'bytes': size,
        'sha256': digest,
    }


def _planner_job(value: dict[str, object]) -> dict[str, object]:
    job_id = _segment(value.get('job_id'), field='planner_job.job_id')
    reply_digest = str(value.get('reply_sha256') or '').strip()
    if not re.fullmatch(r'[0-9a-f]{64}', reply_digest):
        raise ValueError('task-set planner reply_sha256 must be lowercase hex')
    return {'job_id': job_id, 'reply_sha256': reply_digest}


def _plan_revision(project_root: Path, plan_slug: str) -> dict[str, object]:
    context = SimpleNamespace(project=SimpleNamespace(project_root=project_root))
    return plan_revision_authority(context, plan_slug)


def _verified_artifact_digest(context, artifact: dict[str, object]) -> str:
    expected = str(artifact.get('sha256') or '')
    path = Path(context.project.project_root) / str(artifact.get('path') or '')
    try:
        resolved = path.resolve(strict=True)
        project_root = Path(context.project.project_root).resolve()
    except FileNotFoundError as exc:
        raise ValueError('terminal_child_artifact_missing') from exc
    if resolved != project_root and project_root not in resolved.parents:
        raise ValueError('terminal_child_artifact_outside_project')
    observed = _file_digest(resolved)
    if not expected or observed != expected:
        raise ValueError('terminal_child_artifact_digest_mismatch')
    return observed


def _find_task_set(context, task_set_id: str) -> tuple[dict[str, object], Path]:
    plans_root = Path(context.project.project_root) / 'docs' / 'plantree' / 'plans'
    matches = sorted(plans_root.glob(f'*/task-sets/{task_set_id}/task-set.json'))
    if len(matches) != 1:
        raise ValueError(f'task set not found or ambiguous: {task_set_id}')
    record = _read_json(matches[0])
    _validate_task_set(record)
    return record, matches[0]


def _validate_task_set(record: dict[str, object]) -> None:
    if record.get('schema') != TASK_SET_SCHEMA:
        raise ValueError('invalid task-set schema')
    _segment(record.get('task_set_id'), field='task_set_id')
    _positive_int(record.get('task_set_revision'), field='task_set_revision')
    missing = sorted(_TASK_SET_REQUIRED_KEYS - set(record))
    extra = sorted(set(record) - _TASK_SET_REQUIRED_KEYS - {'pending_revision', 'pending_children'})
    if missing or extra:
        raise ValueError(f'invalid task-set fields: missing={missing}, extra={extra}')
    state = str(record.get('state') or '')
    if state not in {
        'binding', 'running', 'revising', 'closure_pending', 'system_failure',
        'closed', 'partial', 'replan_required', 'blocked',
    }:
        raise ValueError('invalid task-set state')
    if state == 'revising' and not {'pending_revision', 'pending_children'} <= set(record):
        raise ValueError('revising task set is missing pending revision authority')
    children = record.get('children')
    if not isinstance(children, list) or not children:
        raise ValueError('invalid task-set children')
    task_ids: list[str] = []
    required_ids: list[str] = []
    for order, child in enumerate(children):
        if not isinstance(child, dict) or set(child) != {'task_id', 'task_revision', 'required', 'order'}:
            raise ValueError('invalid task-set child record')
        task_id = _segment(child.get('task_id'), field='child.task_id')
        _positive_int(child.get('task_revision'), field='child.task_revision')
        if not isinstance(child.get('required'), bool) or child.get('order') != order:
            raise ValueError('invalid task-set child membership authority')
        if task_id in task_ids:
            raise ValueError('duplicate task-set child authority')
        task_ids.append(task_id)
        if child['required']:
            required_ids.append(task_id)
    if record.get('ordered_required_children') != required_ids:
        raise ValueError('task-set ordered required children do not match child authority')


def _validate_closure(record: dict[str, object]) -> None:
    if record.get('schema') != CLOSURE_SCHEMA or set(record) != _CLOSURE_KEYS:
        raise ValueError('invalid task-set closure schema or fields')
    _positive_int(record.get('task_set_revision'), field='task_set_revision')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', str(record.get('expected_plan_revision') or '')):
        raise ValueError('invalid task-set closure expected plan revision digest')
    if record.get('status') not in {'closure_pending', 'system_failure'}:
        raise ValueError('invalid task-set closure status')
    expected = dict(record)
    digest = str(expected.pop('closure_digest') or '')
    if digest != _digest(expected):
        raise ValueError('task-set closure digest mismatch')


def _validate_intent(record: dict[str, object]) -> None:
    allowed = _INTENT_REQUIRED_KEYS | {
        'stale_reason', 'stale_at', 'transport_ref', 'feedback_closed_at',
    }
    if (
        record.get('schema') != INTENT_SCHEMA
        or not _INTENT_REQUIRED_KEYS <= set(record)
        or set(record) - allowed
    ):
        raise ValueError('invalid task-set closure intent schema or fields')
    if record.get('status') not in {'pending_planner_backfill', 'stale', 'feedback_closed'}:
        raise ValueError('invalid task-set closure intent status')
    if record.get('status') == 'feedback_closed' and (
        not isinstance(record.get('transport_ref'), dict)
        or not str(record.get('feedback_closed_at') or '')
    ):
        raise ValueError('closed task-set closure intent lacks transport authority')


def _show_task(context, task_id: str, *, plan_task_fn: Callable) -> dict[str, object]:
    payload = plan_task_fn(context, SimpleNamespace(action='task-show', task_id=task_id))
    task = payload.get('task') if isinstance(payload.get('task'), dict) else None
    if task is None:
        raise ValueError(f'task-set task authority missing: {task_id}')
    return task


def _task_set_root(context, plan_slug: str, task_set_id: str) -> Path:
    return (
        Path(context.project.project_root)
        / 'docs'
        / 'plantree'
        / 'plans'
        / plan_slug
        / 'task-sets'
        / task_set_id
    )


def _runtime_task_set_root(context, task_set_id: str) -> Path:
    return Path(context.project.project_root) / '.ccb' / 'runtime' / 'task-sets' / task_set_id


def _closure_ref(context, path: Path, closure: dict[str, object]) -> dict[str, object]:
    return {
        'path': str(path.relative_to(context.project.project_root)),
        'closure_digest': closure['closure_digest'],
        'ordered_terminal_evidence_digest': closure['ordered_terminal_evidence_digest'],
    }


def _new_task_set_id(plan_slug: str, source_task_id: str, planner_job_id: str) -> str:
    digest = hashlib.sha256(
        f'{plan_slug}\0{source_task_id}\0{planner_job_id}'.encode('utf-8')
    ).hexdigest()
    return f'ts-{digest[:20]}'


def _plan_task_fn(value: Callable | None) -> Callable:
    if value is not None:
        return value
    from .plan_tasks import plan_task

    return plan_task


def _segment(value: object, *, field: str) -> str:
    text = str(value or '').strip()
    if not _SEGMENT_RE.fullmatch(text):
        raise ValueError(f'{field} is invalid: {text!r}')
    return text


def _positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{field} must be a positive integer')
    return value


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f'invalid task-set JSON authority: {path}') from exc
    if not isinstance(payload, dict):
        raise ValueError(f'task-set JSON authority must be an object: {path}')
    return payload


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    'CLOSURE_SCHEMA',
    'INTENT_SCHEMA',
    'TASK_SET_SCHEMA',
    'create_task_set_authority',
    'evaluate_task_set_closure',
    'find_pending_task_set_closures',
    'revise_task_set_authority',
]
