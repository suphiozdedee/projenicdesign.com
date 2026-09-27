from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
from types import SimpleNamespace
from uuid import uuid4

from cli.models import ParsedAskCommand
from storage.atomic import atomic_write_json, atomic_write_text

from .ask import submit_ask
from .frontdesk_source_request import resolve_frontdesk_source_request
from .loop_orchestration_bundle import (
    ORCHESTRATION_BUNDLE_CANDIDATE_SCHEMA,
    build_single_node_candidate,
    normalize_bundle_candidate,
    task_revision,
)
from .loop_effective_capacity import (
    allows_v2_missing_candidate,
    compile_project_effective_capacity_snapshot,
)
from .plan_tasks import detail_ready_stop_contract_authority, plan_task
from .task_set_closure import create_task_set_authority
from .planner_feedback import (
    PlannerFeedbackError,
    parse_planner_feedback_reply,
    planner_feedback_digest,
    validate_planner_feedback_authority,
)
from .planner_feedback_apply import plan_revision_authority
from .detailer_replan_backfill import apply_detailer_replan_backfill, has_pending_detailer_replan_recovery
from .planner_task_set_import_transaction import (
    PlannerTaskSetImportConflict,
    authority_trace as planner_task_set_transaction_trace,
    commit as commit_planner_task_set_transaction,
    fail as fail_planner_task_set_transaction,
    prepare as prepare_planner_task_set_transaction,
)


_VALID_ROUTES = frozenset({'direct_execution', 'needs_detail', 'macro_adjustment_request', 'blocked', 'partial_completion'})
_EXECUTION_ROUTES = frozenset({'direct_execution', 'partial_completion'})
_NEEDS_DETAIL_READINESS = frozenset({'ready', 'needs_clarification'})
_VALID_READINESS = frozenset({'ready', 'needs_clarification', 'blocked', 'not_ready'})
_VALID_STATUS_RECOMMENDATIONS = frozenset(
    {'blocked', 'detail_ready', 'needs_clarification', 'ready_for_orchestration', 'replan_required'}
)
_DETAIL_PACKET_MANIFEST_SCHEMA = 'ccb.detail_packet_manifest.v1'
_DETAIL_PACKET_OUTCOMES = {
    'local_detail_ready': ('detail_ready', frozenset({'none'})),
    'planner_replan_required': ('planner_replan_required', frozenset({'macro'})),
    'needs_clarification': ('needs_clarification', frozenset({'none', 'bounded'})),
    'blocked': ('blocked', frozenset({'none', 'bounded', 'macro'})),
}
_TERMINAL_STATUS_CONSTRAINT_SCHEMA_VERSION = 1
_TERMINAL_STATUS_CONSTRAINT_BASIS = 'verified_detail_ready_stop_contract'
_SEGMENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
_SLUG_RE = re.compile(r'[^A-Za-z0-9_-]+')
_PLANNER_CONTRACT_SINGLE_TASK = 'single_task'
_PLANNER_CONTRACT_TASK_SET = 'task_set'
_PLANNER_CONTRACT_DETAILER_REPLAN = 'detailer_replan'
_PLANNER_CONTRACTS = frozenset({_PLANNER_CONTRACT_SINGLE_TASK, _PLANNER_CONTRACT_TASK_SET, _PLANNER_CONTRACT_DETAILER_REPLAN})
_FRONTDESK_SINGLE_TASK_SEMANTIC_SECTIONS = (
    'goal',
    'acceptance criteria',
    'interface contracts',
    'constraints and non-goals',
    'execution decomposition inputs',
)
_TASK_SET_INTENT_MARKERS = (
    'route-mix',
    'route mix',
    'l1-l4',
    'l1/l4',
    'multiple tasks',
    'multi-task',
    'task set',
    'bounded task set',
    'route-mix validation',
    'task set validation',
)
_GIT_SCOPE_CHECK_RE = re.compile(r'(?im)^\s*(?:[-*]\s*)?`?git`?\s+(?:diff|status)\b')
_PHASE6B_L1_L4_EXPECTED_TASK_IDS = (
    'phase6b-l1-doc-direct-execution',
    'phase6b-l2-code-test-direct-execution',
    'phase6b-l3-needs-detail',
    'phase6b-l4-macro-adjustment-request',
    'phase6b-l4-blocked-prerequisite',
)


def consume_explicit_role_output(context, command, services=None) -> dict[str, object]:
    deps = _deps(services)
    job_id = _normalize_job_id(getattr(command, 'role_job_id', None))
    activation = _activation_for_job(context, job_id)
    return _consume_job(context, command, deps, job_id=job_id, activation=activation)


def consume_activation_role_output(context, command, services=None) -> dict[str, object] | None:
    deps = _deps(services)
    first_pending: dict[str, object] | None = None
    for activation_path, activation in _iter_activation_records(context):
        ask = activation.get('ask') if isinstance(activation.get('ask'), dict) else {}
        job_id = str(ask.get('job_id') or '').strip()
        target = str(ask.get('target') or '').strip()
        if not job_id or _job_settled_for_activation_scan(context, job_id):
            continue
        if target not in {'planner', 'orchestrator', 'task_detailer'}:
            continue
        if _activation_already_satisfied(context, deps, activation=activation, target=target):
            continue
        payload = _consume_job(context, command, deps, job_id=job_id, activation=activation)
        if payload is None:
            continue
        if payload.get('loop_runner_status') == 'pending':
            payload['activation_path'] = str(activation_path)
            if first_pending is None:
                first_pending = payload
            continue
        return payload
    return first_pending


def _consume_job(context, command, deps, *, job_id: str, activation: dict[str, object] | None) -> dict[str, object] | None:
    original_job_id = job_id
    consumed_record = _consumed_import_record(context, job_id)
    if consumed_record is not None:
        return _already_consumed_payload(context, job_id=job_id, record=consumed_record)
    snapshot = _load_job_snapshot(context, job_id)
    if snapshot is None:
        return _pending_payload(context, job_id=job_id, agent_name=None, reason='missing_completion_snapshot')
    decision = snapshot.get('latest_decision') if isinstance(snapshot.get('latest_decision'), dict) else {}
    terminal = bool(decision.get('terminal') or (snapshot.get('state') or {}).get('terminal'))
    agent_name = str(snapshot.get('agent_name') or '').strip()
    if not terminal:
        return _pending_payload(context, job_id=job_id, agent_name=agent_name, reason='job_not_terminal')
    status = str(decision.get('status') or '').strip().lower()
    retry_successor_used = False
    if status != 'completed':
        retry = _retry_successor_for_job(context, job_id, agent_name=agent_name)
        if retry is not None:
            retry_status = str(retry.get('status') or '')
            retry_job_id = str(retry.get('job_id') or '').strip()
            retry_agent_name = str(retry.get('agent_name') or agent_name or '').strip() or None
            if retry_status == 'pending':
                return _pending_payload(
                    context,
                    job_id=retry_job_id or job_id,
                    agent_name=retry_agent_name,
                    reason=str(retry.get('reason') or 'retry_successor_not_terminal'),
                )
            retry_snapshot = retry.get('snapshot')
            if isinstance(retry_snapshot, dict):
                snapshot = retry_snapshot
                job_id = str(snapshot.get('job_id') or retry_job_id or job_id)
                retry_successor_used = job_id != original_job_id
                decision = snapshot.get('latest_decision') if isinstance(snapshot.get('latest_decision'), dict) else {}
                terminal = bool(decision.get('terminal') or (snapshot.get('state') or {}).get('terminal'))
                agent_name = str(snapshot.get('agent_name') or agent_name or '').strip()
                if not terminal:
                    return _pending_payload(context, job_id=job_id, agent_name=agent_name, reason='retry_successor_not_terminal')
                status = str(decision.get('status') or '').strip().lower()
    reply = str(decision.get('reply') or '')
    # Provider retry successors are not part of the Detailer handoff authority:
    # activation/intent/task bind the original job and no durable successor edge
    # is persisted there.  Reject before parsing rather than falling back.
    origin_detailer_replan = _resolve_detailer_replan_authority(context, deps, job_id=original_job_id)
    if retry_successor_used and origin_detailer_replan['claimed']:
        return _detailer_replan_blocked_payload(
            context, job_id=original_job_id, agent_name=agent_name,
            reason='detailer_replan_retry_successor_unsupported',
            evidence={'origin_job_id': original_job_id, 'effective_job_id': job_id},
        )
    if status != 'completed':
        return _blocked_for_detailer_replan_claim(
            context,
            deps=deps,
            job_id=job_id,
            agent_name=agent_name,
            reason='terminal_job_not_completed',
            evidence={'terminal_status': status or 'unknown'},
        )
    resolved_reply = _resolve_completion_reply_artifact(context, reply)
    if resolved_reply.get('status') == 'blocked':
        return _blocked_for_detailer_replan_claim(
            context,
            deps=deps,
            job_id=job_id,
            agent_name=agent_name,
            reason=str(resolved_reply.get('reason') or 'completion_reply_artifact_unavailable'),
            evidence=resolved_reply,
        )
    reply = str(resolved_reply.get('reply') or reply)
    detailer_replan = _resolve_detailer_replan_authority(context, deps, job_id=job_id)
    if detailer_replan['claimed'] and detailer_replan['error']:
        return _detailer_replan_blocked_payload(
            context, job_id=job_id, agent_name=agent_name,
            reason='detailer_replan_authority_invalid', evidence={'error': detailer_replan['error']},
        )
    if not reply.strip():
        if detailer_replan['claimed']:
            return _detailer_replan_blocked_payload(
                context, job_id=job_id, agent_name=agent_name,
                reason='detailer_replan_missing_reply', evidence={},
            )
        return _blocked_payload(context, job_id=job_id, agent_name=agent_name, reason='missing_reply')
    normalized_agent = _base_agent_name(agent_name)
    if detailer_replan['claimed']:
        activation = detailer_replan['activation']
    stale_activation = _stale_activation_revision(context, deps, activation=activation)
    strict_detailer_replan = (
        _parse_task_detailer_reply(reply).get('result') == 'planner_replan_required'
        if normalized_agent == 'task_detailer'
        else False
    )
    accepted_detailer_replan = (
        _accepted_detailer_replan_intent(context, source_job_id=job_id)
        if strict_detailer_replan
        else None
    )
    if stale_activation is not None and accepted_detailer_replan is None:
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=agent_name,
            reason='stale_managed_activation_task_revision',
            evidence=stale_activation,
        )
    def _with_retry_metadata(payload: dict[str, object]) -> dict[str, object]:
        return _attach_retry_metadata(payload, snapshot=snapshot, original_job_id=original_job_id)

    if normalized_agent == 'frontdesk':
        return _with_retry_metadata(_consume_frontdesk(context, command, deps, snapshot=snapshot, reply=reply))
    if normalized_agent == 'planner':
        return _with_retry_metadata(_consume_planner(context, command, deps, snapshot=snapshot, reply=reply, activation=activation))
    if normalized_agent == 'orchestrator':
        return _with_retry_metadata(_consume_orchestrator(context, command, deps, snapshot=snapshot, reply=reply, activation=activation))
    if normalized_agent == 'task_detailer':
        return _with_retry_metadata(_consume_task_detailer(context, command, deps, snapshot=snapshot, reply=reply, activation=activation))
    return _blocked_payload(
        context,
        job_id=job_id,
        agent_name=agent_name,
        reason='unsupported_role_output_agent',
        evidence={'supported_agents': ['frontdesk', 'planner', 'orchestrator', 'task_detailer']},
    )


def _consume_frontdesk(context, command, deps, *, snapshot: dict[str, object], reply: str) -> dict[str, object]:
    job_id = str(snapshot.get('job_id') or '')
    agent_name = str(snapshot.get('agent_name') or 'frontdesk')
    existing_handoff = _frontdesk_handoff_marker(context, job_id)
    if existing_handoff is not None:
        return _consume_existing_frontdesk_handoff(context, snapshot=snapshot, reply=reply, handoff=existing_handoff)
    missing = _frontdesk_intake_missing_fields(reply)
    if missing:
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=str(snapshot.get('agent_name') or ''),
            reason='frontdesk_reply_missing_required_anchors',
            evidence={'missing_fields': missing},
        )
    source_request = _frontdesk_source_request_for_job(
        context,
        job_id=job_id,
        agent_name=agent_name,
    )
    if source_request.get('status') != 'ok':
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=agent_name,
            reason=str(source_request.get('reason') or 'frontdesk_source_request_unavailable'),
            evidence=source_request,
        )
    original_request = str(source_request.get('text') or '')
    plan_slug, plan_result = _resolve_or_bootstrap_plan(context, command)
    if plan_slug is None:
        return plan_result
    activation_id = f'act-{uuid4().hex[:12]}'
    semantic_input = '\n\n'.join(part for part in (original_request, reply) if part)
    planner_contract = planner_contract_for_frontdesk_text(semantic_input)
    expected_task_ids = planner_expected_task_ids_for_frontdesk_text(semantic_input)
    activation = {
        'schema_version': 1,
        'record_type': 'ccb_loop_frontdesk_planner_activation',
        'activation_id': activation_id,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'activate_planner_from_frontdesk',
        'plan_slug': plan_slug,
        'source_job': _job_trace(snapshot, reply),
        'source_request': _frontdesk_source_request_evidence(source_request),
        'planner_contract': planner_contract,
        'required_next_output': planner_required_output_for_contract(
            planner_contract,
            expected_task_ids=expected_task_ids,
        ),
        'script_write_rules': planner_script_write_rules_for_contract(
            planner_contract,
            expected_task_ids=expected_task_ids,
        ),
        'expected_task_ids': list(expected_task_ids),
    }
    source_task_id = _job_request_task_id(
        context,
        job_id=job_id,
        agent_name=str(snapshot.get('agent_name') or 'frontdesk'),
    )
    if source_task_id:
        activation['source_task_id'] = source_task_id
    activation_path = _activation_path(context, activation_id)
    atomic_write_json(activation_path, activation)
    summary = deps.submit_ask(
        context,
        ParsedAskCommand(
            project=None,
            target='planner',
            sender='system',
            message=_planner_from_frontdesk_message(
                activation,
                reply,
                original_request=original_request,
            ),
            task_id=activation_id,
            compact=True,
            inline_request=False,
        ),
    )
    job = _single_job(summary.jobs, target='planner')
    activation['ask'] = {
        'target': 'planner',
        'job_id': str(job['job_id']),
        'status': job.get('status'),
    }
    atomic_write_json(activation_path, activation)
    record = _log_import(
        context,
        {
            'action': 'activated_planner_from_frontdesk',
            'status': 'ok',
            'source_job': _job_trace(snapshot, reply),
            'plan_slug': plan_slug,
            'activation_id': activation_id,
            'activation_path': str(activation_path),
            'ask': activation['ask'],
            'plan_bootstrap': plan_result,
        },
    )
    return _base_payload(
        context,
        loop_runner_status='ok',
        action='activated_planner_from_frontdesk',
        job_id=job_id,
        agent_name=str(snapshot.get('agent_name') or ''),
        extra={
            'plan_slug': plan_slug,
            'activation_id': activation_id,
            'activation_path': str(activation_path),
            'ask': activation['ask'],
            'role_output_import': record,
            'next_activation': 'stop_after_one_activation',
        },
    )


def _consume_existing_frontdesk_handoff(
    context,
    *,
    snapshot: dict[str, object],
    reply: str,
    handoff: dict[str, object],
) -> dict[str, object]:
    job_id = str(snapshot.get('job_id') or '')
    status = str(handoff.get('status') or '').strip().lower()
    if status not in {'starting', 'started'}:
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=str(snapshot.get('agent_name') or ''),
            reason='frontdesk_handoff_not_started',
            evidence={
                'handoff_status': status or 'unknown',
                'handoff_reason': handoff.get('reason'),
                'handoff_marker_path': handoff.get('marker_path'),
            },
        )
    result = _frontdesk_handoff_result(handoff)
    ask = result.get('ask') if isinstance(result, dict) and isinstance(result.get('ask'), dict) else None
    planner_job_id = result.get('planner_job_id') if isinstance(result, dict) else None
    record = _log_import(
        context,
        {
            'action': 'frontdesk_handoff_already_started',
            'status': 'ok',
            'source_job': _job_trace(snapshot, reply),
            'handoff': _compact_frontdesk_handoff(handoff),
            'handoff_result': result,
            'ask': ask,
            'planner_job_id': planner_job_id,
        },
    )
    return _base_payload(
        context,
        loop_runner_status='ok',
        action='frontdesk_handoff_already_started',
        job_id=job_id,
        agent_name=str(snapshot.get('agent_name') or ''),
        extra={
            'handoff': _compact_frontdesk_handoff(handoff),
            'handoff_result': result,
            'ask': ask,
            'planner_job_id': planner_job_id,
            'role_output_import': record,
            'next_activation': 'stop_after_existing_frontdesk_handoff',
        },
    )


def _frontdesk_handoff_marker(context, job_id: str) -> dict[str, object] | None:
    marker_path = Path(context.project.project_root) / '.ccb' / 'runtime' / 'frontdesk-handoff' / f'{job_id}.json'
    try:
        payload = json.loads(marker_path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload = dict(payload)
    payload['marker_path'] = str(marker_path)
    return payload


def _frontdesk_handoff_result(handoff: dict[str, object]) -> dict[str, object] | None:
    stdout_path = str(handoff.get('stdout_path') or '').strip()
    if not stdout_path:
        return None
    try:
        payload = json.loads(Path(stdout_path).read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _compact_frontdesk_handoff(handoff: dict[str, object]) -> dict[str, object]:
    return {
        key: handoff.get(key)
        for key in (
            'status',
            'job_id',
            'plan_slug',
            'pid',
            'stdout_path',
            'stderr_path',
            'marker_path',
            'recorded_at',
        )
        if handoff.get(key) is not None
    }


def _consume_planner(
    context,
    command,
    deps,
    *,
    snapshot: dict[str, object],
    reply: str,
    activation: dict[str, object] | None,
) -> dict[str, object]:
    job_id = str(snapshot.get('job_id') or '')
    detailer_replan = _resolve_detailer_replan_authority(context, deps, job_id=job_id)
    if detailer_replan['claimed'] and detailer_replan['error']:
        return _detailer_replan_blocked_payload(
            context, job_id=job_id, agent_name=str(snapshot.get('agent_name') or ''),
            reason='detailer_replan_authority_invalid', evidence={'error': detailer_replan['error']},
        )
    if detailer_replan['claimed']:
        activation = detailer_replan['activation']
    planner_contract = _planner_contract_from_activation(activation, reply=reply)
    parsed = _parse_planner_reply_for_contract(reply, planner_contract=planner_contract)
    if parsed.get('status') != 'ok':
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=str(snapshot.get('agent_name') or ''),
            reason=str(parsed.get('reason') or 'planner_reply_invalid'),
            evidence=dict(parsed),
        )
    if parsed.get('planner_contract') == _PLANNER_CONTRACT_SINGLE_TASK:
        semantic_check = _validate_frontdesk_single_task_semantics(parsed, activation=activation)
        if semantic_check.get('status') != 'ok':
            return _blocked_payload(
                context,
                job_id=job_id,
                agent_name=str(snapshot.get('agent_name') or ''),
                reason=str(semantic_check.get('reason') or 'planner_task_packet_semantics_invalid'),
                evidence=dict(semantic_check),
            )
    terminal_constraint = _validated_planner_terminal_status_constraint(
        context,
        deps,
        activation=activation,
    )
    if terminal_constraint.get('status') == 'blocked':
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=str(snapshot.get('agent_name') or ''),
            reason=str(terminal_constraint.get('reason') or 'planner_terminal_status_constraint_invalid'),
            evidence=dict(terminal_constraint),
        )
    if terminal_constraint.get('status') == 'ok':
        if parsed.get('planner_contract') != _PLANNER_CONTRACT_SINGLE_TASK:
            return _blocked_payload(
                context,
                job_id=job_id,
                agent_name=str(snapshot.get('agent_name') or ''),
                reason='planner_terminal_status_reply_contract_mismatch',
                evidence={'planner_contract': parsed.get('planner_contract')},
            )
        reply_check = _validate_planner_terminal_status_reply(
            parsed,
            constraint=terminal_constraint['constraint'],
        )
        if reply_check.get('status') != 'ok':
            return _blocked_payload(
                context,
                job_id=job_id,
                agent_name=str(snapshot.get('agent_name') or ''),
                reason=str(reply_check.get('reason') or 'planner_terminal_status_reply_invalid'),
                evidence=dict(reply_check),
            )
        return _settle_planner_terminal_status_constraint(
            context,
            snapshot=snapshot,
            reply=reply,
            task_payload=terminal_constraint['task_payload'],
            constraint=terminal_constraint['constraint'],
            reply_check=reply_check,
        )
    if parsed.get('planner_contract') == _PLANNER_CONTRACT_TASK_SET:
        task_id_check = _validate_task_set_expected_task_ids(parsed, activation=activation)
        if task_id_check.get('status') != 'ok':
            return _blocked_payload(
                context,
                job_id=job_id,
                agent_name=str(snapshot.get('agent_name') or ''),
                reason=str(task_id_check.get('reason') or 'planner_task_set_unexpected_task_ids'),
                evidence=dict(task_id_check),
            )
        contract_check = _validate_task_set_contracts_for_activation(parsed, activation=activation)
        if contract_check.get('status') != 'ok':
            return _blocked_payload(
                context,
                job_id=job_id,
                agent_name=str(snapshot.get('agent_name') or ''),
                reason=str(contract_check.get('reason') or 'planner_task_set_contract_invalid'),
                evidence=dict(contract_check),
            )
        return _consume_planner_task_set(
            context,
            command,
            deps,
            snapshot=snapshot,
            reply=reply,
            activation=activation,
            parsed=parsed,
        )
    if parsed.get('planner_contract') == _PLANNER_CONTRACT_DETAILER_REPLAN:
        return _consume_detailer_replan_planner_backfill(
            context, deps, snapshot=snapshot, reply=reply, activation=activation,
        )
    plan_slug, plan_result = _resolve_or_bootstrap_plan(context, command, activation=activation)
    if plan_slug is None:
        return plan_result
    task_id = _first_optional_text(
        getattr(command, 'task_id', None),
        activation.get('task_id') if activation is not None else None,
    )
    title = str(parsed.get('title') or 'Planner task').strip()
    task_payload = _ensure_task(context, deps, plan_slug=plan_slug, title=title, task_id=task_id, snapshot=snapshot, reply=reply)
    task_id = str(task_payload.get('task_id') or '')
    expected_revision = _task_payload_revision(task_payload)
    import_root = _role_import_dir(context, job_id)
    task_packet_path = import_root / 'task_packet.md'
    execution_contract_path = import_root / 'execution_contract.md'
    atomic_write_text(task_packet_path, str(parsed['task_packet']))
    atomic_write_text(execution_contract_path, str(parsed['execution_contract']))
    task_packet_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='task_packet',
            file_path=str(task_packet_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(task_packet_import)
    execution_contract_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='execution_contract',
            file_path=str(execution_contract_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(execution_contract_import)
    ready = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-status',
            task_id=task_id,
            status='ready_for_orchestration',
            next_owner='orchestrator',
            activation_reason='planner_reply_imported',
            expected_task_revision=expected_revision,
        ),
    )
    source_task_settlement = _settle_frontdesk_single_task_source_task(
        context,
        deps,
        plan_slug=plan_slug,
        job_id=job_id,
        snapshot=snapshot,
        reply=reply,
        activation=activation,
        import_root=import_root,
        imported_task_id=task_id,
    )
    record_payload = {
        'action': 'imported_planner_task_authority',
        'status': 'ok',
        'source_job': _job_trace(snapshot, reply),
        'plan_slug': plan_slug,
        'task_id': task_id,
        'created_task': bool(task_payload.get('created')),
        'artifacts': {
            'task_packet': task_packet_import.get('artifact'),
            'execution_contract': execution_contract_import.get('artifact'),
        },
        'status_transition': _compact_plan_payload(ready),
        'plan_bootstrap': plan_result,
    }
    if source_task_settlement is not None:
        record_payload['source_task_settlement'] = source_task_settlement
    record = _log_import(
        context,
        record_payload,
    )
    payload_extra = {
        'plan_slug': plan_slug,
        'task_id': task_id,
        'task_status': ready.get('status'),
        'next_owner': ready.get('next_owner'),
        'created_task': bool(task_payload.get('created')),
        'imports': {
            'task_packet': _compact_plan_payload(task_packet_import),
            'execution_contract': _compact_plan_payload(execution_contract_import),
        },
        'role_output_import': record,
        'next_activation': 'orchestrator',
    }
    if source_task_settlement is not None:
        payload_extra['source_task_settlement'] = source_task_settlement
    return _base_payload(
        context,
        loop_runner_status='ok',
        action='imported_planner_task_authority',
        job_id=job_id,
        agent_name=str(snapshot.get('agent_name') or ''),
        extra=payload_extra,
    )


def _consume_planner_task_set(
    context,
    command,
    deps,
    *,
    snapshot: dict[str, object],
    reply: str,
    activation: dict[str, object] | None,
    parsed: dict[str, object],
) -> dict[str, object]:
    job_id = str(snapshot.get('job_id') or '')
    task_set_authority_enabled = _task_set_authority_enabled(context, activation)
    source_task_id = _source_task_id_for_task_set(context, activation=activation)
    if task_set_authority_enabled:
        identity_error = _task_set_source_identity_error(
            activation,
            source_task_id=source_task_id,
        )
        if identity_error is not None:
            return _blocked_payload(
                context,
                job_id=job_id,
                agent_name=str(snapshot.get('agent_name') or ''),
                reason=str(identity_error['reason']),
                evidence=identity_error,
            )
    plan_slug, plan_result = _resolve_or_bootstrap_plan(context, command, activation=activation)
    if plan_slug is None:
        return plan_result
    if task_set_authority_enabled:
        source_task_error = _task_set_source_task_error(
            context,
            deps,
            source_task_id=source_task_id,
            plan_slug=plan_slug,
        )
        if source_task_error is not None:
            return _blocked_payload(
                context,
                job_id=job_id,
                agent_name=str(snapshot.get('agent_name') or ''),
                reason=str(source_task_error['reason']),
                evidence=source_task_error,
            )
        return _consume_controlled_planner_task_set(
            context,
            deps,
            snapshot=snapshot,
            reply=reply,
            activation=activation,
            parsed=parsed,
            plan_slug=plan_slug,
            plan_result=plan_result,
            source_task_id=source_task_id,
        )
    import_root = _role_import_dir(context, job_id)
    imported_tasks: list[dict[str, object]] = []
    for task in tuple(parsed.get('tasks') or ()):
        if not isinstance(task, dict):
            continue
        requested_task_id = str(task.get('task_id') or '')
        task_id = _task_set_import_task_id(context, deps, requested_task_id=requested_task_id, job_id=job_id)
        title = str(task.get('title') or _task_title_from_packet(str(task.get('task_packet') or ''))).strip()
        task_payload = _ensure_task(
            context,
            deps,
            plan_slug=plan_slug,
            title=title,
            task_id=task_id,
            snapshot=snapshot,
            reply=reply,
        )
        task_id = str(task_payload.get('task_id') or task_id)
        expected_revision = _task_payload_revision(task_payload)
        task_import_root = import_root / task_id
        task_packet_path = task_import_root / 'task_packet.md'
        execution_contract_path = task_import_root / 'execution_contract.md'
        atomic_write_text(task_packet_path, str(task['task_packet']))
        atomic_write_text(execution_contract_path, str(task['execution_contract']))
        task_packet_import = deps.plan_task(
            context,
            SimpleNamespace(
                action='task-artifact',
                task_id=task_id,
                artifact_kind='task_packet',
                file_path=str(task_packet_path),
                actor_source='loop_runner_role_output_import',
                actor='loop_runner',
                job_id=job_id,
                expected_task_revision=expected_revision,
            ),
        )
        expected_revision = _task_payload_revision(task_packet_import)
        execution_contract_import = deps.plan_task(
            context,
            SimpleNamespace(
                action='task-artifact',
                task_id=task_id,
                artifact_kind='execution_contract',
                file_path=str(execution_contract_path),
                actor_source='loop_runner_role_output_import',
                actor='loop_runner',
                job_id=job_id,
                expected_task_revision=expected_revision,
            ),
        )
        expected_revision = _task_payload_revision(execution_contract_import)
        ready = deps.plan_task(
            context,
            SimpleNamespace(
                action='task-status',
                task_id=task_id,
                status='ready_for_orchestration',
                next_owner='orchestrator',
                activation_reason='planner_task_set_imported',
                expected_task_revision=expected_revision,
            ),
        )
        imported_tasks.append(
            {
                'task_id': task_id,
                'planner_task_id': requested_task_id,
                'title': title,
                'route': task.get('route'),
                'readiness': task.get('readiness'),
                'required': bool(task.get('required', True)),
                'created_task': bool(task_payload.get('created')),
                'artifacts': {
                    'task_packet': task_packet_import.get('artifact'),
                    'execution_contract': execution_contract_import.get('artifact'),
                },
                'status_transition': _compact_plan_payload(ready),
            }
        )
    task_ids = [task['task_id'] for task in imported_tasks]
    task_set_authority = None
    if task_set_authority_enabled:
        task_set_authority = create_task_set_authority(
            context,
            plan_slug=plan_slug,
            source_task_id=source_task_id,
            source_request=dict(activation['source_request']),
            planner_job={
                'job_id': job_id,
                'reply_sha256': hashlib.sha256(reply.encode('utf-8')).hexdigest(),
            },
            children=[
                {'task_id': task['task_id'], 'required': task['required']}
                for task in imported_tasks
            ],
            plan_task_fn=deps.plan_task,
        )
        parent = task_set_authority['parent_transition']
        source_task_settlement = {
            'status': 'decomposed',
            'task_id': source_task_id,
            'status_transition': _compact_plan_payload(parent),
            'child_task_ids': task_ids,
            'task_set_id': task_set_authority['task_set']['task_set_id'],
            'task_set_revision': task_set_authority['task_set']['task_set_revision'],
        }
    else:
        source_task_settlement = _settle_frontdesk_task_set_source_task(
            context,
            deps,
            plan_slug=plan_slug,
            job_id=job_id,
            snapshot=snapshot,
            reply=reply,
            activation=activation,
            import_root=import_root,
            imported_tasks=imported_tasks,
        )
    single_task_fields = _single_task_set_fields(imported_tasks)
    record_payload = {
        'action': 'imported_planner_task_set_authority',
        'status': 'ok',
        'source_job': _job_trace(snapshot, reply),
        'planner_contract': _PLANNER_CONTRACT_TASK_SET,
        'plan_slug': plan_slug,
        'task_ids': task_ids,
        'tasks': imported_tasks,
        'task_count': len(imported_tasks),
        'plan_bootstrap': plan_result,
    }
    if source_task_settlement is not None:
        record_payload['source_task_settlement'] = source_task_settlement
    if task_set_authority is not None:
        record_payload['task_set_authority'] = {
            'task_set': task_set_authority['task_set'],
            'task_set_path': task_set_authority['task_set_path'],
        }
    record_payload.update(single_task_fields)
    record = _log_import(context, record_payload)
    payload_extra = {
        'plan_slug': plan_slug,
        'planner_contract': _PLANNER_CONTRACT_TASK_SET,
        'task_ids': task_ids,
        'tasks': imported_tasks,
        'task_count': len(imported_tasks),
        'role_output_import': record,
        'next_activation': 'orchestrator',
    }
    if source_task_settlement is not None:
        payload_extra['source_task_settlement'] = source_task_settlement
    if task_set_authority is not None:
        payload_extra['task_set_authority'] = record_payload['task_set_authority']
    payload_extra.update(single_task_fields)
    return _base_payload(
        context,
        loop_runner_status='ok',
        action='imported_planner_task_set_authority',
        job_id=job_id,
        agent_name=str(snapshot.get('agent_name') or ''),
        extra=payload_extra,
    )


def _consume_controlled_planner_task_set(
    context,
    deps,
    *,
    snapshot: dict[str, object],
    reply: str,
    activation: dict[str, object],
    parsed: dict[str, object],
    plan_slug: str,
    plan_result: dict[str, object],
    source_task_id: str,
) -> dict[str, object]:
    job_id = str(snapshot.get('job_id') or '')
    reply_digest = hashlib.sha256(reply.encode('utf-8')).hexdigest()
    allocated: list[dict[str, object]] = []
    for raw in tuple(parsed.get('tasks') or ()):
        if not isinstance(raw, dict):
            continue
        requested = str(raw.get('task_id') or '')
        task_id = _task_set_import_task_id(context, deps, requested_task_id=requested, job_id=job_id)
        allocated.append({
            'requested_task_id': requested,
            'task_id': task_id,
            'title': str(raw.get('title') or _task_title_from_packet(str(raw.get('task_packet') or ''))).strip(),
            'required': bool(raw.get('required', True)),
            'route': raw.get('route'),
            'readiness': raw.get('readiness'),
            'task_packet_sha256': hashlib.sha256(str(raw['task_packet']).encode('utf-8')).hexdigest(),
            'execution_contract_sha256': hashlib.sha256(str(raw['execution_contract']).encode('utf-8')).hexdigest(),
            'task_packet': str(raw['task_packet']),
            'execution_contract': str(raw['execution_contract']),
        })
    task_set_id = 'ts-' + hashlib.sha256(
        f'{plan_slug}\0{source_task_id}\0{job_id}'.encode('utf-8')
    ).hexdigest()[:20]
    source_request = dict(activation['source_request'])
    identity = {
        'project_id': context.project.project_id,
        'plan_slug': plan_slug,
        'plan_revision': plan_revision_authority(context, plan_slug),
        'activation_id': str(activation.get('activation_id') or ''),
        'source_task_id': source_task_id,
        'source_request': source_request,
        'source_job': activation.get('source_job'),
        'planner_job_id': job_id,
        'planner_reply_sha256': reply_digest,
        'task_set_id': task_set_id,
        'ordered_children': [
            {key: child[key] for key in (
                'requested_task_id', 'task_id', 'title', 'required', 'route', 'readiness',
                'task_packet_sha256', 'execution_contract_sha256',
            )}
            for child in allocated
        ],
    }
    try:
        transaction = prepare_planner_task_set_transaction(context, identity=identity)
    except PlannerTaskSetImportConflict as exc:
        return _blocked_payload(
            context, job_id=job_id, agent_name=str(snapshot.get('agent_name') or ''),
            reason='planner_task_set_import_transaction_conflict', evidence={'error': str(exc)},
        )
    if transaction.get('status') == 'committed':
        return _settle_committed_planner_task_set_import(
            context, snapshot=snapshot, transaction=transaction, plan_result=plan_result
        )
    _planner_task_set_import_failure_point('prepared')
    import_root = _role_import_dir(context, job_id)
    imported_tasks: list[dict[str, object]] = []
    try:
        trace = planner_task_set_transaction_trace(transaction, source_job=_job_trace(snapshot, reply))
        for index, child in enumerate(allocated):
            existing = _show_task_optional(context, deps, task_id=str(child['task_id']))
            if existing is None:
                task_payload = deps.plan_task(context, SimpleNamespace(
                    action='task-create', plan_slug=plan_slug, title=child['title'],
                    task_id=child['task_id'], authority_trace=trace,
                ))
                created = True
            else:
                task_payload = existing
                record = existing.get('task') if isinstance(existing.get('task'), dict) else {}
                if (
                    record.get('plan_slug') != plan_slug
                    or record.get('title') != child['title']
                    or record.get('authority_trace') != trace
                ):
                    raise PlannerTaskSetImportConflict(f'child authority conflict: {child["task_id"]}')
                created = False
            _planner_task_set_import_failure_point(f'child_created:{index}')
            expected_revision = _task_payload_revision(task_payload)
            task_root = import_root / str(child['task_id'])
            packet_path = task_root / 'task_packet.md'
            contract_path = task_root / 'execution_contract.md'
            atomic_write_text(packet_path, str(child['task_packet']))
            atomic_write_text(contract_path, str(child['execution_contract']))
            packet = deps.plan_task(context, SimpleNamespace(
                action='task-artifact', task_id=child['task_id'], artifact_kind='task_packet',
                file_path=str(packet_path), actor_source='loop_runner_role_output_import',
                actor='loop_runner', job_id=job_id, expected_task_revision=expected_revision,
            ))
            if str((packet.get('artifact') or {}).get('sha256') or '') != child['task_packet_sha256']:
                raise PlannerTaskSetImportConflict(f'task packet digest conflict: {child["task_id"]}')
            _assert_transaction_artifact_actor(packet.get('artifact'), job_id=job_id, task_id=str(child['task_id']))
            _planner_task_set_import_failure_point(f'artifact_task_packet:{index}')
            contract = deps.plan_task(context, SimpleNamespace(
                action='task-artifact', task_id=child['task_id'], artifact_kind='execution_contract',
                file_path=str(contract_path), actor_source='loop_runner_role_output_import',
                actor='loop_runner', job_id=job_id, expected_task_revision=_task_payload_revision(packet),
            ))
            if str((contract.get('artifact') or {}).get('sha256') or '') != child['execution_contract_sha256']:
                raise PlannerTaskSetImportConflict(f'execution contract digest conflict: {child["task_id"]}')
            _assert_transaction_artifact_actor(contract.get('artifact'), job_id=job_id, task_id=str(child['task_id']))
            _planner_task_set_import_failure_point(f'artifact_execution_contract:{index}')
            imported_tasks.append({
                'task_id': child['task_id'], 'planner_task_id': child['requested_task_id'],
                'title': child['title'], 'route': child['route'], 'readiness': child['readiness'],
                'required': child['required'], 'created_task': created,
                'artifacts': {'task_packet': packet.get('artifact'), 'execution_contract': contract.get('artifact')},
            })
        task_set = create_task_set_authority(
            context, plan_slug=plan_slug, source_task_id=source_task_id,
            source_request=source_request, planner_job={'job_id': job_id, 'reply_sha256': reply_digest},
            children=[{'task_id': child['task_id'], 'required': child['required']} for child in allocated],
            plan_task_fn=deps.plan_task, task_set_id=task_set_id,
        )
        _planner_task_set_import_failure_point('task_set_bound')
        _planner_task_set_import_failure_point('parent_bound')
        for index, item in enumerate(imported_tasks):
            shown = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=item['task_id']))
            _planner_task_set_import_failure_point(f'child_bound:{index}')
            ready = deps.plan_task(context, SimpleNamespace(
                action='task-status', task_id=item['task_id'], status='ready_for_orchestration',
                next_owner='orchestrator', activation_reason='planner_task_set_imported',
                expected_task_revision=_task_payload_revision(shown),
            ))
            item['status_transition'] = _compact_plan_payload(ready)
            _planner_task_set_import_failure_point(f'child_ready:{index}')
        authority = _verify_planner_task_set_import_authority(
            context, deps, transaction=transaction, task_set=task_set, imported_tasks=imported_tasks,
            source_task_id=source_task_id,
        )
        transaction = commit_planner_task_set_transaction(context, transaction, authority=authority)
        _planner_task_set_import_failure_point('committed_before_log')
    except Exception as exc:
        if not isinstance(exc, RuntimeError) or not str(exc).startswith('planner_task_set_import_failure:'):
            fail_planner_task_set_transaction(
                context, transaction, reason='planner_task_set_import_authority_conflict',
                evidence={'type': type(exc).__name__, 'message': str(exc)},
            )
        raise
    return _settle_committed_planner_task_set_import(
        context, snapshot=snapshot, transaction=transaction, plan_result=plan_result
    )


def _verify_planner_task_set_import_authority(
    context, deps, *, transaction, task_set, imported_tasks, source_task_id,
) -> dict[str, object]:
    identity = transaction['identity']
    task_set_record = task_set['task_set']
    if task_set_record.get('task_set_id') != identity['task_set_id'] or task_set_record.get('state') != 'running':
        raise PlannerTaskSetImportConflict('task-set authority is not running with expected identity')
    parent = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=source_task_id))['task']
    parent_binding = parent.get('task_set_parent') if isinstance(parent.get('task_set_parent'), dict) else {}
    if parent.get('status') != 'decomposed' or parent_binding.get('task_set_id') != identity['task_set_id']:
        raise PlannerTaskSetImportConflict('source parent binding authority mismatch')
    children = []
    for expected, imported in zip(identity['ordered_children'], imported_tasks):
        task = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=expected['task_id']))['task']
        binding = task.get('task_set') if isinstance(task.get('task_set'), dict) else {}
        artifacts = task.get('artifacts') if isinstance(task.get('artifacts'), dict) else {}
        if (
            task.get('status') != 'ready_for_orchestration'
            or binding.get('task_set_id') != identity['task_set_id']
            or binding.get('task_set_revision') != 1
            or artifacts.get('task_packet', {}).get('sha256') != expected['task_packet_sha256']
            or artifacts.get('execution_contract', {}).get('sha256') != expected['execution_contract_sha256']
        ):
            raise PlannerTaskSetImportConflict(f'child authority mismatch: {expected["task_id"]}')
        children.append({'task_id': expected['task_id'], 'task_revision': task.get('task_revision'), 'task_set': binding})
    return {
        'task_set_id': identity['task_set_id'], 'task_set_revision': 1,
        'task_set': task_set_record, 'task_set_path': task_set['task_set_path'], 'source_task_id': source_task_id,
        'children': children, 'tasks': imported_tasks,
    }


def _assert_transaction_artifact_actor(artifact: object, *, job_id: str, task_id: str) -> None:
    actor = artifact.get('actor') if isinstance(artifact, dict) and isinstance(artifact.get('actor'), dict) else {}
    if actor.get('source') != 'loop_runner_role_output_import' or actor.get('job_id') != job_id:
        raise PlannerTaskSetImportConflict(f'artifact authority conflict: {task_id}')


def _settle_committed_planner_task_set_import(context, *, snapshot, transaction, plan_result):
    authority = transaction['authority']
    imported_tasks = authority['tasks']
    task_ids = [str(task['task_id']) for task in imported_tasks]
    source_job = _job_trace(snapshot, '')
    source_job['reply_sha256'] = transaction['identity']['planner_reply_sha256']
    source_settlement = {
        'status': 'decomposed', 'task_id': authority['source_task_id'],
        'child_task_ids': task_ids, 'task_set_id': authority['task_set_id'],
        'task_set_revision': authority['task_set_revision'],
    }
    record_payload = {
        'action': 'imported_planner_task_set_authority', 'status': 'ok',
        'source_job': source_job, 'planner_contract': _PLANNER_CONTRACT_TASK_SET,
        'plan_slug': transaction['identity']['plan_slug'], 'task_ids': task_ids,
        'tasks': imported_tasks, 'task_count': len(imported_tasks), 'plan_bootstrap': plan_result,
        'source_task_settlement': source_settlement,
        'task_set_authority': {'task_set': authority['task_set'],
                               'task_set_path': authority['task_set_path']},
        'transaction': {'journal_ref': transaction['journal_ref'], 'transaction_digest': transaction['transaction_digest']},
    }
    record_payload.update(_single_task_set_fields(imported_tasks))
    record = _log_import(context, record_payload)
    return _base_payload(
        context, loop_runner_status='ok', action='imported_planner_task_set_authority',
        job_id=str(transaction['identity']['planner_job_id']), agent_name=str(snapshot.get('agent_name') or ''),
        extra={**record_payload, 'role_output_import': record, 'next_activation': 'orchestrator'},
    )


def _planner_task_set_import_failure_point(stage: str) -> None:
    return None


def _settle_frontdesk_task_set_source_task(
    context,
    deps,
    *,
    plan_slug: str,
    job_id: str,
    snapshot: dict[str, object],
    reply: str,
    activation: dict[str, object] | None,
    import_root: Path,
    imported_tasks: list[dict[str, object]],
) -> dict[str, object] | None:
    source_task_id = _source_task_id_for_task_set(context, activation=activation)
    if not source_task_id:
        return None
    imported_task_ids = [str(task.get('task_id') or '').strip() for task in imported_tasks]
    return _settle_frontdesk_source_task(
        context,
        deps,
        plan_slug=plan_slug,
        job_id=job_id,
        snapshot=snapshot,
        reply=reply,
        activation=activation,
        import_root=import_root,
        source_task_id=source_task_id,
        imported_task_ids=imported_task_ids,
        completion_filename='source_task_set_decomposition_completion.md',
        completion_title='Task Set Decomposition Complete',
        activation_reason='planner_task_set_decomposed_source_task',
    )


def _task_set_authority_enabled(context, activation: dict[str, object] | None = None) -> bool:
    if (
        isinstance(activation, dict)
        and activation.get('source') == 'frontdesk_direct_silence_ask'
    ):
        return True
    snapshot = compile_project_effective_capacity_snapshot(Path(context.project.project_root))
    return int(snapshot.get('config_version') or 0) == 3


def _task_set_source_identity_error(
    activation: dict[str, object] | None,
    *,
    source_task_id: str,
) -> dict[str, object] | None:
    if activation is None or not source_task_id:
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_source_task_identity_missing',
        }
    source_request = activation.get('source_request')
    if not isinstance(source_request, dict):
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_source_request_authority_missing',
        }
    source_job_id = str(source_request.get('source_job_id') or '').strip()
    source_job = activation.get('source_job') if isinstance(activation.get('source_job'), dict) else {}
    if not source_job_id or source_job_id != str(source_job.get('job_id') or '').strip():
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_source_request_identity_mismatch',
        }
    digest = str(source_request.get('sha256') or '').strip()
    size = source_request.get('bytes')
    if not re.fullmatch(r'[0-9a-f]{64}', digest) or isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_source_request_authority_invalid',
        }
    return None


def _task_set_source_task_error(
    context,
    deps,
    *,
    source_task_id: str,
    plan_slug: str,
) -> dict[str, object] | None:
    try:
        payload = deps.plan_task(
            context,
            SimpleNamespace(action='task-show', task_id=source_task_id),
        )
    except ValueError as exc:
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_source_task_authority_missing',
            'error': str(exc),
        }
    task = payload.get('task') if isinstance(payload.get('task'), dict) else {}
    if str(task.get('plan_slug') or '') != plan_slug:
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_source_task_plan_mismatch',
        }
    status = str(task.get('status') or '')
    if status not in {'draft', 'decomposed', 'ready_for_orchestration'}:
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_source_task_not_decomposable',
            'source_status': status,
        }
    return None


def _settle_frontdesk_single_task_source_task(
    context,
    deps,
    *,
    plan_slug: str,
    job_id: str,
    snapshot: dict[str, object],
    reply: str,
    activation: dict[str, object] | None,
    import_root: Path,
    imported_task_id: str,
) -> dict[str, object] | None:
    source_task_id = _source_task_id_for_task_set(context, activation=activation)
    if not source_task_id:
        return None
    return _settle_frontdesk_source_task(
        context,
        deps,
        plan_slug=plan_slug,
        job_id=job_id,
        snapshot=snapshot,
        reply=reply,
        activation=activation,
        import_root=import_root,
        source_task_id=source_task_id,
        imported_task_ids=[imported_task_id],
        completion_filename='source_single_task_handoff_completion.md',
        completion_title='Single Task Handoff Complete',
        activation_reason='planner_single_task_handoff_source_task',
    )


def _settle_frontdesk_source_task(
    context,
    deps,
    *,
    plan_slug: str,
    job_id: str,
    snapshot: dict[str, object],
    reply: str,
    activation: dict[str, object] | None,
    import_root: Path,
    source_task_id: str,
    imported_task_ids: list[str],
    completion_filename: str,
    completion_title: str,
    activation_reason: str,
) -> dict[str, object] | None:
    imported_task_ids = [task_id for task_id in imported_task_ids if task_id]
    if source_task_id in imported_task_ids:
        return None
    try:
        source = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=source_task_id))
    except ValueError:
        return None
    source_task = source.get('task') if isinstance(source.get('task'), dict) else {}
    if str(source_task.get('plan_slug') or '') != plan_slug:
        return None
    source_status = str(source_task.get('status') or '').strip().lower()
    source_next_owner = str(source_task.get('next_owner') or '').strip().lower()
    if source_status == 'done' and source_next_owner == 'terminal':
        return {
            'status': 'already_done',
            'task_id': source_task_id,
            'status_transition': _compact_plan_payload(source),
        }
    if source_status not in {'draft', 'ready_for_orchestration'}:
        return {
            'status': 'skipped',
            'reason': 'source_task_not_settleable',
            'task_id': source_task_id,
            'source_status': source_status,
            'source_next_owner': source_next_owner,
        }
    completion_path = import_root / completion_filename
    child_lines = '\n'.join(f'- {task_id}' for task_id in imported_task_ids)
    atomic_write_text(
        completion_path,
        '\n'.join(
            [
                f'# {completion_title}',
                '',
                f'source_task_id: {source_task_id}',
                f'plan_slug: {plan_slug}',
                f'planner_job_id: {job_id}',
                f'source_job_id: {_activation_source_job_id(activation)}',
                f'child_task_count: {len(imported_task_ids)}',
                '',
                'child_task_ids:',
                child_lines or '- none',
                '',
            ]
        ),
    )
    imported = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=source_task_id,
            artifact_kind='completion',
            file_path=str(completion_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=_activation_task_revision(activation),
        ),
    )
    expected_revision = _task_payload_revision(imported)
    transitioned = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-status',
            task_id=source_task_id,
            status='done',
            next_owner='terminal',
            activation_reason=activation_reason,
            expected_task_revision=expected_revision,
        ),
    )
    return {
        'status': 'done',
        'task_id': source_task_id,
        'artifact': imported.get('artifact'),
        'status_transition': _compact_plan_payload(transitioned),
        'child_task_ids': imported_task_ids,
        'source_job': _job_trace(snapshot, reply),
    }


def _consume_orchestrator(
    context,
    command,
    deps,
    *,
    snapshot: dict[str, object],
    reply: str,
    activation: dict[str, object] | None,
) -> dict[str, object]:
    job_id = str(snapshot.get('job_id') or '')
    task_id = str(getattr(command, 'task_id', None) or '').strip()
    if not task_id and activation is not None:
        task_id = str(activation.get('task_id') or '').strip()
    if not task_id:
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=str(snapshot.get('agent_name') or ''),
            reason='orchestrator_import_requires_task_id',
        )
    parsed = _parse_orchestrator_reply(reply)
    if parsed.get('status') != 'ok':
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=str(snapshot.get('agent_name') or ''),
            reason=str(parsed.get('reason') or 'orchestrator_reply_invalid'),
            evidence=dict(parsed),
        )
    capacity_snapshot: dict[str, object] | None = None
    candidate = parsed.get('orchestration_bundle_candidate')
    if str(parsed['route']) in _EXECUTION_ROUTES:
        capacity_snapshot = deps.effective_capacity_snapshot(context)
        if not isinstance(candidate, dict) and not allows_v2_missing_candidate(capacity_snapshot):
            return _blocked_payload(
                context,
                job_id=job_id,
                agent_name=str(snapshot.get('agent_name') or ''),
                reason='orchestrator_bundle_candidate_required',
                evidence={
                    'route': parsed['route'],
                    'config_version': capacity_snapshot.get('config_version'),
                    'workflow_mode': capacity_snapshot.get('workflow_mode'),
                },
            )
        if isinstance(candidate, dict):
            candidate_check = _prevalidate_orchestrator_bundle_candidate(
                context,
                deps,
                task_id=task_id,
                candidate=candidate,
                capacity_snapshot=capacity_snapshot,
            )
            if candidate_check is not None:
                return _blocked_payload(
                    context,
                    job_id=job_id,
                    agent_name=str(snapshot.get('agent_name') or ''),
                    reason='orchestrator_bundle_candidate_invalid',
                    evidence=candidate_check,
                )
    expected_revision = _expected_revision_for_task(
        context,
        deps,
        activation=activation,
        task_id=task_id,
    )
    import_root = _role_import_dir(context, job_id)
    notes_path = import_root / 'orchestration_notes.md'
    atomic_write_text(notes_path, str(parsed['orchestration_notes']))
    imported = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='orchestration_notes',
            file_path=str(notes_path),
            route=str(parsed['route']),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    bundle_import: dict[str, object] | None = None
    if str(parsed['route']) in _EXECUTION_ROUTES:
        task_record = imported.get('task') if isinstance(imported.get('task'), dict) else {}
        bundle_source = 'loop_runner_role_output_import'
        if not isinstance(candidate, dict):
            candidate = build_single_node_candidate(
                task_record,
                project_root=Path(context.project.project_root),
            )
            bundle_source = 'loop_runner_deterministic_single_node'
        candidate_path = import_root / 'orchestration_bundle.candidate.json'
        atomic_write_json(candidate_path, candidate)
        try:
            bundle_import = deps.plan_task(
                context,
                SimpleNamespace(
                    action='task-artifact',
                    task_id=task_id,
                    artifact_kind='orchestration_bundle',
                    file_path=str(candidate_path),
                    actor_source=bundle_source,
                    actor='loop_runner',
                    job_id=job_id,
                    expected_task_revision=task_revision(task_record),
                    effective_capacity_snapshot=capacity_snapshot,
                    source_reply_digest=hashlib.sha256(reply.encode('utf-8')).hexdigest(),
                ),
            )
        except ValueError as exc:
            return _blocked_payload(
                context,
                job_id=job_id,
                agent_name=str(snapshot.get('agent_name') or ''),
                reason='orchestrator_bundle_import_failed',
                evidence={
                    'task_id': task_id,
                    'route': parsed['route'],
                    'error': str(exc),
                    'candidate_path': str(candidate_path.relative_to(Path(context.project.project_root))),
                },
            )
    record = _log_import(
        context,
        {
            'action': 'imported_orchestration_notes',
            'status': 'ok',
            'source_job': _job_trace(snapshot, reply),
            'task_id': task_id,
            'route': parsed['route'],
            'artifact': imported.get('artifact'),
            'orchestration_bundle': bundle_import.get('artifact') if isinstance(bundle_import, dict) else None,
        },
    )
    return _base_payload(
        context,
        loop_runner_status='ok',
        action='imported_orchestration_notes',
        job_id=job_id,
        agent_name=str(snapshot.get('agent_name') or ''),
        extra={
            'task_id': task_id,
            'task_status': imported.get('status'),
            'next_owner': imported.get('next_owner'),
            'route': parsed['route'],
            'import': _compact_plan_payload(imported),
            'orchestration_bundle': (
                bundle_import.get('artifact') if isinstance(bundle_import, dict) else None
            ),
            'role_output_import': record,
            'next_activation': _next_activation_for_route(str(parsed['route'])),
        },
    )


def _prevalidate_orchestrator_bundle_candidate(
    context,
    deps,
    *,
    task_id: str,
    candidate: dict[str, object],
    capacity_snapshot: dict[str, object],
) -> dict[str, object] | None:
    try:
        shown = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=task_id))
        record = shown.get('task') if isinstance(shown.get('task'), dict) else {}
        normalize_bundle_candidate(
            candidate,
            record=record,
            project_root=Path(context.project.project_root),
            capacity_snapshot=capacity_snapshot,
        )
    except ValueError as exc:
        return {
            'task_id': task_id,
            'error': str(exc),
            'schema': candidate.get('schema'),
        }
    return None


def _single_task_set_fields(imported_tasks: list[dict[str, object]]) -> dict[str, object]:
    if len(imported_tasks) != 1:
        return {}
    task = imported_tasks[0]
    fields: dict[str, object] = {}
    task_id = str(task.get('task_id') or '').strip()
    if task_id:
        fields['task_id'] = task_id
    route = str(task.get('route') or '').strip()
    if route:
        fields['route'] = route
    status_transition = task.get('status_transition') if isinstance(task.get('status_transition'), dict) else {}
    if status_transition:
        fields['status_transition'] = status_transition
    status = str(status_transition.get('status') or '').strip()
    if status:
        fields['task_status'] = status
    next_owner = str(status_transition.get('next_owner') or '').strip()
    if next_owner:
        fields['next_owner'] = next_owner
    return fields


def _source_task_id_for_task_set(context, *, activation: dict[str, object] | None) -> str:
    if not isinstance(activation, dict):
        return ''
    raw = str(activation.get('source_task_id') or '').strip()
    if not raw:
        source_task = activation.get('source_task') if isinstance(activation.get('source_task'), dict) else {}
        raw = str(source_task.get('task_id') or '').strip()
    if not raw:
        source_job = activation.get('source_job') if isinstance(activation.get('source_job'), dict) else {}
        raw = _job_request_task_id(
            context,
            job_id=str(source_job.get('job_id') or '').strip(),
            agent_name=str(source_job.get('agent_name') or '').strip(),
        )
    if not raw or raw.startswith('act-'):
        return ''
    if not _SEGMENT_RE.fullmatch(raw):
        return ''
    return raw


def _activation_source_job_id(activation: dict[str, object] | None) -> str:
    source_job = activation.get('source_job') if isinstance(activation, dict) and isinstance(activation.get('source_job'), dict) else {}
    return str(source_job.get('job_id') or '').strip()


def _job_request_task_id(context, *, job_id: str, agent_name: str) -> str:
    job_id = str(job_id or '').strip()
    agent_name = str(agent_name or '').strip()
    if not job_id or not agent_name or not _SEGMENT_RE.fullmatch(agent_name):
        return ''
    path = Path(context.project.project_root) / '.ccb' / 'agents' / agent_name / 'jobs.jsonl'
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return ''
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or str(record.get('job_id') or '').strip() != job_id:
            continue
        request = record.get('request') if isinstance(record.get('request'), dict) else {}
        task_id = str(request.get('task_id') or '').strip()
        return task_id if _SEGMENT_RE.fullmatch(task_id) else ''
    return ''


def _frontdesk_source_request_for_job(context, *, job_id: str, agent_name: str) -> dict[str, object]:
    job_id = str(job_id or '').strip()
    agent_name = str(agent_name or '').strip()
    if not job_id or agent_name != 'frontdesk':
        return {
            'status': 'blocked',
            'reason': 'frontdesk_source_job_identity_mismatch',
            'source_job_id': job_id,
            'agent_name': agent_name,
        }
    path = Path(context.project.project_root) / '.ccb' / 'agents' / agent_name / 'jobs.jsonl'
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return {
            'status': 'blocked',
            'reason': 'frontdesk_source_job_missing',
            'source_job_id': job_id,
        }
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or str(record.get('job_id') or '').strip() != job_id:
            continue
        return resolve_frontdesk_source_request(context, source_job_id=job_id, job=record)
    return {
        'status': 'blocked',
        'reason': 'frontdesk_source_job_missing',
        'source_job_id': job_id,
    }


def _frontdesk_source_request_evidence(source_request: dict[str, object]) -> dict[str, object]:
    return {
        key: source_request.get(key)
        for key in (
            'source_job_id',
            'agent_name',
            'project_id',
            'to_agent',
            'from_actor',
            'message_type',
            'bytes',
            'sha256',
            'preview',
            'body_artifact',
        )
    }


def _consume_task_detailer(
    context,
    command,
    deps,
    *,
    snapshot: dict[str, object],
    reply: str,
    activation: dict[str, object] | None,
) -> dict[str, object]:
    job_id = str(snapshot.get('job_id') or '')
    task_id = _first_optional_text(
        getattr(command, 'task_id', None),
        activation.get('task_id') if activation is not None else None,
    )
    if not task_id:
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=str(snapshot.get('agent_name') or ''),
            reason='task_detailer_import_requires_task_id',
        )
    expected_revision = _expected_revision_for_task(
        context,
        deps,
        activation=activation,
        task_id=task_id,
    )
    parsed = _parse_task_detailer_reply(
        reply,
        detail_ready_stop_contract=activation.get('detail_ready_stop_contract') if isinstance(activation, dict) else None,
    )
    if str(parsed.get('result') or '') == 'planner_replan_required':
        return _consume_task_detailer_replan_feedback(
            context,
            deps,
            snapshot=snapshot,
            parsed=parsed,
            reply=reply,
            job_id=job_id,
            task_id=task_id,
        )
    if (
        parsed.get('status') == 'blocked'
        and parsed.get('reason') == 'task_detailer_reply_not_detail_ready'
        and str(parsed.get('readiness') or '').strip().lower() == 'needs_clarification'
    ):
        return _consume_task_detailer_clarification(
            context,
            deps,
            snapshot=snapshot,
            parsed=parsed,
            reply=reply,
            job_id=job_id,
            task_id=task_id,
            expected_revision=expected_revision,
        )
    if (
        parsed.get('status') == 'blocked'
        and parsed.get('reason') == 'task_detailer_reply_not_detail_ready'
        and str(parsed.get('readiness') or '').strip().lower() == 'blocked'
    ):
        return _consume_task_detailer_blocker(
            context,
            deps,
            snapshot=snapshot,
            parsed=parsed,
            reply=reply,
            job_id=job_id,
            task_id=task_id,
            expected_revision=expected_revision,
        )
    if parsed.get('status') != 'ok':
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=str(snapshot.get('agent_name') or ''),
            reason=str(parsed.get('reason') or 'task_detailer_reply_invalid'),
            evidence=dict(parsed),
        )
    import_root = _role_import_dir(context, job_id)
    detail_design_path = import_root / 'task-detail-design.md'
    detail_summary_path = import_root / 'brief-update-summary.md'
    detail_packet_path = import_root / 'detail-packet.manifest.json'
    atomic_write_text(detail_design_path, str(parsed['detail_design']))
    atomic_write_text(detail_summary_path, str(parsed['detail_summary']))
    atomic_write_text(detail_packet_path, str(parsed['detail_packet']))
    detail_design_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='detail_design',
            file_path=str(detail_design_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(detail_design_import)
    detail_summary_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='detail_summary',
            file_path=str(detail_summary_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(detail_summary_import)
    detail_packet_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='detail_packet',
            file_path=str(detail_packet_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(detail_packet_import)
    ready = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-status',
            task_id=task_id,
            status='detail_ready',
            next_owner='planner',
            activation_reason='detail_ready_from_task_detailer',
            expected_task_revision=expected_revision,
        ),
    )
    record = _log_import(
        context,
        {
            'action': 'imported_task_detailer_detail_authority',
            'status': 'ok',
            'source_job': _job_trace(snapshot, reply),
            'task_id': task_id,
            'created_task': False,
            'artifacts': {
                'detail_design': detail_design_import.get('artifact'),
                'detail_summary': detail_summary_import.get('artifact'),
                'detail_packet': detail_packet_import.get('artifact'),
            },
            'status_transition': _compact_plan_payload(ready),
        },
    )
    return _base_payload(
        context,
        loop_runner_status='ok',
        action='imported_task_detailer_detail_authority',
        job_id=job_id,
        agent_name=str(snapshot.get('agent_name') or ''),
        extra={
            'task_id': task_id,
            'task_status': ready.get('status'),
            'next_owner': ready.get('next_owner'),
            'created_task': False,
            'imports': {
                'detail_design': _compact_plan_payload(detail_design_import),
                'detail_summary': _compact_plan_payload(detail_summary_import),
                'detail_packet': _compact_plan_payload(detail_packet_import),
            },
            'role_output_import': record,
            'next_activation': 'orchestrator',
        },
    )


def _consume_task_detailer_replan_feedback(
    context,
    deps,
    *,
    snapshot: dict[str, object],
    parsed: dict[str, object],
    reply: str,
    job_id: str,
    task_id: str,
) -> dict[str, object]:
    intent = _accepted_detailer_replan_intent(context, source_job_id=job_id)
    if intent is None:
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=str(snapshot.get('agent_name') or ''),
            reason='task_detailer_planner_replan_direct_handoff_missing',
            evidence={'task_id': task_id, 'result': 'planner_replan_required'},
        )
    shown = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=task_id))
    task = shown.get('task') if isinstance(shown.get('task'), dict) else {}
    feedback = task.get('replan_feedback') if isinstance(task.get('replan_feedback'), dict) else {}
    if (
        feedback.get('request_identity') != intent.get('request_identity')
        or feedback.get('source_detailer_job_id') != job_id
        or int(task.get('task_revision') or 0) < int(feedback.get('accepted_task_revision') or 0)
    ):
        return _blocked_payload(
            context,
            job_id=job_id,
            agent_name=str(snapshot.get('agent_name') or ''),
            reason='task_detailer_planner_replan_authority_mismatch',
            evidence={
                'task_id': task_id,
                'task_status': task.get('status'),
                'next_owner': task.get('next_owner'),
                'request_identity': intent.get('request_identity'),
            },
        )
    record = _log_import(
        context,
        {
            'action': 'imported_task_detailer_replan_feedback',
            'status': 'ok',
            'source_job': _job_trace(snapshot, reply),
            'task_id': task_id,
            'result': 'planner_replan_required',
            'request_identity': intent.get('request_identity'),
            'detail_digest': intent.get('detail_digest'),
            'macro_impact_digest': intent.get('macro_impact_digest'),
            'planner_job_id': intent.get('planner_job_id'),
            'intent_path': intent.get('_path'),
            'detail_sections': {
                'detail_design_present': bool(parsed.get('detail_design')),
                'detail_summary_present': bool(parsed.get('detail_summary')),
                'detail_packet_present': bool(parsed.get('detail_packet')),
            },
        },
    )
    return _base_payload(
        context,
        loop_runner_status='pending',
        action='imported_task_detailer_replan_feedback',
        job_id=job_id,
        agent_name=str(snapshot.get('agent_name') or ''),
        extra={
            'task_id': task_id,
            'task_status': task.get('status'),
            'next_owner': task.get('next_owner'),
            'request_identity': intent.get('request_identity'),
            'planner_job_id': intent.get('planner_job_id'),
            'role_output_import': record,
            'next_activation': (
                'planner'
                if str(task.get('status') or '') == 'replan_required'
                else str(task.get('next_owner') or 'inspect')
            ),
        },
    )


def _consume_task_detailer_clarification(
    context,
    deps,
    *,
    snapshot: dict[str, object],
    parsed: dict[str, object],
    reply: str,
    job_id: str,
    task_id: str,
    expected_revision: int,
) -> dict[str, object]:
    import_root = _role_import_dir(context, job_id)
    detail_design_path = import_root / 'task-detail-design.md'
    detail_summary_path = import_root / 'brief-update-summary.md'
    detail_packet_path = import_root / 'detail-packet.manifest.json'
    detail_design = str(parsed.get('detail_design') or '')
    detail_summary = str(parsed.get('detail_summary') or '')
    detail_packet = str(parsed.get('detail_packet') or '')
    atomic_write_text(detail_design_path, detail_design)
    atomic_write_text(detail_summary_path, detail_summary)
    atomic_write_text(detail_packet_path, detail_packet)
    detail_design_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='detail_design',
            file_path=str(detail_design_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(detail_design_import)
    detail_summary_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='detail_summary',
            file_path=str(detail_summary_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(detail_summary_import)
    detail_packet_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='detail_packet',
            file_path=str(detail_packet_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(detail_packet_import)
    clarified = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-status',
            task_id=task_id,
            status='needs_clarification',
            next_owner='task_detailer',
            activation_reason='needs_clarification_from_task_detailer',
            expected_task_revision=expected_revision,
        ),
    )
    record = _log_import(
        context,
        {
            'action': 'imported_task_detailer_clarification_authority',
            'status': 'ok',
            'source_job': _job_trace(snapshot, reply),
            'task_id': task_id,
            'readiness': str(parsed.get('readiness') or 'needs_clarification'),
            'created_task': False,
            'artifacts': {
                'detail_design': detail_design_import.get('artifact'),
                'detail_summary': detail_summary_import.get('artifact'),
                'detail_packet': detail_packet_import.get('artifact'),
            },
            'status_transition': _compact_plan_payload(clarified),
        },
    )
    return _base_payload(
        context,
        loop_runner_status='paused',
        action='imported_task_detailer_clarification_authority',
        job_id=job_id,
        agent_name=str(snapshot.get('agent_name') or ''),
        extra={
            'task_id': task_id,
            'task_status': clarified.get('status'),
            'next_owner': clarified.get('next_owner'),
            'readiness': str(parsed.get('readiness') or 'needs_clarification'),
            'created_task': False,
            'imports': {
                'detail_design': _compact_plan_payload(detail_design_import),
                'detail_summary': _compact_plan_payload(detail_summary_import),
                'detail_packet': _compact_plan_payload(detail_packet_import),
            },
            'role_output_import': record,
            'next_activation': 'task_detailer',
        },
    )


def _consume_task_detailer_blocker(
    context,
    deps,
    *,
    snapshot: dict[str, object],
    parsed: dict[str, object],
    reply: str,
    job_id: str,
    task_id: str,
    expected_revision: int,
) -> dict[str, object]:
    import_root = _role_import_dir(context, job_id)
    detail_design_path = import_root / 'task-detail-design.md'
    detail_summary_path = import_root / 'brief-update-summary.md'
    detail_packet_path = import_root / 'detail-packet.manifest.json'
    blocker_evidence_path = import_root / 'blocker-evidence.md'
    detail_design = str(parsed.get('detail_design') or '')
    detail_summary = str(parsed.get('detail_summary') or '')
    detail_packet = str(parsed.get('detail_packet') or '')
    atomic_write_text(detail_design_path, detail_design)
    atomic_write_text(detail_summary_path, detail_summary)
    atomic_write_text(detail_packet_path, detail_packet)
    atomic_write_text(
        blocker_evidence_path,
        '\n\n'.join(
            part
            for part in (
                '# Task Detailer Blocker Evidence',
                f'Task: {task_id}',
                'Readiness recommendation: blocked',
                'The task detailer determined that this task cannot be safely refined into implementation work yet.',
                '## Detail Design',
                detail_design,
                '## Detail Summary',
                detail_summary,
                '## Detail Packet',
                detail_packet,
            )
            if part
        )
        + '\n',
    )
    detail_design_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='detail_design',
            file_path=str(detail_design_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(detail_design_import)
    detail_summary_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='detail_summary',
            file_path=str(detail_summary_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(detail_summary_import)
    detail_packet_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='detail_packet',
            file_path=str(detail_packet_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(detail_packet_import)
    blocker_import = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind='blocker_evidence',
            file_path=str(blocker_evidence_path),
            actor_source='loop_runner_role_output_import',
            actor='loop_runner',
            job_id=job_id,
            expected_task_revision=expected_revision,
        ),
    )
    expected_revision = _task_payload_revision(blocker_import)
    blocked = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-status',
            task_id=task_id,
            status='blocked',
            next_owner='terminal',
            activation_reason='blocked_from_task_detailer',
            expected_task_revision=expected_revision,
        ),
    )
    record = _log_import(
        context,
        {
            'action': 'imported_task_detailer_blocker_authority',
            'status': 'ok',
            'source_job': _job_trace(snapshot, reply),
            'task_id': task_id,
            'created_task': False,
            'readiness': parsed.get('readiness'),
            'controller_expected_stop': parsed.get('controller_expected_stop'),
            'artifacts': {
                'detail_design': detail_design_import.get('artifact'),
                'detail_summary': detail_summary_import.get('artifact'),
                'detail_packet': detail_packet_import.get('artifact'),
                'blocker_evidence': blocker_import.get('artifact'),
            },
            'status_transition': _compact_plan_payload(blocked),
        },
    )
    return _base_payload(
        context,
        loop_runner_status='ok',
        action='imported_task_detailer_blocker_authority',
        job_id=job_id,
        agent_name=str(snapshot.get('agent_name') or ''),
        extra={
            'task_id': task_id,
            'task_status': blocked.get('status'),
            'next_owner': blocked.get('next_owner'),
            'created_task': False,
            'imports': {
                'detail_design': _compact_plan_payload(detail_design_import),
                'detail_summary': _compact_plan_payload(detail_summary_import),
                'detail_packet': _compact_plan_payload(detail_packet_import),
                'blocker_evidence': _compact_plan_payload(blocker_import),
            },
            'role_output_import': record,
            'next_activation': 'none',
        },
    )


def _validated_planner_terminal_status_constraint(
    context,
    deps,
    *,
    activation: dict[str, object] | None,
) -> dict[str, object]:
    if not isinstance(activation, dict) or 'terminal_status_constraint' not in activation:
        return {'status': 'not_applicable'}
    constraint = activation.get('terminal_status_constraint')
    if not isinstance(constraint, dict):
        return {
            'status': 'blocked',
            'reason': 'planner_terminal_status_constraint_not_object',
        }
    raw_task_id = constraint.get('task_id')
    raw_required_reason = constraint.get('required_reason')
    task_id = raw_task_id.strip() if isinstance(raw_task_id, str) else ''
    required_reason = raw_required_reason.strip() if isinstance(raw_required_reason, str) else ''
    authority_digest = str(constraint.get('authority_digest') or '').strip().lower()
    basis_digest = str(constraint.get('basis_digest') or '').strip().lower()
    task_revision = constraint.get('task_revision')
    state_version = constraint.get('state_version')
    required = {
        'schema_version': constraint.get('schema_version'),
        'status': constraint.get('status'),
        'basis': constraint.get('basis'),
        'task_id': task_id,
        'task_revision': task_revision,
        'state_version': state_version,
        'authority_digest': authority_digest,
        'basis_digest': basis_digest,
        'required_reason': required_reason,
    }
    if (
        required['schema_version'] != _TERMINAL_STATUS_CONSTRAINT_SCHEMA_VERSION
        or required['status'] != 'detail_ready'
        or required['basis'] != _TERMINAL_STATUS_CONSTRAINT_BASIS
        or not isinstance(raw_task_id, str)
        or not _SEGMENT_RE.fullmatch(task_id)
        or isinstance(task_revision, bool)
        or not isinstance(task_revision, int)
        or task_revision <= 0
        or isinstance(state_version, bool)
        or not isinstance(state_version, int)
        or state_version <= 0
        or not re.fullmatch(r'[0-9a-f]{64}', authority_digest)
        or not re.fullmatch(r'[0-9a-f]{64}', basis_digest)
        or not isinstance(raw_required_reason, str)
        or not _SEGMENT_RE.fullmatch(required_reason)
    ):
        return {
            'status': 'blocked',
            'reason': 'planner_terminal_status_constraint_invalid_shape',
            'constraint': required,
        }
    if (
        str(activation.get('task_id') or '').strip() != task_id
        or activation.get('task_revision') != task_revision
        or activation.get('task_status') != 'detail_ready'
    ):
        return {
            'status': 'blocked',
            'reason': 'planner_terminal_status_constraint_activation_mismatch',
            'constraint': required,
        }
    try:
        task_payload = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=task_id))
    except ValueError as exc:
        return {
            'status': 'blocked',
            'reason': 'planner_terminal_status_constraint_task_unavailable',
            'constraint': required,
            'error': str(exc),
        }
    record = task_payload.get('task') if isinstance(task_payload.get('task'), dict) else {}
    authority = detail_ready_stop_contract_authority(
        record,
        project_root=Path(context.project.project_root),
    )
    if authority is None:
        return {
            'status': 'blocked',
            'reason': 'planner_terminal_status_constraint_stale_authority',
            'constraint': required,
        }
    observed = {
        'task_id': record.get('task_id'),
        'task_revision': authority.get('task_revision'),
        'state_version': authority.get('state_version'),
        'authority_digest': authority.get('authority_digest'),
        'basis_digest': authority.get('basis_digest'),
    }
    expected = {
        'task_id': task_id,
        'task_revision': task_revision,
        'state_version': state_version,
        'authority_digest': authority_digest,
        'basis_digest': basis_digest,
    }
    if observed != expected:
        return {
            'status': 'blocked',
            'reason': 'planner_terminal_status_constraint_stale_authority',
            'constraint': required,
            'observed': observed,
        }
    return {
        'status': 'ok',
        'constraint': required,
        'task_payload': task_payload,
    }


def _validate_planner_terminal_status_reply(
    parsed: dict[str, object],
    *,
    constraint: dict[str, object],
) -> dict[str, object]:
    observed = {
        'readiness': parsed.get('readiness_status'),
        'route': parsed.get('route'),
        'status_recommendation': parsed.get('status_recommendation'),
        'reason': parsed.get('reason'),
        'allowed_paths': parsed.get('allowed_paths'),
        'blockers': parsed.get('blockers'),
    }
    if (
        observed['readiness'] != 'ready'
        or observed['route'] != 'needs_detail'
        or observed['status_recommendation'] != 'detail_ready'
        or observed['reason'] != constraint['required_reason']
        or bool(observed['allowed_paths'])
        or bool(observed['blockers'])
    ):
        return {
            'status': 'blocked',
            'reason': 'planner_terminal_status_reply_mismatch',
            'constraint': {
                'status': constraint['status'],
                'required_reason': constraint['required_reason'],
            },
            'observed': observed,
        }
    return {'status': 'ok', 'observed': observed}


def _settle_planner_terminal_status_constraint(
    context,
    *,
    snapshot: dict[str, object],
    reply: str,
    task_payload: dict[str, object],
    constraint: dict[str, object],
    reply_check: dict[str, object],
) -> dict[str, object]:
    task = task_payload.get('task') if isinstance(task_payload.get('task'), dict) else {}
    job_id = str(snapshot.get('job_id') or '')
    record = _log_import(
        context,
        {
            'action': 'settled_planner_terminal_status_constraint',
            'status': 'ok',
            'source_job': _job_trace(snapshot, reply),
            'task_id': task.get('task_id'),
            'task_status': task.get('status'),
            'next_owner': task.get('next_owner'),
            'terminal_status_constraint': constraint,
            'reply_check': reply_check,
        },
    )
    return _base_payload(
        context,
        loop_runner_status='ok',
        action='settled_planner_terminal_status_constraint',
        job_id=job_id,
        agent_name=str(snapshot.get('agent_name') or ''),
        extra={
            'task_id': task.get('task_id'),
            'task_status': task.get('status'),
            'next_owner': task.get('next_owner'),
            'terminal_status_constraint': constraint,
            'role_output_import': record,
            'next_activation': 'none',
        },
    )


def _parse_planner_reply(reply: str) -> dict[str, object]:
    task_packet = _fenced_section(reply, ('task-packet.md', 'task_packet.md'))
    readiness_text = _fenced_section(reply, ('readiness.json',))
    missing = []
    if not task_packet:
        missing.append('task-packet.md fenced section')
    if not readiness_text:
        missing.append('readiness.json fenced section')
    if missing:
        return {'status': 'blocked', 'reason': 'planner_reply_missing_required_sections', 'missing_fields': missing}
    try:
        readiness = json.loads(readiness_text)
    except json.JSONDecodeError as exc:
        return {'status': 'blocked', 'reason': 'planner_readiness_json_invalid', 'error': str(exc)}
    if not isinstance(readiness, dict):
        return {'status': 'blocked', 'reason': 'planner_readiness_json_not_object'}
    readiness_value = str(readiness.get('readiness') or '').strip().lower()
    route = str(readiness.get('route') or '').strip().lower()
    if route not in _VALID_ROUTES:
        return {'status': 'blocked', 'reason': 'unknown_route', 'route': route or None, 'expected_routes': sorted(_VALID_ROUTES)}
    if readiness_value not in _VALID_READINESS:
        return {
            'status': 'blocked',
            'reason': 'unknown_readiness',
            'readiness': readiness_value or 'missing',
            'route': route,
            'expected_readiness': sorted(_VALID_READINESS),
        }
    if route in _EXECUTION_ROUTES and readiness_value != 'ready':
        return {
            'status': 'blocked',
            'reason': 'planner_readiness_not_ready',
            'readiness': readiness_value or 'missing',
            'route': route,
        }
    if route == 'needs_detail' and readiness_value not in _NEEDS_DETAIL_READINESS:
        return {
            'status': 'blocked',
            'reason': 'planner_readiness_incompatible_with_route',
            'readiness': readiness_value or 'missing',
            'route': route,
            'expected_readiness': sorted(_NEEDS_DETAIL_READINESS),
        }
    allowed_paths = _string_list(readiness.get('allowed_paths'))
    try:
        verification = _canonicalize_verification_commands(_string_list(readiness.get('verification')))
    except ValueError as exc:
        return {
            'status': 'blocked',
            'reason': 'planner_readiness_invalid_verification',
            'error': str(exc),
        }
    blockers = _string_list(readiness.get('blockers'))
    status_recommendation = _optional_planner_readiness_text(readiness, field='status_recommendation')
    if status_recommendation is not None and status_recommendation not in _VALID_STATUS_RECOMMENDATIONS:
        return {
            'status': 'blocked',
            'reason': 'planner_readiness_invalid_status_recommendation',
            'status_recommendation': status_recommendation,
            'expected_status_recommendations': sorted(_VALID_STATUS_RECOMMENDATIONS),
        }
    reason = _optional_planner_readiness_text(readiness, field='reason')
    if reason is not None and not _SEGMENT_RE.fullmatch(reason):
        return {
            'status': 'blocked',
            'reason': 'planner_readiness_invalid_reason',
            'reply_reason': reason,
        }
    missing_fields = []
    if route in _EXECUTION_ROUTES and not allowed_paths:
        missing_fields.append('readiness.allowed_paths')
    if not verification:
        missing_fields.append('readiness.verification')
    if route == 'needs_detail' and readiness_value == 'needs_clarification' and not blockers:
        missing_fields.append('readiness.blockers')
    if missing_fields:
        return {'status': 'blocked', 'reason': 'planner_readiness_missing_required_fields', 'missing_fields': missing_fields}
    invalid_allowed_paths = _invalid_allowed_paths(allowed_paths)
    if invalid_allowed_paths:
        return {
            'status': 'blocked',
            'reason': 'planner_readiness_invalid_allowed_paths',
            'invalid_allowed_paths': invalid_allowed_paths,
        }
    execution_contract = _task_set_execution_contract(
        raw_contract=_fenced_section(reply, ('execution-contract.md', 'execution_contract.md')),
        route=route,
        allowed_paths=allowed_paths,
        verification=verification,
    )
    title = _task_title_from_packet(task_packet)
    return {
        'status': 'ok',
        'task_packet': task_packet.strip() + '\n',
        'execution_contract': execution_contract.strip() + '\n',
        'readiness': readiness,
        'readiness_status': readiness_value,
        'route': route,
        'allowed_paths': allowed_paths,
        'verification': verification,
        'blockers': blockers,
        'status_recommendation': status_recommendation,
        'reason': reason,
        'title': title,
    }


def _optional_planner_readiness_text(readiness: dict[str, object], *, field: str) -> str | None:
    if field not in readiness:
        return None
    value = readiness.get(field)
    if not isinstance(value, str):
        return ''
    return value.strip().lower() if field == 'status_recommendation' else value.strip()


def _parse_planner_reply_for_contract(reply: str, *, planner_contract: str) -> dict[str, object]:
    if planner_contract == _PLANNER_CONTRACT_DETAILER_REPLAN:
        try:
            proposal = parse_planner_feedback_reply(reply)
        except PlannerFeedbackError as exc:
            return {'status': 'blocked', 'reason': exc.code, 'error': str(exc)}
        return {'status': 'ok', 'planner_contract': _PLANNER_CONTRACT_DETAILER_REPLAN, 'proposal': proposal}
    if planner_contract == _PLANNER_CONTRACT_TASK_SET:
        parsed = _parse_planner_task_set_reply(reply)
        if parsed.get('status') == 'ok':
            return parsed
        evidence = dict(parsed)
        if _has_single_task_planner_sections(reply):
            evidence['single_task_reply_detected'] = True
            evidence['reason'] = 'planner_task_set_required'
            return evidence
        return evidence
    parsed = _parse_planner_reply(reply)
    if parsed.get('status') == 'ok':
        parsed['planner_contract'] = _PLANNER_CONTRACT_SINGLE_TASK
    return parsed


def _consume_detailer_replan_planner_backfill(context, deps, *, snapshot, reply: str, activation):
    """Import the sole Planner backfill contract used after a Detailer replan.

    This deliberately has no packet-parser fallback: a replan activation can only
    consume the strict planner-backfill section authenticated by its activation.
    """
    job_id = str(snapshot.get('job_id') or '')
    resolved = _resolve_detailer_replan_authority(context, deps, job_id=job_id)
    if not resolved['claimed'] or resolved['error']:
        return _detailer_replan_blocked_payload(context, job_id=job_id, agent_name='planner', reason='detailer_replan_authority_invalid', evidence={'error': resolved['error'] or 'detailer replan authority missing'})
    activation = resolved['activation']
    authority = activation.get('planner_authority') if isinstance(activation, dict) else None
    if not isinstance(authority, dict):
        return _blocked_payload(context, job_id=job_id, agent_name='planner', reason='detailer_replan_authority_missing', evidence={})
    try:
        proposal = parse_planner_feedback_reply(reply)
        validate_planner_feedback_authority(
            proposal,
            mode='detailer_replan',
            expected_plan_revision=str(authority['expected_plan_revision']),
            task_or_task_set_id=str(authority['task_id']),
            task_or_task_set_revision=int(authority['task_revision']),
            closure_evidence_digest=str(authority['closure_evidence_digest']),
            aggregate_result='replan_required',
            evidence_refs=list(authority['evidence_refs']),
        )
        if proposal.result != 'task_set_replanned':
            raise PlannerFeedbackError('planner_backfill_result_laundering', 'detailer replan must result in task_set_replanned')
        applied = apply_detailer_replan_backfill(context, proposal, authority=authority, planner_job_id=job_id)
        settled = deps.plan_task(context, SimpleNamespace(
            action='task-complete-detailer-replan', task_id=str(authority['task_id']),
            expected_task_revision=int(authority['task_revision']), planner_job_id=job_id,
            planner_feedback_digest=planner_feedback_digest(proposal),
            backfill_path=applied['backfill_path'],
        ))
    except (KeyError, TypeError, ValueError, PlannerFeedbackError) as exc:
        return _blocked_payload(context, job_id=job_id, agent_name='planner', reason='detailer_replan_backfill_invalid', evidence={'error': str(exc)})
    record = _log_import(context, {
        'action': 'imported_detailer_replan_planner_backfill', 'status': 'ok',
        'source_job': _job_trace(snapshot, reply), 'planner_contract': _PLANNER_CONTRACT_DETAILER_REPLAN,
        'authority': authority, 'backfill': applied, 'task_transition': _compact_plan_payload(settled),
    })
    return _base_payload(context, loop_runner_status='ok', action='imported_detailer_replan_planner_backfill', job_id=job_id, agent_name='planner', extra={
        'task_id': authority['task_id'], 'task_status': settled.get('status'),
        'next_owner': settled.get('next_owner'), 'backfill': applied,
        'role_output_import': record, 'next_activation': 'orchestrator',
    })


def _validate_frontdesk_single_task_semantics(
    parsed: dict[str, object],
    *,
    activation: dict[str, object] | None,
) -> dict[str, object]:
    if not _is_frontdesk_planner_activation(activation):
        return {'status': 'ok'}
    sections = _markdown_h2_sections(str(parsed.get('task_packet') or ''))
    required = _FRONTDESK_SINGLE_TASK_SEMANTIC_SECTIONS
    missing = [name for name in required if not sections.get(name)]
    if missing:
        return {
            'status': 'blocked',
            'reason': 'planner_task_packet_missing_semantic_sections',
            'missing_fields': [f'task_packet.{name}' for name in missing],
            'required_sections': list(required),
        }
    return {'status': 'ok', 'semantic_sections': list(required)}


def _is_frontdesk_planner_activation(activation: dict[str, object] | None) -> bool:
    if not isinstance(activation, dict):
        return False
    return (
        str(activation.get('record_type') or '').strip() == 'ccb_loop_frontdesk_planner_activation'
        or str(activation.get('action') or '').strip() == 'activate_planner_from_frontdesk'
    )


def _markdown_h2_sections(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in str(markdown or '').splitlines():
        heading = re.match(r'^\s*##\s+(.+?)\s*$', line)
        if heading:
            normalized = re.sub(r'\s+', ' ', heading.group(1).strip().lower())
            current = _canonical_frontdesk_semantic_heading(normalized)
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {name: '\n'.join(lines).strip() for name, lines in sections.items()}


def _canonical_frontdesk_semantic_heading(heading: str) -> str:
    if heading in _FRONTDESK_SINGLE_TASK_SEMANTIC_SECTIONS:
        return heading
    candidates = [
        expected
        for expected in _FRONTDESK_SINGLE_TASK_SEMANTIC_SECTIONS
        if _within_one_edit(heading, expected)
    ]
    return candidates[0] if len(candidates) == 1 else heading


def _within_one_edit(left: str, right: str) -> bool:
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) == 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    short_index = 0
    long_index = 0
    skipped = False
    while short_index < len(shorter) and long_index < len(longer):
        if shorter[short_index] == longer[long_index]:
            short_index += 1
            long_index += 1
            continue
        if skipped:
            return False
        skipped = True
        long_index += 1
    return True


def _parse_planner_task_set_reply(reply: str) -> dict[str, object]:
    task_set_text = _fenced_section(reply, ('task-set.json', 'task_set.json'))
    if not task_set_text:
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_required',
            'missing_fields': ['task-set.json fenced section'],
            'planner_contract': _PLANNER_CONTRACT_TASK_SET,
        }
    try:
        task_set = json.loads(task_set_text)
    except json.JSONDecodeError as exc:
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_json_invalid',
            'error': str(exc),
            'planner_contract': _PLANNER_CONTRACT_TASK_SET,
        }
    if not isinstance(task_set, dict):
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_json_not_object',
            'planner_contract': _PLANNER_CONTRACT_TASK_SET,
        }
    raw_tasks = task_set.get('tasks')
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_missing_tasks',
            'planner_contract': _PLANNER_CONTRACT_TASK_SET,
        }
    if len(raw_tasks) > 12:
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_too_large',
            'task_count': len(raw_tasks),
            'max_tasks': 12,
            'planner_contract': _PLANNER_CONTRACT_TASK_SET,
        }
    tasks: list[dict[str, object]] = []
    seen_task_ids: set[str] = set()
    for index, raw_task in enumerate(raw_tasks):
        parsed = _parse_planner_task_set_item(raw_task, index=index)
        if parsed.get('status') != 'ok':
            return {
                **parsed,
                'planner_contract': _PLANNER_CONTRACT_TASK_SET,
            }
        task_id = str(parsed['task_id'])
        if task_id in seen_task_ids:
            return {
                'status': 'blocked',
                'reason': 'planner_task_set_duplicate_task_id',
                'task_id': task_id,
                'planner_contract': _PLANNER_CONTRACT_TASK_SET,
            }
        seen_task_ids.add(task_id)
        tasks.append(parsed)
    return {
        'status': 'ok',
        'planner_contract': _PLANNER_CONTRACT_TASK_SET,
        'tasks': tasks,
        'task_count': len(tasks),
    }


def _validate_task_set_expected_task_ids(
    parsed: dict[str, object],
    *,
    activation: dict[str, object] | None,
) -> dict[str, object]:
    expected = _expected_task_ids_from_activation(activation)
    if not expected:
        return {'status': 'ok'}
    tasks = [task for task in tuple(parsed.get('tasks') or ()) if isinstance(task, dict)]
    observed = [str(task.get('task_id') or '') for task in tasks]
    expected_set = set(expected)
    observed_set = set(observed)
    missing = [task_id for task_id in expected if task_id not in observed_set]
    unexpected = [task_id for task_id in observed if task_id and task_id not in expected_set]
    duplicate = sorted({task_id for task_id in observed if observed.count(task_id) > 1})
    if missing or unexpected or duplicate or len(observed) != len(expected):
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_unexpected_task_ids',
            'expected_task_ids': list(expected),
            'observed_task_ids': observed,
            'missing_task_ids': missing,
            'unexpected_task_ids': unexpected,
            'duplicate_task_ids': duplicate,
            'expected_task_count': len(expected),
            'observed_task_count': len(observed),
            'planner_contract': _PLANNER_CONTRACT_TASK_SET,
        }
    return {'status': 'ok', 'expected_task_ids': list(expected)}


def _validate_task_set_contracts_for_activation(
    parsed: dict[str, object],
    *,
    activation: dict[str, object] | None,
) -> dict[str, object]:
    expected = _expected_task_ids_from_activation(activation)
    if expected != _PHASE6B_L1_L4_EXPECTED_TASK_IDS:
        return {'status': 'ok'}
    violations: list[dict[str, object]] = []
    for task in tuple(parsed.get('tasks') or ()):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get('task_id') or '').strip()
        route = str(task.get('route') or '').strip()
        haystacks = [
            ('execution_contract', str(task.get('execution_contract') or '')),
            ('verification', '\n'.join(str(item) for item in tuple(task.get('verification') or ()))),
        ]
        fields = [field for field, text in haystacks if _GIT_SCOPE_CHECK_RE.search(text)]
        if fields:
            violations.append({'task_id': task_id, 'route': route, 'fields': fields})
    if not violations:
        return {'status': 'ok'}
    return {
        'status': 'blocked',
        'reason': 'planner_task_set_git_scope_check_unsupported',
        'unsupported_scope_checks': violations,
        'expected_task_ids': list(expected),
        'message': 'L1-L4 real-provider lab projects are not guaranteed to be git repositories; use repo-independent allowed-path verification.',
        'planner_contract': _PLANNER_CONTRACT_TASK_SET,
    }


def _expected_task_ids_from_activation(activation: dict[str, object] | None) -> tuple[str, ...]:
    if activation is None:
        return ()
    raw = activation.get('expected_task_ids')
    if not isinstance(raw, list):
        return ()
    task_ids: list[str] = []
    for item in raw:
        text = str(item or '').strip()
        if _SEGMENT_RE.fullmatch(text):
            task_ids.append(text)
    return tuple(task_ids)


def _parse_planner_task_set_item(raw_task: object, *, index: int) -> dict[str, object]:
    prefix = f'tasks[{index}]'
    if not isinstance(raw_task, dict):
        return {'status': 'blocked', 'reason': 'planner_task_set_task_not_object', 'task_index': index}
    try:
        task_id = _normalize_segment(raw_task.get('task_id'), label=f'{prefix}.task_id')
    except ValueError as exc:
        return {'status': 'blocked', 'reason': 'planner_task_set_invalid_task_id', 'task_index': index, 'error': str(exc)}
    task_packet = str(raw_task.get('task_packet') or '').strip()
    route = str(raw_task.get('route') or '').strip().lower()
    readiness_value = str(raw_task.get('readiness') or '').strip().lower()
    title = str(raw_task.get('title') or '').strip() or _task_title_from_packet(task_packet)
    allowed_paths = _string_list(raw_task.get('allowed_paths'))
    try:
        verification = _canonicalize_verification_commands(_string_list(raw_task.get('verification')))
    except ValueError as exc:
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_invalid_verification',
            'task_index': index,
            'task_id': task_id,
            'error': str(exc),
        }
    blockers = _string_list(raw_task.get('blockers'))
    required = raw_task.get('required', True)
    if not isinstance(required, bool):
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_invalid_membership',
            'task_index': index,
            'task_id': task_id,
        }
    missing_fields: list[str] = []
    if not task_packet:
        missing_fields.append(f'{prefix}.task_packet')
    if not title:
        missing_fields.append(f'{prefix}.title')
    if route not in _VALID_ROUTES:
        return {
            'status': 'blocked',
            'reason': 'unknown_route',
            'task_index': index,
            'task_id': task_id,
            'route': route or None,
            'expected_routes': sorted(_VALID_ROUTES),
        }
    if readiness_value not in _VALID_READINESS:
        return {
            'status': 'blocked',
            'reason': 'unknown_readiness',
            'task_index': index,
            'task_id': task_id,
            'readiness': readiness_value or 'missing',
            'route': route,
            'expected_readiness': sorted(_VALID_READINESS),
        }
    if route in _EXECUTION_ROUTES and readiness_value != 'ready':
        return {
            'status': 'blocked',
            'reason': 'planner_readiness_not_ready',
            'task_index': index,
            'task_id': task_id,
            'readiness': readiness_value or 'missing',
            'route': route,
        }
    if route == 'needs_detail' and readiness_value not in _NEEDS_DETAIL_READINESS:
        return {
            'status': 'blocked',
            'reason': 'planner_readiness_incompatible_with_route',
            'task_index': index,
            'task_id': task_id,
            'readiness': readiness_value or 'missing',
            'route': route,
            'expected_readiness': sorted(_NEEDS_DETAIL_READINESS),
        }
    if route in _EXECUTION_ROUTES and not allowed_paths:
        missing_fields.append(f'{prefix}.allowed_paths')
    if not verification:
        missing_fields.append(f'{prefix}.verification')
    if route == 'needs_detail' and readiness_value == 'needs_clarification' and not blockers:
        missing_fields.append(f'{prefix}.blockers')
    if route == 'blocked' and not blockers:
        missing_fields.append(f'{prefix}.blockers')
    if missing_fields:
        return {
            'status': 'blocked',
            'reason': 'planner_task_set_missing_required_fields',
            'task_index': index,
            'task_id': task_id,
            'missing_fields': missing_fields,
        }
    invalid_allowed_paths = _invalid_allowed_paths(allowed_paths)
    if invalid_allowed_paths:
        return {
            'status': 'blocked',
            'reason': 'planner_readiness_invalid_allowed_paths',
            'task_index': index,
            'task_id': task_id,
            'invalid_allowed_paths': invalid_allowed_paths,
        }
    execution_contract = _task_set_execution_contract(
        raw_contract=str(raw_task.get('execution_contract') or '').strip(),
        route=route,
        allowed_paths=allowed_paths,
        verification=verification,
    )
    return {
        'status': 'ok',
        'task_id': task_id,
        'title': title,
        'route': route,
        'readiness': readiness_value,
        'task_packet': task_packet + '\n',
        'execution_contract': execution_contract.strip() + '\n',
        'allowed_paths': allowed_paths,
        'verification': verification,
        'blockers': blockers,
        'required': required,
    }


def _has_single_task_planner_sections(reply: str) -> bool:
    return bool(_fenced_section(reply, ('task-packet.md', 'task_packet.md')) or _fenced_section(reply, ('readiness.json',)))


def _parse_task_detailer_reply(
    reply: str,
    *,
    detail_ready_stop_contract: object | None = None,
) -> dict[str, object]:
    detail_terminator_labels = (
        'task-detail-design.md',
        'brief-update-summary.md',
        'detail-packet.manifest.json',
    )
    detail_design = _labeled_section(
        reply,
        ('task-detail-design.md', 'task-detail-design'),
        terminator_names=detail_terminator_labels,
    )
    detail_summary = _labeled_section(
        reply,
        ('brief-update-summary.md', 'brief-update-summary'),
        terminator_names=detail_terminator_labels,
    )
    manifest = _strict_detail_packet_manifest(reply)
    missing = []
    if not detail_design:
        missing.append('task-detail-design.md section')
    if not detail_summary:
        missing.append('brief-update-summary.md section')
    if manifest is None:
        missing.append('detail-packet.manifest.json section')
    if missing:
        return {'status': 'blocked', 'reason': 'task_detailer_reply_missing_required_sections', 'missing_fields': missing}
    if manifest is None:
        return {'status': 'blocked', 'reason': 'task_detailer_reply_invalid_manifest'}
    result = str(manifest['detail_result'])
    readiness = str(manifest['readiness'])
    detail_packet = json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
    if result == 'planner_replan_required':
        return {
            'status': 'ok',
            'result': 'planner_replan_required',
            'readiness': readiness,
            'detail_design': detail_design,
            'detail_summary': detail_summary,
            'detail_packet': detail_packet,
        }
    if result != 'local_detail_ready':
        return {
            'status': 'blocked',
            'reason': 'task_detailer_reply_not_detail_ready',
            'readiness': readiness or 'missing',
            'detail_design': detail_design,
            'detail_summary': detail_summary,
            'detail_packet': detail_packet,
        }
    return {
        'status': 'ok',
        'result': result,
        'detail_design': detail_design,
        'detail_summary': detail_summary,
        'detail_packet': detail_packet,
        'readiness': readiness,
        'detail_readiness_recommendation': readiness,
    }


def _strict_detail_packet_manifest(reply: str) -> dict[str, object] | None:
    if re.search(r'(?mi)^\s*(?:#+\s*)?(?:artifact:\s*)?`?detail-packet(?:\.md)?`?\s*:?\s*$', reply):
        return None
    matches = list(re.finditer(
        r'(?m)^detail-packet\.manifest\.json:\n```json\n(.*?)\n```\s*$',
        reply,
        flags=re.DOTALL,
    ))
    if len(matches) != 1:
        return None
    try:
        manifest = json.loads(matches[0].group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(manifest, dict) or set(manifest) != {
        'schema', 'detail_result', 'readiness', 'global_impact',
    }:
        return None
    if manifest.get('schema') != _DETAIL_PACKET_MANIFEST_SCHEMA:
        return None
    result = manifest.get('detail_result')
    readiness = manifest.get('readiness')
    impact = manifest.get('global_impact')
    if not all(isinstance(value, str) for value in (result, readiness, impact)):
        return None
    expected = _DETAIL_PACKET_OUTCOMES.get(result)
    if expected is None or readiness != expected[0] or impact not in expected[1]:
        return None
    return manifest


def _resolve_completion_reply_artifact(context, reply: str) -> dict[str, object]:
    if 'CCB completion reply' not in reply or 'Full text:' not in reply:
        return {'status': 'ok', 'reply': reply}
    path_match = re.search(r'(?m)^Full text:\s*(.+?)\s*$', reply)
    sha_match = re.search(r'(?m)^SHA256:\s*([0-9a-fA-F]{64})\s*$', reply)
    if not path_match or not sha_match:
        return {'status': 'blocked', 'reason': 'completion_reply_artifact_notice_incomplete'}
    artifact_path = Path(path_match.group(1).strip())
    project_root = Path(context.project.project_root)
    if not _path_within(artifact_path, project_root):
        return {
            'status': 'blocked',
            'reason': 'completion_reply_artifact_outside_project',
            'artifact_path': str(artifact_path),
        }
    try:
        artifact_text = artifact_path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {
            'status': 'blocked',
            'reason': 'completion_reply_artifact_missing',
            'artifact_path': str(artifact_path),
        }
    actual_sha = hashlib.sha256(artifact_text.encode('utf-8')).hexdigest()
    expected_sha = sha_match.group(1).lower()
    if actual_sha != expected_sha:
        return {
            'status': 'blocked',
            'reason': 'completion_reply_artifact_sha256_mismatch',
            'artifact_path': str(artifact_path),
            'expected_sha256': expected_sha,
            'actual_sha256': actual_sha,
        }
    return {
        'status': 'ok',
        'reply': artifact_text,
        'artifact_path': str(artifact_path),
        'sha256': actual_sha,
    }


def _parse_orchestrator_reply(reply: str) -> dict[str, object]:
    match = re.search(r'(?mi)^\s*[-*]?\s*route\s*:\s*([A-Za-z_]+)\b', reply)
    if not match:
        return {'status': 'blocked', 'reason': 'orchestrator_reply_missing_route'}
    route = match.group(1).strip().lower()
    if route not in _VALID_ROUTES:
        return {'status': 'blocked', 'reason': 'unknown_route', 'route': route, 'expected_routes': sorted(_VALID_ROUTES)}
    if not re.search(r'(?mi)^\s*[-*]?\s*orchestration[_ ]notes\s*:', reply):
        return {'status': 'blocked', 'reason': 'orchestrator_reply_missing_orchestration_notes'}
    parsed: dict[str, object] = {'status': 'ok', 'route': route, 'orchestration_notes': reply.strip() + '\n'}
    bundle_text, bundle_error = _strict_orchestration_bundle_candidate(reply)
    if bundle_error:
        return {'status': 'blocked', 'reason': 'orchestrator_reply_bundle_requires_fenced_json'}
    if bundle_text:
        try:
            candidate = json.loads(bundle_text)
        except json.JSONDecodeError as exc:
            return {
                'status': 'blocked',
                'reason': 'orchestrator_reply_bundle_invalid_json',
                'error': str(exc),
            }
        if not isinstance(candidate, dict):
            return {'status': 'blocked', 'reason': 'orchestrator_reply_bundle_not_object'}
        if str(candidate.get('schema') or '') != ORCHESTRATION_BUNDLE_CANDIDATE_SCHEMA:
            return {
                'status': 'blocked',
                'reason': 'orchestrator_reply_bundle_unknown_schema',
                'schema': candidate.get('schema'),
                'expected_schema': ORCHESTRATION_BUNDLE_CANDIDATE_SCHEMA,
            }
        if (
            reply.count(ORCHESTRATION_BUNDLE_CANDIDATE_SCHEMA) != 1
            or not _schema_is_top_level_only(candidate)
        ):
            return {'status': 'blocked', 'reason': 'orchestrator_reply_bundle_schema_not_top_level'}
        parsed['orchestration_bundle_candidate'] = candidate
    return parsed


def _strict_orchestration_bundle_candidate(reply: str) -> tuple[str, bool]:
    label_matches = list(re.finditer(r'(?mi)^\s*(?:#+\s*)?orchestration[_ ]bundle\s*:', reply))
    if not label_matches:
        return '', False
    matches = list(re.finditer(
        r'(?m)^orchestration_bundle:\n```json\n(.*?)\n```\s*$',
        reply,
        flags=re.DOTALL,
    ))
    if len(label_matches) != 1 or len(matches) != 1:
        return '', True
    return matches[0].group(1), False


def _schema_is_top_level_only(candidate: dict[str, object]) -> bool:
    def has_nested_schema(value: object) -> bool:
        if isinstance(value, dict):
            return 'schema' in value or any(has_nested_schema(item) for item in value.values())
        if isinstance(value, list):
            return any(has_nested_schema(item) for item in value)
        return False

    return not any(
        has_nested_schema(value)
        for key, value in candidate.items()
        if key != 'schema'
    )


def _fenced_section(text: str, names: tuple[str, ...]) -> str:
    for name in names:
        pattern = (
            rf'(?is)(?:^|\n)\s*{_label_heading_fragment(name)}\s*:?\s*\n'
            r'```[A-Za-z0-9_-]*\s*\n(.*?)\n```'
        )
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ''


def _labeled_section(text: str, names: tuple[str, ...], *, terminator_names: tuple[str, ...] | None = None) -> str:
    for name in names:
        pattern = rf'(?im)(?:^|\n)\s*{_label_heading_fragment(name)}\s*$'
        match = re.search(pattern, text)
        if match:
            body_start = match.end()
            tail = text[body_start:]
            terminator = _labeled_section_terminator(tail, terminator_names or names)
            body = tail[:terminator].strip()
            fenced = _fenced_block(body)
            return fenced or body
    return ''


def _labeled_section_terminator(text: str, names: tuple[str, ...]) -> int:
    matches = []
    for name in names:
        pattern = rf'(?im)^\s*{_label_heading_fragment(name)}\s*$'
        match = re.search(pattern, text)
        if match:
            matches.append(match.start())
    return min(matches) if matches else len(text)


def _label_heading_fragment(name: str) -> str:
    escaped = re.escape(name).replace(r'\-', '[-_ ]')
    return (
        r'(?:#+\s*)?'
        r'(?:\*\*)?'
        r'\s*(?:Artifact\s*:\s*)?'
        r'`?'
        rf'{escaped}'
        r'`?'
        r'\s*(?:\*\*)?'
    )


def _fenced_block(text: str) -> str:
    match = re.search(r'(?is)```[A-Za-z0-9_-]*\s*\n(.*?)\n```', text)
    if not match:
        return ''
    return match.group(1).strip()


def _task_detailer_readiness(reply: str) -> str:
    patterns = (
        r'(?mi)^\s*(?:detail[_\s]+)?readiness[_\s]+recommendation\s*:\s*`?([A-Za-z_]+)`?\s*$',
        r'(?mi)^\s*(?:detail\s+)?readiness(?:\s+recommendation)?\s*:\s*`?([A-Za-z_]+)`?\s*$',
        r'(?mi)^\s*detail\s+status\s*:\s*`?([A-Za-z_]+)`?\s*$',
        r'(?mi)^\s*readiness\s*:\s*`?([A-Za-z_]+)`?\s*$',
    )
    for pattern in patterns:
        match = re.search(pattern, reply)
        if match:
            return match.group(1).strip().lower()
    match = re.search(r'(?im)^\s*#+\s*readiness\s+recommendation\s*$', reply)
    if match:
        tail = reply[match.end():]
        next_heading = re.search(r'(?m)^\s*#+\s+\S', tail)
        section = tail[: next_heading.start()] if next_heading else tail
        for raw_line in section.splitlines():
            line = raw_line.strip().strip('`')
            if not line:
                continue
            if re.fullmatch(r'[A-Za-z_]+', line):
                return line.lower()
            break
    return ''


def _task_detailer_controller_expected_stop(reply: str) -> str:
    patterns = (
        r'(?mi)^\s*controller[_\s-]+expected[_\s-]+stop\s*:\s*`?([A-Za-z_]+)`?\s*$',
        r'(?mi)^\s*controller[_\s-]+expected[_\s-]+(?:status|outcome)\s*:\s*`?([A-Za-z_]+)`?\s*$',
    )
    for pattern in patterns:
        match = re.search(pattern, reply)
        if match:
            return match.group(1).strip().lower()
    return ''


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _normalized_execution_contract(*, route: str, allowed_paths: tuple[str, ...], verification: tuple[str, ...]) -> str:
    verification = _canonicalize_verification_commands(verification)
    lines = [
        '# Execution Contract',
        '',
        f'Route: {route}',
        '',
        'Allowed Change Paths:',
    ]
    lines.extend(f'- {path}' for path in allowed_paths)
    lines.extend(['', 'Verification:'])
    lines.extend(f'- {item}' for item in verification)
    return '\n'.join(lines)


def _task_set_execution_contract(
    *,
    raw_contract: str,
    route: str,
    allowed_paths: tuple[str, ...],
    verification: tuple[str, ...],
) -> str:
    verification = _canonicalize_verification_commands(verification)
    if not raw_contract:
        return _normalized_execution_contract(route=route, allowed_paths=allowed_paths, verification=verification)
    raw_contract = _canonicalize_verification_text(raw_contract)
    if route not in _EXECUTION_ROUTES or not allowed_paths:
        return raw_contract
    lines = [raw_contract.rstrip()]
    if not _execution_contract_declares_allowed_change_paths(raw_contract):
        lines.extend(['', 'Allowed Change Paths:'])
        lines.extend(f'- {path}' for path in allowed_paths)
    if verification and not _execution_contract_declares_verification_commands(raw_contract):
        lines.extend(['', 'Verification:'])
        lines.extend(f'- {item}' for item in verification)
    return '\n'.join(lines)


def _execution_contract_declares_allowed_change_paths(text: str) -> bool:
    for raw_line in str(text or '').splitlines():
        heading = raw_line.strip().lstrip('#').strip().rstrip(':').lower()
        if heading in {
            'allowed_change_paths',
            'allowed change paths',
            'allowed_change_path',
            'allowed change path',
            'changed_files',
            'changed files',
        }:
            return True
        if heading.startswith(
            (
                'allowed_change_paths:',
                'allowed change paths:',
                'allowed_change_path:',
                'allowed change path:',
                'changed_files:',
                'changed files:',
            )
        ):
            return True
    return False


def _execution_contract_declares_verification_commands(text: str) -> bool:
    for raw_line in str(text or '').splitlines():
        heading = raw_line.strip().lstrip('#').strip().rstrip(':').lower()
        if heading in {'verification', 'verification commands'}:
            return True
    return False


def _canonicalize_verification_commands(commands: tuple[str, ...]) -> tuple[str, ...]:
    canonical: list[str] = []
    for command in commands:
        normalized = _canonicalize_unittest_file_command(command)
        _validate_direct_verification_command(normalized)
        canonical.append(normalized)
    return tuple(canonical)


def _validate_direct_verification_command(command: str) -> None:
    text = str(command or '').strip()
    if not text:
        raise ValueError('verification command must not be empty')
    if '\n' in text or '\r' in text:
        raise ValueError('verification command must be a single line')
    if '$(' in text or '`' in text:
        raise ValueError('verification command must not use shell command substitution')
    try:
        argv = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f'verification command is not valid argv syntax: {exc}') from exc
    if not argv:
        raise ValueError('verification command must produce argv')
    if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', argv[0]):
        raise ValueError('verification command must not rely on shell environment assignment')
    if argv[0] in {'cd', 'source', '.', 'export', 'alias'}:
        raise ValueError('verification command must be directly executable, not a shell state mutation')
    shell_tokens = {'&&', '||', '|', ';', '>', '<', '>>', '<<'}
    if any(token in shell_tokens for token in argv):
        raise ValueError('verification command must be one direct argv command, not a shell compound')


def _canonicalize_verification_text(text: str) -> str:
    lines: list[str] = []
    for raw_line in str(text or '').splitlines():
        bullet = re.match(r'^(\s*[-*]\s+)(.+?)\s*$', raw_line)
        if bullet:
            command = bullet.group(2).strip()
            canonical = _canonicalize_unittest_file_command(command)
            lines.append(f'{bullet.group(1)}{canonical}' if canonical != command else raw_line)
            continue
        label = re.match(r'^(\s*(?:verification|verify)\s*:\s*)(.+?)\s*$', raw_line, flags=re.IGNORECASE)
        if label:
            command = label.group(2).strip()
            canonical = _canonicalize_unittest_file_command(command)
            lines.append(f'{label.group(1)}{canonical}' if canonical != command else raw_line)
            continue
        stripped = raw_line.strip()
        canonical = _canonicalize_unittest_file_command(stripped)
        if canonical != stripped:
            indent = raw_line[: len(raw_line) - len(raw_line.lstrip())]
            lines.append(f'{indent}{canonical}')
        else:
            lines.append(raw_line)
    return '\n'.join(lines)


def _canonicalize_unittest_file_command(command: str) -> str:
    text = str(command or '').strip()
    if not text:
        return text
    try:
        parts = shlex.split(text)
    except ValueError:
        return text
    if len(parts) < 4:
        return text
    env_prefix: list[str] = []
    index = 0
    while index < len(parts) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', parts[index]):
        env_prefix.append(parts[index])
        index += 1
    remaining = parts[index:]
    if len(remaining) != 4:
        return text
    python_cmd, dash_m, module, test_path = remaining
    if dash_m != '-m' or module != 'unittest':
        return text
    if not re.fullmatch(r'python(?:3(?:\.\d+)?)?', python_cmd):
        return text
    normalized_path = test_path[2:] if test_path.startswith('./') else test_path
    if not normalized_path.startswith('tests/') or not normalized_path.endswith('.py'):
        return text
    posix_path = PurePosixPath(normalized_path)
    if posix_path.is_absolute() or '..' in posix_path.parts or len(posix_path.parts) < 2:
        return text
    search_dir = '/'.join(posix_path.parts[:-1])
    pattern = posix_path.name
    canonical_parts = [
        *env_prefix,
        python_cmd,
        '-m',
        'unittest',
        'discover',
        '-s',
        search_dir,
        '-p',
        pattern,
    ]
    return ' '.join(shlex.quote(part) for part in canonical_parts)


def _task_title_from_packet(task_packet: str) -> str:
    for raw_line in task_packet.splitlines():
        line = raw_line.strip()
        if not line.startswith('#'):
            continue
        text = line.lstrip('#').strip()
        if text.lower().startswith('task:'):
            text = text.split(':', 1)[1].strip()
        if text:
            return text
    return 'Planner task'


def _ensure_task(
    context,
    deps,
    *,
    plan_slug: str,
    title: str,
    task_id: str | None,
    snapshot: dict[str, object],
    reply: str,
) -> dict[str, object]:
    if task_id:
        try:
            payload = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=task_id))
            payload['created'] = False
            return payload
        except ValueError:
            pass
    payload = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-create',
            plan_slug=plan_slug,
            title=title,
            task_id=task_id,
            authority_trace={
                'source': 'loop_runner_role_output_import',
                'source_job': _job_trace(snapshot, reply),
            },
        ),
    )
    payload['created'] = True
    return payload


def _task_set_import_task_id(context, deps, *, requested_task_id: str, job_id: str) -> str:
    existing = _show_task_optional(context, deps, task_id=requested_task_id)
    if existing is None or _task_payload_source_job_id(existing) == job_id:
        return requested_task_id
    suffix = _task_set_job_suffix(job_id)
    candidate = _suffixed_task_id(requested_task_id, suffix)
    existing_candidate = _show_task_optional(context, deps, task_id=candidate)
    if existing_candidate is None or _task_payload_source_job_id(existing_candidate) == job_id:
        return candidate
    for index in range(2, 100):
        alternate = _suffixed_task_id(requested_task_id, f'{suffix}{index}')
        existing_alternate = _show_task_optional(context, deps, task_id=alternate)
        if existing_alternate is None or _task_payload_source_job_id(existing_alternate) == job_id:
            return alternate
    raise ValueError(f'unable to allocate unique task_id for planner task-set child: {requested_task_id}')


def _show_task_optional(context, deps, *, task_id: str) -> dict[str, object] | None:
    try:
        return deps.plan_task(context, SimpleNamespace(action='task-show', task_id=task_id))
    except ValueError:
        return None


def _task_payload_source_job_id(payload: dict[str, object] | None) -> str:
    if not isinstance(payload, dict):
        return ''
    record = payload.get('task') if isinstance(payload.get('task'), dict) else payload
    if not isinstance(record, dict):
        return ''
    trace = record.get('authority_trace')
    if not isinstance(trace, dict):
        return ''
    source_job = trace.get('source_job')
    if not isinstance(source_job, dict):
        return ''
    return str(source_job.get('job_id') or '').strip()


def _task_set_job_suffix(job_id: str) -> str:
    seed = str(job_id or '').strip()
    if seed.startswith('job_'):
        seed = seed[4:]
    token = re.sub(r'[^A-Za-z0-9]+', '', seed)
    if not token:
        token = hashlib.sha256(str(job_id).encode('utf-8')).hexdigest()[:13]
    return 'j' + token[:13]


def _suffixed_task_id(task_id: str, suffix: str) -> str:
    suffix = re.sub(r'[^A-Za-z0-9_-]+', '', str(suffix or '').strip()) or 'jtask'
    budget = max(1, 80 - len(suffix) - 1)
    prefix = str(task_id or '').strip()[:budget].rstrip('-_') or 'task'
    return f'{prefix}-{suffix}'


def _resolve_or_bootstrap_plan(
    context,
    command,
    *,
    activation: dict[str, object] | None = None,
) -> tuple[str | None, dict[str, object]]:
    raw = str(getattr(command, 'plan_slug', None) or '').strip()
    if not raw and activation is not None:
        raw = str(activation.get('plan_slug') or '').strip()
    if not raw:
        existing = _existing_plan_slugs(context)
        if len(existing) == 1:
            raw = existing[0]
    if not raw:
        return None, _blocked_payload(
            context,
            job_id=str(getattr(command, 'role_job_id', None) or ''),
            agent_name=None,
            reason='role_output_import_requires_plan_slug',
            evidence={'hint': 'pass --plan <plan_slug> or create exactly one plan root first'},
        )
    try:
        plan_slug = _normalize_segment(raw, label='plan')
    except ValueError as exc:
        return None, _blocked_payload(
            context,
            job_id=str(getattr(command, 'role_job_id', None) or ''),
            agent_name=None,
            reason='invalid_plan_slug',
            evidence={'error': str(exc)},
        )
    plan_root = Path(context.project.project_root) / 'docs' / 'plantree' / 'plans' / plan_slug
    created = False
    if not plan_root.is_dir():
        _bootstrap_plan_root(context, plan_slug=plan_slug)
        created = True
    return plan_slug, {
        'status': 'ok',
        'plan_slug': plan_slug,
        'created': created,
        'plan_root': str(plan_root.relative_to(context.project.project_root)),
    }


def _bootstrap_plan_root(context, *, plan_slug: str) -> None:
    root = Path(context.project.project_root)
    plantree = root / 'docs' / 'plantree'
    plan_root = plantree / 'plans' / plan_slug
    if not (plantree / 'README.md').exists():
        atomic_write_text(plantree / 'README.md', '# Plan Tree\n\nScript-owned CCB plan tree.\n')
    if not (plan_root / 'README.md').exists():
        atomic_write_text(plan_root / 'README.md', f'# {plan_slug}\n\nScript-owned plan root.\n')
    if not (plan_root / 'brief.md').exists():
        atomic_write_text(plan_root / 'brief.md', f'# {plan_slug} Brief\n\nCreated by loop runner role-output import.\n')
    (plan_root / 'tasks').mkdir(parents=True, exist_ok=True)


def planner_contract_for_frontdesk_text(text: str) -> str:
    return _PLANNER_CONTRACT_TASK_SET if _frontdesk_text_requests_task_set(text) else _PLANNER_CONTRACT_SINGLE_TASK


def planner_expected_task_ids_for_frontdesk_text(text: str) -> tuple[str, ...]:
    if _frontdesk_text_requests_phase6b_l1_l4_route_mix(text):
        return _PHASE6B_L1_L4_EXPECTED_TASK_IDS
    return ()


def planner_required_output_for_contract(
    planner_contract: str,
    *,
    expected_task_ids: tuple[str, ...] = (),
) -> str:
    if planner_contract == _PLANNER_CONTRACT_TASK_SET:
        if expected_task_ids:
            return 'reply-only task-set.json with exact bounded task IDs for supervisor-owned import'
        return 'reply-only task-set.json with bounded planner tasks for supervisor-owned import'
    return 'reply-only task-packet.md plus readiness.json for supervisor-owned import'


def planner_script_write_rules_for_contract(
    planner_contract: str,
    *,
    expected_task_ids: tuple[str, ...] = (),
) -> list[str]:
    base_rules = [
        'Reply only; do not run ccb, ccb_test, ccb plan, ccb loop, ccb ask, artifact import, or wrapper commands.',
        'Supervisor/runner scripts own plan/task authority creation, artifact imports, and status transitions.',
    ]
    if planner_contract == _PLANNER_CONTRACT_TASK_SET:
        rules = [
            base_rules[0],
            'Return exactly one fenced **task-set.json** section with one task object per requested bounded task.',
            'For direct_execution and partial_completion, include allowed_paths and make execution_contract declare Allowed Change Paths.',
            'Do not collapse multi-task or route-mix validation into a controller-owned meta task.',
        ]
        if expected_task_ids:
            rules.append('Use exactly these task_id values and no others: ' + ', '.join(expected_task_ids) + '.')
            rules.append('Do not require git diff, git status, or any git-only scope check; lab projects may not be git repositories.')
        rules.append(base_rules[1])
        return rules
    return [
        base_rules[0],
        'Return explicit fenced **task-packet.md** and **readiness.json** sections.',
        base_rules[1],
    ]


def _planner_contract_from_activation(
    activation: dict[str, object] | None,
    *,
    reply: str = '',
) -> str:
    raw_contract = ''
    if activation is not None:
        raw_contract = str(activation.get('planner_contract') or '').strip()
    if raw_contract in _PLANNER_CONTRACTS:
        return raw_contract
    if activation is None:
        return _PLANNER_CONTRACT_SINGLE_TASK
    if activation is not None:
        record_type = str(activation.get('record_type') or '').strip()
        action = str(activation.get('action') or '').strip()
        if record_type != 'ccb_loop_frontdesk_planner_activation' and action != 'activate_planner_from_frontdesk':
            return _PLANNER_CONTRACT_SINGLE_TASK
    intake_preview = ''
    if activation is not None and isinstance(activation.get('source_intake'), dict):
        intake_preview = str(activation['source_intake'].get('preview') or '')
    return planner_contract_for_frontdesk_text('\n'.join(part for part in (intake_preview, reply) if part))


def _frontdesk_text_requests_task_set(text: str) -> bool:
    lowered = str(text or '').lower()
    if any(marker in lowered for marker in _TASK_SET_INTENT_MARKERS):
        return True
    route_mentions = sum(1 for route in _VALID_ROUTES if route in lowered)
    if route_mentions >= 2 and ('task' in lowered or 'validation' in lowered):
        return True
    return False


def _frontdesk_text_requests_phase6b_l1_l4_route_mix(text: str) -> bool:
    lowered = str(text or '').lower()
    level_markers = ('l1', 'l2', 'l3', 'l4')
    route_marker_groups = (
        ('direct_execution', 'direct execution'),
        ('needs_detail', 'needs-detail', 'needs detail'),
        ('macro_adjustment_request', 'macro-adjustment', 'macro adjustment'),
        ('blocked', 'blocked-prerequisite', 'blocked prerequisite'),
    )
    return (
        ('route-mix' in lowered or 'route mix' in lowered)
        and all(marker in lowered for marker in level_markers)
        and all(any(marker in lowered for marker in group) for group in route_marker_groups)
    )


def _planner_from_frontdesk_message(
    activation: dict[str, object],
    frontdesk_reply: str,
    *,
    original_request: str = '',
) -> str:
    planner_contract = _planner_contract_from_activation(activation, reply=frontdesk_reply)
    if planner_contract == _PLANNER_CONTRACT_TASK_SET:
        return _planner_task_set_from_frontdesk_message(
            activation,
            frontdesk_reply,
            original_request=original_request,
        )
    return _planner_single_task_from_frontdesk_message(
        activation,
        frontdesk_reply,
        original_request=original_request,
    )


def _planner_single_task_from_frontdesk_message(
    activation: dict[str, object],
    frontdesk_reply: str,
    *,
    original_request: str = '',
) -> str:
    return (
        'Role: planner\n'
        f"Activation id: {activation.get('activation_id')}\n"
        f"Plan: {activation.get('plan_slug')}\n"
        f"Source frontdesk job: {(activation.get('source_job') or {}).get('job_id')}\n\n"
        f'Planner contract: {_PLANNER_CONTRACT_SINGLE_TASK}\n\n'
        f'{_original_request_evidence(original_request)}'
        'Frontdesk intake evidence:\n'
        f'{frontdesk_reply.strip()}\n\n'
        'Required reply-only output. Use these exact labels and fenced blocks; do not use alternate headings, '
        'unfenced JSON, tables, or prose-only summaries:\n'
        '**task-packet.md**\n'
        '```markdown\n'
        '# Task: <title>\n'
        'Route: <direct_execution|needs_detail|macro_adjustment_request|blocked|partial_completion>\n'
        '## Goal\n'
        '<preserve the complete product outcome from intake>\n'
        '## Acceptance Criteria\n'
        '- <observable behavior; preserve every intake requirement>\n'
        '## Interface Contracts\n'
        '- <concrete module/import path, callable/signature, CLI, data/error shape, or "None declared">\n'
        '## Constraints And Non-Goals\n'
        '- <constraint or explicit non-goal>\n'
        '## Execution Decomposition Inputs\n'
        '- Independently reviewable surfaces: <surfaces or none>\n'
        '- Stable interfaces available: <interfaces or none>\n'
        '- Unresolved ordering constraints requiring predecessor output: <constraints or none>\n'
        'Allowed paths:\n'
        '- <relative path>\n'
        'Verification:\n'
        '- <command>\n'
        '```\n\n'
        '**readiness.json**\n'
        '```json\n'
        '{"readiness":"ready","route":"direct_execution","blockers":[],"allowed_paths":["path"],"verification":["command"]}\n'
        '```\n\n'
        'For route needs_detail, use readiness "needs_clarification", include non-empty blockers and verification, '
        'and use "allowed_paths":[] because implementation is not authorized yet.\n\n'
        'For route blocked, use readiness "blocked", include non-empty blockers and blocker verification, '
        'and use "allowed_paths":[] because implementation is not authorized.\n\n'
        'A behavioral requirement alone is not a stable cross-node interface. Treat an interface as available only '
        'when the intake already supplies the concrete import/module path and callable/signature, CLI, or data/error '
        'shape needed by every consumer. If a downstream test, documentation example, or module would have to guess '
        'a new symbol or output contract, record a predecessor-output ordering constraint instead of claiming parallelism.\n\n'
        'Authority boundary:\n'
        '- Reply only with semantic planning artifacts.\n'
        '- Do not run ccb, ccb_test, ccb plan, ccb loop, ccb ask, artifact import, status, runtime, cleanup, or wrapper commands.\n'
        '- Supervisor/runner scripts own plan/task authority creation, artifact imports, and status transitions.'
    )


def _planner_task_set_from_frontdesk_message(
    activation: dict[str, object],
    frontdesk_reply: str,
    *,
    original_request: str = '',
) -> str:
    expected_task_ids = _expected_task_ids_from_activation(activation)
    exact_id_rules = ''
    if expected_task_ids:
        exact_id_rules = (
            'Exact task_id contract for this intake:\n'
            '- Use exactly these task_id values, once each, and no other task_id values:\n'
            + ''.join(f'  - {task_id}\n' for task_id in expected_task_ids)
            + '- Do not append route/status suffixes such as "-detail-ready" or "-replan-required".\n\n'
        )
    return (
        'Role: planner\n'
        f"Activation id: {activation.get('activation_id')}\n"
        f"Plan: {activation.get('plan_slug')}\n"
        f"Source frontdesk job: {(activation.get('source_job') or {}).get('job_id')}\n\n"
        f'Planner contract: {_PLANNER_CONTRACT_TASK_SET}\n\n'
        f'{_original_request_evidence(original_request)}'
        'Frontdesk intake evidence:\n'
        f'{frontdesk_reply.strip()}\n\n'
        'Required reply-only output for this multi-task/route-mix intake. Use exactly one fenced '
        '**task-set.json** block. Do not collapse this into a controller-owned validation task, report task, '
        'B7 task, cleanup task, or other meta task:\n'
        f'{exact_id_rules}'
        '**task-set.json**\n'
        '```json\n'
        '{\n'
        '  "tasks": [\n'
        '    {\n'
        '      "task_id": "bounded-feature-slice-1",\n'
        '      "title": "Bounded feature slice",\n'
        '      "route": "direct_execution",\n'
        '      "readiness": "ready",\n'
        '      "task_packet": "# Task: Bounded feature slice\\nRoute: direct_execution\\n",\n'
        '      "execution_contract": "# Execution Contract\\nRoute: direct_execution\\n\\nAllowed Change Paths:\\n- relative/path\\n",\n'
        '      "allowed_paths": ["relative/path"],\n'
        '      "verification": ["command"],\n'
        '      "blockers": []\n'
        '    }\n'
        '  ]\n'
        '}\n'
        '```\n\n'
        'Task-set rules:\n'
        '- Include one task object for each bounded task requested by frontdesk.\n'
        '- Each task_id must be stable, unique, and match [A-Za-z0-9][A-Za-z0-9_-]{0,79}.\n'
        '- Routes must be direct_execution, needs_detail, macro_adjustment_request, blocked, or partial_completion.\n'
        '- direct_execution and partial_completion tasks must be readiness "ready" with non-empty allowed_paths and verification.\n'
        '- For direct_execution and partial_completion, execution_contract must declare Allowed Change Paths matching allowed_paths and a Verification section.\n'
        '- Each verification entry must be one direct argv command executed without a shell; do not use &&, ||, pipes, redirection, command substitution, variable assignment, cd, source, or export.\n'
        '- Split multi-step smoke verification into multiple verification entries. Use fixed project-relative scratch paths under .ccb/runtime/verification/ when state must persist across commands.\n'
        '- Do not require git diff, git status, or any git-only scope check; real-provider lab projects may not be git repositories.\n'
        '- Scope verification must be repo-independent: use allowed_paths plus file existence/content checks or explicit manifest checks.\n'
        '- needs_detail tasks may use readiness "needs_clarification" with blockers, allowed_paths [], and verification.\n'
        '- blocked tasks must use readiness "blocked", blockers, allowed_paths [], and verification.\n\n'
        'Authority boundary:\n'
        '- Reply only with semantic planning artifacts.\n'
        '- Do not run ccb, ccb_test, ccb plan, ccb loop, ccb ask, artifact import, status, runtime, cleanup, or wrapper commands.\n'
        '- Supervisor/runner scripts own plan/task authority creation, artifact imports, and status transitions.'
    )


def frontdesk_intake_missing_fields(reply: str) -> list[str]:
    return _frontdesk_intake_missing_fields(reply)


def planner_from_frontdesk_intake_message(
    activation: dict[str, object],
    frontdesk_reply: str,
    *,
    original_request: str = '',
) -> str:
    return _planner_from_frontdesk_message(
        activation,
        frontdesk_reply,
        original_request=original_request,
    )


def _original_request_evidence(original_request: str) -> str:
    text = str(original_request or '').strip()
    if not text:
        return ''
    return (
        'Original user request (controller-loaded source-job evidence):\n'
        '<original-user-request>\n'
        f'{text}\n'
        '</original-user-request>\n'
        'Preserve every concrete requirement, signature, field, path, CLI contract, error behavior, and constraint '
        'from this source. Use frontdesk intake for macro routing context; never let its compression erase source details.\n\n'
    )


def _load_job_snapshot(context, job_id: str) -> dict[str, object] | None:
    path = Path(context.project.project_root) / '.ccb' / 'ccbd' / 'snapshots' / f'{job_id}.json'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f'completion snapshot is invalid JSON: {path}') from exc
    if not isinstance(payload, dict):
        raise ValueError(f'completion snapshot is invalid: {path}')
    return payload


def _retry_successor_for_job(context, job_id: str, *, agent_name: str | None) -> dict[str, object] | None:
    source_job_id = job_id
    current_job_id = job_id
    lineage: list[dict[str, object]] = []
    for _depth in range(8):
        record = _latest_retry_successor_record(context, current_job_id, agent_name=agent_name)
        if record is None:
            return None
        successor_job_id = str(record.get('job_id') or '').strip()
        if not successor_job_id or successor_job_id == current_job_id:
            return None
        successor_agent = str(record.get('agent_name') or agent_name or '').strip()
        lineage.append(
            {
                'retry_source_job_id': current_job_id,
                'job_id': successor_job_id,
                'agent_name': successor_agent,
                'status': record.get('status'),
            }
        )
        snapshot = _load_job_snapshot(context, successor_job_id)
        if snapshot is None:
            return {
                'status': 'pending',
                'job_id': successor_job_id,
                'agent_name': successor_agent,
                'reason': 'retry_successor_missing_completion_snapshot',
                'retry_source_job_id': source_job_id,
                'retry_lineage': lineage,
            }
        decision = snapshot.get('latest_decision') if isinstance(snapshot.get('latest_decision'), dict) else {}
        terminal = bool(decision.get('terminal') or (snapshot.get('state') or {}).get('terminal'))
        if not terminal:
            return {
                'status': 'pending',
                'job_id': successor_job_id,
                'agent_name': successor_agent,
                'reason': 'retry_successor_not_terminal',
                'retry_source_job_id': source_job_id,
                'retry_lineage': lineage,
            }
        status = str(decision.get('status') or '').strip().lower()
        if status == 'completed':
            resolved = dict(snapshot)
            resolved['retry_source_job_id'] = source_job_id
            resolved['retry_successor_job_id'] = successor_job_id
            resolved['retry_lineage'] = lineage
            return {
                'status': 'completed',
                'job_id': successor_job_id,
                'agent_name': successor_agent,
                'snapshot': resolved,
                'retry_source_job_id': source_job_id,
                'retry_successor_job_id': successor_job_id,
                'retry_lineage': lineage,
            }
        current_job_id = successor_job_id
    return None


def _latest_retry_successor_record(context, source_job_id: str, *, agent_name: str | None) -> dict[str, object] | None:
    candidates: dict[str, dict[str, object]] = {}
    for record in _iter_agent_job_records(context, agent_name=agent_name):
        provider_options = record.get('provider_options') if isinstance(record.get('provider_options'), dict) else {}
        if str(provider_options.get('retry_source_job_id') or '').strip() != source_job_id:
            continue
        retry_job_id = str(record.get('job_id') or '').strip()
        if not retry_job_id:
            continue
        candidates[retry_job_id] = record
    if not candidates:
        return None
    return sorted(
        candidates.values(),
        key=lambda item: str(item.get('updated_at') or item.get('created_at') or item.get('job_id') or ''),
    )[-1]


def _iter_agent_job_records(context, *, agent_name: str | None):
    agents_root = Path(context.project.project_root) / '.ccb' / 'agents'
    if agent_name:
        paths = (agents_root / _base_agent_name(agent_name) / 'jobs.jsonl',)
    else:
        paths = tuple(agents_root.glob('*/jobs.jsonl'))
    for path in paths:
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def _iter_activation_records(context):
    activations_dir = Path(context.project.project_root) / '.ccb' / 'runtime' / 'loops' / 'activations'
    if not activations_dir.is_dir():
        return
    for path in sorted(activations_dir.glob('act-*.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield path, payload


def _activation_for_job(context, job_id: str) -> dict[str, object] | None:
    matches = []
    for _path, activation in _iter_activation_records(context):
        ask = activation.get('ask') if isinstance(activation.get('ask'), dict) else {}
        if str(ask.get('job_id') or '').strip() == job_id:
            matches.append(activation)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return {'_activation_error': 'duplicate managed activations for Planner job'}
    return None


def _detailer_replan_wrapper(context, job_id: str) -> dict[str, object] | None:
    records = [record for record in _iter_agent_job_records(context, agent_name='planner') if str(record.get('job_id') or '') == job_id]
    if len(records) != 1:
        return None
    request = records[0].get('request') if isinstance(records[0].get('request'), dict) else {}
    body = request.get('body')
    if not isinstance(body, str):
        return None
    try:
        wrapper = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(wrapper, dict) or wrapper.get('schema') != 'ccb.detailer.planner_activation.v1':
        return None
    required = {'schema', 'mode', 'authority', 'source_request', 'source_request_body', 'source_request_body_sha256'}
    if set(wrapper) != required or wrapper.get('mode') != 'detailer_replan':
        return {'_invalid': 'Detailer replan Planner wrapper fields invalid'}
    if not isinstance(wrapper['source_request_body'], str) or wrapper['source_request_body_sha256'] != hashlib.sha256(wrapper['source_request_body'].encode('utf-8')).hexdigest():
        return {'_invalid': 'Detailer replan Planner wrapper raw request invalid'}
    try:
        if json.loads(wrapper['source_request_body']) != wrapper['source_request']:
            return {'_invalid': 'Detailer replan Planner wrapper source request mismatch'}
    except json.JSONDecodeError:
        return {'_invalid': 'Detailer replan Planner wrapper source request invalid'}
    return wrapper


def _blocked_for_detailer_replan_claim(context, *, deps, job_id: str, agent_name: str | None, reason: str, evidence: dict[str, object]) -> dict[str, object]:
    """Preserve ordinary import auditing unless durable controller state claims replan."""
    resolved = _resolve_detailer_replan_authority(context, deps, job_id=job_id)
    if resolved['claimed']:
        return _detailer_replan_blocked_payload(context, job_id=job_id, agent_name=agent_name, reason=reason, evidence=evidence)
    return _blocked_payload(context, job_id=job_id, agent_name=agent_name, reason=reason, evidence=evidence)


def _resolve_detailer_replan_authority(context, deps, *, job_id: str) -> dict[str, object]:
    """Resolve the controller-owned Detailer→Planner authority fail-closed.

    A provider reply is deliberately not an input.  Once any durable record
    identifies this Planner job as a Detailer replan, all four controller
    records must be present exactly once and bind byte-for-byte.
    """
    planner_records = [r for r in _iter_agent_job_records(context, agent_name='planner') if str(r.get('job_id') or '') == job_id]
    activations = [a for _p, a in _iter_activation_records(context) if str((a.get('ask') or {}).get('job_id') or '') == job_id]
    intents = _detailer_replan_intents_for_job(context, job_id)
    feedbacks = _detailer_replan_feedbacks_for_job(context, deps, job_id)
    claimed = bool(intents or feedbacks or any(_activation_claims_detailer_replan(a) for a in activations))
    # A valid wrapper is itself a claim.  A damaged wrapper remains a claim
    # whenever another durable record binds this job, which prevents fallback.
    for record in planner_records:
        request = record.get('request') if isinstance(record.get('request'), dict) else {}
        # Script-owned envelope metadata survives a body deletion/corruption.
        claimed = claimed or (
            request.get('from_actor') == 'task_detailer'
            and bool(re.fullmatch(r'detailer-replan-[0-9a-f]{32}', str(request.get('task_id') or '')))
            and request.get('to_agent') == 'planner'
        )
        body = request.get('body')
        if isinstance(body, str):
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = None
            claimed = claimed or isinstance(parsed, dict) and (
                parsed.get('schema') == 'ccb.detailer.planner_activation.v1' or parsed.get('mode') == 'detailer_replan'
            )
    if not claimed:
        return {'claimed': False, 'error': None, 'activation': None}
    if len(planner_records) != 1:
        return _detailer_replan_resolution_error('planner_job_record_count')
    if len(activations) != 1:
        return _detailer_replan_resolution_error('activation_record_count')
    if len(intents) != 1:
        return _detailer_replan_resolution_error('intent_record_count')
    if len(feedbacks) != 1:
        return _detailer_replan_resolution_error('task_feedback_record_count')
    activation, intent, feedback = activations[0], intents[0], feedbacks[0]
    wrapper = _detailer_replan_wrapper(context, job_id)
    if not isinstance(wrapper, dict) or '_invalid' in wrapper:
        return _detailer_replan_resolution_error('planner_wrapper_invalid')
    activation_error = _detailer_replan_activation_error(activation, job_id=job_id)
    if activation_error:
        return _detailer_replan_resolution_error('activation_invalid')
    error = _detailer_replan_cross_binding_error(
        context=context,
        planner_record=planner_records[0], wrapper=wrapper, activation=activation,
        intent=intent, feedback=feedback, job_id=job_id,
    )
    if error:
        return _detailer_replan_resolution_error(error)
    return {'claimed': True, 'error': None, 'activation': activation}


def _detailer_replan_resolution_error(code: str) -> dict[str, object]:
    return {'claimed': True, 'error': f'detailer_replan_{code}', 'activation': None}


def _activation_claims_detailer_replan(activation: dict[str, object]) -> bool:
    return (
        activation.get('record_type') == 'ccb_loop_detailer_planner_replan_activation'
        or activation.get('planner_contract') == _PLANNER_CONTRACT_DETAILER_REPLAN
    )


def _detailer_replan_intents_for_job(context, job_id: str) -> list[dict[str, object]]:
    root = Path(context.project.project_root) / '.ccb' / 'runtime' / 'detailer-replan'
    matches = []
    for path in sorted(root.glob('*.json')) if root.is_dir() else ():
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and str(value.get('planner_job_id') or '') == job_id:
            matches.append(value)
    return matches


def _detailer_replan_feedbacks_for_job(context, deps, job_id: str) -> list[dict[str, object]]:
    # plan-task is the sole controller API for the current accepted task.
    root = Path(context.project.project_root) / 'docs' / 'plantree' / 'plans'
    matches = []
    for index in root.glob('*/tasks/index.json') if root.is_dir() else ():
        try:
            tasks = json.loads(index.read_text(encoding='utf-8')).get('tasks', [])
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            feedback = task.get('replan_feedback')
            if isinstance(feedback, dict) and str(feedback.get('planner_job_id') or '') == job_id:
                shown = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=str(task.get('task_id') or '')))
                current = shown.get('task') if isinstance(shown.get('task'), dict) else {}
                if current.get('replan_feedback') == feedback:
                    matches.append(current)
                else:
                    matches.append({'_stale_task_record': True})
    return matches


def _detailer_replan_cross_binding_error(*, context, planner_record, wrapper, activation, intent, feedback, job_id: str) -> str | None:
    intent_error = _detailer_replan_intent_error(intent)
    if intent_error:
        return intent_error
    task_error = _detailer_replan_task_feedback_error(feedback, job_id=job_id)
    if task_error:
        return task_error
    request = planner_record.get('request') if isinstance(planner_record.get('request'), dict) else {}
    source = activation.get('source_replan_request')
    authority = activation.get('planner_authority')
    intent_request = intent.get('request') if isinstance(intent.get('request'), dict) else {}
    task_feedback = feedback.get('replan_feedback') if isinstance(feedback.get('replan_feedback'), dict) else {}
    if not isinstance(source, dict) or not isinstance(authority, dict) or not isinstance(task_feedback, dict):
        return 'nested_record_invalid'
    raw = source.get('source_request_body')
    if not isinstance(raw, str) or hashlib.sha256(raw.encode('utf-8')).hexdigest() != source.get('source_request_body_sha256'):
        return 'raw_request_digest_mismatch'
    try:
        parsed_raw = json.loads(raw)
    except json.JSONDecodeError:
        return 'raw_request_json_invalid'
    raw_error = _detailer_replan_raw_request_error(parsed_raw)
    if raw_error:
        return raw_error
    if not isinstance(parsed_raw, dict) or parsed_raw != wrapper.get('source_request') or raw != wrapper.get('source_request_body'):
        return 'raw_request_mismatch'
    if source.get('source_request_body_sha256') != wrapper.get('source_request_body_sha256') or intent_request.get('body') != raw:
        return 'raw_request_binding_mismatch'
    if intent.get('request_body_sha256') != hashlib.sha256(raw.encode('utf-8')).hexdigest():
        return 'intent_raw_request_mismatch'
    expected_envelope_task_id = f"detailer-replan-{str(source.get('request_identity') or '').removeprefix('sha256:')[:32]}"
    if request.get('to_agent') != 'planner' or request.get('from_actor') != 'task_detailer' or request.get('task_id') != expected_envelope_task_id or request.get('message_type') != 'ask' or request.get('delivery_scope') != 'single' or request.get('silence_on_success') is not True:
        return 'planner_envelope_mismatch'
    if wrapper.get('authority') != authority:
        return 'planner_authority_mismatch'
    pairs = {
        'request_identity': (intent.get('request_identity'), source.get('request_identity'), authority.get('request_identity'), task_feedback.get('request_identity')),
        'detail_digest': (intent.get('detail_digest'), source.get('detail_digest'), authority.get('detail_digest'), task_feedback.get('detail_digest')),
        'macro_impact_digest': (intent.get('macro_impact_digest'), source.get('macro_impact_digest'), authority.get('macro_impact_digest'), task_feedback.get('macro_impact_digest')),
        'source_detailer_job_id': (intent.get('source_detailer_job_id'), source.get('source_detailer_job_id'), task_feedback.get('source_detailer_job_id')),
    }
    if any(len(set(values)) != 1 for values in pairs.values()):
        return 'source_binding_mismatch'
    if intent.get('task_id') != activation.get('task_id') or authority.get('task_id') != activation.get('task_id') or feedback.get('task_id') != activation.get('task_id'):
        return 'task_id_mismatch'
    if intent.get('source_task_revision') != activation.get('source_task_revision') or task_feedback.get('source_task_revision') != activation.get('source_task_revision'):
        return 'source_revision_mismatch'
    if intent.get('accepted_task_revision') != activation.get('task_revision') or task_feedback.get('accepted_task_revision') != activation.get('task_revision') or authority.get('task_revision') != activation.get('task_revision'):
        return 'accepted_revision_mismatch'
    if task_feedback.get('planner_job_id') != job_id or intent.get('planner_job_id') != job_id:
        return 'planner_job_binding_mismatch'
    settled = feedback.get('planner_replan_backfill') if isinstance(feedback.get('planner_replan_backfill'), dict) else {}
    active_state = feedback.get('status') == 'replan_required' and feedback.get('owner') == 'planner' and feedback.get('next_owner') == 'planner'
    replay_state = feedback.get('status') == 'ready_for_orchestration' and settled.get('planner_job_id') == job_id
    if not active_state and not replay_state:
        return 'task_feedback_state_invalid'
    if feedback.get('plan_slug') != activation.get('plan_slug'):
        return 'task_plan_slug_mismatch'
    artifacts = feedback.get('artifacts') if isinstance(feedback.get('artifacts'), dict) else {}
    if any(not isinstance(artifacts.get(kind), dict) or artifacts[kind].get('authority_status') != 'superseded' for kind in task_feedback.get('superseded_artifacts', ())):
        return 'superseded_authority_invalid'
    detail = parsed_raw['detail']
    evidence_refs = list(dict.fromkeys([*detail['artifact_refs'], *detail['clarification_refs']]))
    expected_authority = {
        'task_id': feedback['task_id'], 'task_revision': feedback['task_revision'], 'plan_slug': feedback['plan_slug'],
        'expected_plan_revision': plan_revision_authority(context, feedback['plan_slug'])['digest'],
        'closure_evidence_digest': _detailer_replan_canonical_digest({
            'request_identity': parsed_raw['request_identity'], 'detail_digest': parsed_raw['detail_digest'],
            'macro_impact_digest': parsed_raw['macro_impact_digest'], 'evidence_refs': tuple(evidence_refs),
        }),
        'evidence_refs': evidence_refs, 'request_identity': parsed_raw['request_identity'],
        'detail_digest': parsed_raw['detail_digest'], 'macro_impact_digest': parsed_raw['macro_impact_digest'],
    }
    if active_state:
        try:
            recovery = has_pending_detailer_replan_recovery(
                context, authority=authority, planner_job_id=job_id,
            )
        except ValueError:
            return 'recovery_transaction_invalid'
        if authority != expected_authority and not recovery:
            return 'independent_authority_mismatch'
    return None


def _detailer_replan_intent_error(intent: object) -> str | None:
    if not isinstance(intent, dict):
        return 'intent_schema_invalid'
    required = {
        'schema', 'record_type', 'status', 'request_identity', 'task_id', 'source_task_revision',
        'detail_digest', 'macro_impact_digest', 'source_detailer_job_id', 'request_body_sha256', 'request',
        'created_at', 'accepted_task_revision', 'activation_id', 'activation_path', 'updated_at',
        'planner_job_id', 'planner_job_status',
    }
    optional = {'runner_start_error'}
    if set(intent) - optional != required or intent.get('schema') != 'ccb.detailer.replan_intent.v1' or intent.get('record_type') != 'ccb_detailer_replan_intent':
        return 'intent_schema_invalid'
    allowed_statuses = {'planner_submitted', 'planner_submitted_runner_start_failed'}
    if intent.get('status') not in allowed_statuses or (intent.get('status') == 'planner_submitted_runner_start_failed') != ('runner_start_error' in intent):
        return 'intent_status_invalid'
    request = intent.get('request')
    request_fields = {'project_id', 'to_agent', 'from_actor', 'body', 'task_id', 'reply_to', 'message_type', 'delivery_scope', 'silence_on_success', 'route_options', 'body_artifact'}
    if not isinstance(request, dict) or set(request) != request_fields:
        return 'intent_request_schema_invalid'
    if request.get('to_agent') != 'planner' or request.get('from_actor') != 'task_detailer' or request.get('message_type') != 'ask' or request.get('delivery_scope') != 'single' or request.get('silence_on_success') is not True or request.get('reply_to') is not None or request.get('body_artifact') is not None or not isinstance(request.get('route_options'), dict) or request['route_options']:
        return 'intent_request_route_invalid'
    strings = ('request_identity', 'task_id', 'detail_digest', 'macro_impact_digest', 'source_detailer_job_id', 'request_body_sha256', 'activation_id', 'activation_path', 'planner_job_id', 'planner_job_status')
    if any(not isinstance(intent.get(key), str) or not intent[key] for key in strings):
        return 'intent_scalar_invalid'
    if isinstance(intent.get('source_task_revision'), bool) or not isinstance(intent.get('source_task_revision'), int) or isinstance(intent.get('accepted_task_revision'), bool) or not isinstance(intent.get('accepted_task_revision'), int):
        return 'intent_revision_invalid'
    if not re.fullmatch(r'[0-9a-f]{64}', intent['request_body_sha256']) or any(not re.fullmatch(r'sha256:[0-9a-f]{64}', intent[key]) for key in ('request_identity', 'detail_digest', 'macro_impact_digest')):
        return 'intent_digest_invalid'
    if not isinstance(request.get('body'), str) or hashlib.sha256(request['body'].encode('utf-8')).hexdigest() != intent['request_body_sha256']:
        return 'intent_raw_request_invalid'
    return None


def _detailer_replan_task_feedback_error(task: object, *, job_id: str) -> str | None:
    if not isinstance(task, dict):
        return 'task_authority_schema_invalid'
    feedback = task.get('replan_feedback')
    fields = {
        'schema', 'request_identity', 'detail_digest', 'macro_impact_digest', 'source_task_revision',
        'accepted_task_revision', 'source_detailer_job_id', 'planner_job_id', 'superseded_artifacts', 'accepted_at', 'updated_at',
    }
    if not isinstance(feedback, dict) or set(feedback) != fields or feedback.get('schema') != 'ccb.detailer.replan_acceptance.v1':
        return 'task_feedback_schema_invalid'
    if feedback.get('planner_job_id') != job_id or not isinstance(feedback.get('superseded_artifacts'), list) or not feedback['superseded_artifacts']:
        return 'task_feedback_binding_invalid'
    if any(not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value) for value in (feedback.get('request_identity'), feedback.get('detail_digest'), feedback.get('macro_impact_digest'))):
        return 'task_feedback_digest_invalid'
    if any(isinstance(feedback.get(key), bool) or not isinstance(feedback.get(key), int) or feedback[key] <= 0 for key in ('source_task_revision', 'accepted_task_revision')):
        return 'task_feedback_revision_invalid'
    if task.get('task_revision') != feedback['accepted_task_revision'] or not isinstance(task.get('plan_slug'), str) or not task['plan_slug']:
        return 'task_authority_binding_invalid'
    return None


def _detailer_replan_raw_request_error(payload: object) -> str | None:
    fields = {
        'schema', 'request_identity', 'task_id', 'task_revision', 'source_detailer_job_id', 'source_role',
        'target_role', 'silence', 'detail', 'detail_digest', 'macro_impact', 'macro_impact_digest',
    }
    if not isinstance(payload, dict) or set(payload) != fields or payload.get('schema') != 'ccb.detailer.replan_request.v1':
        return 'raw_request_schema_invalid'
    detail, macro = payload.get('detail'), payload.get('macro_impact')
    detail_fields = {'summary', 'artifact_refs', 'clarification_refs'}
    macro_fields = {'categories', 'summary', 'preserved_facts', 'proposed_changes', 'acceptance_impacts', 'dependency_impacts', 'roadmap_impacts'}
    if not isinstance(detail, dict) or set(detail) != detail_fields or not isinstance(macro, dict) or set(macro) != macro_fields:
        return 'raw_request_nested_schema_invalid'
    if payload.get('source_role') != 'task_detailer' or payload.get('target_role') != 'planner' or payload.get('silence') is not True:
        return 'raw_request_route_invalid'
    if isinstance(payload.get('task_revision'), bool) or not isinstance(payload.get('task_revision'), int) or payload['task_revision'] <= 0:
        return 'raw_request_revision_invalid'
    if not all(isinstance(value, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value) for value in (payload.get('request_identity'), payload.get('detail_digest'), payload.get('macro_impact_digest'))):
        return 'raw_request_digest_format_invalid'
    if payload['detail_digest'] != _detailer_replan_canonical_digest(detail) or payload['macro_impact_digest'] != _detailer_replan_canonical_digest(macro):
        return 'raw_request_digest_mismatch'
    if payload['request_identity'] != _detailer_replan_canonical_digest({'task_id': payload['task_id'], 'task_revision': payload['task_revision'], 'detail_digest': payload['detail_digest']}):
        return 'raw_request_identity_mismatch'
    return None


def _detailer_replan_canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return 'sha256:' + hashlib.sha256(encoded).hexdigest()


def _detailer_replan_activation_error(activation: dict[str, object] | None, *, job_id: str) -> str | None:
    if not isinstance(activation, dict):
        return None
    if '_activation_error' in activation:
        return str(activation['_activation_error'])
    record_type = str(activation.get('record_type') or '')
    if record_type != 'ccb_loop_detailer_planner_replan_activation':
        if activation.get('planner_contract') == _PLANNER_CONTRACT_DETAILER_REPLAN:
            return 'Detailer replan activation record type invalid'
        return None
    required = {
        'schema_version', 'record_type', 'activation_id', 'status', 'target', 'task_id', 'task_revision',
        'source_task_revision', 'plan_slug', 'planner_contract', 'reason_for_activation', 'source_job',
        'source_replan_request', 'planner_authority', 'ask', 'auto_runner',
    }
    # auto_runner is written after submission; its absence is valid only before import starts.
    if set(activation) - {'auto_runner'} != required - {'auto_runner'}:
        return 'Detailer replan activation fields invalid'
    ask = activation.get('ask') if isinstance(activation.get('ask'), dict) else {}
    source = activation.get('source_replan_request') if isinstance(activation.get('source_replan_request'), dict) else {}
    authority = activation.get('planner_authority') if isinstance(activation.get('planner_authority'), dict) else {}
    source_job = activation.get('source_job') if isinstance(activation.get('source_job'), dict) else {}
    if set(source_job) != {'job_id', 'agent_name'} or set(ask) != {'target', 'job_id', 'status', 'sender', 'silence'}:
        return 'Detailer replan activation nested fields invalid'
    if set(source) != {
        'schema', 'request_identity', 'source_detailer_job_id', 'detail_digest', 'macro_impact_digest',
        'intent_path', 'source_request_version', 'source_request_body', 'source_request_body_sha256',
    } or set(authority) != {
        'task_id', 'task_revision', 'plan_slug', 'expected_plan_revision', 'closure_evidence_digest',
        'evidence_refs', 'request_identity', 'detail_digest', 'macro_impact_digest',
    }:
        return 'Detailer replan activation nested fields invalid'
    if (
        activation.get('schema_version') != 1 or activation.get('target') != 'planner'
        or activation.get('planner_contract') != _PLANNER_CONTRACT_DETAILER_REPLAN
        or str(ask.get('target') or '') != 'planner' or str(ask.get('job_id') or '') != job_id
        or not str(activation.get('task_id') or '') or not isinstance(activation.get('task_revision'), int)
        or authority.get('task_id') != activation.get('task_id')
        or authority.get('task_revision') != activation.get('task_revision')
        or authority.get('plan_slug') != activation.get('plan_slug')
        or source_job.get('job_id') != source.get('source_detailer_job_id')
        or ask.get('sender') != 'task_detailer' or ask.get('silence') is not True
        or ask.get('status') not in {'accepted', 'queued', 'running'}
        or activation.get('status') != 'planner_submitted'
    ):
        return 'Detailer replan activation binding invalid'
    body = source.get('source_request_body')
    digest = source.get('source_request_body_sha256')
    if not isinstance(body, str) or digest != hashlib.sha256(body.encode('utf-8')).hexdigest():
        return 'Detailer replan raw request binding invalid'
    if authority.get('request_identity') != source.get('request_identity') or authority.get('detail_digest') != source.get('detail_digest') or authority.get('macro_impact_digest') != source.get('macro_impact_digest'):
        return 'Detailer replan authority/source binding invalid'
    return None


def _accepted_detailer_replan_intent(context, *, source_job_id: str) -> dict[str, object] | None:
    root = Path(context.project.project_root) / '.ccb' / 'runtime' / 'detailer-replan'
    if not root.is_dir():
        return None
    matches: list[dict[str, object]] = []
    for path in sorted(root.glob('*.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get('source_detailer_job_id') or '') != source_job_id:
            continue
        if str(payload.get('status') or '') not in {
            'authority_accepted',
            'planner_submitted',
            'planner_submitted_runner_start_failed',
        }:
            continue
        payload['_path'] = str(path)
        matches.append(payload)
    if len(matches) > 1:
        raise ValueError(f'multiple accepted Detailer replan intents reference source job {source_job_id}')
    return matches[0] if matches else None


def _activation_already_satisfied(context, deps, *, activation: dict[str, object], target: str) -> bool:
    task_id = str(activation.get('task_id') or '').strip()
    if not task_id:
        return False
    ask = activation.get('ask') if isinstance(activation.get('ask'), dict) else {}
    job_id = str(ask.get('job_id') or '').strip()
    if not job_id:
        return False
    try:
        shown = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=task_id))
    except ValueError:
        return False
    task = shown.get('task') if isinstance(shown.get('task'), dict) else {}
    artifacts = task.get('artifacts') if isinstance(task.get('artifacts'), dict) else {}
    if target == 'orchestrator':
        artifact = artifacts.get('orchestration_notes')
        reason = str(activation.get('reason_for_activation') or '')
        if not isinstance(artifact, dict):
            return False
        if reason == 'orchestrator_route_needs_detail_detail_ready':
            return _artifact_imported_from_job(artifact, job_id=job_id)
        route = str(artifact.get('orchestrator_route') or '').strip()
        if route in _EXECUTION_ROUTES:
            return isinstance(artifacts.get('orchestration_bundle'), dict)
        return True
    if target == 'planner':
        return _artifact_imported_from_job(
            artifacts.get('task_packet'),
            job_id=job_id,
        ) and _artifact_imported_from_job(
            artifacts.get('execution_contract'),
            job_id=job_id,
        )
    if target == 'task_detailer':
        if str(task.get('status') or '') == 'blocked':
            return _artifact_imported_from_job(artifacts.get('blocker_evidence'), job_id=job_id)
        return str(task.get('status') or '') == 'detail_ready' and all(
            isinstance(artifacts.get(kind), dict)
            for kind in ('detail_design', 'detail_summary', 'detail_packet')
        )
    return False


def _artifact_imported_from_job(artifact: object, *, job_id: str) -> bool:
    if not isinstance(artifact, dict):
        return False
    actor = artifact.get('actor') if isinstance(artifact.get('actor'), dict) else {}
    return str(actor.get('source') or '') == 'loop_runner_role_output_import' and str(actor.get('job_id') or '') == job_id


def _role_import_dir(context, job_id: str) -> Path:
    path = Path(context.project.project_root) / '.ccb' / 'runtime' / 'role-output-imports' / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _activation_path(context, activation_id: str) -> Path:
    path = Path(context.project.project_root) / '.ccb' / 'runtime' / 'loops' / 'activations' / f'{activation_id}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _import_log_path(context) -> Path:
    return Path(context.project.project_root) / '.ccb' / 'runtime' / 'role-output-imports.jsonl'


def _log_import(context, record: dict[str, object]) -> dict[str, object]:
    payload = {
        'schema_version': 1,
        'record_type': 'ccb_loop_role_output_import',
        'imported_at': _utc_now(),
        **record,
    }
    path = _import_log_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(payload, sort_keys=True) + '\n')
    return payload


def _job_already_consumed(context, job_id: str) -> bool:
    return _consumed_import_record(context, job_id) is not None


def _consumed_import_record(context, job_id: str) -> dict[str, object] | None:
    for record in _iter_import_log(context):
        if _import_record_matches_job(record, job_id=job_id):
            if str(record.get('status') or '') == 'ok':
                return record
    return None


def _job_settled_for_activation_scan(context, job_id: str) -> bool:
    for record in _iter_import_log(context):
        if not _import_record_matches_job(record, job_id=job_id):
            continue
        status = str(record.get('status') or '')
        if status == 'ok':
            return True
        if (
            str(record.get('action') or '') == 'role_output_import_blocked'
            and str(record.get('reason') or '') == 'terminal_job_not_completed'
        ):
            return True
    return False


def _import_record_job_id(record: dict[str, object]) -> str:
    source_job = record.get('source_job') if isinstance(record.get('source_job'), dict) else {}
    return str(source_job.get('job_id') or record.get('job_id') or '')


def _import_record_matches_job(record: dict[str, object], *, job_id: str) -> bool:
    if _import_record_job_id(record) == job_id:
        return True
    source_job = record.get('source_job') if isinstance(record.get('source_job'), dict) else {}
    if str(source_job.get('retry_source_job_id') or '') == job_id:
        return True
    lineage = source_job.get('retry_lineage')
    if isinstance(lineage, list):
        for item in lineage:
            if not isinstance(item, dict):
                continue
            if str(item.get('retry_source_job_id') or '') == job_id:
                return True
    return False


def _iter_import_log(context):
    path = _import_log_path(context)
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _already_consumed_payload(context, *, job_id: str, record: dict[str, object]) -> dict[str, object]:
    extra: dict[str, object] = {
        'idempotent': True,
        'consumed_action': str(record.get('action') or ''),
        'role_output_import': record,
        'next_activation': _next_activation_for_consumed_record(record),
    }
    for key in (
        'ask',
        'artifact',
        'artifacts',
        'created_task',
        'handoff',
        'handoff_result',
        'next_owner',
        'plan_slug',
        'planner_job_id',
        'route',
        'status_transition',
        'task_id',
        'task_count',
        'task_ids',
        'task_set_authority',
        'task_status',
        'tasks',
    ):
        if key in record:
            extra[key] = record[key]
    tasks = record.get('tasks') if isinstance(record.get('tasks'), list) else []
    if 'task_id' not in extra:
        extra.update(_single_task_set_fields([task for task in tasks if isinstance(task, dict)]))
    status_transition = record.get('status_transition') if isinstance(record.get('status_transition'), dict) else {}
    if 'task_id' not in extra and status_transition.get('task_id'):
        extra['task_id'] = status_transition['task_id']
    if 'task_status' not in extra and status_transition.get('status'):
        extra['task_status'] = status_transition['status']
    if 'next_owner' not in extra and status_transition.get('next_owner'):
        extra['next_owner'] = status_transition['next_owner']
    source_job = record.get('source_job') if isinstance(record.get('source_job'), dict) else {}
    return _base_payload(
        context,
        loop_runner_status='ok',
        action='role_output_already_consumed',
        job_id=job_id,
        agent_name=str(source_job.get('agent_name') or '') or None,
        extra=extra,
    )


def _next_activation_for_consumed_record(record: dict[str, object]) -> str:
    action = str(record.get('action') or '')
    if action == 'frontdesk_handoff_already_started':
        return 'auto_runner'
    if action in {'imported_planner_task_authority', 'imported_planner_task_set_authority'}:
        return 'orchestrator'
    if action == 'imported_orchestration_notes':
        return 'ask_first_execution'
    if action == 'imported_task_detailer_clarification_authority':
        return 'task_detailer'
    if action in {
        'imported_task_detailer_detail_authority',
        'imported_task_detailer_blocker_authority',
        'settled_planner_terminal_status_constraint',
    }:
        return 'terminal'
    return 'inspect'


def _pending_payload(context, *, job_id: str, agent_name: str | None, reason: str) -> dict[str, object]:
    return _base_payload(
        context,
        loop_runner_status='pending',
        action='role_output_pending',
        job_id=job_id,
        agent_name=agent_name,
        extra={'reason': reason, 'next_activation': 'role_output_import'},
    )


def _blocked_payload(
    context,
    *,
    job_id: str,
    agent_name: str | None,
    reason: str,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    evidence_payload = evidence or {}
    trace = _log_import(
        context,
        {
            'action': 'role_output_import_blocked',
            'status': 'blocked',
            'job_id': job_id,
            'agent_name': agent_name,
            'reason': reason,
            'evidence': evidence_payload,
        },
    )
    return _base_payload(
        context,
        loop_runner_status='blocked',
        action='role_output_import_blocked',
        job_id=job_id,
        agent_name=agent_name,
        extra={'reason': reason, 'evidence': evidence_payload, 'role_output_import': trace, 'next_activation': 'inspect'},
    )


def _detailer_replan_blocked_payload(
    context,
    *,
    job_id: str,
    agent_name: str | None,
    reason: str,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    """Reject unauthenticated controller authority without creating import state."""
    return _base_payload(
        context,
        loop_runner_status='blocked',
        action='role_output_import_blocked',
        job_id=job_id,
        agent_name=agent_name,
        extra={'reason': reason, 'evidence': evidence or {}, 'next_activation': 'inspect'},
    )


def _base_payload(
    context,
    *,
    loop_runner_status: str,
    action: str,
    job_id: str,
    agent_name: str | None,
    extra: dict[str, object],
) -> dict[str, object]:
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': loop_runner_status,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': action,
        'source': 'loop_runner_role_output_import',
        'job_id': job_id,
        'agent_name': agent_name,
        **extra,
    }


def _attach_retry_metadata(
    payload: dict[str, object],
    *,
    snapshot: dict[str, object],
    original_job_id: str,
) -> dict[str, object]:
    retry_source_job_id = str(snapshot.get('retry_source_job_id') or '').strip()
    retry_successor_job_id = str(snapshot.get('retry_successor_job_id') or snapshot.get('job_id') or '').strip()
    if not retry_source_job_id or retry_source_job_id == original_job_id == retry_successor_job_id:
        return payload
    payload['retry_source_job_id'] = retry_source_job_id
    payload['retry_successor_job_id'] = retry_successor_job_id
    lineage = snapshot.get('retry_lineage')
    if isinstance(lineage, list):
        payload['retry_lineage'] = lineage
    return payload


def _job_trace(snapshot: dict[str, object], reply: str) -> dict[str, object]:
    decision = snapshot.get('latest_decision') if isinstance(snapshot.get('latest_decision'), dict) else {}
    trace = {
        'job_id': snapshot.get('job_id'),
        'agent_name': snapshot.get('agent_name'),
        'terminal_status': decision.get('status'),
        'finished_at': decision.get('finished_at'),
        'reply_sha256': hashlib.sha256(reply.encode('utf-8')).hexdigest(),
    }
    retry_source_job_id = str(snapshot.get('retry_source_job_id') or '').strip()
    retry_successor_job_id = str(snapshot.get('retry_successor_job_id') or '').strip()
    if retry_source_job_id:
        trace['retry_source_job_id'] = retry_source_job_id
    if retry_successor_job_id:
        trace['retry_successor_job_id'] = retry_successor_job_id
    lineage = snapshot.get('retry_lineage')
    if isinstance(lineage, list):
        trace['retry_lineage'] = lineage
    return trace


def _compact_plan_payload(payload: dict[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {}
    return {
        'action': payload.get('action'),
        'task_id': payload.get('task_id'),
        'status': payload.get('status'),
        'next_owner': payload.get('next_owner'),
        'plan_slug': payload.get('plan_slug'),
        'task_root': payload.get('task_root'),
    }


def _next_activation_for_route(route: str) -> str:
    if route in {'direct_execution', 'partial_completion'}:
        return 'ask_first_execution'
    if route == 'needs_detail':
        return 'task_detailer'
    if route == 'macro_adjustment_request':
        return 'planner_status_transition_required'
    if route == 'blocked':
        return 'blocker_evidence_required'
    return 'inspect'


def _existing_plan_slugs(context) -> tuple[str, ...]:
    plans_root = Path(context.project.project_root) / 'docs' / 'plantree' / 'plans'
    if not plans_root.is_dir():
        return ()
    return tuple(sorted(path.name for path in plans_root.iterdir() if path.is_dir()))


def _has_heading(text: str, heading: str) -> bool:
    escaped = re.escape(heading)
    return bool(re.search(rf'(?mi)^\s*(?:#+\s*)?(?:\*\*)?\s*{escaped}\s*(?:\*\*)?\s*$', text))


def _has_label(text: str, label: str) -> bool:
    escaped = re.escape(label)
    return bool(re.search(rf'(?mi)^\s*(?:[-*]\s*)?(?:\*\*)?\s*{escaped}\s*(?:\*\*)?\s*:', text))


def _frontdesk_intake_missing_fields(reply: str) -> list[str]:
    has_blocked_evidence = _has_structured_blocked_evidence(reply)
    has_request_detail = any(
        (
            _has_heading(reply, 'Macro Task Request'),
            _has_heading(reply, 'User Request'),
            _has_heading(reply, 'User Intent'),
            _has_label(reply, 'Requested validation'),
            _has_label(reply, 'Macro request'),
            _has_label(reply, 'User request'),
            _has_label(reply, 'User intent'),
        )
    )
    has_legacy_contract = _has_heading(reply, 'Execution Contract') or _has_heading(reply, 'Acceptance Criteria')
    has_intake_contract = _has_label(reply, 'Required behavior') and (
        _has_label(reply, 'Scope') or _has_label(reply, 'Constraints')
    )
    has_request_anchor = any(
        (
            _has_heading(reply, 'Macro Task Request'),
            _has_heading(reply, 'User Request'),
            _has_heading(reply, 'Intake Evidence'),
            has_blocked_evidence,
            has_request_detail and (has_legacy_contract or has_intake_contract),
        )
    )
    missing = []
    if not has_request_anchor:
        missing.append('Macro Task Request, User Request, or Intake Evidence')
    if not has_request_detail:
        missing.append('Macro request or User request detail')
    if not has_legacy_contract and not has_intake_contract and not has_blocked_evidence:
        missing.append('Execution Contract, Acceptance Criteria, or Required behavior with Scope/Constraints')
    return missing


def _has_structured_blocked_evidence(reply: str) -> bool:
    return (
        _has_heading(reply, 'Blocked Evidence')
        and _has_label(reply, 'Requested validation')
        and _has_label(reply, 'Blocker')
        and _has_label(reply, 'Routing recommendation')
        and (
            _has_label(reply, 'Prohibited actions')
            or _has_label(reply, 'Constraints')
            or _has_label(reply, 'Required behavior')
        )
    )


def _single_job(jobs, *, target: str) -> dict[str, object]:
    matches = [job for job in tuple(jobs or ()) if str(job.get('agent_name') or job.get('target_name') or '') == target]
    if len(matches) != 1:
        raise RuntimeError(f'expected one ask job for {target}; got {len(matches)}')
    job = dict(matches[0])
    if not str(job.get('job_id') or '').strip():
        raise RuntimeError(f'ask job for {target} did not return job_id')
    return job


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result = []
    for item in value:
        text = str(item or '').strip()
        if text:
            result.append(text)
    return tuple(result)


def _first_optional_text(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _invalid_allowed_paths(paths: tuple[str, ...]) -> list[str]:
    invalid: list[str] = []
    for raw in paths:
        path = Path(raw)
        if raw in {'.', './'} or path.is_absolute() or '..' in path.parts:
            invalid.append(raw)
            continue
        if path.parts and path.parts[0] in {'.ccb', '.git'}:
            invalid.append(raw)
    return invalid


def _normalize_job_id(value: object) -> str:
    text = str(value or '').strip()
    if not _SEGMENT_RE.fullmatch(text):
        raise ValueError(f'job_id must match {_SEGMENT_RE.pattern}: {text!r}')
    return text


def _normalize_segment(value: object, *, label: str) -> str:
    text = str(value or '').strip()
    if not _SEGMENT_RE.fullmatch(text):
        raise ValueError(f'{label} must match {_SEGMENT_RE.pattern}: {text!r}')
    return text


def _base_agent_name(agent_name: str) -> str:
    text = str(agent_name or '').strip()
    if text in {'frontdesk', 'planner', 'orchestrator'}:
        return text
    return text


def _activation_task_revision(activation: dict[str, object] | None) -> int | None:
    if not isinstance(activation, dict) or 'task_revision' not in activation:
        return None
    value = activation.get('task_revision')
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('managed activation task_revision must be a positive integer')
    return value


def _task_payload_revision(payload: dict[str, object]) -> int:
    record = payload.get('task') if isinstance(payload.get('task'), dict) else {}
    return task_revision(record)


def _expected_revision_for_task(
    context,
    deps,
    *,
    activation: dict[str, object] | None,
    task_id: str,
) -> int:
    activation_task_id = str(activation.get('task_id') or '').strip() if isinstance(activation, dict) else ''
    activation_revision = _activation_task_revision(activation)
    if activation_revision is not None and activation_task_id == task_id:
        return activation_revision
    shown = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=task_id))
    return _task_payload_revision(shown)


def _stale_activation_revision(
    context,
    deps,
    *,
    activation: dict[str, object] | None,
) -> dict[str, object] | None:
    expected = _activation_task_revision(activation)
    if expected is None:
        return None
    task_id = str(activation.get('task_id') or '').strip() if isinstance(activation, dict) else ''
    if not task_id:
        return {'expected_task_revision': expected, 'reason': 'activation_task_id_missing'}
    try:
        shown = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=task_id))
    except ValueError as exc:
        return {
            'task_id': task_id,
            'expected_task_revision': expected,
            'reason': 'activation_task_unavailable',
            'error': str(exc),
        }
    record = shown.get('task') if isinstance(shown.get('task'), dict) else {}
    current = task_revision(record)
    if current == expected:
        return None
    return {
        'task_id': task_id,
        'expected_task_revision': expected,
        'current_task_revision': current,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _deps(services):
    services = services or SimpleNamespace()
    return SimpleNamespace(
        plan_task=getattr(services, 'plan_task', plan_task),
        effective_capacity_snapshot=getattr(
            services,
            'effective_capacity_snapshot',
            lambda context: compile_project_effective_capacity_snapshot(
                Path(context.project.project_root)
            ),
        ),
        submit_ask=getattr(services, 'submit_ask', submit_ask),
    )


__all__ = [
    'consume_activation_role_output',
    'consume_explicit_role_output',
    'frontdesk_intake_missing_fields',
    'planner_contract_for_frontdesk_text',
    'planner_from_frontdesk_intake_message',
    'planner_required_output_for_contract',
    'planner_script_write_rules_for_contract',
]
