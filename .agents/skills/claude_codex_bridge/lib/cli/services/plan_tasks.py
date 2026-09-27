from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from storage.atomic import atomic_write_json, atomic_write_text
from storage.locks import file_lock

from .loop_orchestration_bundle import (
    bundle_digest,
    bundle_text,
    load_task_orchestration_bundle,
    normalize_bundle_candidate,
    task_revision,
)
from .task_stop_contract import match_detail_ready_stop_contract
from .loop_effective_capacity import compile_project_effective_capacity_snapshot
from .planner_task_set_import_transaction import runner_transaction_committed


_SEGMENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
_SLUG_RE = re.compile(r'[^A-Za-z0-9_-]+')
_ARTIFACT_FILES = {
    'brief': 'brief.md',
    'task_packet': 'task_packet.md',
    'execution_contract': 'execution_contract.md',
    'orchestration_notes': 'orchestration_notes.md',
    'orchestration_bundle': 'orchestration_bundle.json',
    'round_summary': 'round_summary.md',
    'requirements': 'requirements.md',
    'acceptance': 'acceptance-criteria.md',
    'verification': 'verification-contract.md',
    'risk': 'risk-notes.md',
    'handoff': 'handoff.md',
    'detail_design': 'details/task-detail-design.md',
    'detail_summary': 'details/brief-update-summary.md',
    'detail_packet': 'details/detail-packet.manifest.json',
    'detail_step_1': 'details/steps/step-1.md',
    'detail_step_2': 'details/steps/step-2.md',
    'macro_adjustment_request': 'details/macro-adjustment-request.json',
    'blocker_evidence': 'blocker-evidence.md',
    'review': 'review.md',
    'completion': 'completion.md',
    'round_pass': 'round-pass.md',
    'round_partial': 'round-partial.md',
    'round_replan': 'round-replan.md',
    'round_blocker': 'round-blocker.md',
}
_PLAN_ROOT_ARTIFACTS = frozenset({'brief'})
_SEMANTIC_TASK_ARTIFACTS = frozenset(
    {
        'task_packet',
        'execution_contract',
        'detail_design',
        'detail_summary',
        'detail_packet',
        'orchestration_notes',
    }
)
_DETAIL_READY_REQUIRED = frozenset({'detail_design', 'detail_summary', 'detail_packet'})
_STOP_CONTRACT_ARTIFACTS = frozenset({'task_packet', 'execution_contract', 'orchestration_notes'})
_ORCHESTRATION_READY_REQUIRED = frozenset({'task_packet', 'execution_contract'})
_READY_REQUIRED = frozenset({'requirements', 'acceptance', 'verification', 'handoff', 'review'})
_PLAN_REVIEW_REQUIRED = frozenset({'requirements', 'acceptance', 'verification', 'handoff'})
_TERMINAL_STATUSES = frozenset({'done', 'blocked'})
_VALID_NEXT_OWNERS = frozenset({'planner', 'orchestrator', 'task_detailer', 'frontdesk', 'terminal'})
_VALID_ORCHESTRATOR_ROUTES = frozenset(
    {'direct_execution', 'needs_detail', 'macro_adjustment_request', 'blocked', 'partial_completion'}
)
_ROUND_RESULT_MAP = {
    'pass': ('round_summary', 'done', 'round_pass'),
    'partial': ('round_summary', 'partial', 'round_partial'),
    'replan_required': ('round_summary', 'replan_required', 'round_replan'),
    'blocked': ('round_summary', 'blocked', 'round_blocker'),
}
_PLANNER_COMPACT_IMPORT_POLICIES = {
    'detail_summary': {
        'policy': 'planner_compact_import',
        'authority': 'artifact_only_no_plan_mutation',
        'planner_action': 'review_for_brief_or_task_refs',
        'allowed_updates': ['brief', 'roadmap_status_handoff', 'decision_links', 'open_question_links', 'task_refs'],
        'forbidden_updates': ['detail_design_body', 'source_evidence_map', 'task_local_clarification', 'worker_handoff_detail'],
    },
    'macro_adjustment_request': {
        'policy': 'planner_compact_import',
        'authority': 'request_only_no_auto_mutation',
        'planner_action': 'review_before_plan_update',
        'allowed_updates': ['brief', 'roadmap_status_handoff', 'decision_links', 'open_question_links', 'task_refs'],
        'forbidden_updates': ['automatic_roadmap_mutation', 'automatic_decision_mutation', 'automatic_status_mutation'],
    },
    'round_summary': {
        'policy': 'planner_compact_import',
        'authority': 'script_owned_task_import_round',
        'planner_action': 'rehydrate_for_brief_or_next_task_planning',
        'allowed_updates': ['brief', 'roadmap_status_handoff', 'decision_links', 'open_question_links', 'task_refs'],
        'forbidden_updates': ['provider_reply_authority', 'worker_handoff_detail', 'source_evidence_map'],
    },
}
_MAINLINE_STATUSES = frozenset(
    {
        'draft',
        'decomposed',
        'ready_for_orchestration',
        'running',
        'partial',
        'replan_required',
        'done',
        'blocked',
    }
)
_LEGACY_STATUSES = frozenset({'needs_clarification', 'detail_ready', 'ready'})
_VALID_STATUSES = _MAINLINE_STATUSES | _LEGACY_STATUSES
_STATUS_EDGES = {
    'draft': {'draft', 'decomposed', 'needs_clarification', 'detail_ready', 'ready', 'ready_for_orchestration', 'done'},
    'decomposed': {'decomposed'},
    'needs_clarification': {'needs_clarification', 'draft'},
    'detail_ready': {'detail_ready', 'ready', 'ready_for_orchestration'},
    'ready': {'ready', 'running'},
    'ready_for_orchestration': {
        'ready_for_orchestration',
        'needs_clarification',
        'detail_ready',
        'running',
        'replan_required',
        'blocked',
        'done',
    },
    'running': {'running', 'partial', 'replan_required', 'done', 'blocked'},
    'partial': {'partial', 'replan_required', 'done'},
    'replan_required': {'replan_required', 'draft', 'ready_for_orchestration'},
    'blocked': {'blocked', 'draft'},
    'done': {'done'},
}


def plan_task(context, command) -> dict[str, object]:
    action = str(command.action or '').strip().lower()
    if action == 'task-create':
        return _task_create(context, command)
    if action == 'task-artifact':
        return _task_artifact(context, command)
    if action == 'task-status':
        return _task_status(context, command)
    if action == 'task-reconcile-detail-ready':
        return _task_reconcile_detail_ready(context, command)
    if action == 'task-accept-detailer-replan':
        return _task_accept_detailer_replan(context, command)
    if action == 'task-complete-detailer-replan':
        return _task_complete_detailer_replan(context, command)
    if action == 'task-bind-loop':
        return _task_bind_loop(context, command)
    if action == 'task-import-round':
        return _task_import_round(context, command)
    if action == 'task-bind-task-set':
        return _task_bind_task_set(context, command)
    if action == 'task-unbind-task-set':
        return _task_unbind_task_set(context, command)
    if action == 'task-show':
        return _task_show(context, command)
    if action == 'task-list':
        return _task_list(context, command)
    if action == 'breadcrumb':
        payload = _task_show(context, command)
        payload['breadcrumb'] = _breadcrumb_text(payload)
        return payload
    raise ValueError(f'unsupported plan task action: {action}')


def settle_task_set_parent(
    context,
    *,
    task_id: str,
    task_set_id: str,
    task_set_revision: int,
    aggregate_result: str,
    closure_digest: str,
    planner_feedback_digest: str,
) -> dict[str, object]:
    """Settle a decomposed source task exactly once from imported closure authority."""
    status_by_result = {
        'pass': 'done',
        'partial': 'partial',
        'replan_required': 'replan_required',
        'blocked': 'blocked',
    }
    if aggregate_result not in status_by_result:
        raise ValueError('task-set parent aggregate result invalid')
    task = _require_task(context, task_id)
    with file_lock(_task_lock_path(context, task['record'])):
        task = _require_task(context, task_id)
        record = _materialize_task_revision(task['record'])
        binding = record.get('task_set_parent') if isinstance(record.get('task_set_parent'), dict) else {}
        if binding.get('task_set_id') != task_set_id or binding.get('task_set_revision') != task_set_revision:
            raise ValueError('task-set parent binding authority mismatch')
        settlement = {
            'schema': 'ccb.plan.task_set_parent_settlement.v1',
            'task_set_id': task_set_id,
            'task_set_revision': task_set_revision,
            'aggregate_result': aggregate_result,
            'closure_digest': closure_digest,
            'planner_feedback_digest': planner_feedback_digest,
        }
        existing = record.get('task_set_closure')
        if existing is not None:
            if existing != settlement:
                raise ValueError('task-set parent settlement authority conflict')
            return {'status': 'settled', 'task': record, 'idempotent': True}
        if record.get('status') != 'decomposed':
            raise ValueError('task-set parent is not decomposed')
        status = status_by_result[aggregate_result]
        record['task_set_closure'] = settlement
        record['status'] = status
        record['owner'] = _owner_for_status(status)
        record['next_owner'] = _default_next_owner_for_status(status)
        record['activation_reason'] = f'task_set_closed:{task_set_id}:r{task_set_revision}'
        record['updated_at'] = _utc_now()
        _replace_record(task['tasks_root'], task['index'], record)
        _write_task_readme(context, record)
        return {'status': 'settled', 'task': record, 'idempotent': False}


def _task_create(context, command) -> dict[str, object]:
    plan_slug = _normalize_segment(command.plan_slug, label='plan')
    title = str(command.title or '').strip()
    if not title:
        raise ValueError('plan task-create requires --title')
    plan_root = _plan_root(context, plan_slug)
    if not plan_root.is_dir():
        raise ValueError(f'unknown plan slug {plan_slug!r}; expected {plan_root}')
    tasks_root = plan_root / 'tasks'
    index = _load_index(tasks_root, plan_slug=plan_slug, plan_root=plan_root)
    task_id = _normalize_task_id(command.task_id) if command.task_id else _new_task_id(title=title, tasks_root=tasks_root)
    if _find_task(context, task_id) is not None:
        raise ValueError(f'plan task already exists: {task_id}')
    now = _utc_now()
    task_root = tasks_root / task_id
    task_root.mkdir(parents=True, exist_ok=False)
    record = {
        'task_id': task_id,
        'title': title,
        'plan_slug': plan_slug,
        'plan_root': str(plan_root.relative_to(context.project.project_root)),
        'status': 'draft',
        'task_revision': 1,
        'state_version': 1,
        'current_loop': None,
        'owner': 'planner',
        'next_owner': 'planner',
        'activation_reason': 'task_created',
        'created_at': now,
        'updated_at': now,
        'task_root': str(task_root.relative_to(context.project.project_root)),
        'artifacts': {},
    }
    authority_trace = getattr(command, 'authority_trace', None)
    if isinstance(authority_trace, dict) and authority_trace:
        record['authority_trace'] = dict(authority_trace)
    index['tasks'].append(record)
    index['updated_at'] = now
    _write_index(tasks_root, index)
    _write_task_readme(context, record)
    return _payload(context, action='task-create', record=record)


def _task_artifact(context, command) -> dict[str, object]:
    task = _require_task(context, command.task_id)
    artifact_kind = str(command.artifact_kind or '').strip().lower()
    if artifact_kind not in _ARTIFACT_FILES:
        known = ', '.join(sorted(_ARTIFACT_FILES))
        raise ValueError(f'unknown plan artifact kind {artifact_kind!r}; expected one of: {known}')
    if artifact_kind == 'round_summary':
        raise ValueError('plan task-artifact cannot import round_summary directly; use plan task-import-round')
    with file_lock(_task_lock_path(context, task['record'])):
        locked_task = _require_task(context, command.task_id)
        record = _materialize_task_revision(locked_task['record'])
        _assert_expected_task_revision(command, record)
        if artifact_kind == 'orchestration_bundle':
            return _task_orchestration_bundle_artifact(
                context,
                command,
                task=locked_task,
                record=record,
            )
        extra: dict[str, object] = _planner_compact_import_extra(artifact_kind)
        route = _optional_orchestrator_route(command)
        if route:
            if artifact_kind != 'orchestration_notes':
                raise ValueError('plan task artifact --route is only valid for orchestration_notes')
            extra['orchestrator_route'] = route
        source_path = _safe_project_file(context.project.project_root, command.file_path)
        text = _read_utf8_artifact(source_path)
        text_sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
        existing = _artifact_record(record, artifact_kind)
        refresh_role_output_detail = (
            _is_role_output_detail_import(command, artifact_kind=artifact_kind)
            and existing is not None
            and int(existing.get('task_revision') or 0) != task_revision(record)
        )
        if (
            artifact_kind in _SEMANTIC_TASK_ARTIFACTS
            and existing is not None
            and str(existing.get('sha256') or '') == text_sha
            and not refresh_role_output_detail
        ):
            if route and str(existing.get('orchestrator_route') or '') != route:
                raise ValueError('semantic artifact metadata conflicts with byte-identical import')
            if 'task_revision' not in locked_task['record']:
                record['updated_at'] = _utc_now()
                _replace_record(locked_task['tasks_root'], locked_task['index'], record)
                _write_task_readme(context, record)
            payload = _payload(context, action='task-artifact', record=record)
            payload['artifact'] = dict(existing)
            payload['idempotent'] = True
            return payload
        if artifact_kind in _SEMANTIC_TASK_ARTIFACTS:
            _reject_running_semantic_mutation(record)
            if existing is not None and not _is_role_output_detail_import(command, artifact_kind=artifact_kind):
                record['task_revision'] = task_revision(record) + 1
            extra['task_revision'] = task_revision(record)
        record, artifact = _import_text_artifact(
            context,
            record,
            artifact_kind=artifact_kind,
            file_path=source_path,
            text=text,
            extra=extra,
            actor=_artifact_actor_metadata(context, command),
        )
        if artifact_kind in _STOP_CONTRACT_ARTIFACTS:
            record = _synchronize_stop_contract_revisions(record)
            artifact = dict(record['artifacts'][artifact_kind])
        now = str(artifact['imported_at'])
        record['updated_at'] = now
        _replace_record(locked_task['tasks_root'], locked_task['index'], record)
        _write_task_readme(context, record)
        payload = _payload(context, action='task-artifact', record=record)
        payload['artifact'] = artifact
        if artifact_kind in _SEMANTIC_TASK_ARTIFACTS:
            payload['idempotent'] = False
        return payload


def _task_orchestration_bundle_artifact(
    context,
    command,
    *,
    task: dict[str, object],
    record: dict[str, object],
) -> dict[str, object]:
    route = _orchestrator_route_for_record(record)
    if route not in {'direct_execution', 'partial_completion'}:
        raise ValueError('orchestration_bundle requires direct_execution or partial_completion orchestration_notes')
    source_path = _safe_project_file(context.project.project_root, command.file_path)
    source_text = _read_utf8_artifact(source_path)
    try:
        candidate = json.loads(source_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f'orchestration bundle candidate must be valid JSON: {exc}') from exc
    source = _first_text(
        getattr(command, 'actor_source', None),
        os.environ.get('CCB_ARTIFACT_SOURCE'),
        'script_owned_import',
    )
    supplied_capacity = getattr(command, 'effective_capacity_snapshot', None)
    capacity_snapshot = (
        supplied_capacity
        if supplied_capacity is not None
        else compile_project_effective_capacity_snapshot(Path(context.project.project_root))
    )
    normalized, work_packets = normalize_bundle_candidate(
        candidate,
        record=record,
        project_root=Path(context.project.project_root),
        capacity_snapshot=capacity_snapshot,
    )
    normalized_text = bundle_text(normalized)
    normalized_digest = bundle_digest(normalized)
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    existing = artifacts.get('orchestration_bundle') if isinstance(artifacts, dict) else None
    if isinstance(existing, dict):
        existing_digest = str(existing.get('bundle_digest') or existing.get('sha256') or '').strip()
        existing_revision = _positive_record_int(existing.get('bundle_revision'), field='bundle_revision')
        normalized_revision = int(normalized['bundle_revision'])
        if normalized_revision == existing_revision and existing_digest != normalized_digest:
            raise ValueError('plan task orchestration_bundle conflicts with existing bundle')
        if normalized_revision == existing_revision:
            existing_bundle, _existing_artifact = load_task_orchestration_bundle(
                Path(context.project.project_root),
                record,
                capacity_snapshot=capacity_snapshot,
            )
            if bundle_digest(existing_bundle) != normalized_digest:
                raise ValueError('plan task existing orchestration_bundle does not match its recorded digest')
            payload = _payload(context, action='task-artifact', record=record)
            payload['artifact'] = dict(existing)
            payload['idempotent'] = True
            return payload
        if normalized_revision != existing_revision + 1:
            raise ValueError(
                'plan task orchestration_bundle revision must increase exactly once: '
                f'expected {existing_revision + 1}, got {normalized_revision}'
            )
        if str(record.get('status') or '') == 'running' or str(record.get('current_loop') or '').strip():
            raise ValueError('plan task cannot replace orchestration_bundle while task is bound to running loop')
    elif int(normalized['bundle_revision']) != 1:
        raise ValueError('plan task first orchestration_bundle revision must be 1')

    for relative_path, packet_text in sorted(work_packets.items()):
        packet_path = Path(context.project.project_root) / relative_path
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(packet_path, packet_text)

    nodes = normalized.get('nodes') if isinstance(normalized.get('nodes'), list) else []
    record, artifact = _import_text_artifact(
        context,
        record,
        artifact_kind='orchestration_bundle',
        file_path=source_path,
        text=normalized_text,
        extra={
            'bundle_schema': normalized.get('schema'),
            'bundle_revision': normalized.get('bundle_revision'),
            'bundle_digest': normalized_digest,
            'task_revision': normalized.get('task_revision'),
            'task_digest': normalized.get('task_digest'),
            'capacity_digest': normalized.get('capacity_digest'),
            'capacity_snapshot': capacity_snapshot,
            'bundle_source': source,
            'source_reply_digest': _first_text(
                getattr(command, 'source_reply_digest', None),
                hashlib.sha256(source_text.encode('utf-8')).hexdigest(),
            ),
            'node_count': len(nodes),
            'node_ids': [str(node.get('node_id') or '') for node in nodes if isinstance(node, dict)],
        },
        actor=_artifact_actor_metadata(context, command),
    )
    now = str(artifact['imported_at'])
    record['updated_at'] = now
    _replace_record(task['tasks_root'], task['index'], record)
    _write_task_readme(context, record)
    payload = _payload(context, action='task-artifact', record=record)
    payload['artifact'] = artifact
    payload['idempotent'] = False
    return payload


def _task_status(context, command) -> dict[str, object]:
    task = _require_task(context, command.task_id)
    with file_lock(_task_lock_path(context, task['record'])):
        task = _require_task(context, command.task_id)
        record = _materialize_task_revision(task['record'])
        _assert_expected_task_revision(command, record)
        status = str(command.status or '').strip().lower()
        if status not in _VALID_STATUSES:
            known = ', '.join(sorted(_VALID_STATUSES))
            raise ValueError(f'unknown plan task status {status!r}; expected one of: {known}')
        current = str(record.get('status') or 'draft')
        if status not in _STATUS_EDGES.get(current, ()):
            raise ValueError(f'invalid plan task status transition: {current} -> {status}')
        _validate_status_requirements(record, status)
        next_owner = _next_owner_for_status_command(command, status=status)
        now = _utc_now()
        record['status'] = status
        record['owner'] = _owner_for_status(status)
        record['next_owner'] = next_owner
        record['activation_reason'] = _activation_reason_for_command(
            command,
            default=f'status:{current}->{status}',
        )
        record.pop('detail_ready_reconciliation', None)
        record['updated_at'] = now
        _replace_record(task['tasks_root'], task['index'], record)
        _write_task_readme(context, record)
        return _payload(context, action='task-status', record=record)


def _task_reconcile_detail_ready(context, command) -> dict[str, object]:
    task = _require_task(context, command.task_id)
    with file_lock(_task_lock_path(context, task['record'])):
        task = _require_task(context, command.task_id)
        record = _materialize_task_revision(task['record'])
        expected_authority = str(getattr(command, 'expected_authority_digest', None) or '').strip()
        if not re.fullmatch(r'[0-9a-f]{64}', expected_authority):
            raise ValueError('expected_authority_digest must be 64 lowercase hex characters')
        current_authority = _detail_ready_reconcile_authority(
            record,
            project_root=Path(context.project.project_root),
            allowed_statuses={'ready_for_orchestration', 'detail_ready'},
        )
        if current_authority is None:
            raise ValueError('stale detail_ready reconciliation authority')
        if (
            record.get('status') == 'detail_ready'
            and record.get('owner') == _owner_for_status('detail_ready')
            and record.get('next_owner') == 'planner'
            and not str(record.get('current_loop') or '').strip()
            and record.get('activation_reason') == 'reconciled_detail_ready_stop_contract'
            and isinstance(record.get('detail_ready_reconciliation'), dict)
            and record['detail_ready_reconciliation'].get('authority_digest') == expected_authority
            and record['detail_ready_reconciliation'].get('basis_digest') == current_authority['basis_digest']
            and record['detail_ready_reconciliation'].get('post_state_digest')
            == _post_reconcile_state_digest(record, basis_digest=current_authority['basis_digest'])
        ):
            payload = _payload(context, action='task-reconcile-detail-ready', record=record)
            payload['idempotent'] = True
            return payload
        if current_authority['authority_digest'] != expected_authority:
            raise ValueError('stale detail_ready reconciliation authority')
        expected = {
            'status': getattr(command, 'expected_status', None),
            'owner': getattr(command, 'expected_owner', None),
            'next_owner': getattr(command, 'expected_next_owner', None),
            'current_loop': getattr(command, 'expected_current_loop', None),
            'task_revision': getattr(command, 'expected_task_revision', None),
            'state_version': getattr(command, 'expected_state_version', None),
            'activation_reason': getattr(command, 'expected_activation_reason', None),
        }
        observed = {
            'status': record.get('status'),
            'owner': record.get('owner'),
            'next_owner': record.get('next_owner'),
            'current_loop': record.get('current_loop'),
            'task_revision': task_revision(record),
            'state_version': task_state_version(record),
            'activation_reason': record.get('activation_reason'),
        }
        if observed != expected:
            raise ValueError('stale detail_ready reconciliation task state')
        if record.get('status') != 'ready_for_orchestration':
            raise ValueError('detail_ready reconciliation requires ready_for_orchestration')
        now = _utc_now()
        record['status'] = 'detail_ready'
        record['owner'] = _owner_for_status('detail_ready')
        record['next_owner'] = 'planner'
        record['activation_reason'] = 'reconciled_detail_ready_stop_contract'
        record['detail_ready_reconciliation'] = {
            'authority_digest': expected_authority,
            'basis_digest': current_authority['basis_digest'],
            'post_state_digest': _post_reconcile_state_digest(
                record,
                basis_digest=current_authority['basis_digest'],
                state_version=task_state_version(record) + 1,
            ),
        }
        record['updated_at'] = now
        _replace_record(task['tasks_root'], task['index'], record)
        _write_task_readme(context, record)
        payload = _payload(context, action='task-reconcile-detail-ready', record=record)
        payload['idempotent'] = False
        return payload


def _task_accept_detailer_replan(context, command) -> dict[str, object]:
    task = _require_task(context, command.task_id)
    with file_lock(_task_lock_path(context, task['record'])):
        task = _require_task(context, command.task_id)
        record = _materialize_task_revision(task['record'])
        identity = _required_sha256(getattr(command, 'request_identity', None), field='request_identity')
        detail_digest = _required_sha256(getattr(command, 'detail_digest', None), field='detail_digest')
        macro_digest = _required_sha256(
            getattr(command, 'macro_impact_digest', None),
            field='macro_impact_digest',
        )
        source_revision = getattr(command, 'source_task_revision', None)
        if isinstance(source_revision, bool) or not isinstance(source_revision, int) or source_revision <= 0:
            raise ValueError('source_task_revision must be a positive integer')
        source_job_id = _normalize_job_id(getattr(command, 'source_detailer_job_id', None))
        planner_job_id = _optional_normalized_job_id(getattr(command, 'planner_job_id', None))
        existing = record.get('replan_feedback') if isinstance(record.get('replan_feedback'), dict) else None
        if existing is not None:
            expected = {
                'request_identity': identity,
                'detail_digest': detail_digest,
                'macro_impact_digest': macro_digest,
                'source_task_revision': source_revision,
                'source_detailer_job_id': source_job_id,
            }
            conflicts = [key for key, value in expected.items() if existing.get(key) != value]
            if conflicts:
                raise ValueError('detailer replan request identity conflict: ' + ', '.join(conflicts))
            current_planner_job = str(existing.get('planner_job_id') or '').strip()
            if current_planner_job and planner_job_id and current_planner_job != planner_job_id:
                raise ValueError('detailer replan request already references another Planner job')
            if planner_job_id and not current_planner_job:
                updated_feedback = dict(existing)
                updated_feedback['planner_job_id'] = planner_job_id
                updated_feedback['updated_at'] = _utc_now()
                record['replan_feedback'] = updated_feedback
                record['updated_at'] = updated_feedback['updated_at']
                _replace_record(task['tasks_root'], task['index'], record)
                _write_task_readme(context, record)
            payload = _payload(context, action='task-accept-detailer-replan', record=record)
            payload['idempotent'] = True
            return payload

        current_revision = task_revision(record)
        if source_revision != current_revision:
            raise ValueError(
                'stale detailer replan task revision: '
                f'expected {source_revision}, current {current_revision}'
            )
        current_status = str(record.get('status') or 'draft')
        if current_status not in {
            'draft',
            'needs_clarification',
            'detail_ready',
            'ready_for_orchestration',
        }:
            raise ValueError(f'detailer replan cannot fence task status: {current_status}')
        if str(record.get('current_loop') or '').strip():
            raise ValueError('detailer replan cannot fence a task bound to a running loop')
        next_revision = source_revision + 1
        artifacts = dict(record.get('artifacts') or {})
        superseded: list[str] = []
        for kind in ('orchestration_notes', 'orchestration_bundle'):
            artifact = artifacts.get(kind)
            if not isinstance(artifact, dict):
                continue
            marked = dict(artifact)
            marked['authority_status'] = 'superseded'
            marked['superseded_by'] = identity
            marked['superseded_at_task_revision'] = next_revision
            artifacts[kind] = marked
            superseded.append(kind)
        now = _utc_now()
        record['artifacts'] = artifacts
        record['task_revision'] = next_revision
        record['status'] = 'replan_required'
        record['owner'] = 'planner'
        record['next_owner'] = 'planner'
        record['activation_reason'] = 'planner_replan_required_from_task_detailer'
        record['current_loop'] = None
        record['replan_feedback'] = {
            'schema': 'ccb.detailer.replan_acceptance.v1',
            'request_identity': identity,
            'detail_digest': detail_digest,
            'macro_impact_digest': macro_digest,
            'source_task_revision': source_revision,
            'accepted_task_revision': next_revision,
            'source_detailer_job_id': source_job_id,
            'planner_job_id': planner_job_id,
            'superseded_artifacts': superseded,
            'accepted_at': now,
            'updated_at': now,
        }
        record['updated_at'] = now
        _replace_record(task['tasks_root'], task['index'], record)
        _write_task_readme(context, record)
        payload = _payload(context, action='task-accept-detailer-replan', record=record)
        payload['idempotent'] = False
        return payload


def _task_complete_detailer_replan(context, command) -> dict[str, object]:
    task = _require_task(context, command.task_id)
    with file_lock(_task_lock_path(context, task['record'])):
        task = _require_task(context, command.task_id)
        record = _materialize_task_revision(task['record'])
        expected = getattr(command, 'expected_task_revision', None)
        if expected != task_revision(record):
            raise ValueError('detailer replan completion revision conflict')
        feedback = record.get('replan_feedback') if isinstance(record.get('replan_feedback'), dict) else {}
        job_id = _normalize_job_id(getattr(command, 'planner_job_id', None))
        if feedback.get('planner_job_id') != job_id:
            raise ValueError('detailer replan completion Planner job binding conflict')
        digest = _required_sha256(getattr(command, 'planner_feedback_digest', None), field='planner_feedback_digest')
        backfill_path = str(getattr(command, 'backfill_path', None) or '').strip()
        if not backfill_path:
            raise ValueError('detailer replan completion backfill path required')
        existing = record.get('planner_replan_backfill') if isinstance(record.get('planner_replan_backfill'), dict) else None
        expected_record = {'planner_job_id': job_id, 'planner_feedback_digest': digest, 'backfill_path': backfill_path}
        if existing is not None and existing != expected_record:
            raise ValueError('detailer replan completion authority conflict')
        if existing is not None:
            if record.get('status') != 'ready_for_orchestration' or record.get('current_loop') is not None:
                raise ValueError('detailer replan completion replay state conflict')
            payload = _payload(context, action='task-complete-detailer-replan', record=record)
            payload['idempotent'] = True
            return payload
        if record.get('status') != 'replan_required':
            raise ValueError('detailer replan completion status conflict')
        record['planner_replan_backfill'] = expected_record
        record['status'] = 'ready_for_orchestration'
        record['owner'] = 'orchestrator'
        record['next_owner'] = 'orchestrator'
        record['activation_reason'] = 'detailer_replan_planner_backfill_imported'
        record['current_loop'] = None
        record['updated_at'] = _utc_now()
        _replace_record(task['tasks_root'], task['index'], record)
        _write_task_readme(context, record)
        payload = _payload(context, action='task-complete-detailer-replan', record=record)
        payload['idempotent'] = False
        return payload


def _task_bind_loop(context, command) -> dict[str, object]:
    loop_id = _normalize_segment(getattr(command, 'loop_id', None), label='loop_id')
    task = _require_task(context, command.task_id)
    with file_lock(_task_lock_path(context, task['record'])):
        task = _require_task(context, command.task_id)
        record = _materialize_task_revision(task['record'])
        _assert_expected_task_revision(command, record)
        current_status = str(record.get('status') or 'draft')
        current_loop = str(record.get('current_loop') or '').strip()
        if current_status in _TERMINAL_STATUSES:
            raise ValueError(f'plan task cannot bind terminal status: {current_status}')
        if current_status == 'running':
            if current_loop == loop_id:
                return _payload(context, action='task-bind-loop', record=record)
            raise ValueError(f'plan task already bound to loop: {current_loop or "unknown"}')
        if current_status not in {'ready_for_orchestration', 'ready'}:
            raise ValueError(
                'plan task bind requires status ready_for_orchestration '
                f'or legacy ready; current status is {current_status}'
            )
        _validate_status_requirements(record, current_status)
        if current_loop and current_loop != loop_id:
            raise ValueError(f'plan task already bound to loop: {current_loop}')
        now = _utc_now()
        record['status'] = 'running'
        record['owner'] = 'loop_runner'
        record['next_owner'] = 'orchestrator'
        record['activation_reason'] = f'loop_bound:{loop_id}'
        record['current_loop'] = loop_id
        record['loop_lease'] = {
            'loop_id': loop_id,
            'lease_id': f'lease-{uuid4().hex[:12]}',
            'status': 'active',
            'owner': 'loop_runner',
            'acquired_at': now,
        }
        record['updated_at'] = now
        _replace_record(task['tasks_root'], task['index'], record)
        _write_task_readme(context, record)
        return _payload(context, action='task-bind-loop', record=record)


def _task_import_round(context, command) -> dict[str, object]:
    loop_id = _normalize_segment(getattr(command, 'loop_id', None), label='loop_id')
    result = str(getattr(command, 'result', None) or '').strip().lower()
    if result not in _ROUND_RESULT_MAP:
        known = ', '.join(sorted(_ROUND_RESULT_MAP))
        raise ValueError(f'unknown round result {result!r}; expected one of: {known}')
    artifact_kind, target_status, legacy_artifact_kind = _ROUND_RESULT_MAP[result]
    source_path = _safe_project_file(context.project.project_root, command.file_path)
    text = _read_utf8_artifact(source_path)
    text_sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    task = _require_task(context, command.task_id)
    with file_lock(_task_lock_path(context, task['record'])):
        task = _require_task(context, command.task_id)
        record = _materialize_task_revision(task['record'])
        _assert_expected_task_revision(command, record)
        existing = _existing_round_import(record, loop_id=loop_id, result=result, sha256=text_sha)
        if existing is not None:
            payload = _payload(context, action='task-import-round', record=record)
            payload['artifact'] = existing
            payload['legacy_artifact'] = _legacy_artifact_for_round(record, legacy_artifact_kind)
            payload['round_result'] = result
            payload['idempotent'] = True
            return payload
        current_loop = str(record.get('current_loop') or '').strip()
        if current_loop != loop_id:
            raise ValueError(
                f'plan task round import requires current_loop={loop_id}; '
                f'current_loop is {current_loop or "none"}'
            )
        current_status = str(record.get('status') or 'draft')
        if current_status != 'running':
            raise ValueError(f'plan task round import requires status running; current status is {current_status}')
        record, artifact = _import_text_artifact(
            context,
            record,
            artifact_kind=artifact_kind,
            file_path=command.file_path,
            text=text,
            extra={
                'loop_id': loop_id,
                'round_result': result,
                'legacy_artifact_kind': legacy_artifact_kind,
                **_planner_compact_import_extra(artifact_kind),
            },
            actor=_artifact_actor_metadata(context, command),
        )
        record = _add_round_legacy_alias(record, artifact=artifact, legacy_artifact_kind=legacy_artifact_kind)
        now = str(artifact['imported_at'])
        counters = dict(record.get('round_counters') or {})
        counters[result] = int(counters.get(result) or 0) + 1
        record['round_counters'] = counters
        record['last_round'] = {
            'loop_id': loop_id,
            'result': result,
            'artifact_kind': artifact_kind,
            'legacy_artifact_kind': legacy_artifact_kind,
            'artifact_path': artifact.get('path'),
            'sha256': artifact.get('sha256'),
            'imported_at': artifact.get('imported_at'),
        }
        record['status'] = target_status
        record['owner'] = _owner_for_status(target_status)
        record['next_owner'] = _default_next_owner_for_status(target_status)
        record['activation_reason'] = f'round_summary:{result}'
        record['current_loop'] = None
        lease = dict(record.get('loop_lease') or {})
        if lease:
            lease['status'] = 'imported'
            lease['released_at'] = now
            record['loop_lease'] = lease
        record['updated_at'] = now
        _replace_record(task['tasks_root'], task['index'], record)
        _write_task_readme(context, record)
        payload = _payload(context, action='task-import-round', record=record)
        payload['artifact'] = artifact
        payload['legacy_artifact'] = _legacy_artifact_for_round(record, legacy_artifact_kind)
        payload['round_result'] = result
        payload['idempotent'] = False
        return payload


def _task_bind_task_set(context, command) -> dict[str, object]:
    task_set_id = _normalize_segment(getattr(command, 'task_set_id', None), label='task_set_id')
    task_set_revision = _positive_record_int(
        getattr(command, 'task_set_revision', None),
        field='task_set_revision',
    )
    binding_role = str(getattr(command, 'binding_role', None) or '').strip().lower()
    if binding_role not in {'parent', 'child'}:
        raise ValueError('plan task-set binding_role must be parent or child')
    task = _require_task(context, command.task_id)
    with file_lock(_task_lock_path(context, task['record'])):
        task = _require_task(context, command.task_id)
        record = _materialize_task_revision(task['record'])
        _assert_expected_task_revision(command, record)
        field = 'task_set_parent' if binding_role == 'parent' else 'task_set'
        required = getattr(command, 'required', True)
        if not isinstance(required, bool):
            raise ValueError('plan task-set child required must be boolean')
        order = getattr(command, 'order', None)
        if binding_role == 'child':
            if isinstance(order, bool) or not isinstance(order, int) or order < 0:
                raise ValueError('plan task-set child order must be a non-negative integer')
        binding = {
            'schema': 'ccb.plan.task_set_binding.v1',
            'task_set_id': task_set_id,
            'task_set_revision': task_set_revision,
            'binding_role': binding_role,
            'bound_task_revision': task_revision(record),
        }
        if binding_role == 'child':
            binding['required'] = required
            binding['order'] = order
        existing = record.get(field) if isinstance(record.get(field), dict) else None
        if existing is not None:
            existing_revision = _positive_record_int(
                existing.get('task_set_revision'),
                field='task_set_revision',
            )
            if str(existing.get('task_set_id') or '') != task_set_id:
                raise ValueError('plan task is already bound to a different task set')
            if existing_revision > task_set_revision:
                raise ValueError('plan task-set binding revision is stale')
            if existing_revision == task_set_revision:
                if existing != binding:
                    raise ValueError('plan task-set binding conflicts with existing authority')
                payload = _payload(context, action='task-bind-task-set', record=record)
                payload['binding'] = dict(existing)
                payload['idempotent'] = True
                return payload
        if binding_role == 'parent':
            status = str(record.get('status') or 'draft')
            if status not in {'draft', 'decomposed', 'ready_for_orchestration'}:
                raise ValueError(f'plan task-set parent cannot bind from status {status}')
            record['status'] = 'decomposed'
            record['owner'] = 'planner'
            record['next_owner'] = 'planner'
            record['activation_reason'] = f'task_set_decomposed:{task_set_id}:r{task_set_revision}'
        record[field] = binding
        record['updated_at'] = _utc_now()
        _replace_record(task['tasks_root'], task['index'], record)
        _write_task_readme(context, record)
        payload = _payload(context, action='task-bind-task-set', record=record)
        payload['binding'] = binding
        payload['idempotent'] = False
        return payload


def _task_unbind_task_set(context, command) -> dict[str, object]:
    task_set_id = _normalize_segment(getattr(command, 'task_set_id', None), label='task_set_id')
    expected_revision = _positive_record_int(
        getattr(command, 'task_set_revision', None),
        field='task_set_revision',
    )
    task = _require_task(context, command.task_id)
    with file_lock(_task_lock_path(context, task['record'])):
        task = _require_task(context, command.task_id)
        record = _materialize_task_revision(task['record'])
        binding = record.get('task_set') if isinstance(record.get('task_set'), dict) else None
        if binding is None:
            return _payload(context, action='task-unbind-task-set', record=record)
        if (
            str(binding.get('task_set_id') or '') != task_set_id
            or binding.get('task_set_revision') != expected_revision
        ):
            raise ValueError('plan task-set unbind authority does not match current binding')
        record.pop('task_set', None)
        record['updated_at'] = _utc_now()
        _replace_record(task['tasks_root'], task['index'], record)
        _write_task_readme(context, record)
        return _payload(context, action='task-unbind-task-set', record=record)


def _task_show(context, command) -> dict[str, object]:
    task = _require_task(context, command.task_id)
    return _payload(context, action='task-show', record=task['record'])


def _task_list(context, command) -> dict[str, object]:
    plan_slug = _normalize_segment(command.plan_slug, label='plan')
    plan_root = _plan_root(context, plan_slug)
    if not plan_root.is_dir():
        raise ValueError(f'unknown plan slug {plan_slug!r}; expected {plan_root}')
    tasks_root = plan_root / 'tasks'
    index = _load_index(tasks_root, plan_slug=plan_slug, plan_root=plan_root)
    for record in tuple(index.get('tasks') or ()):
        if isinstance(record, dict):
            _validate_task_record(record)
    return {
        'plan_task_status': 'ok',
        'action': 'task-list',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'plan_slug': plan_slug,
        'plan_root': str(plan_root.relative_to(context.project.project_root)),
        'tasks_root': str(tasks_root.relative_to(context.project.project_root)),
        'task_count': len(tuple(index.get('tasks') or ())),
        'tasks': tuple(index.get('tasks') or ()),
    }


def _validate_status_requirements(record: dict[str, object], status: str) -> None:
    artifacts = set((record.get('artifacts') or {}).keys()) if isinstance(record.get('artifacts'), dict) else set()
    if status == 'ready_for_orchestration':
        missing = sorted(_ORCHESTRATION_READY_REQUIRED - artifacts)
        if missing:
            raise ValueError(f'plan task ready_for_orchestration requires artifacts: {", ".join(missing)}')
    if status == 'ready':
        missing = sorted(_READY_REQUIRED - artifacts)
        if missing:
            raise ValueError(f'plan task ready requires artifacts: {", ".join(missing)}')
    if status == 'detail_ready':
        missing = sorted(_DETAIL_READY_REQUIRED - artifacts)
        if missing:
            raise ValueError(f'plan task detail_ready requires artifacts: {", ".join(missing)}')
    if status == 'done' and 'completion' not in artifacts and not _has_round_evidence(record, result='pass'):
        raise ValueError('plan task done requires artifact: completion or round_summary')
    if status == 'partial' and not _has_round_evidence(record, result='partial'):
        raise ValueError('plan task partial requires artifact: round_summary')
    if (
        status == 'replan_required'
        and 'macro_adjustment_request' not in artifacts
        and not _has_round_evidence(record, result='replan_required')
    ):
        raise ValueError('plan task replan_required requires artifact: round_summary or macro_adjustment_request')
    if (
        status == 'blocked'
        and 'completion' not in artifacts
        and 'blocker_evidence' not in artifacts
        and not _has_round_evidence(record, result='blocked')
    ):
        raise ValueError('plan task blocked requires artifact: completion, round_summary, or blocker_evidence')


def _has_round_evidence(record: dict[str, object], *, result: str) -> bool:
    artifact_kind, _target_status, legacy_artifact_kind = _ROUND_RESULT_MAP[result]
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    artifact = artifacts.get(artifact_kind) if isinstance(artifacts, dict) else None
    if isinstance(artifact, dict) and str(artifact.get('round_result') or '') == result:
        return True
    legacy = artifacts.get(legacy_artifact_kind) if isinstance(artifacts, dict) else None
    return isinstance(legacy, dict)


def _owner_for_status(status: str) -> str:
    if status == 'needs_clarification':
        return 'task_detailer'
    if status in {'draft', 'decomposed', 'replan_required'}:
        return 'planner'
    if status == 'detail_ready':
        return 'plan_reviewer'
    if status in {'ready', 'ready_for_orchestration', 'running'}:
        return 'loop_runner'
    if status == 'partial':
        return 'planner'
    return 'frontdesk'


def _default_next_owner_for_status(status: str) -> str:
    if status in {'draft', 'decomposed', 'partial', 'replan_required', 'detail_ready'}:
        return 'planner'
    if status in {'ready_for_orchestration', 'ready', 'running'}:
        return 'orchestrator'
    if status == 'needs_clarification':
        return 'task_detailer'
    return 'terminal'


def _next_owner_for_status_command(command, *, status: str) -> str:
    raw = str(getattr(command, 'next_owner', None) or '').strip().lower()
    next_owner = raw or _default_next_owner_for_status(status)
    if next_owner not in _VALID_NEXT_OWNERS:
        known = ', '.join(sorted(_VALID_NEXT_OWNERS))
        raise ValueError(f'unknown plan task next_owner {next_owner!r}; expected one of: {known}')
    _validate_status_next_owner(status=status, next_owner=next_owner)
    return next_owner


def _validate_status_next_owner(*, status: str, next_owner: str) -> None:
    expected = _default_next_owner_for_status(status)
    if next_owner != expected:
        raise ValueError(f'plan task status {status} requires next_owner {expected}; got {next_owner}')


def _activation_reason_for_command(command, *, default: str) -> str:
    reason = str(getattr(command, 'activation_reason', None) or '').strip()
    reason = reason or default
    _validate_activation_reason_text(reason)
    return reason


def _validate_activation_reason_text(reason: str) -> None:
    if '\n' in reason or '\r' in reason:
        raise ValueError('plan task activation_reason must be a single line')
    if len(reason) > 240:
        raise ValueError('plan task activation_reason must be 240 characters or fewer')


def _optional_orchestrator_route(command) -> str:
    route = str(getattr(command, 'route', None) or '').strip().lower()
    if not route:
        return ''
    if route not in _VALID_ORCHESTRATOR_ROUTES:
        known = ', '.join(sorted(_VALID_ORCHESTRATOR_ROUTES))
        raise ValueError(f'unknown orchestrator route {route!r}; expected one of: {known}')
    return route


def _payload(context, *, action: str, record: dict[str, object]) -> dict[str, object]:
    record = _materialize_task_revision(record)
    task_root = Path(context.project.project_root) / str(record.get('task_root') or '')
    _validate_task_record(record)
    return {
        'plan_task_status': 'ok',
        'action': action,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'task': record,
        'task_id': record.get('task_id'),
        'status': record.get('status'),
        'next_owner': record.get('next_owner'),
        'current_loop': record.get('current_loop'),
        'activation_reason': record.get('activation_reason'),
        'plan_slug': record.get('plan_slug'),
        'task_root': str(task_root.relative_to(context.project.project_root)),
        'readme_path': str((task_root / 'README.md').relative_to(context.project.project_root)),
    }


_STATUS_STOP_PATTERNS = {
    'replan_required': (
        r'\bstop(?:s|ped|ping)?\s+(?:at|as|on)\s+`?replan_required`?\b',
        r'\bcontroller-visible\s+task\s+outcome\s+remains\s+`?replan_required`?\b',
        r'\bexpected\s+controller-visible\s+(?:task\s+)?(?:outcome|status|stop)\s+is\s+`?replan_required`?\b',
    ),
}


def find_first_ready_task(context) -> dict[str, object] | None:
    plantree_root = Path(context.project.project_root) / 'docs' / 'plantree' / 'plans'
    if not plantree_root.is_dir():
        return None
    for index_path in sorted(plantree_root.glob('*/tasks/index.json')):
        tasks_root = index_path.parent
        index = _read_json_object(index_path)
        for record in tuple(index.get('tasks') or ()):
            if not isinstance(record, dict):
                continue
            _validate_task_record(record)
            action = _runner_action_for_record(record, project_root=context.project.project_root)
            if (
                action is not None
                and action['action'] == 'execute'
                and not str(record.get('current_loop') or '').strip()
            ):
                return {
                    'record': record,
                    'index': index,
                    'tasks_root': tasks_root,
                    'runner_action': 'execute',
                    'runner_reason': action['reason'],
                    'next_owner': 'orchestrator',
                }
    return None


def find_first_actionable_task(context, *, task_id: str | None = None) -> dict[str, object] | None:
    requested_task_id = str(task_id or '').strip()
    plantree_root = Path(context.project.project_root) / 'docs' / 'plantree' / 'plans'
    if not plantree_root.is_dir():
        return None
    candidates: list[dict[str, object]] = []
    for index_path in sorted(plantree_root.glob('*/tasks/index.json')):
        tasks_root = index_path.parent
        index = _read_json_object(index_path)
        for record in tuple(index.get('tasks') or ()):
            if not isinstance(record, dict):
                continue
            _validate_task_record(record)
            if requested_task_id and str(record.get('task_id') or '') != requested_task_id:
                continue
            action = _runner_action_for_record(record, project_root=context.project.project_root)
            if action is None:
                continue
            candidates.append({
                'record': record,
                'index': index,
                'tasks_root': tasks_root,
                'runner_action': action['action'],
                'runner_reason': action['reason'],
                'next_owner': action['next_owner'],
            })
    if not candidates:
        return None
    priority = {
        'activate_orchestrator': 0,
        'reconcile_detail_ready': 0,
        'ask_first_execute': 0,
        'ask_first_execution_not_ready': 0,
        'execute': 0,
        'activate_task_detailer': 1,
        'planner_next_action_required': 1,
        'blocker_evidence_required': 1,
        'activate_planner': 2,
        'activate_plan_reviewer': 3,
        'paused': 4,
        'blocked': 5,
        'terminal': 6,
    }
    return min(candidates, key=lambda item: _runner_candidate_priority(item, priority=priority))
    return None


def _runner_candidate_priority(item: dict[str, object], *, priority: dict[str, int]) -> tuple[int, int]:
    action = str(item.get('runner_action') or '')
    record = item.get('record') if isinstance(item.get('record'), dict) else {}
    current_loop = str(record.get('current_loop') or '').strip()
    bound_execution = 1 if action == 'execute' and current_loop else 0
    return priority.get(action, 99), bound_execution


def _runner_action_for_record(
    record: dict[str, object],
    *,
    project_root: Path | None = None,
) -> dict[str, str] | None:
    if project_root is not None and not runner_transaction_committed(Path(project_root), record):
        return None
    if _has_activation_metadata(record):
        return _activation_runner_action_for_record(record, project_root=project_root)
    return _legacy_runner_action_for_record(record)


def _activation_runner_action_for_record(
    record: dict[str, object],
    *,
    project_root: Path | None = None,
) -> dict[str, str] | None:
    current_loop = str(record.get('current_loop') or '').strip()
    status = str(record.get('status') or 'draft').strip().lower()
    next_owner = _validated_record_next_owner(record, status=status)
    if next_owner == 'terminal':
        if status == 'blocked':
            return {'action': 'blocked', 'reason': 'blocked_task', 'next_owner': 'terminal'}
        return {'action': 'terminal', 'reason': f'{status}_task', 'next_owner': 'terminal'}
    if status == 'ready_for_orchestration' and next_owner == 'orchestrator' and not current_loop:
        route = _orchestrator_route_for_record(record)
        if not route:
            return {
                'action': 'activate_orchestrator',
                'reason': 'ready_for_orchestration',
                'next_owner': 'orchestrator',
            }
        if route in {'direct_execution', 'partial_completion'}:
            return {
                'action': 'ask_first_execute',
                'reason': f'orchestrator_route_{route}',
                'next_owner': 'orchestrator',
            }
        if route == 'needs_detail':
            if project_root is not None and detail_ready_reconcile_authority(
                record,
                project_root=Path(project_root),
            ) is not None:
                return {
                    'action': 'reconcile_detail_ready',
                    'reason': 'explicit_detail_ready_stop_contract',
                    'next_owner': 'orchestrator',
                }
            if _detail_packet_ready(record):
                if project_root is not None and _detail_reconcile_provenance(
                    Path(project_root),
                    record,
                    record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {},
                ) is None and _has_role_output_detail_artifacts(record):
                    return {
                        'action': 'activate_task_detailer',
                        'reason': 'orchestrator_route_needs_detail_stale_detail_authority',
                        'next_owner': 'orchestrator',
                    }
                return {
                    'action': 'activate_orchestrator',
                    'reason': 'orchestrator_route_needs_detail_detail_ready',
                    'next_owner': 'orchestrator',
                }
            return {
                'action': 'activate_task_detailer',
                'reason': 'orchestrator_route_needs_detail',
                'next_owner': 'orchestrator',
            }
        if route == 'macro_adjustment_request':
            return {
                'action': 'planner_next_action_required',
                'reason': 'orchestrator_route_macro_adjustment_request',
                'next_owner': 'planner',
            }
        if route == 'blocked':
            return {
                'action': 'blocker_evidence_required',
                'reason': 'orchestrator_route_blocked',
                'next_owner': 'frontdesk',
            }
    if status == 'ready' and next_owner == 'orchestrator' and not current_loop:
        return {'action': 'execute', 'reason': 'ready_task', 'next_owner': 'orchestrator'}
    if status == 'running' and next_owner == 'orchestrator' and current_loop:
        return {
            'action': 'ask_first_execution_not_ready',
            'reason': 'running_task_bound_to_loop',
            'next_owner': 'orchestrator',
        }
    if (
        status in {'detail_ready', 'replan_required'}
        and next_owner == 'planner'
        and not current_loop
        and _task_declares_status_stop(record, status=status, project_root=project_root)
    ):
        return None
    if status in {'draft', 'partial', 'replan_required', 'detail_ready'} and next_owner == 'planner' and not current_loop:
        return {'action': 'activate_planner', 'reason': f'{status}_task', 'next_owner': 'planner'}
    if status == 'needs_clarification' and next_owner == 'task_detailer':
        return {'action': 'paused', 'reason': 'needs_clarification', 'next_owner': 'task_detailer'}
    if next_owner == 'frontdesk':
        return {'action': 'paused', 'reason': f'{status}_task', 'next_owner': 'frontdesk'}
    return None


def _legacy_runner_action_for_record(record: dict[str, object]) -> dict[str, str] | None:
    current_loop = str(record.get('current_loop') or '').strip()
    status = str(record.get('status') or 'draft').strip().lower()
    if status == 'ready' and not current_loop:
        return {'action': 'execute', 'reason': 'ready_task', 'next_owner': 'orchestrator'}
    if status == 'draft' and not current_loop:
        if _ready_for_plan_review(record):
            if _needs_task_detail(record):
                return {'action': 'activate_task_detailer', 'reason': 'detail_required', 'next_owner': 'planner'}
            return {'action': 'activate_plan_reviewer', 'reason': 'review_required', 'next_owner': 'planner'}
        return {'action': 'activate_planner', 'reason': f'{status}_task', 'next_owner': 'planner'}
    if status == 'detail_ready' and not current_loop:
        return {'action': 'activate_plan_reviewer', 'reason': 'detail_ready', 'next_owner': 'planner'}
    if status in {'partial', 'replan_required'} and not current_loop:
        return {'action': 'activate_planner', 'reason': f'{status}_task', 'next_owner': 'planner'}
    if status == 'needs_clarification':
        return {'action': 'paused', 'reason': 'needs_clarification', 'next_owner': 'task_detailer'}
    if status == 'blocked':
        return {'action': 'blocked', 'reason': 'blocked_task', 'next_owner': 'terminal'}
    if status in {'done', 'cancelled'}:
        return {'action': 'terminal', 'reason': f'{status}_task', 'next_owner': 'terminal'}
    return None


def _has_activation_metadata(record: dict[str, object]) -> bool:
    return 'next_owner' in record or 'activation_reason' in record


def _validated_record_next_owner(record: dict[str, object], *, status: str) -> str:
    raw = str(record.get('next_owner') or '').strip().lower()
    next_owner = raw or _default_next_owner_for_status(status)
    if next_owner not in _VALID_NEXT_OWNERS:
        known = ', '.join(sorted(_VALID_NEXT_OWNERS))
        raise ValueError(f'unknown plan task next_owner {next_owner!r}; expected one of: {known}')
    _validate_status_next_owner(status=status, next_owner=next_owner)
    return next_owner


def _validate_task_record(record: dict[str, object]) -> None:
    status = str(record.get('status') or 'draft').strip().lower()
    if status not in _VALID_STATUSES:
        known = ', '.join(sorted(_VALID_STATUSES))
        raise ValueError(f'unknown plan task status {status!r}; expected one of: {known}')
    _validated_record_next_owner(record, status=status)
    if 'current_loop' not in record:
        raise ValueError('plan task metadata missing current_loop')
    reason = record.get('activation_reason')
    if reason is not None:
        _validate_activation_reason_text(str(reason))
    task_revision(record)
    task_state_version(record)


def _ready_for_plan_review(record: dict[str, object]) -> bool:
    artifacts = set((record.get('artifacts') or {}).keys()) if isinstance(record.get('artifacts'), dict) else set()
    return _PLAN_REVIEW_REQUIRED <= artifacts and 'review' not in artifacts


def _needs_task_detail(record: dict[str, object]) -> bool:
    artifacts = set((record.get('artifacts') or {}).keys()) if isinstance(record.get('artifacts'), dict) else set()
    return 'brief' in artifacts and not _DETAIL_READY_REQUIRED <= artifacts


def _orchestrator_route_for_record(record: dict[str, object]) -> str:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    notes = artifacts.get('orchestration_notes') if isinstance(artifacts, dict) else None
    route = str(notes.get('orchestrator_route') or '').strip().lower() if isinstance(notes, dict) else ''
    if route and route not in _VALID_ORCHESTRATOR_ROUTES:
        known = ', '.join(sorted(_VALID_ORCHESTRATOR_ROUTES))
        raise ValueError(f'unknown orchestrator route {route!r}; expected one of: {known}')
    return route


def _detail_packet_ready(record: dict[str, object]) -> bool:
    artifacts = set((record.get('artifacts') or {}).keys()) if isinstance(record.get('artifacts'), dict) else set()
    return _DETAIL_READY_REQUIRED <= artifacts


def _is_role_output_detail_import(command, *, artifact_kind: str) -> bool:
    return (
        artifact_kind in _DETAIL_READY_REQUIRED
        and str(getattr(command, 'actor_source', None) or '') == 'loop_runner_role_output_import'
        and str(getattr(command, 'actor', None) or '') == 'loop_runner'
        and bool(str(getattr(command, 'job_id', None) or '').strip())
    )


def _has_role_output_detail_artifacts(record: dict[str, object]) -> bool:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    return all(
        isinstance(artifacts.get(kind), dict)
        and isinstance(artifacts[kind].get('actor'), dict)
        and artifacts[kind]['actor'].get('source') == 'loop_runner_role_output_import'
        and artifacts[kind]['actor'].get('actor') == 'loop_runner'
        and bool(str(artifacts[kind]['actor'].get('job_id') or '').strip())
        for kind in _DETAIL_READY_REQUIRED
    )


def detail_ready_reconcile_authority(
    record: dict[str, object],
    *,
    project_root: Path,
) -> dict[str, object] | None:
    return _detail_ready_reconcile_authority(
        record,
        project_root=project_root,
        allowed_statuses={'ready_for_orchestration'},
    )


def detail_ready_stop_contract_authority(
    record: dict[str, object],
    *,
    project_root: Path,
) -> dict[str, object] | None:
    """Return verified detail-ready terminal authority for a settled task."""
    return _detail_ready_reconcile_authority(
        record,
        project_root=project_root,
        allowed_statuses={'detail_ready'},
    )


def detail_ready_stop_contract_match(
    record: dict[str, object],
    *,
    project_root: Path,
) -> dict[str, object] | None:
    corpus = _task_stop_contract_corpus(record, project_root=project_root)
    if corpus is None:
        return None
    return match_detail_ready_stop_contract(corpus, task_id=record.get('task_id'))


def _detail_ready_reconcile_authority(
    record: dict[str, object],
    *,
    project_root: Path,
    allowed_statuses: set[str],
) -> dict[str, object] | None:
    if str(record.get('status') or '') not in allowed_statuses:
        return None
    status = str(record.get('status') or '')
    expected_next_owner = 'orchestrator' if status == 'ready_for_orchestration' else 'planner'
    if status == 'detail_ready' and record.get('owner') != _owner_for_status('detail_ready'):
        return None
    if str(record.get('next_owner') or '') != expected_next_owner:
        return None
    if str(record.get('current_loop') or '').strip() or _orchestrator_route_for_record(record) != 'needs_detail':
        return None
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    detail_provenance = _detail_reconcile_provenance(project_root, record, artifacts)
    if detail_provenance is None:
        return None
    verified: dict[str, dict[str, str]] = dict(detail_provenance['artifacts'])
    contract_corpus: dict[str, str] = {}
    for kind in ('task_packet', 'execution_contract', 'orchestration_notes'):
        item = artifacts.get(kind) if isinstance(artifacts, dict) else None
        if item is None:
            continue
        checked = _verified_reconcile_artifact(project_root, record, kind, item)
        if checked is None:
            return None
        verified[kind] = checked
        contract_corpus[kind] = checked['text']
    stop_match = match_detail_ready_stop_contract(contract_corpus, task_id=record.get('task_id'))
    if stop_match is None:
        return None
    basis = {
        'route': 'needs_detail',
        'detail_provenance': detail_provenance['authority'],
        'artifacts': {
            kind: {'path': item['path'], 'sha256': item['sha256']}
            for kind, item in sorted(verified.items())
        },
        'stop_contract_sha256': _corpus_digest(contract_corpus),
    }
    basis_digest = _digest_json(basis)
    authority = {
        'state_version': task_state_version(record),
        'status': record.get('status'),
        'owner': record.get('owner'),
        'next_owner': record.get('next_owner'),
        'current_loop': record.get('current_loop'),
        'activation_reason': record.get('activation_reason'),
        'task_revision': task_revision(record),
        'basis_digest': basis_digest,
    }
    encoded = json.dumps(authority, sort_keys=True, separators=(',', ':')).encode('utf-8')
    authority['authority_digest'] = hashlib.sha256(encoded).hexdigest()
    authority['basis'] = basis
    authority['basis_digest'] = basis_digest
    authority['stop_match'] = stop_match
    return authority


def _detail_reconcile_provenance(
    project_root: Path,
    record: dict[str, object],
    artifacts: dict[str, object],
) -> dict[str, object] | None:
    task_id = str(record.get('task_id') or '').strip()
    if not task_id:
        return None
    verified: dict[str, dict[str, str]] = {}
    job_id = ''
    source_revision: int | None = None
    for kind in sorted(_DETAIL_READY_REQUIRED):
        item = artifacts.get(kind)
        checked = _verified_reconcile_artifact(project_root, record, kind, item)
        if checked is None or not isinstance(item, dict):
            return None
        actor = item.get('actor') if isinstance(item.get('actor'), dict) else {}
        item_job_id = str(actor.get('job_id') or '').strip()
        item_revision = item.get('task_revision')
        expected_path = str(Path(str(record.get('task_root') or '')) / _ARTIFACT_FILES[kind])
        expected_source = str(Path('.ccb') / 'runtime' / 'role-output-imports' / item_job_id / _ARTIFACT_FILES[kind].split('/')[-1])
        if (
            actor.get('source') != 'loop_runner_role_output_import'
            or actor.get('actor') != 'loop_runner'
            or not item_job_id
            or not isinstance(item_revision, int)
            or item_revision <= 0
            or str(item.get('path') or '') != expected_path
            or str(item.get('source_path') or '') != expected_source
        ):
            return None
        if job_id and item_job_id != job_id:
            return None
        if source_revision is not None and item_revision != source_revision:
            return None
        job_id = item_job_id
        source_revision = item_revision
        verified[kind] = checked
    if source_revision != task_revision(record):
        return None
    activation = _task_detailer_activation_authority(
        project_root,
        task_id=task_id,
        job_id=job_id,
        task_revision=source_revision,
    )
    if activation is None or not _detailer_completion_authority(
        project_root,
        task_id=task_id,
        job_id=job_id,
        artifacts=verified,
    ):
        return None
    authority = {
        'job_id': job_id,
        'task_id': task_id,
        'task_revision': source_revision,
        'activation_id': activation['activation_id'],
        'artifacts': {
            kind: {
                'path': item['path'],
                'sha256': item['sha256'],
                'source_path': str(Path('.ccb') / 'runtime' / 'role-output-imports' / job_id / _ARTIFACT_FILES[kind].split('/')[-1]),
            }
            for kind, item in sorted(verified.items())
        },
    }
    return {'artifacts': verified, 'authority': authority}


def _task_detailer_activation_authority(
    project_root: Path,
    *,
    task_id: str,
    job_id: str,
    task_revision: int | None,
) -> dict[str, str] | None:
    root = project_root / '.ccb' / 'runtime' / 'loops' / 'activations'
    if not root.is_dir() or task_revision is None:
        return None
    matches: list[dict[str, str]] = []
    for path in sorted(root.glob('act-*.json')):
        try:
            activation = json.loads(path.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if not isinstance(activation, dict):
            continue
        ask = activation.get('ask') if isinstance(activation.get('ask'), dict) else {}
        if (
            str(ask.get('job_id') or '') == job_id
            and str(ask.get('target') or '') == 'task_detailer'
            and str(activation.get('task_id') or '') == task_id
            and activation.get('task_revision') == task_revision
        ):
            matches.append({'activation_id': str(activation.get('activation_id') or '')})
    return matches[0] if len(matches) == 1 and matches[0]['activation_id'] else None


def _detailer_completion_authority(
    project_root: Path,
    *,
    task_id: str,
    job_id: str,
    artifacts: dict[str, dict[str, str]],
) -> bool:
    path = project_root / '.ccb' / 'runtime' / 'role-output-imports.jsonl'
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return False
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        source_job = entry.get('source_job') if isinstance(entry.get('source_job'), dict) else {}
        imported = entry.get('artifacts') if isinstance(entry.get('artifacts'), dict) else {}
        if (
            entry.get('action') != 'imported_task_detailer_detail_authority'
            or str(entry.get('task_id') or '') != task_id
            or str(source_job.get('job_id') or '') != job_id
        ):
            continue
        if all(
            isinstance(imported.get(kind), dict)
            and str(imported[kind].get('sha256') or '') == artifact['sha256']
            for kind, artifact in artifacts.items()
        ):
            return True
    return False


def _verified_reconcile_artifact(
    project_root: Path,
    record: dict[str, object],
    kind: str,
    artifact: object,
) -> dict[str, str] | None:
    if not isinstance(artifact, dict):
        return None
    relative = str(artifact.get('path') or '').strip()
    recorded_sha = str(artifact.get('sha256') or '').strip().lower()
    if not relative or not re.fullmatch(r'[0-9a-f]{64}', recorded_sha):
        return None
    try:
        root = project_root.resolve()
        path = (project_root / relative).resolve(strict=True)
    except FileNotFoundError:
        return None
    if path == root or root not in path.parents or not path.is_file():
        return None
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return None
    actual_sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if actual_sha != recorded_sha:
        return None
    return {'path': relative, 'sha256': recorded_sha, 'text': text}


def _task_declares_status_stop(
    record: dict[str, object],
    *,
    status: str,
    project_root: Path | None,
) -> bool:
    if status == 'detail_ready':
        return project_root is not None and detail_ready_stop_contract_match(
            record,
            project_root=project_root,
        ) is not None
    patterns = _STATUS_STOP_PATTERNS.get(status)
    if not patterns:
        return False
    text = _task_stop_contract_text(record, project_root=project_root)
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _task_stop_contract_corpus(record: dict[str, object], *, project_root: Path) -> dict[str, str] | None:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    corpus: dict[str, str] = {}
    for kind in ('task_packet', 'execution_contract', 'orchestration_notes'):
        artifact = artifacts.get(kind) if isinstance(artifacts, dict) else None
        if not isinstance(artifact, dict):
            continue
        checked = _verified_reconcile_artifact(project_root, record, kind, artifact)
        if checked is None:
            return None
        revision = artifact.get('task_revision')
        if revision != task_revision(record):
            return None
        corpus[kind] = checked['text']
    return corpus or None


def _digest_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _corpus_digest(corpus: dict[str, str]) -> str:
    return _digest_json(corpus)


def _synchronize_stop_contract_revisions(record: dict[str, object]) -> dict[str, object]:
    artifacts = dict(record.get('artifacts') or {})
    revision = task_revision(record)
    for kind in _STOP_CONTRACT_ARTIFACTS:
        artifact = artifacts.get(kind)
        if isinstance(artifact, dict):
            updated = dict(artifact)
            updated['task_revision'] = revision
            artifacts[kind] = updated
    updated_record = dict(record)
    updated_record['artifacts'] = artifacts
    return updated_record


def _post_reconcile_state_digest(
    record: dict[str, object],
    *,
    basis_digest: str,
    state_version: int | None = None,
) -> str:
    return _digest_json(
        {
            'status': record.get('status'),
            'owner': record.get('owner'),
            'next_owner': record.get('next_owner'),
            'current_loop': record.get('current_loop'),
            'activation_reason': record.get('activation_reason'),
            'task_revision': task_revision(record),
            'state_version': task_state_version(record) if state_version is None else state_version,
            'basis_digest': basis_digest,
        }
    )


def _task_stop_contract_text(record: dict[str, object], *, project_root: Path | None) -> str:
    sections = [
        str(record.get('title') or ''),
        str(record.get('activation_reason') or ''),
    ]
    if project_root is None:
        return '\n'.join(sections)
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    for kind in ('task_packet', 'execution_contract', 'orchestration_notes'):
        artifact = artifacts.get(kind) if isinstance(artifacts, dict) else None
        relative = str(artifact.get('path') or '').strip() if isinstance(artifact, dict) else ''
        if not relative:
            continue
        path = project_root / relative
        try:
            resolved = path.resolve(strict=True)
            root = project_root.resolve()
        except FileNotFoundError:
            continue
        if resolved != root and root not in resolved.parents:
            continue
        sections.append(resolved.read_text(encoding='utf-8')[:12000])
    return '\n'.join(sections)


def task_execution_text(context, task_id: object) -> str:
    task = _require_task(context, task_id)
    record = task['record']
    task_root = Path(context.project.project_root) / str(record.get('task_root') or '')
    payload = _payload(context, action='task-show', record=record)
    sections = [_breadcrumb_text(payload)]
    for title, artifact_kind in (
        ('Task Packet', 'task_packet'),
        ('Execution Contract', 'execution_contract'),
        ('Orchestration Notes', 'orchestration_notes'),
        ('Handoff', 'handoff'),
        ('Acceptance Criteria', 'acceptance'),
        ('Verification Contract', 'verification'),
    ):
        artifact = (record.get('artifacts') or {}).get(artifact_kind) if isinstance(record.get('artifacts'), dict) else None
        path_text = str(artifact.get('path') or '') if isinstance(artifact, dict) else ''
        if not path_text:
            continue
        path = Path(context.project.project_root) / path_text
        try:
            text = path.read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            text = ''
        if text:
            sections.append(f'{title}:\n{text}')
    sections.append(f'Task Root: {task_root.relative_to(context.project.project_root)}')
    return '\n\n'.join(sections)


def _require_task(context, task_id: object) -> dict[str, object]:
    normalized = _normalize_task_id(task_id)
    task = _find_task(context, normalized)
    if task is None:
        raise ValueError(f'plan task not found: {normalized}')
    return task


def _find_task(context, task_id: str) -> dict[str, object] | None:
    plantree_root = Path(context.project.project_root) / 'docs' / 'plantree' / 'plans'
    if not plantree_root.is_dir():
        return None
    matches: list[dict[str, object]] = []
    for index_path in sorted(plantree_root.glob('*/tasks/index.json')):
        tasks_root = index_path.parent
        index = _read_json_object(index_path)
        for record in tuple(index.get('tasks') or ()):
            if isinstance(record, dict) and str(record.get('task_id') or '') == task_id:
                matches.append({'record': record, 'index': index, 'tasks_root': tasks_root})
    if len(matches) > 1:
        raise ValueError(f'plan task id is ambiguous across plans: {task_id}')
    return matches[0] if matches else None


def _load_index(tasks_root: Path, *, plan_slug: str, plan_root: Path) -> dict[str, object]:
    path = tasks_root / 'index.json'
    payload = _read_json_object(path)
    if payload:
        tasks = payload.get('tasks')
        if not isinstance(tasks, list):
            raise ValueError(f'plan task index is invalid: {path}')
        return payload
    return {
        'schema_version': 1,
        'record_type': 'ccb_plan_task_index',
        'plan_slug': plan_slug,
        'plan_root': str(plan_root),
        'updated_at': None,
        'tasks': [],
    }


def _write_index(tasks_root: Path, index: dict[str, object]) -> None:
    atomic_write_json(tasks_root / 'index.json', index)


def _replace_record(tasks_root: Path, index: dict[str, object], record: dict[str, object]) -> None:
    record['state_version'] = task_state_version(record) + 1
    task_id = str(record.get('task_id') or '')
    tasks = []
    replaced = False
    for item in tuple(index.get('tasks') or ()):
        if isinstance(item, dict) and str(item.get('task_id') or '') == task_id:
            tasks.append(record)
            replaced = True
        else:
            tasks.append(item)
    if not replaced:
        raise ValueError(f'plan task not found in index: {task_id}')
    index['tasks'] = tasks
    index['updated_at'] = record.get('updated_at')
    _write_index(tasks_root, index)


def _task_lock_path(context, record: dict[str, object]) -> Path:
    return Path(context.project.project_root) / str(record['task_root']) / 'task.lock'


def _planner_compact_import_extra(artifact_kind: str) -> dict[str, object]:
    policy = _PLANNER_COMPACT_IMPORT_POLICIES.get(artifact_kind)
    if policy is None:
        return {}
    return {'planner_compact_import': dict(policy)}


def _import_text_artifact(
    context,
    record: dict[str, object],
    *,
    artifact_kind: str,
    file_path: object,
    text: str | None = None,
    extra: dict[str, object] | None = None,
    actor: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    source_path = _safe_project_file(context.project.project_root, file_path)
    if text is None:
        text = _read_utf8_artifact(source_path)
    task_root = Path(context.project.project_root) / str(record['task_root'])
    if artifact_kind in _PLAN_ROOT_ARTIFACTS:
        dest = Path(context.project.project_root) / str(record['plan_root']) / _ARTIFACT_FILES[artifact_kind]
    else:
        dest = task_root / _ARTIFACT_FILES[artifact_kind]
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(dest, text)
    encoded = text.encode('utf-8')
    now = _utc_now()
    artifact = {
        'kind': artifact_kind,
        'artifact_kind': artifact_kind,
        'path': str(dest.relative_to(context.project.project_root)),
        'artifact_path': str(dest.relative_to(context.project.project_root)),
        'source_path': str(source_path.relative_to(context.project.project_root)),
        'scope': 'plan' if artifact_kind in _PLAN_ROOT_ARTIFACTS else 'task',
        'sha256': hashlib.sha256(encoded).hexdigest(),
        'bytes': len(encoded),
        'imported_at': now,
        'actor': actor or _artifact_actor_metadata(context, None),
    }
    if extra:
        artifact.update(extra)
    artifacts = dict(record.get('artifacts') or {})
    artifacts[artifact_kind] = artifact
    record = dict(record)
    record['artifacts'] = artifacts
    return record, artifact


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


def _existing_round_import(
    record: dict[str, object],
    *,
    loop_id: str,
    result: str,
    sha256: str,
) -> dict[str, object] | None:
    artifact_kind, _target_status, legacy_artifact_kind = _ROUND_RESULT_MAP[result]
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    artifact = artifacts.get(artifact_kind) if isinstance(artifacts, dict) else None
    if not isinstance(artifact, dict):
        artifact = artifacts.get(legacy_artifact_kind) if isinstance(artifacts, dict) else None
    if not isinstance(artifact, dict):
        return None
    if (
        str(artifact.get('loop_id') or '') == loop_id
        and str(artifact.get('round_result') or '') == result
        and str(artifact.get('sha256') or '') == sha256
    ):
        return artifact
    raise ValueError(f'plan task round import conflicts with existing {artifact_kind} artifact')


def _add_round_legacy_alias(
    record: dict[str, object],
    *,
    artifact: dict[str, object],
    legacy_artifact_kind: str,
) -> dict[str, object]:
    artifacts = dict(record.get('artifacts') or {})
    legacy = dict(artifact)
    legacy['kind'] = legacy_artifact_kind
    legacy['artifact_kind'] = legacy_artifact_kind
    legacy['legacy_of'] = 'round_summary'
    artifacts[legacy_artifact_kind] = legacy
    record = dict(record)
    record['artifacts'] = artifacts
    return record


def _legacy_artifact_for_round(record: dict[str, object], legacy_artifact_kind: str) -> dict[str, object] | None:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    artifact = artifacts.get(legacy_artifact_kind) if isinstance(artifacts, dict) else None
    return dict(artifact) if isinstance(artifact, dict) else None


def _write_task_readme(context, record: dict[str, object]) -> None:
    task_root = Path(context.project.project_root) / str(record['task_root'])
    lines = [
        f'Task: {record.get("title", "")}',
        f'Task ID: {record.get("task_id", "")}',
        f'Task Revision: {task_revision(record)}',
        f'Plan Root: {record.get("plan_slug", "")}',
        f'Status: {record.get("status", "")}',
        f'Current Loop: {record.get("current_loop") or "none"}',
        f'Owner: {record.get("owner", "")}',
        f'Next Owner: {record.get("next_owner", "")}',
        f'Activation Reason: {record.get("activation_reason", "")}',
        f'Created: {record.get("created_at", "")}',
        f'Updated: {record.get("updated_at", "")}',
        '',
    ]
    atomic_write_text(task_root / 'README.md', '\n'.join(lines))


def _breadcrumb_text(payload: dict[str, object]) -> str:
    record = payload.get('task') if isinstance(payload.get('task'), dict) else {}
    return '\n'.join(
        [
            f'Task: {record.get("task_id", "")}',
            f'Plan: {record.get("plan_slug", "")}',
            f'Status: {record.get("status", "")}',
            f'Owner: {record.get("owner", "")}',
            f'Next Owner: {record.get("next_owner", "")}',
            f'Current Loop: {record.get("current_loop") or "none"}',
            f'Activation Reason: {record.get("activation_reason", "")}',
            f'Artifacts: {", ".join(sorted((record.get("artifacts") or {}).keys())) if isinstance(record.get("artifacts"), dict) else ""}',
        ]
    )


def _plan_root(context, plan_slug: str) -> Path:
    return Path(context.project.project_root) / 'docs' / 'plantree' / 'plans' / plan_slug


def _safe_project_file(project_root: Path, value: object) -> Path:
    raw = str(value or '').strip()
    if not raw:
        raise ValueError('plan task-artifact requires --file')
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root / path
    try:
        resolved = path.resolve(strict=True)
        root = project_root.resolve()
    except FileNotFoundError as exc:
        raise ValueError(f'plan artifact file not found: {path}') from exc
    if resolved != root and root not in resolved.parents:
        raise ValueError(f'plan artifact file must be inside project root: {resolved}')
    if not resolved.is_file():
        raise ValueError(f'plan artifact path is not a file: {resolved}')
    return resolved


def _read_utf8_artifact(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError(f'plan artifact must be UTF-8 text: {path}') from exc


def _normalize_segment(value: object, *, label: str) -> str:
    text = str(value or '').strip()
    if not _SEGMENT_RE.fullmatch(text):
        raise ValueError(f'{label} must match {_SEGMENT_RE.pattern}: {text!r}')
    return text


def _normalize_task_id(value: object) -> str:
    return _normalize_segment(value, label='task_id')


def _new_task_id(*, title: str, tasks_root: Path) -> str:
    slug = _SLUG_RE.sub('-', title.strip().lower()).strip('-') or 'task'
    slug = slug[:40].strip('-') or 'task'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    base = f'{slug}-{stamp}'
    candidate = base[:80]
    suffix = 1
    while (tasks_root / candidate).exists():
        suffix += 1
        suffix_text = f'-{suffix}'
        candidate = f'{base[:80 - len(suffix_text)]}{suffix_text}'
    return candidate


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f'plan task index is invalid JSON: {path}') from exc
    if not isinstance(payload, dict):
        raise ValueError(f'plan task index is invalid: {path}')
    return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _materialize_task_revision(record: dict[str, object]) -> dict[str, object]:
    materialized = dict(record)
    materialized['task_revision'] = task_revision(record)
    materialized['state_version'] = task_state_version(record)
    return materialized


def task_state_version(record: dict[str, object]) -> int:
    value = record.get('state_version', 1)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('plan task state_version must be a positive integer')
    return value


def _assert_expected_task_revision(command, record: dict[str, object]) -> None:
    expected = getattr(command, 'expected_task_revision', None)
    if expected is None:
        return
    if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
        raise ValueError('expected_task_revision must be a positive integer')
    current = task_revision(record)
    if expected != current:
        raise ValueError(
            'stale managed activation task_revision: '
            f'expected {expected}, current {current}'
        )


def _required_sha256(value: object, *, field: str) -> str:
    text = str(value or '').strip().lower()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', text):
        raise ValueError(f'{field} must use sha256:<64 lowercase hex>')
    return text


def _normalize_job_id(value: object) -> str:
    return _normalize_segment(value, label='job_id')


def _optional_normalized_job_id(value: object) -> str | None:
    text = str(value or '').strip()
    return _normalize_job_id(text) if text else None


def _artifact_record(record: dict[str, object], artifact_kind: str) -> dict[str, object] | None:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    artifact = artifacts.get(artifact_kind) if isinstance(artifacts, dict) else None
    return dict(artifact) if isinstance(artifact, dict) else None


def _reject_running_semantic_mutation(record: dict[str, object]) -> None:
    if str(record.get('status') or '') == 'running' or str(record.get('current_loop') or '').strip():
        raise ValueError('cannot replace semantic artifact while task is bound to running loop')


def _positive_record_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'plan task {field} must be a positive integer')
    return value


__all__ = [
    'detail_ready_stop_contract_authority',
    'find_first_actionable_task',
    'find_first_ready_task',
    'plan_task',
    'task_execution_text',
]
