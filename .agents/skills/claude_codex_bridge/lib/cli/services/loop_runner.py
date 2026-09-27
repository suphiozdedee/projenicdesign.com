from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

from agents.config_loader import load_project_config
from cli.models import ParsedAskCommand, ParsedClearCommand
from cli.models_mailbox import ParsedTraceCommand
from storage.atomic import atomic_write_json, atomic_write_text
from storage.locks import file_lock

from .auto_runner_lock import AutoRunnerLock
from .ask import submit_ask
from .clear import clear_agent_context
from .loop_ask_first import release_ask_first_execution_round, run_ask_first_execution_round
from .loop_orchestration_bundle import bundle_summary, load_task_orchestration_bundle, task_revision
from .loop_effective_capacity import (
    compile_project_effective_capacity_snapshot,
    effective_capacity_digest,
)
from .multi_workgroup_scheduler import (
    resume_pending_multi_workgroup_scheduler,
    run_multi_workgroup_scheduler,
)
from .loop_run_once import loop_run_once
from .loop_topology import loop_topology
from .plan_tasks import (
    detail_ready_reconcile_authority,
    detail_ready_stop_contract_authority,
    detail_ready_stop_contract_match,
    find_first_actionable_task,
    plan_task,
    task_state_version,
)
from .questions import question_refs
from .role_output_import import consume_activation_role_output, consume_explicit_role_output
from .task_set_feedback_runtime import advance_task_set_feedback_runtime
from .trace import trace_target
from .watch_fallback import (
    load_persisted_terminal_watch_payload,
    persisted_delegated_callback_pending,
)

_ORCHESTRATOR_ROUTES = ('direct_execution', 'needs_detail', 'macro_adjustment_request', 'blocked', 'partial_completion')
_ROUND_REVIEWER_FIELD = 'ccb_round_reviewer'
_LEGACY_ROUND_CHECKER_FIELD = 'round_checker'
_INLINE_COMPACT_ARTIFACT_CONTENT_LIMIT = 500
def loop_runner_auto(context, command, services=None) -> dict[str, object]:
    deps = _deps(services)
    lock = AutoRunnerLock(context.project.project_root)
    if not lock.acquire():
        state = lock.existing_state
        return {
            'schema_version': 1,
            'record_type': 'ccb_loop_runner_auto',
            'loop_runner_status': 'paused',
            'project_id': context.project.project_id,
            'project_root': str(context.project.project_root),
            'action': 'auto_runner_already_active',
            'lock_path': str(lock.path),
            'pid': state.pid if state is not None else None,
            'next_activation': 'existing_auto_runner',
        }
    steps: list[dict[str, object]] = []
    try:
        wait_job = str(getattr(command, 'wait_job_id', None) or '').strip()
        seed_command = None
        if wait_job:
            wait_result = _wait_for_job_terminal(context, wait_job, deps, command)
            if str(wait_result.get('status') or '') != 'completed':
                return _auto_payload(
                    context,
                    status='blocked',
                    action='auto_runner_seed_job_failed',
                    steps=steps,
                    extra={
                        'wait_job_id': wait_job,
                        'wait_job_status': wait_result.get('status'),
                        'wait_job_reason': wait_result.get('reason'),
                        'next_activation': 'repair_or_resubmit_seed_job',
                    },
                )
        max_steps = max(1, int(getattr(command, 'max_steps', 24) or 24))
        once_command = replace(command, once=True, auto=False, wait_job_id=None, json_output=True)
        if wait_job:
            seed_command = replace(
                once_command,
                role_job_id=wait_job,
                consume_role_output=True,
            )
        previous_scheduler_signature = None
        for index in range(max_steps):
            step_command = seed_command if index == 0 and seed_command is not None else once_command
            payload = loop_runner_once(context, step_command, deps.services)
            steps.append(_auto_step(payload))
            if str(payload.get('action') or '') == 'multi_workgroup_execution_pending':
                wait_job_ids = _payload_wait_job_ids(payload)
                signature = (
                    payload.get('scheduler_action'),
                    payload.get('controller_status'),
                    tuple(wait_job_ids),
                    bool(payload.get('submission_unknown')),
                )
                if signature == previous_scheduler_signature:
                    return _auto_payload(
                        context,
                        status='blocked',
                        action='auto_runner_scheduler_no_progress',
                        steps=steps,
                        extra={
                            'reason': 'scheduler pending-job signature did not advance after terminal trace',
                            'pending_job_ids': wait_job_ids,
                            'scheduler_action': payload.get('scheduler_action'),
                            'controller_status': payload.get('controller_status'),
                            'next_activation': 'inspect_scheduler_job_authority_then_rerun',
                        },
                    )
                if not wait_job_ids:
                    break
                previous_scheduler_signature = signature
                _wait_for_any_job_terminal(context, wait_job_ids, deps, command)
                continue
            if _auto_should_stop(payload):
                break
            job_id = _payload_wait_job_id(payload)
            if job_id:
                _wait_for_job_terminal(context, job_id, deps, command)
        else:
            return _auto_payload(
                context,
                status='paused',
                action='auto_runner_step_limit_reached',
                steps=steps,
                extra={'max_steps': max_steps, 'next_activation': 'rerun_auto_runner'},
            )
        final = steps[-1] if steps else {}
        return _auto_payload(
            context,
            status=str(final.get('loop_runner_status') or 'idle'),
            action='auto_runner_finished',
            steps=steps,
            extra={'final_action': final.get('action'), 'final_reason': final.get('reason')},
        )
    finally:
        lock.release()


def loop_runner_once(context, command, services=None) -> dict[str, object]:
    deps = _deps(services)
    requested_task_id = str(getattr(command, 'task_id', None) or '').strip() or None
    if bool(getattr(command, 'consume_role_output', False)):
        payload = deps.consume_explicit_role_output(context, command, deps.services)
        return _release_consumed_activation_topology(context, deps, payload)
    resumed_scheduler = deps.resume_multi_workgroup_scheduler(
        context,
        task_id=requested_task_id,
        services=deps.services,
    )
    if resumed_scheduler is not None:
        return resumed_scheduler
    pending_role_output = None
    if requested_task_id is None:
        role_output = deps.consume_activation_role_output(context, command, deps.services)
        if role_output is not None:
            role_output = _release_consumed_activation_topology(context, deps, role_output)
            if role_output.get('loop_runner_status') != 'pending':
                return role_output
            pending_role_output = role_output
    task = find_first_actionable_task(context, task_id=requested_task_id)
    if task is None:
        if pending_role_output is not None:
            return pending_role_output
        if requested_task_id is None:
            task_set_feedback = deps.task_set_feedback(context, deps.services)
            if task_set_feedback is not None:
                return task_set_feedback
        payload = {
            'schema_version': 1,
            'record_type': 'ccb_loop_runner_once',
            'loop_runner_status': 'idle',
            'project_id': context.project.project_id,
            'project_root': str(context.project.project_root),
            'action': 'none',
            'reason': 'no_actionable_task_for_task' if requested_task_id else 'no_actionable_task',
        }
        if requested_task_id:
            payload['task_id'] = requested_task_id
        return payload

    runner_action = str(task.get('runner_action') or '')
    if runner_action == 'reconcile_detail_ready':
        return _reconcile_detail_ready(context, deps, task)
    if runner_action == 'activate_orchestrator':
        task_identity = str(task['record'].get('task_id') or 'unknown')
        lock_name = hashlib.sha256(task_identity.encode('utf-8')).hexdigest()
        lock_path = (
            Path(context.project.project_root)
            / '.ccb'
            / 'runtime'
            / 'loops'
            / 'activations'
            / f'orchestrator-{lock_name}.lock'
        )
        with file_lock(lock_path):
            return _activate_orchestrator(context, command, deps, task)
    if runner_action == 'ask_first_execute':
        return _run_ask_first_execution_round(context, command, deps, task)
    if runner_action == 'ask_first_execution_not_ready':
        record = task['record']
        current_loop = str(record.get('current_loop') or '').strip()
        if current_loop and _task_has_ask_first_execution_route(record):
            return _run_ask_first_execution_round(context, command, deps, task)
        return _phase4_not_ready(
            context,
            task,
            reason=str(task.get('runner_reason') or 'phase4_not_ready'),
            loop_id=current_loop or None,
        )
    if runner_action == 'execute':
        return _run_execution_round(context, command, deps, task)
    if runner_action == 'activate_planner':
        return _activate_planner(context, command, deps, task)
    if runner_action == 'activate_task_detailer':
        return _activate_task_detailer(context, command, deps, task)
    if runner_action == 'activate_plan_reviewer':
        return _activate_plan_reviewer(context, command, deps, task)
    if runner_action in {'planner_next_action_required', 'blocker_evidence_required'}:
        return _finalize_script_owned_terminal_route(context, deps, task)
    return _stop_without_activation(context, task)


def _reconcile_detail_ready(context, deps, task: dict[str, object]) -> dict[str, object]:
    record = task['record']
    authority = detail_ready_reconcile_authority(
        record,
        project_root=Path(context.project.project_root),
    )
    if authority is None:
        raise ValueError('stale detail_ready reconciliation candidate')
    reconciled = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-reconcile-detail-ready',
            task_id=record.get('task_id'),
            expected_status=record.get('status'),
            expected_owner=record.get('owner'),
            expected_next_owner=record.get('next_owner'),
            expected_current_loop=record.get('current_loop'),
            expected_task_revision=task_revision(record),
            expected_state_version=task_state_version(record),
            expected_activation_reason=record.get('activation_reason'),
            expected_authority_digest=authority['authority_digest'],
        ),
    )
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'ok',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'reconciled_detail_ready',
        'reason': 'explicit_detail_ready_stop_contract',
        'task_id': record.get('task_id'),
        'task_status': reconciled.get('status'),
        'next_owner': reconciled.get('next_owner'),
        'activation_reason': reconciled.get('activation_reason'),
        'idempotent': reconciled.get('idempotent'),
    }


def _run_ask_first_execution_round(context, command, deps, task: dict[str, object]) -> dict[str, object]:
    record = task['record']
    task_id = str(record.get('task_id') or '')
    capacity_snapshot = deps.effective_capacity_snapshot(context)
    try:
        orchestration_bundle, bundle_artifact = load_task_orchestration_bundle(
            Path(context.project.project_root),
            record,
            capacity_snapshot=capacity_snapshot,
        )
    except ValueError as exc:
        return _ask_first_bundle_not_ready(context, record=record, reason=str(exc))
    bundle_nodes = orchestration_bundle.get('nodes') if isinstance(orchestration_bundle.get('nodes'), list) else []
    current_loop = str(record.get('current_loop') or '').strip()
    if current_loop:
        loop_id = current_loop
        bind = None
    else:
        loop_id = f'lp{uuid4().hex[:6]}'
        bind = deps.plan_task(
            context,
            SimpleNamespace(
                action='task-bind-loop',
                task_id=task_id,
                loop_id=loop_id,
                expected_task_revision=task_revision(record),
            ),
        )
    if len(bundle_nodes) > 1 or int(capacity_snapshot.get('config_version') or 0) == 3:
        payload = deps.multi_workgroup_scheduler(
            context,
            loop_id=loop_id,
            task_record=record,
            bundle=orchestration_bundle,
            bundle_artifact=bundle_artifact,
            services=deps.services,
        )
        payload['bind'] = _compact_plan_payload(bind)
        payload['orchestration_bundle'] = bundle_summary(orchestration_bundle, bundle_artifact)
        payload['next_activation'] = (
            'callback_or_runner_once'
            if payload.get('loop_runner_status') == 'pending'
            else _next_activation(payload.get('task_status'))
        )
        return payload
    round_payload = deps.ask_first_execution(
        context,
        SimpleNamespace(
            kind='loop-ask-first-execution',
            project=None,
            loop_id=loop_id,
            task_id=task_id,
            timeout_s=getattr(command, 'timeout_s', None),
            json_output=True,
        ),
        deps.services,
    )
    if _ask_first_round_pending(round_payload):
        return _ask_first_pending_response(
            context,
            task_id=task_id,
            loop_id=loop_id,
            bind=bind,
            round_payload=round_payload,
        )
    round_result, round_result_source = _round_result(round_payload)
    report_path = _round_report_path(round_payload)
    imported = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-import-round',
            task_id=task_id,
            loop_id=loop_id,
            result=round_result,
            file_path=report_path,
            actor_source='loop_runner',
            actor='loop_runner',
            job_id=str(_first_job_id(round_payload) or ''),
            expected_task_revision=task_revision(record),
        ),
    )
    release = deps.ask_first_release(context, round_payload, deps.services)
    _record_round_import(round_payload, imported=imported, release=release)
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'ok',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'ran_one_round',
        'dispatch_source': round_payload.get('dispatch_source') or 'ask_first_mount_topology',
        'execution_mode': 'ask_first_direct_execution',
        'task_id': task_id,
        'loop_id': loop_id,
        'round_result': round_result,
        'round_result_source': round_result_source,
        'task_status': imported.get('status'),
        'orchestration_bundle': bundle_summary(orchestration_bundle, bundle_artifact),
        'bind': _compact_plan_payload(bind),
        'round': {
            'loop_run_status': round_payload.get('loop_run_status'),
            'round_path': report_path,
            'round_json_path': _round_json_path(round_payload),
        },
        'topology': _compact_topology_payload(round_payload.get('topology')),
        'release': _compact_release_payload(release),
        'import': _compact_plan_payload(imported),
        'next_activation': _next_activation(imported.get('status')),
    }


def _ask_first_bundle_not_ready(
    context,
    *,
    record: dict[str, object],
    reason: str,
    action: str = 'ask_first_execution_not_ready',
    bundle: dict[str, object] | None = None,
    bundle_artifact: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'paused',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': action,
        'reason': reason,
        'task_id': record.get('task_id'),
        'task_revision': task_revision(record),
        'task_status': record.get('status'),
        'next_owner': record.get('next_owner'),
        'next_activation': 'orchestration_bundle_required',
    }
    if bundle is not None:
        payload['orchestration_bundle'] = bundle_summary(bundle, bundle_artifact)
    return payload


def _run_execution_round(context, command, deps, task: dict[str, object]) -> dict[str, object]:
    record = task['record']
    task_id = str(record.get('task_id') or '')
    current_loop = str(record.get('current_loop') or '').strip()
    if current_loop:
        return _phase4_not_ready(
            context,
            task,
            loop_id=current_loop,
            reason='running_task_bound_to_loop',
        )
    else:
        loop_id = f'lp{uuid4().hex[:6]}'
        bind = deps.plan_task(
            context,
            SimpleNamespace(
                action='task-bind-loop',
                task_id=task_id,
                loop_id=loop_id,
                expected_task_revision=task_revision(record),
            ),
        )
        round_payload = deps.loop_run_once(
            context,
            SimpleNamespace(
                kind='loop-run-once',
                project=None,
                loop_id=loop_id,
                task=None,
                task_id=task_id,
                worker_profile='worker',
                reviewer_profile='code_reviewer',
                orchestrator='orchestrator',
                round_checker='round_checker',
                timeout_s=getattr(command, 'timeout_s', None),
                json_output=True,
            ),
            deps.services,
        )
    round_result, round_result_source = _round_result(round_payload)
    report_path = _round_report_path(round_payload)
    imported = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-import-round',
            task_id=task_id,
            loop_id=loop_id,
            result=round_result,
            file_path=report_path,
            actor_source='loop_runner',
            actor='loop_runner',
            job_id=str(_first_job_id(round_payload) or ''),
            expected_task_revision=task_revision(record),
        ),
    )
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'ok',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'ran_one_round',
        'dispatch_source': round_payload.get('dispatch_source') or 'fixed_worker_reviewer',
        'task_id': task_id,
        'loop_id': loop_id,
        'round_result': round_result,
        'round_result_source': round_result_source,
        'task_status': imported.get('status'),
        'bind': _compact_plan_payload(bind),
        'round': {
            'loop_run_status': round_payload.get('loop_run_status'),
            'round_path': report_path,
        },
        'import': _compact_plan_payload(imported),
        'next_activation': _next_activation(imported.get('status')),
    }


def _phase4_not_ready(
    context,
    task: dict[str, object],
    *,
    reason: str,
    loop_id: str | None = None,
) -> dict[str, object]:
    record = dict(task['record'])
    resolved_loop_id = loop_id or str(record.get('current_loop') or '').strip() or None
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'paused',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'ask_first_execution_not_ready',
        'reason': (
            f'{reason}; Phase 4 ask-first execution can only start from an unbound '
            'direct_execution task, and topology dispatch is legacy/disabled for loop runner mainline'
        ),
        'task_id': record.get('task_id'),
        'loop_id': resolved_loop_id,
        'task_status': record.get('status'),
        'next_owner': record.get('next_owner') or task.get('next_owner'),
        'next_activation': 'phase4_ask_first_runner_required',
    }


def _finalize_script_owned_terminal_route(context, deps, task: dict[str, object]) -> dict[str, object]:
    action = str(task.get('runner_action') or '')
    record = dict(task['record'])
    task_id = str(record.get('task_id') or '').strip()
    if not task_id:
        raise RuntimeError('loop runner cannot finalize script-owned route without task_id')
    if action == 'planner_next_action_required':
        route = 'macro_adjustment_request'
        artifact_kind = 'macro_adjustment_request'
        source_filename = 'macro-adjustment-request.json'
        target_status = 'replan_required'
        next_owner = 'planner'
        return_action = 'imported_macro_adjustment_request'
    elif action == 'blocker_evidence_required':
        route = 'blocked'
        artifact_kind = 'blocker_evidence'
        source_filename = 'blocker-evidence.md'
        target_status = 'blocked'
        next_owner = 'terminal'
        return_action = 'imported_blocker_evidence'
    else:
        raise RuntimeError(f'unsupported script-owned terminal route action: {action}')

    reason = str(task.get('runner_reason') or f'orchestrator_route_{route}')
    notes_ref = _orchestration_notes_ref(record)
    evidence_path = (
        Path(context.project.project_root)
        / '.ccb'
        / 'runtime'
        / 'loops'
        / 'route-evidence'
        / task_id
        / source_filename
    )
    evidence_payload = {
        'task_id': task_id,
        'route': route,
        'source': 'loop_runner/script-owned',
        'reason': reason,
        'status_transition': target_status,
        'next_owner': next_owner,
        'orchestration_notes': {
            'path': notes_ref.get('path', ''),
            'sha256': notes_ref.get('sha256', ''),
        },
    }
    if artifact_kind == 'macro_adjustment_request':
        atomic_write_json(evidence_path, evidence_payload)
    else:
        atomic_write_text(
            evidence_path,
            '\n'.join(
                [
                    '# Blocker Evidence',
                    '',
                    f'task_id: {task_id}',
                    f'route: {route}',
                    'source: loop_runner/script-owned',
                    f'reason: {reason}',
                    f'status_transition: {target_status}',
                    f'next_owner: {next_owner}',
                    f'orchestration_notes_path: {notes_ref.get("path", "")}',
                    f'orchestration_notes_sha256: {notes_ref.get("sha256", "")}',
                    '',
                ]
            ),
        )

    imported = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-artifact',
            task_id=task_id,
            artifact_kind=artifact_kind,
            file_path=str(evidence_path),
            actor_source='loop_runner/script-owned',
            actor='loop_runner',
            expected_task_revision=task_revision(record),
        ),
    )
    transitioned = deps.plan_task(
        context,
        SimpleNamespace(
            action='task-status',
            task_id=task_id,
            status=target_status,
            next_owner=next_owner,
            activation_reason=f'{reason}:script_owned_route',
            expected_task_revision=task_revision(record),
        ),
    )
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'ok',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': return_action,
        'reason': reason,
        'task_id': task_id,
        'route': route,
        'task_status': transitioned.get('status'),
        'next_owner': transitioned.get('next_owner'),
        'artifact': imported.get('artifact'),
        'import': _compact_plan_payload(transitioned),
        'next_activation': _next_activation(transitioned.get('status')),
    }


def _orchestration_notes_ref(record: dict[str, object]) -> dict[str, object]:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    notes = artifacts.get('orchestration_notes') if isinstance(artifacts, dict) else None
    if not isinstance(notes, dict):
        return {}
    ref: dict[str, object] = {}
    for key in ('path', 'sha256'):
        value = notes.get(key)
        if value:
            ref[key] = value
    return ref


def _ask_first_round_pending(payload: dict[str, object]) -> bool:
    return str(payload.get('loop_run_status') or '').strip() == 'pending'


def _ask_first_pending_response(
    context,
    *,
    task_id: str,
    loop_id: str,
    bind: dict[str, object] | None,
    round_payload: dict[str, object],
) -> dict[str, object]:
    paths = round_payload.get('paths') if isinstance(round_payload.get('paths'), dict) else {}
    pending = round_payload.get('pending') if isinstance(round_payload.get('pending'), dict) else {}
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'paused',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'ask_first_execution_pending',
        'dispatch_source': round_payload.get('dispatch_source') or 'ask_first_mount_topology',
        'execution_mode': 'ask_first_direct_execution',
        'task_id': task_id,
        'loop_id': loop_id,
        'round_result': 'pending',
        'round_result_source': 'ask_job_pending',
        'task_status': 'running',
        'bind': _compact_plan_payload(bind),
        'round': {
            'loop_run_status': round_payload.get('loop_run_status'),
            'pending_json_path': str(paths.get('pending_json') or ''),
        },
        'topology': _compact_topology_payload(round_payload.get('topology')),
        'pending': dict(pending),
        'next_activation': 'phase4_ask_first_runner_required',
    }


def _task_has_ask_first_execution_route(record: dict[str, object]) -> bool:
    return _orchestrator_route_for_record(record) in {'direct_execution', 'partial_completion'}


def _orchestrator_route_for_record(record: dict[str, object]) -> str:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    notes = artifacts.get('orchestration_notes') if isinstance(artifacts, dict) else None
    route = str(notes.get('orchestrator_route') or '').strip().lower() if isinstance(notes, dict) else ''
    if route and route not in _ORCHESTRATOR_ROUTES:
        known = ', '.join(sorted(_ORCHESTRATOR_ROUTES))
        raise ValueError(f'unknown orchestrator route {route!r}; expected one of: {known}')
    return route


def _activate_orchestrator(context, command, deps, task: dict[str, object]) -> dict[str, object]:
    record = dict(task['record'])
    task_id = str(record.get('task_id') or '')
    target, target_is_configured = _activation_target_for_role(
        context,
        role_id='agentroles.ccb_orchestrator',
        fallback='orchestrator',
    )
    existing = _consume_existing_activation_for_task(
        context,
        command,
        deps,
        task_id=task_id,
        target=target,
        record=record,
    )
    if existing is not None:
        return existing
    activation_id = f'act-{uuid4().hex[:12]}'
    activation = _orchestrator_activation_packet(
        context,
        record,
        activation_id=activation_id,
        reason=str(task.get('runner_reason') or 'ready_for_orchestration'),
        effective_capacity_snapshot=deps.effective_capacity_snapshot(context),
    )
    activation_path = _activation_path(context, activation_id)
    activation['topology'] = _mount_activation_topology(
        context,
        deps,
        activation_id=activation_id,
        target=target,
        profile='ccb_orchestrator',
        window_name='ccb-plan',
        configured=target_is_configured,
    )
    atomic_write_json(activation_path, activation)
    if _activation_topology_not_ready(activation['topology']):
        return _activation_topology_failure(context, activation, activation_path=activation_path)
    freshness = _prepare_immaculate_activation(
        context,
        deps,
        activation_id=activation_id,
        target=target,
        role='ccb_orchestrator',
        reason='fresh_before_orchestrator_ask',
    )
    activation['freshness'] = freshness
    atomic_write_json(activation_path, activation)
    if str(freshness.get('status') or '') != 'cleared':
        return _activation_freshness_failure(
            context,
            deps,
            activation,
            activation_path=activation_path,
        )
    activation['submission'] = {'status': 'prepared', 'target': target}
    atomic_write_json(activation_path, activation)
    try:
        summary = deps.submit_ask(
            context,
            ParsedAskCommand(
                project=None,
                target=target,
                sender='system',
                message=_orchestrator_message(activation),
                task_id=activation_id,
                compact=True,
                inline_request=False,
            ),
        )
    except Exception as exc:
        activation['submission'] = {
            'status': 'unknown',
            'target': target,
            'error': f'{type(exc).__name__}: {exc}',
        }
        atomic_write_json(activation_path, activation)
        raise
    job = _single_job(summary.jobs, target=target)
    activation['ask'] = {
        'target': target,
        'job_id': str(job['job_id']),
        'status': job.get('status'),
    }
    activation['submission'] = {'status': 'accepted', 'target': target, 'job_id': str(job['job_id'])}
    atomic_write_json(activation_path, activation)
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'ok',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'activated_orchestrator',
        'reason': activation['reason_for_activation'],
        'task_id': task_id,
        'task_status': record.get('status'),
        'next_owner': 'orchestrator',
        'activation_id': activation_id,
        'activation_path': str(activation_path),
        'topology': activation['topology'],
        'freshness': freshness,
        'ask': activation['ask'],
        'next_activation': 'stop_after_one_activation',
    }


def _activate_planner(context, command, deps, task: dict[str, object]) -> dict[str, object]:
    record = dict(task['record'])
    task_id = str(record.get('task_id') or '')
    existing = _consume_existing_activation_for_task(
        context,
        command,
        deps,
        task_id=task_id,
        target='planner',
        record=record,
    )
    if existing is not None:
        return existing
    activation_id = f'act-{uuid4().hex[:12]}'
    activation = _planner_activation_packet(
        context,
        record,
        activation_id=activation_id,
        action=str(task.get('runner_action') or 'activate_planner'),
        reason=str(task.get('runner_reason') or 'planner_state'),
    )
    activation_path = _activation_path(context, activation_id)
    atomic_write_json(activation_path, activation)
    summary = deps.submit_ask(
        context,
        ParsedAskCommand(
            project=None,
            target='planner',
            sender='system',
            message=_planner_message(activation),
            task_id=activation_id,
            compact=True,
            inline_request=True,
        ),
    )
    job = _single_job(summary.jobs, target='planner')
    activation['ask'] = {
        'target': 'planner',
        'job_id': str(job['job_id']),
        'status': job.get('status'),
    }
    atomic_write_json(activation_path, activation)
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'ok',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'activated_planner',
        'reason': activation['reason_for_activation'],
        'task_id': task_id,
        'task_status': record.get('status'),
        'next_owner': 'planner',
        'activation_id': activation_id,
        'activation_path': str(activation_path),
        'ask': activation['ask'],
        'next_activation': 'stop_after_one_activation',
    }


def _activate_plan_reviewer(context, command, deps, task: dict[str, object]) -> dict[str, object]:
    record = dict(task['record'])
    task_id = str(record.get('task_id') or '')
    activation_id = f'act-{uuid4().hex[:12]}'
    activation = _plan_reviewer_activation_packet(
        context,
        record,
        activation_id=activation_id,
        reason=str(task.get('runner_reason') or 'review_required'),
    )
    activation_path = _activation_path(context, activation_id)
    atomic_write_json(activation_path, activation)
    summary = deps.submit_ask(
        context,
        ParsedAskCommand(
            project=None,
            target='plan_reviewer',
            sender='system',
            message=_plan_reviewer_message(activation),
            task_id=activation_id,
            compact=True,
            inline_request=True,
        ),
    )
    job = _single_job(summary.jobs, target='plan_reviewer')
    activation['ask'] = {
        'target': 'plan_reviewer',
        'job_id': str(job['job_id']),
        'status': job.get('status'),
    }
    atomic_write_json(activation_path, activation)
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'ok',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'activated_plan_reviewer',
        'reason': activation['reason_for_activation'],
        'task_id': task_id,
        'task_status': record.get('status'),
        'next_owner': 'planner',
        'activation_id': activation_id,
        'activation_path': str(activation_path),
        'ask': activation['ask'],
        'next_activation': 'stop_after_one_activation',
    }


def _activate_task_detailer(context, command, deps, task: dict[str, object]) -> dict[str, object]:
    record = dict(task['record'])
    task_id = str(record.get('task_id') or '')
    next_owner = str(task.get('next_owner') or record.get('next_owner') or 'planner')
    target, target_is_configured = _activation_target_for_role(
        context,
        role_id='agentroles.ccb_task_detailer',
        fallback='task_detailer',
    )
    existing = _consume_existing_activation_for_task(
        context,
        command,
        deps,
        task_id=task_id,
        target=target,
        record=record,
    )
    if existing is not None:
        return existing
    activation_id = f'act-{uuid4().hex[:12]}'
    activation = _task_detailer_activation_packet(
        context,
        record,
        activation_id=activation_id,
        reason=str(task.get('runner_reason') or 'detail_required'),
    )
    activation_path = _activation_path(context, activation_id)
    activation['topology'] = _mount_activation_topology(
        context,
        deps,
        activation_id=activation_id,
        target=target,
        profile='ccb_task_detailer',
        window_name='ccb-user',
        configured=target_is_configured,
    )
    atomic_write_json(activation_path, activation)
    if _activation_topology_not_ready(activation['topology']):
        return _activation_topology_failure(context, activation, activation_path=activation_path)
    freshness = _prepare_immaculate_activation(
        context,
        deps,
        activation_id=activation_id,
        target=target,
        role='ccb_task_detailer',
        reason='fresh_before_task_detailer_ask',
    )
    activation['freshness'] = freshness
    atomic_write_json(activation_path, activation)
    if str(freshness.get('status') or '') != 'cleared':
        return _activation_freshness_failure(
            context,
            deps,
            activation,
            activation_path=activation_path,
        )
    summary = deps.submit_ask(
        context,
        ParsedAskCommand(
            project=None,
            target=target,
            sender='system',
            message=_task_detailer_message(activation),
            task_id=activation_id,
            compact=True,
            inline_request=True,
        ),
    )
    job = _single_job(summary.jobs, target=target)
    activation['ask'] = {
        'target': target,
        'job_id': str(job['job_id']),
        'status': job.get('status'),
    }
    atomic_write_json(activation_path, activation)
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'ok',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'activated_task_detailer',
        'reason': activation['reason_for_activation'],
        'task_id': task_id,
        'task_status': record.get('status'),
        'next_owner': next_owner,
        'activation_id': activation_id,
        'activation_path': str(activation_path),
        'topology': activation['topology'],
        'freshness': freshness,
        'ask': activation['ask'],
        'next_activation': 'stop_after_one_activation',
    }


def _stop_without_activation(context, task: dict[str, object]) -> dict[str, object]:
    record = dict(task['record'])
    action = str(task.get('runner_action') or 'stop')
    paused_actions = {'paused', 'planner_next_action_required', 'blocker_evidence_required'}
    next_activation = 'none'
    if action == 'planner_next_action_required':
        next_activation = 'planner_status_transition_required'
    elif action == 'blocker_evidence_required':
        next_activation = 'blocker_evidence_required'
    payload = {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'paused' if action in paused_actions else action,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': action,
        'reason': task.get('runner_reason'),
        'task_id': record.get('task_id'),
        'task_revision': task_revision(record),
        'task_status': record.get('status'),
        'next_owner': task.get('next_owner'),
        'next_activation': next_activation,
    }
    if action == 'paused' and str(record.get('task_id') or '').strip():
        payload['question_refs'] = question_refs(context, record.get('task_id'))
    return payload


def _consume_role_output_disabled(context) -> dict[str, object]:
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'rejected',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'consume_role_output_disabled',
        'reason': (
            '--consume-role-output is legacy/disabled for the Decision 020 mainline; '
            'use script-owned artifact imports and explicit task status transitions instead'
        ),
        'next_activation': 'none',
    }


def _deps(services):
    services = services or SimpleNamespace()
    return SimpleNamespace(
        ask_first_execution=getattr(services, 'ask_first_execution', run_ask_first_execution_round),
        ask_first_release=getattr(services, 'ask_first_release', release_ask_first_execution_round),
        loop_run_once=getattr(services, 'loop_run_once', loop_run_once),
        plan_task=getattr(services, 'plan_task', plan_task),
        consume_activation_role_output=getattr(
            services,
            'consume_activation_role_output',
            consume_activation_role_output,
        ),
        consume_explicit_role_output=getattr(
            services,
            'consume_explicit_role_output',
            consume_explicit_role_output,
        ),
        clear_agent_context=getattr(services, 'clear_agent_context', clear_agent_context),
        loop_topology=getattr(services, 'loop_topology', loop_topology),
        submit_ask=getattr(services, 'submit_ask', submit_ask),
        trace_target=getattr(services, 'trace_target', trace_target),
        persisted_terminal_watch=getattr(
            services,
            'persisted_terminal_watch',
            load_persisted_terminal_watch_payload,
        ),
        delegated_callback_pending=getattr(
            services,
            'delegated_callback_pending',
            persisted_delegated_callback_pending,
        ),
        task_set_feedback=getattr(
            services,
            'task_set_feedback',
            advance_task_set_feedback_runtime,
        ),
        effective_capacity_snapshot=getattr(
            services,
            'effective_capacity_snapshot',
            lambda context: compile_project_effective_capacity_snapshot(
                Path(context.project.project_root)
            ),
        ),
        multi_workgroup_scheduler=getattr(
            services,
            'multi_workgroup_scheduler',
            run_multi_workgroup_scheduler,
        ),
        resume_multi_workgroup_scheduler=getattr(
            services,
            'resume_multi_workgroup_scheduler',
            resume_pending_multi_workgroup_scheduler,
        ),
        sleep=getattr(services, 'sleep', time.sleep),
        services=services,
    )


def _activation_target_for_role(context, *, role_id: str, fallback: str) -> tuple[str, bool]:
    try:
        config = load_project_config(
            Path(context.project.project_root),
            include_loop_overlays=False,
        ).config
    except Exception:
        return fallback, False
    agents = getattr(config, 'agents', None)
    if not isinstance(agents, dict):
        return fallback, False
    if fallback in agents:
        return fallback, True
    for agent_name, spec in agents.items():
        if str(getattr(spec, 'role', '') or '').strip() == role_id:
            return str(agent_name), True
    return fallback, False


def _mount_activation_topology(
    context,
    deps,
    *,
    activation_id: str,
    target: str,
    profile: str,
    window_name: str,
    configured: bool,
) -> dict[str, object]:
    if configured:
        return {
            'mode': 'configured',
            'target': target,
            'profile': profile,
            'window_name': window_name,
            'loop_topology_status': 'configured',
        }
    capacity_snapshot = deps.effective_capacity_snapshot(context)
    if int(capacity_snapshot.get('config_version') or 0) == 3:
        dynamic_profiles = capacity_snapshot.get('dynamic_profiles')
        v3_profile = profile.removeprefix('ccb_')
        if isinstance(dynamic_profiles, dict) and v3_profile in dynamic_profiles:
            profile = v3_profile
    proposal_path = _activation_path(context, activation_id).with_suffix('.topology.proposal.json')
    proposal = {
        'schema': 'ccb.loop.agent_mount_topology.v1',
        'owner': {'kind': 'loop', 'loop_id': activation_id},
        'release_policy': {'policy': 'auto', 'idle_only': True},
        'windows': [
            {
                'name': window_name,
                'class': 'user' if window_name == 'ccb-user' else 'planning',
                'max_panes': 6,
                'layout_policy': 'append-or-create-window',
            }
        ],
        'agents': [
            {
                'id': target,
                'profile': profile,
                'desired_state': 'present',
                'window_name': window_name,
                'pane_order': 0,
                'lifecycle': 'ephemeral',
                'release_policy': 'auto',
            }
        ],
    }
    if int(capacity_snapshot.get('config_version') or 0) == 3:
        proposal['capacity_digest'] = effective_capacity_digest(capacity_snapshot)
    atomic_write_json(proposal_path, proposal)
    try:
        proposed = deps.loop_topology(
            context,
            SimpleNamespace(
                action='propose',
                loop_id=activation_id,
                from_path=str(proposal_path),
                proposal_id='role-activation',
                json_output=True,
            ),
        )
        committed = deps.loop_topology(
            context,
            SimpleNamespace(
                action='commit',
                loop_id=activation_id,
                proposal_id='role-activation',
                apply=True,
                json_output=True,
            ),
        )
        status = deps.loop_topology(
            context,
            SimpleNamespace(action='status', loop_id=activation_id, json_output=True),
        )
    except Exception as exc:
        return {
            'mode': 'dynamic',
            'target': target,
            'profile': profile,
            'window_name': window_name,
            'loop_id': activation_id,
            'proposal_source_path': str(proposal_path),
            'loop_topology_status': 'failed',
            'error': str(exc),
        }
    return {
        'mode': 'dynamic',
        'target': target,
        'profile': profile,
        'window_name': window_name,
        'loop_id': activation_id,
        'proposal_source_path': str(proposal_path),
        'propose': proposed,
        'commit': committed,
        'status': status,
        'loop_topology_status': status.get('loop_topology_status'),
    }


def _activation_topology_not_ready(topology: object) -> bool:
    if not isinstance(topology, dict):
        return True
    return str(topology.get('loop_topology_status') or '') not in {'configured', 'ready'}


def _activation_topology_failure(context, activation: dict[str, object], *, activation_path: Path) -> dict[str, object]:
    topology = activation.get('topology') if isinstance(activation.get('topology'), dict) else {}
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'blocked',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'activation_topology_not_ready',
        'reason': topology.get('error') or topology.get('loop_topology_status') or 'topology_not_ready',
        'task_id': activation.get('task_id'),
        'task_status': activation.get('task_status'),
        'activation_id': activation.get('activation_id'),
        'activation_path': str(activation_path),
        'topology': topology,
        'next_activation': 'repair_activation_topology',
    }


def _activation_freshness_failure(
    context,
    deps,
    activation: dict[str, object],
    *,
    activation_path: Path,
) -> dict[str, object]:
    freshness = activation.get('freshness') if isinstance(activation.get('freshness'), dict) else {}
    topology = activation.get('topology') if isinstance(activation.get('topology'), dict) else {}
    release: dict[str, object] | None = None
    if str(topology.get('mode') or '') == 'dynamic':
        loop_id = str(topology.get('loop_id') or activation.get('activation_id') or '').strip()
        if loop_id:
            try:
                release = deps.loop_topology(
                    context,
                    SimpleNamespace(
                        action='release',
                        loop_id=loop_id,
                        policy='auto',
                        idle_only=True,
                        json_output=True,
                    ),
                )
            except Exception as exc:
                release = {
                    'loop_topology_status': 'release_failed',
                    'error': str(exc),
                }
            activation['topology_release'] = release
            activation['topology_released_at'] = _utc_now()
            atomic_write_json(activation_path, activation)
    status = str(freshness.get('status') or 'missing')
    payload: dict[str, object] = {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_once',
        'loop_runner_status': 'blocked',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'activation_freshness_not_ready',
        'reason': f'immaculate activation freshness is not proven: {status}',
        'task_id': activation.get('task_id'),
        'task_status': activation.get('task_status'),
        'activation_id': activation.get('activation_id'),
        'activation_path': str(activation_path),
        'freshness': freshness,
        'topology': topology,
        'next_activation': 'repair_activation_freshness',
    }
    if release is not None:
        payload['activation_topology_release'] = release
    return payload


def _release_consumed_activation_topology(context, deps, payload: dict[str, object]) -> dict[str, object]:
    if not isinstance(payload, dict) or str(payload.get('loop_runner_status') or '') == 'pending':
        return payload
    job_id = str(payload.get('job_id') or '').strip()
    if not job_id:
        return payload
    for activation_path, activation in _iter_activation_records(context):
        ask = activation.get('ask') if isinstance(activation.get('ask'), dict) else {}
        if str(ask.get('job_id') or '').strip() != job_id:
            continue
        topology = activation.get('topology') if isinstance(activation.get('topology'), dict) else {}
        if str(topology.get('mode') or '') != 'dynamic':
            return payload
        previous_release = (
            activation.get('topology_release')
            if isinstance(activation.get('topology_release'), dict)
            else None
        )
        if previous_release is not None and str(previous_release.get('loop_topology_status') or '') == 'released':
            result = dict(payload)
            result['activation_topology_release'] = previous_release
            return result
        loop_id = str(topology.get('loop_id') or activation.get('activation_id') or '').strip()
        if not loop_id:
            return payload
        release = deps.loop_topology(
            context,
            SimpleNamespace(
                action='release',
                loop_id=loop_id,
                policy='auto',
                idle_only=True,
                json_output=True,
            ),
        )
        activation['topology_release'] = release
        activation['topology_released_at'] = _utc_now()
        atomic_write_json(activation_path, activation)
        result = dict(payload)
        result['activation_topology_release'] = release
        if str(release.get('loop_topology_status') or '') != 'released':
            result['role_output_action'] = result.get('action')
            result['loop_runner_status'] = 'blocked'
            result['action'] = 'activation_topology_release_incomplete'
            result['next_activation'] = 'repair_activation_topology_release'
        return result
    return payload


def _wait_for_job_terminal(context, job_id: str, deps, command) -> dict[str, object]:
    poll_interval = max(0.0, float(getattr(command, 'poll_interval_s', 2.0) or 0.0))
    while True:
        persisted = deps.persisted_terminal_watch(context, job_id)
        if persisted is not None:
            return {
                'job_id': job_id,
                'status': str(persisted.get('status') or 'completed').strip().lower(),
                'reason': None,
            }
        payload = deps.trace_target(context, ParsedTraceCommand(project=None, target=job_id))
        job = payload.get('job') if isinstance(payload, dict) else None
        status = str(job.get('status') or '').strip().lower() if isinstance(job, dict) else ''
        if status in {'completed', 'failed', 'cancelled', 'timed_out'}:
            decision = job.get('terminal_decision') if isinstance(job, dict) else None
            if deps.delegated_callback_pending(context, job_id) or _delegated_job_waits_for_continuation(decision):
                deps.sleep(poll_interval)
                continue
            reason = str(decision.get('reason') or '') if isinstance(decision, dict) else ''
            return {'job_id': job_id, 'status': status, 'reason': reason or None}
        deps.sleep(poll_interval)


def _wait_for_any_job_terminal(
    context,
    job_ids: list[str],
    deps,
    command,
) -> dict[str, object]:
    poll_interval = max(0.0, float(getattr(command, 'poll_interval_s', 2.0) or 0.0))
    while True:
        for job_id in job_ids:
            persisted = deps.persisted_terminal_watch(context, job_id)
            if persisted is not None:
                return {
                    'job_id': job_id,
                    'status': str(persisted.get('status') or 'completed').strip().lower(),
                    'reason': None,
                }
            payload = deps.trace_target(context, ParsedTraceCommand(project=None, target=job_id))
            job = payload.get('job') if isinstance(payload, dict) else None
            status = str(job.get('status') or '').strip().lower() if isinstance(job, dict) else ''
            if status not in {'completed', 'failed', 'cancelled', 'timed_out'}:
                continue
            decision = job.get('terminal_decision') if isinstance(job, dict) else None
            if deps.delegated_callback_pending(context, job_id) or _delegated_job_waits_for_continuation(decision):
                continue
            reason = str(decision.get('reason') or '') if isinstance(decision, dict) else ''
            return {'job_id': job_id, 'status': status, 'reason': reason or None}
        deps.sleep(poll_interval)


def _delegated_job_waits_for_continuation(decision) -> bool:
    return isinstance(decision, dict) and bool(
        decision.get('delegated') or decision.get('chain_edge_id')
    )


def _prepare_immaculate_activation(
    context,
    deps,
    *,
    activation_id: str,
    target: str,
    role: str,
    reason: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        'schema_version': 1,
        'record_type': 'ccb_immaculate_activation_freshness',
        'activation_id': activation_id,
        'target': target,
        'role': role,
        'reason': reason,
        'required': True,
        'freshness_mechanism': 'provider_native_clear_before_ask',
        'created_at': _utc_now(),
    }
    try:
        summary = deps.clear_agent_context(
            context,
            ParsedClearCommand(project=None, agent_names=(target,)),
        )
    except Exception as exc:
        payload.update(
            {
                'status': 'unavailable',
                'reason_detail': str(exc)[:300],
            }
        )
        return payload
    payload['clear_summary'] = _compact_clear_summary(summary)
    payload['status'] = _clear_status_for_target(summary, target)
    return payload


def _compact_clear_summary(summary: object) -> dict[str, object]:
    if not isinstance(summary, dict):
        return {'status': 'unknown', 'raw_type': type(summary).__name__}
    compact: dict[str, object] = {
        'status': summary.get('status'),
    }
    results = summary.get('results')
    if isinstance(results, list):
        compact['results'] = [
            {
                key: item.get(key)
                for key in ('agent', 'status', 'reason', 'pane_id', 'command')
                if isinstance(item, dict) and item.get(key) is not None
            }
            for item in results
            if isinstance(item, dict)
        ]
    return compact


def _clear_status_for_target(summary: object, target: str) -> str:
    if not isinstance(summary, dict):
        return 'unknown'
    results = summary.get('results')
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            if str(item.get('agent') or '').strip() == target:
                status = str(item.get('status') or '').strip()
                return status or 'unknown'
    status = str(summary.get('status') or '').strip()
    return status or 'unknown'


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _payload_ask_job_id(payload: dict[str, object]) -> str | None:
    ask = payload.get('ask') if isinstance(payload.get('ask'), dict) else None
    if not ask:
        return None
    job_id = str(ask.get('job_id') or '').strip()
    return job_id or None


def _payload_wait_job_id(payload: dict[str, object]) -> str | None:
    ask_job_id = _payload_ask_job_id(payload)
    if ask_job_id:
        return ask_job_id
    if (
        str(payload.get('action') or '').strip() == 'role_output_pending'
        and str(payload.get('loop_runner_status') or '').strip() == 'pending'
    ):
        job_id = str(payload.get('job_id') or '').strip()
        if job_id:
            return job_id
    return None


def _payload_wait_job_ids(payload: dict[str, object]) -> list[str]:
    values = payload.get('pending_job_ids')
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        job_id = str(value or '').strip()
        if job_id and job_id not in result:
            result.append(job_id)
    return result


def _auto_should_stop(payload: dict[str, object]) -> bool:
    action = str(payload.get('action') or '').strip()
    status = str(payload.get('loop_runner_status') or '').strip()
    if action in {'reconciled_detail_ready', 'settled_planner_terminal_status_constraint'}:
        return True
    if action in {'activated_orchestrator', 'activated_planner', 'activated_task_detailer', 'activated_plan_reviewer'}:
        return False
    if action == 'ran_one_round':
        round_result = str(payload.get('round_result') or '').strip()
        task_status = str(payload.get('task_status') or '').strip()
        return not (round_result == 'pass' and task_status == 'done')
    if action == 'multi_workgroup_execution_pending':
        return not _payload_wait_job_ids(payload)
    if action in {
        'imported_planner_task_authority',
        'imported_orchestration_notes',
        'imported_macro_adjustment_request',
        'imported_blocker_evidence',
    }:
        return False
    return status in {'idle', 'paused', 'blocked', 'terminal'}


def _auto_step(payload: dict[str, object]) -> dict[str, object]:
    step = {
        'loop_runner_status': payload.get('loop_runner_status'),
        'action': payload.get('action'),
        'task_id': payload.get('task_id'),
        'task_status': payload.get('task_status'),
        'next_activation': payload.get('next_activation'),
        'reason': payload.get('reason'),
    }
    job_id = _payload_ask_job_id(payload)
    if job_id:
        step['ask_job_id'] = job_id
        ask = payload.get('ask') if isinstance(payload.get('ask'), dict) else {}
        step['ask_target'] = ask.get('target')
    source_job_id = str(payload.get('job_id') or '').strip()
    if source_job_id:
        step['job_id'] = source_job_id
    retry_source_job_id = str(payload.get('retry_source_job_id') or '').strip()
    if retry_source_job_id:
        step['retry_source_job_id'] = retry_source_job_id
    retry_successor_job_id = str(payload.get('retry_successor_job_id') or '').strip()
    if retry_successor_job_id:
        step['retry_successor_job_id'] = retry_successor_job_id
    if payload.get('round_result') is not None:
        step['round_result'] = payload.get('round_result')
        step['round_result_source'] = payload.get('round_result_source')
    if payload.get('scheduler_action') is not None:
        step['scheduler_action'] = payload.get('scheduler_action')
    pending_job_ids = _payload_wait_job_ids(payload)
    if pending_job_ids:
        step['pending_job_ids'] = pending_job_ids
    if payload.get('release') is not None:
        release = payload.get('release') if isinstance(payload.get('release'), dict) else {}
        step['released_count'] = release.get('released_count')
        step['retained_count'] = release.get('retained_count')
    return {key: value for key, value in step.items() if value is not None}


def _auto_payload(
    context,
    *,
    status: str,
    action: str,
    steps: list[dict[str, object]],
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = {
        'schema_version': 1,
        'record_type': 'ccb_loop_runner_auto',
        'loop_runner_status': status,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': action,
        'steps': steps,
        'step_count': len(steps),
    }
    if extra:
        payload.update(extra)
    return payload


def _round_result(payload: dict[str, object]) -> tuple[str, str]:
    structured = str(payload.get('round_result') or '').strip().lower()
    if structured:
        mapping = {
            'pass': 'pass',
            'partial': 'partial',
            'replan_required': 'replan_required',
            'blocked': 'blocked',
            'global_blocker': 'blocked',
        }
        if structured not in mapping:
            known = ', '.join(('blocked', 'partial', 'pass', 'replan_required'))
            raise RuntimeError(f'unknown round result {structured!r}; expected one of: {known}')
        source = str(payload.get('round_result_source') or '').strip() or 'round_payload'
        return mapping[structured], source
    declared, source_field = _declared_round_result(payload)
    if declared is not None:
        source = 'round_checker_reply' if source_field == _LEGACY_ROUND_CHECKER_FIELD else 'round_reviewer_reply'
        return declared, source
    if str(payload.get('loop_run_status') or '') == 'ok':
        if isinstance(payload.get(_ROUND_REVIEWER_FIELD), dict):
            return 'blocked', 'missing_round_reviewer_result'
        return 'blocked', 'missing_round_checker_result'
    return 'blocked', 'loop_run_status'


def _declared_round_result(payload: dict[str, object]) -> tuple[str | None, str]:
    reviewer = payload.get(_ROUND_REVIEWER_FIELD) if isinstance(payload.get(_ROUND_REVIEWER_FIELD), dict) else {}
    source_field = _ROUND_REVIEWER_FIELD
    if not reviewer and isinstance(payload.get(_LEGACY_ROUND_CHECKER_FIELD), dict):
        reviewer = payload[_LEGACY_ROUND_CHECKER_FIELD]
        source_field = _LEGACY_ROUND_CHECKER_FIELD
    reply = str(reviewer.get('reply') or '')
    mapping = {
        'pass': 'pass',
        'partial': 'partial',
        'replan_required': 'replan_required',
        'blocked': 'blocked',
        'global_blocker': 'blocked',
    }
    for raw_line in reply.splitlines():
        line = raw_line.strip().lower().lstrip('-').strip()
        if not line.startswith('round result:'):
            continue
        value = line.split(':', 1)[1].strip().split()[0].strip('`.,;')
        if value not in mapping:
            known = ', '.join(('blocked', 'partial', 'pass', 'replan_required'))
            raise RuntimeError(f'unknown round result {value!r}; expected one of: {known}')
        return mapping[value], source_field
    return None, source_field


def _round_report_path(payload: dict[str, object]) -> str:
    paths = payload.get('paths') if isinstance(payload.get('paths'), dict) else {}
    path = str(paths.get('round') or '').strip()
    if not path:
        raise RuntimeError('loop runner cannot import round result without round path')
    if not Path(path).is_file():
        raise RuntimeError(f'loop runner round report is missing: {path}')
    return path


def _compact_plan_payload(payload: dict[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {}
    return {
        'action': payload.get('action'),
        'task_id': payload.get('task_id'),
        'status': payload.get('status'),
        'plan_slug': payload.get('plan_slug'),
        'task_root': payload.get('task_root'),
        'idempotent': payload.get('idempotent'),
    }


def _compact_topology_payload(payload: object) -> dict[str, object]:
    topology = payload if isinstance(payload, dict) else {}
    commit = topology.get('commit') if isinstance(topology.get('commit'), dict) else {}
    reconcile = commit.get('reconcile') if isinstance(commit.get('reconcile'), dict) else {}
    status = topology.get('status') if isinstance(topology.get('status'), dict) else {}
    return {
        'dispatch_source': 'ask_first_mount_topology',
        'proposal_source_path': topology.get('proposal_source_path'),
        'proposal_path': (topology.get('propose') or {}).get('proposal_path') if isinstance(topology.get('propose'), dict) else None,
        'desired_path': commit.get('desired_path'),
        'observed_path': reconcile.get('observed_path') or status.get('observed_path'),
        'status': status.get('loop_topology_status'),
        'agent_count': reconcile.get('agent_count'),
        'released_count': (topology.get('release') or {}).get('released_count') if isinstance(topology.get('release'), dict) else None,
        'retained_count': (topology.get('release') or {}).get('retained_count') if isinstance(topology.get('release'), dict) else None,
    }


def _compact_release_payload(payload: dict[str, object]) -> dict[str, object]:
    return {
        'loop_topology_status': payload.get('loop_topology_status'),
        'loop_id': payload.get('loop_id'),
        'desired_path': payload.get('desired_path'),
        'observed_path': payload.get('observed_path'),
        'released_count': payload.get('released_count'),
        'retained_count': payload.get('retained_count'),
        'released_agents': payload.get('released_agents'),
    }


def _record_round_import(
    round_payload: dict[str, object],
    *,
    imported: dict[str, object],
    release: dict[str, object],
) -> None:
    round_payload['authority_import'] = _compact_plan_payload(imported)
    round_payload['release'] = _compact_release_payload(release)
    path_text = _round_json_path(round_payload)
    if path_text:
        atomic_write_json(Path(path_text), round_payload)


def _round_json_path(payload: dict[str, object]) -> str:
    paths = payload.get('paths') if isinstance(payload.get('paths'), dict) else {}
    return str(paths.get('round_json') or '').strip()


def _next_activation(status: object) -> str:
    value = str(status or '')
    if value == 'done':
        return 'stop'
    if value in {'ready_for_orchestration', 'ready'}:
        return 'orchestrator'
    if value in {'partial', 'replan_required'}:
        return 'planner'
    if value == 'blocked':
        return 'terminal'
    return 'inspect'


def _orchestrator_activation_packet(
    context,
    record: dict[str, object],
    *,
    activation_id: str,
    reason: str,
    effective_capacity_snapshot: dict[str, object],
) -> dict[str, object]:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    task_root = Path(context.project.project_root) / str(record.get('task_root') or '')
    refs = {
        kind: artifact.get('path')
        for kind, artifact in sorted(artifacts.items())
        if kind in {'task_packet', 'execution_contract', 'orchestration_notes'}
        and isinstance(artifact, dict)
        and artifact.get('path')
    }
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_orchestrator_activation',
        'activation_id': activation_id,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'task_id': record.get('task_id'),
        'task_revision': task_revision(record),
        'task_status': record.get('status'),
        'action': 'activate_orchestrator',
        'reason_for_activation': reason,
        'required_next_output': (
            'reply-only route decision, compact orchestration notes, and an orchestration bundle candidate '
            'for Config V3 execution routes or any decomposed Config V2 execution route'
        ),
        'task_packet_root': str(task_root.relative_to(context.project.project_root)),
        'artifact_refs': refs,
        'compact_artifacts': _compact_artifacts(
            context,
            artifacts,
            refs.keys(),
            content_limit=_INLINE_COMPACT_ARTIFACT_CONTENT_LIMIT,
        ),
        'allowed_routes': _ORCHESTRATOR_ROUTES,
        'effective_capacity_snapshot': effective_capacity_snapshot,
        'expected_bundle_revision': _expected_next_bundle_revision(record),
        'script_write_rules': [
            'Reply only; do not run ccb, ccb_test, artifact import commands, or wrapper commands.',
            'Choose exactly one route: direct_execution, needs_detail, macro_adjustment_request, blocked, or partial_completion.',
            'Provide compact orchestration notes with citations to task_packet and execution_contract refs.',
            'For Config V3 direct_execution or partial_completion, always include one fenced JSON orchestration_bundle candidate, including one-node tasks.',
            'Config V2 route-only compatibility may omit a candidate only for one deterministic workgroup.',
            'Select the smallest workgroup count justified by task complexity, cutability, independent scopes, and effective capacity; capacity is a ceiling, not a target.',
            'Supervisor/script-owned import validates and records orchestration_notes, work packets, and orchestration_bundle; provider text is not authority.',
            'Do not edit task status, index, current_loop, runtime topology, or task artifacts directly.',
            'Do not rely on provider reply text as durable route/status authority.',
            'Do not start task_detailer, worker, reviewer, loop_run_once, or topology dispatch from this activation.',
        ],
        'stop_limits': [
            'one orchestrator activation per loop runner --once',
            'no recursive detail or execution activation inside orchestrator triage',
            'artifact links preferred over pasted runtime logs',
        ],
    }


def _planner_activation_packet(
    context,
    record: dict[str, object],
    *,
    activation_id: str,
    action: str,
    reason: str,
) -> dict[str, object]:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    task_root = Path(context.project.project_root) / str(record.get('task_root') or '')
    round_refs = [
        artifact
        for kind, artifact in sorted(artifacts.items())
        if str(kind).startswith('round_') and isinstance(artifact, dict)
    ]
    activation = {
        'schema_version': 1,
        'record_type': 'ccb_loop_planner_activation',
        'activation_id': activation_id,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'task_id': record.get('task_id'),
        'task_revision': task_revision(record),
        'task_status': record.get('status'),
        'action': action,
        'reason_for_activation': reason,
        'required_next_output': 'plan brief, task-packet artifacts, and readiness recommendation',
        'plan_brief_ref': str((Path(context.project.project_root) / str(record.get('plan_root') or '') / 'brief.md').relative_to(context.project.project_root)),
        'task_packet_root': str(task_root.relative_to(context.project.project_root)),
        'artifact_refs': {
            kind: artifact.get('path')
            for kind, artifact in sorted(artifacts.items())
            if isinstance(artifact, dict) and artifact.get('path')
        },
        'round_evidence_refs': tuple(
            {
                'kind': artifact.get('kind'),
                'path': artifact.get('path'),
                'round_result': artifact.get('round_result'),
                'loop_id': artifact.get('loop_id'),
            }
            for artifact in round_refs
        ),
        'open_question_refs': _planner_question_refs(context, record),
        'script_write_rules': [
            'Reply only; do not run ccb, ccb_test, artifact import commands, or wrapper commands.',
            'Return plan brief, macro task-packet artifacts, readiness recommendation, and blocker reports for supervisor-owned import.',
            'Planner may propose brief and macro task-packet artifacts; detail bodies belong to task_detailer.',
            'Supervisor/runner scripts own authoritative writes and route/status imports.',
            'Do not edit task status, index, current_loop, runtime topology, or task artifacts directly.',
            'Return needs_clarification, blocked, not_ready, or ready instead of lowering acceptance criteria.',
        ],
        'stop_limits': [
            'one planner activation per loop runner --once',
            'no recursive execution inside planner activation',
            'artifact links preferred over pasted runtime logs',
        ],
    }
    terminal_authority = detail_ready_stop_contract_authority(
        record,
        project_root=Path(context.project.project_root),
    )
    if terminal_authority is not None:
        activation['terminal_status_constraint'] = {
            'schema_version': 1,
            'status': 'detail_ready',
            'basis': 'verified_detail_ready_stop_contract',
            'task_id': record.get('task_id'),
            'task_revision': terminal_authority['task_revision'],
            'state_version': terminal_authority['state_version'],
            'authority_digest': terminal_authority['authority_digest'],
            'basis_digest': terminal_authority['basis_digest'],
            'required_reason': reason,
        }
    return activation


def _task_detailer_activation_packet(
    context,
    record: dict[str, object],
    *,
    activation_id: str,
    reason: str,
) -> dict[str, object]:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    task_root = Path(context.project.project_root) / str(record.get('task_root') or '')
    plan_root = Path(context.project.project_root) / str(record.get('plan_root') or '')
    compact_artifacts = _compact_artifacts(
        context,
        artifacts,
        ('task_packet', 'execution_contract', 'orchestration_notes'),
        content_limit=_INLINE_COMPACT_ARTIFACT_CONTENT_LIMIT,
    )
    detail_ready_stop_contract = detail_ready_stop_contract_match(
        record,
        project_root=Path(context.project.project_root),
    )
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_task_detailer_activation',
        'activation_id': activation_id,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'task_id': record.get('task_id'),
        'task_revision': task_revision(record),
        'task_status': record.get('status'),
        'action': 'activate_task_detailer',
        'reason_for_activation': reason,
        'required_next_output': 'task-scoped detail docs, stable summary backfill, detail packet, and detail readiness',
        'plan_brief_ref': str((plan_root / 'brief.md').relative_to(context.project.project_root)),
        'task_packet_root': str(task_root.relative_to(context.project.project_root)),
        'detail_root': str((task_root / 'details').relative_to(context.project.project_root)),
        'artifact_refs': {
            kind: artifact.get('path')
            for kind, artifact in sorted(artifacts.items())
            if isinstance(artifact, dict) and artifact.get('path')
        },
        'compact_artifacts': compact_artifacts,
        'detail_ready_stop_contract': detail_ready_stop_contract,
        'script_write_rules': [
            'Do not edit roadmap, decisions, open questions, task status, index, current_loop, runtime capacity, or tmux state directly.',
            'Return detail_design, detail_summary, and detail_packet artifacts for script import.',
            'Return macro-adjustment-request as an artifact/ref; do not apply it as planner authority.',
            'Do not start worker/checker/orchestrator execution from this activation.',
        ],
        'stop_limits': [
            'one task_detailer activation per loop runner --once',
            'no recursive execution inside detail activation',
            'detail links and stable summary preferred over planner-owned detail body',
        ],
    }


def _plan_reviewer_activation_packet(
    context,
    record: dict[str, object],
    *,
    activation_id: str,
    reason: str,
) -> dict[str, object]:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    task_root = Path(context.project.project_root) / str(record.get('task_root') or '')
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_plan_reviewer_activation',
        'activation_id': activation_id,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'task_id': record.get('task_id'),
        'task_revision': task_revision(record),
        'task_status': record.get('status'),
        'action': 'activate_plan_reviewer',
        'reason_for_activation': reason,
        'required_next_output': 'review artifact and readiness recommendation',
        'task_packet_root': str(task_root.relative_to(context.project.project_root)),
        'artifact_refs': {
            kind: artifact.get('path')
            for kind, artifact in sorted(artifacts.items())
            if isinstance(artifact, dict) and artifact.get('path')
        },
        'detail_refs': {
            kind: artifact.get('path')
            for kind, artifact in sorted(artifacts.items())
            if isinstance(artifact, dict) and str(kind).startswith('detail_') and artifact.get('path')
        },
        'script_write_rules': [
            'Reply only; do not run ccb, ccb_test, artifact import commands, or wrapper commands.',
            'Return the review artifact and readiness recommendation for supervisor-owned import.',
            'Supervisor/runner scripts own review artifact import and task status transitions.',
            'Do not edit task status, index, current_loop, runtime topology, or task artifacts directly.',
            'Return not_ready, needs_clarification, blocked, or ready without lowering acceptance criteria.',
        ],
        'stop_limits': [
            'one plan_reviewer activation per loop runner --once',
            'no recursive execution inside review activation',
            'artifact links preferred over pasted runtime logs',
        ],
    }


def _question_refs(artifacts: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for kind, artifact in sorted(artifacts.items()):
        if 'question' not in str(kind):
            continue
        if isinstance(artifact, dict) and artifact.get('path'):
            refs.append(str(artifact['path']))
    return refs


def _consume_existing_activation_for_task(
    context,
    command,
    deps,
    *,
    task_id: str,
    target: str,
    record: dict[str, object],
) -> dict[str, object] | None:
    task_id = str(task_id or '').strip()
    target = str(target or '').strip()
    if not task_id or target not in {'planner', 'orchestrator', 'task_detailer'}:
        return None
    for activation_path, activation in _iter_activation_records(context):
        if str(activation.get('task_id') or '').strip() != task_id:
            continue
        activation_revision = activation.get('task_revision')
        if activation_revision is not None and activation_revision != task_revision(record):
            continue
        ask = activation.get('ask') if isinstance(activation.get('ask'), dict) else {}
        submission = activation.get('submission') if isinstance(activation.get('submission'), dict) else {}
        topology = activation.get('topology') if isinstance(activation.get('topology'), dict) else {}
        submission_target = str(submission.get('target') or topology.get('target') or '').strip()
        if (
            not ask
            and submission_target == target
            and str(submission.get('status') or '') in {'prepared', 'unknown'}
        ):
            return {
                'schema_version': 1,
                'record_type': 'ccb_loop_runner_once',
                'loop_runner_status': 'paused',
                'project_id': context.project.project_id,
                'project_root': str(context.project.project_root),
                'action': 'activation_submission_unknown',
                'reason': 'activation ask has no receipt authority; submission outcome requires manual audit',
                'task_id': task_id,
                'task_status': record.get('status'),
                'activation_id': activation.get('activation_id'),
                'activation_path': str(activation_path),
                'submission': submission,
                'next_activation': 'inspect_activation_submission_authority',
            }
        if str(ask.get('target') or '').strip() != target:
            continue
        job_id = str(ask.get('job_id') or '').strip()
        if not job_id:
            continue
        if _activation_satisfied_by_task_record(activation, target=target, record=record, job_id=job_id):
            continue
        consume_command = replace(command, role_job_id=job_id, task_id=task_id, consume_role_output=True)
        payload = deps.consume_explicit_role_output(context, consume_command, deps.services)
        payload = _release_consumed_activation_topology(context, deps, payload)
        if isinstance(payload, dict):
            payload.setdefault('activation_id', activation.get('activation_id'))
            payload.setdefault('activation_path', str(activation_path))
            payload.setdefault('task_id', task_id)
        return payload
    return None


def _activation_satisfied_by_task_record(
    activation: dict[str, object],
    *,
    target: str,
    record: dict[str, object],
    job_id: str,
) -> bool:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    if target == 'orchestrator':
        notes = artifacts.get('orchestration_notes') if isinstance(artifacts, dict) else None
        if not isinstance(notes, dict):
            return False
        reason = str(activation.get('reason_for_activation') or '')
        if reason == 'orchestrator_route_needs_detail_detail_ready':
            return _artifact_actor_job_id(notes) == job_id
        return True
    if target == 'planner':
        return all(isinstance(artifacts.get(kind), dict) for kind in ('task_packet', 'execution_contract'))
    if target == 'task_detailer':
        status = str(record.get('status') or '')
        if status == 'blocked':
            return isinstance(artifacts.get('blocker_evidence'), dict)
        return status == 'detail_ready' and all(
            isinstance(artifacts.get(kind), dict)
            for kind in ('detail_design', 'detail_summary', 'detail_packet')
        )
    return False


def _artifact_actor_job_id(artifact: object) -> str:
    if not isinstance(artifact, dict):
        return ''
    actor = artifact.get('actor') if isinstance(artifact.get('actor'), dict) else {}
    return str(actor.get('job_id') or '')


def _iter_activation_records(context):
    activations_dir = Path(context.paths.runtime_state_root) / 'runtime' / 'loops' / 'activations'
    if not activations_dir.is_dir():
        return
    for path in sorted(activations_dir.glob('act-*.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield path, payload


def _planner_question_refs(context, record: dict[str, object]) -> dict[str, object] | tuple[str, ...]:
    task_id = str(record.get('task_id') or '').strip()
    if task_id:
        refs = question_refs(context, task_id)
        if int(refs.get('artifact_count') or 0) > 0:
            return refs
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    return tuple(_question_refs(artifacts))


def _compact_artifacts(
    context,
    artifacts: dict[str, object],
    kinds,
    *,
    content_limit: int = 4000,
) -> dict[str, dict[str, object]]:
    root = Path(context.project.project_root)
    compact: dict[str, dict[str, object]] = {}
    for kind in sorted(kinds):
        artifact = artifacts.get(kind) if isinstance(artifacts, dict) else None
        if not isinstance(artifact, dict):
            continue
        relative_path = str(artifact.get('path') or '').strip()
        if not relative_path:
            continue
        item: dict[str, object] = {'path': relative_path}
        try:
            text = (root / relative_path).read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            text = ''
        if text:
            item['content'] = _compact_text_excerpt(text, content_limit)
            item['truncated'] = len(text) > content_limit
        compact[kind] = item
    return compact


def _compact_text_excerpt(text: str, limit: int) -> str:
    if limit <= 0:
        return ''
    if len(text) <= limit:
        return text
    if limit <= 16:
        return text[:limit]
    marker = '\n...\n'
    head_len = max(1, (limit - len(marker)) // 2)
    tail_len = max(1, limit - len(marker) - head_len)
    return f'{text[:head_len]}{marker}{text[-tail_len:]}'


def _orchestrator_message(activation: dict[str, object]) -> str:
    bundle_revision = activation.get('expected_bundle_revision')
    return (
        'Role: ccb_orchestrator\n'
        f"Activation id: {activation.get('activation_id')}\n"
        f"Task: {activation.get('task_id')}\n"
        f"Status: {activation.get('task_status')}\n"
        f"Reason: {activation.get('reason_for_activation')}\n"
        f"Task packet root: {activation.get('task_packet_root')}\n"
        f"Artifact refs: {activation.get('artifact_refs')}\n"
        f"Compact artifacts: {activation.get('compact_artifacts')}\n"
        f"Effective capacity snapshot: {activation.get('effective_capacity_snapshot')}\n"
        f"Expected bundle revision: {bundle_revision}\n"
        f"Allowed routes: {', '.join(_ORCHESTRATOR_ROUTES)}\n\n"
        'Required reply-only output:\n'
        '- route: <one of direct_execution|needs_detail|macro_adjustment_request|blocked|partial_completion>\n'
        '- orchestration_notes: compact rationale and citations to task_packet and execution_contract refs\n'
        '- for every Config V3 execution route, add an orchestration_bundle candidate; Config V2 may omit it only for one deterministic workgroup:\n'
        '  orchestration_bundle:\n'
        '  ```json\n'
        f'  {{"schema":"ccb.loop.orchestration_bundle_candidate.v1","task_id":"<task-id>","bundle_revision":{bundle_revision},"selection":{{"workgroup_count":"<integer within effective capacity>","complexity":"<atomic|bounded|complex|very_complex>","cutability":"<none|limited|high>","execution_shape":"<single_unit|parallel|serial|mixed_dag>","rationale":"<short semantic reason>"}},"nodes":[...],"integration":{{...}},"policy":{{...}}}}'
        '\n  ```\n'
        '- candidate root fields are exactly schema, task_id, bundle_revision, selection, nodes, integration, and policy\n'
        '- selection.workgroup_count must equal node count; choose the smallest justified count from 1 to 4 without trying to fill capacity\n'
        '- each node must include node_id, workgroup_id, coder/code_reviewer profiles, depends_on, parallel_group, work_packet, allowed_paths, acceptance_refs, verification_refs, and integration_order\n'
        '- independent nodes need disjoint allowed_paths; coupled scopes need explicit depends_on ordering\n\n'
        'Authority boundary:\n'
        '- Reply only; do not run ccb, ccb_test, artifact import commands, or wrapper commands.\n'
        '- Supervisor/script-owned import validates and records orchestration_notes, work packets, and orchestration_bundle.\n'
        '- do not edit task index, status, current_loop, runtime capacity, topology, or task artifacts directly\n'
        '- do not rely on provider reply text as durable route/status authority\n'
        '- do not start task_detailer, worker, reviewer, loop_run_once, or topology dispatch from this activation'
    )


def _expected_next_bundle_revision(record: dict[str, object]) -> int:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    bundle = artifacts.get('orchestration_bundle') if isinstance(artifacts, dict) else None
    if not isinstance(bundle, dict):
        return 1
    value = bundle.get('bundle_revision')
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('existing orchestration_bundle bundle_revision must be a positive integer')
    return value + 1


def _planner_message(activation: dict[str, object]) -> str:
    message = (
        'Role: planner\n'
        f"Activation id: {activation.get('activation_id')}\n"
        f"Task: {activation.get('task_id')}\n"
        f"Status: {activation.get('task_status')}\n"
        f"Reason: {activation.get('reason_for_activation')}\n"
        f"Task packet root: {activation.get('task_packet_root')}\n"
        f"Artifact refs: {activation.get('artifact_refs')}\n"
        f"Open question refs: {activation.get('open_question_refs')}\n"
        f"Round evidence refs: {activation.get('round_evidence_refs')}\n\n"
        'Required next output:\n'
        '- draft or update task-packet artifacts\n'
        '- keep brief.md compact; do not include task_detailer detail bodies\n'
        '- readiness recommendation: ready|needs_clarification|blocked|not_ready\n'
        '- candidate questions only when current-phase user input is blocking\n\n'
        'Authority boundary:\n'
        '- reply only with semantic artifacts, readiness recommendations, and blocker reports\n'
        '- do not run ccb, ccb_test, ccb plan, ccb loop, ccb ask, wrapper commands, or provider/runtime mutation commands\n'
        '- supervisor/runner scripts own authoritative writes and route/status imports\n'
        '- do not edit task index, status, current_loop, runtime capacity, or tmux state directly\n'
        '- do not start worker/checker/orchestrator execution from this activation'
    )
    constraint = activation.get('terminal_status_constraint')
    if not isinstance(constraint, dict):
        return message
    return (
        f'{message}\n\n'
        'Terminal status constraint:\n'
        f'- Controller-verified constraint: {constraint}\n'
        '- Return route `needs_detail`, status_recommendation `detail_ready`, '
        f'and reason `{constraint.get("required_reason")}`.\n'
        '- Return an empty allowed_paths list and no blockers; this preserves the '
        'controller-visible terminal status rather than authorizing execution.\n'
    )


def _task_detailer_message(activation: dict[str, object]) -> str:
    detail_ready_stop_guidance = ''
    if activation.get('detail_ready_stop_contract'):
        detail_ready_stop_guidance = (
            '\nDetail-ready stop contract:\n'
            '- Task artifacts explicitly require the controller-visible stop/status detail_ready for this activation.\n'
            '- Produce the requested detail artifacts and use "detail readiness recommendation: detail_ready" when those artifacts are complete.\n'
            '- Do not downgrade to needs_clarification solely because implementation dispatch remains intentionally out of scope.\n'
        )
    return (
        'Role: task_detailer\n'
        f"Activation id: {activation.get('activation_id')}\n"
        f"Activation evidence: .ccb/runtime/loops/activations/{activation.get('activation_id')}.json\n"
        f"Task: {activation.get('task_id')}\n"
        f"Task revision: {activation.get('task_revision')}\n"
        f"Status: {activation.get('task_status')}\n"
        f"Reason: {activation.get('reason_for_activation')}\n"
        f"Plan brief ref: {activation.get('plan_brief_ref')}\n"
        f"Task packet root: {activation.get('task_packet_root')}\n"
        f"Detail root: {activation.get('detail_root')}\n"
        f"Artifact refs: {activation.get('artifact_refs')}\n"
        f"Compact artifacts: {activation.get('compact_artifacts')}\n"
        f"Detail-ready stop contract: {activation.get('detail_ready_stop_contract')}\n\n"
        'Required next output:\n'
        '- task-scoped detail design, stable brief-update summary, and detail packet manifest\n'
        '- detail result: local_detail_ready|planner_replan_required|needs_clarification|blocked\n'
        '- detail readiness recommendation: detail_ready|planner_replan_required|needs_clarification|blocked\n'
        '- planner_replan_required must use the sole managed direct silent Planner handoff with ccb.detailer.replan_request.v1\n'
        '- the activation record contains the current source Detailer job id required by that request\n'
        f'{detail_ready_stop_guidance}\n'
        'Authority boundary:\n'
        '- reply only with task-scoped detail artifact content and recommendations\n'
        '- do not run ccb, ccb_test, ccb plan, ccb loop, generic ccb ask, wrapper commands, or provider/runtime mutation commands\n'
        '- the only command capability is the versioned direct silent Planner replan handoff; do not chain, wait, watch, or poll\n'
        '- supervisor/runner scripts own detail artifact import and task status transitions\n'
        '- do not edit roadmap, task index, status, current_loop, runtime capacity, or tmux state directly\n'
        '- do not write supervisor import files into the project tree for later self-import\n'
        '- do not start worker/checker/orchestrator execution from this activation'
    )


def _plan_reviewer_message(activation: dict[str, object]) -> str:
    return (
        'Role: plan_reviewer\n'
        f"Activation id: {activation.get('activation_id')}\n"
        f"Task: {activation.get('task_id')}\n"
        f"Status: {activation.get('task_status')}\n"
        f"Reason: {activation.get('reason_for_activation')}\n"
        f"Task packet root: {activation.get('task_packet_root')}\n"
        f"Artifact refs: {activation.get('artifact_refs')}\n\n"
        'Required next output:\n'
        '- review artifact covering ambiguity, risk, acceptance, and verification\n'
        '- readiness recommendation: ready|needs_clarification|blocked|not_ready\n\n'
        'Authority boundary:\n'
        '- reply only with the review artifact and readiness recommendation\n'
        '- do not run ccb, ccb_test, ccb plan, ccb loop, ccb ask, wrapper commands, or provider/runtime mutation commands\n'
        '- supervisor/runner scripts own review artifact import and task status transitions\n'
        '- do not edit task index, status, current_loop, runtime capacity, or tmux state directly\n'
        '- do not start worker/checker/orchestrator execution from this activation'
    )


def _activation_path(context, activation_id: str) -> Path:
    return Path(context.paths.runtime_state_root) / 'runtime' / 'loops' / 'activations' / f'{activation_id}.json'


def _single_job(jobs: tuple[dict, ...], *, target: str) -> dict:
    if len(jobs) != 1:
        raise RuntimeError(f'expected one ask job for {target}; got {len(jobs)}')
    job = dict(jobs[0])
    if not str(job.get('job_id') or ''):
        raise RuntimeError(f'ask job for {target} did not return job_id')
    return job


def _first_job_id(payload: dict[str, object]) -> str:
    for key in ('worker', 'reviewer', 'aggregation', 'round_checker'):
        value = payload.get(key)
        if isinstance(value, dict) and str(value.get('job_id') or '').strip():
            return str(value['job_id'])
    return ''


__all__ = ['loop_runner_auto', 'loop_runner_once']
