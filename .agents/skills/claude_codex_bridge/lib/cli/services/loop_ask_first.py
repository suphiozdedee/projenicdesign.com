from __future__ import annotations

from datetime import datetime, timezone
import filecmp
from io import StringIO
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

from agents.config_loader import load_project_config
from cli.models import ParsedAskCommand, ParsedClearCommand
from storage.atomic import atomic_write_json, atomic_write_text
from storage.locks import file_lock

from .ask import submit_ask, watch_ask_job
from .clear import clear_agent_context
from .loop_execution_scope import (
    declared_allowed_change_paths,
    path_allowed_by_scope,
    safe_relative_path,
    split_declared_paths,
)
from .loop_orchestration_bundle import bundle_summary, load_task_orchestration_bundle
from .loop_effective_capacity import compile_project_effective_capacity_snapshot
from .loop_topology import loop_topology
from .plan_tasks import plan_task, task_execution_text
from .watch_fallback import load_persisted_terminal_watch_payload

WORKER_PROFILE = 'coder'
REVIEWER_PROFILE = 'code_reviewer'
ORCHESTRATOR_TARGET = 'ccb_orchestrator'
ROUND_REVIEWER_TARGET = 'ccb_round_reviewer'
ROUND_REVIEWER_FIELD = 'ccb_round_reviewer'
ROUND_REVIEWER_CORRECTION_PURPOSE = 'ccb_round_reviewer_correction'
LEGACY_ROUND_CHECKER_FIELD = 'round_checker'
RUNNER_ASK_SENDER = 'system'
ORCHESTRATOR_ROLE_ID = 'agentroles.ccb_orchestrator'
ROUND_REVIEWER_ROLE_ID = 'agentroles.ccb_round_reviewer'
MAX_PROMOTED_WORKSPACE_FILES = 50
ROUND_REVIEWER_EVIDENCE_SNIPPET_LIMIT = 4000
TEST_COMMAND_PREFIXES = (
    'test_command:',
    'test command:',
    'verification_command:',
    'verification command:',
)
WORKER_CHANGED_FILE_PREFIXES = (
    'changed_files:',
    'changed files:',
)


class _AskSubmissionError(RuntimeError):
    def __init__(self, *, target: str, purpose: str, stage: str, error: str) -> None:
        super().__init__(error)
        self.target = target
        self.purpose = purpose
        self.stage = stage


class _AskWatchError(RuntimeError):
    def __init__(self, *, target: str, purpose: str, stage: str, job_id: str, error: str) -> None:
        super().__init__(error)
        self.target = target
        self.purpose = purpose
        self.stage = stage
        self.job_id = job_id


class _ImmaculateActivationError(RuntimeError):
    def __init__(self, *, target: str, purpose: str, stage: str, freshness: dict[str, object]) -> None:
        status = str(freshness.get('status') or 'missing')
        detail = str(freshness.get('reason_detail') or '').strip()
        message = f'immaculate activation freshness is not proven for {target}: status={status}'
        if detail:
            message = f'{message}; {detail}'
        super().__init__(message)
        self.target = target
        self.purpose = purpose
        self.stage = stage
        self.freshness = dict(freshness)


def run_ask_first_execution_round(context, command, services=None) -> dict[str, object]:
    deps = _deps(services)
    loop_id = str(getattr(command, 'loop_id', None) or '').strip()
    task_id = str(getattr(command, 'task_id', None) or '').strip()
    if not loop_id:
        raise ValueError('ask-first execution requires loop_id')
    if not task_id:
        raise ValueError('ask-first execution requires task_id')
    timeout = getattr(command, 'timeout_s', None)
    loop_dir = _loop_dir(context, loop_id)
    _ensure_loop_dirs(loop_dir)
    task_text = task_execution_text(context, task_id)
    task_record = _task_record(deps.plan_task(context, SimpleNamespace(action='task-show', task_id=task_id)))
    orchestration_bundle, bundle_artifact = load_task_orchestration_bundle(
        Path(context.project.project_root),
        task_record,
        capacity_snapshot=deps.effective_capacity_snapshot(context),
    )
    bundle_nodes = orchestration_bundle.get('nodes') if isinstance(orchestration_bundle.get('nodes'), list) else []
    if len(bundle_nodes) != 1:
        raise ValueError(
            f'ask-first compatibility engine requires exactly one orchestration bundle node; got {len(bundle_nodes)}'
        )
    bundle_node = dict(bundle_nodes[0])
    node_id = str(bundle_node.get('node_id') or '')
    bundle_revision = int(orchestration_bundle['bundle_revision'])
    node_work_packet_ref, node_work_packet = _node_work_packet(
        Path(context.project.project_root),
        bundle_node,
    )
    orchestration_bundle_summary = bundle_summary(orchestration_bundle, bundle_artifact)
    artifact_refs = _artifact_refs(task_record)
    project_root_authority_required = _requires_project_root_authority(task_record, task_text)
    orchestrator_agent, _orchestrator_is_configured = _agent_for_role(
        context,
        ORCHESTRATOR_ROLE_ID,
        fallback=ORCHESTRATOR_TARGET,
    )
    round_reviewer_agent, round_reviewer_is_configured = _agent_for_role(
        context,
        ROUND_REVIEWER_ROLE_ID,
        fallback=ROUND_REVIEWER_TARGET,
    )
    worker_agent = f'loop-{loop_id}-{WORKER_PROFILE}-1'
    reviewer_agent = f'loop-{loop_id}-{REVIEWER_PROFILE}-1'
    stage_state = _load_stage_state(loop_dir, loop_id=loop_id, task_id=task_id)
    state_artifacts = _state_current_artifacts(stage_state)
    node_artifacts = _state_node_artifacts(stage_state, node_id=node_id)
    started_at = str(stage_state.get('round_started_at') or '').strip() or _utc_now()
    _append_event(
        loop_dir,
        loop_id=loop_id,
        kind='ask_first_round_resumed' if stage_state else 'ask_first_round_started',
        payload={'task_id': task_id},
    )
    topology = _apply_mount_topology(
        context,
        deps,
        loop_dir=loop_dir,
        loop_id=loop_id,
        worker_agent=worker_agent,
        reviewer_agent=reviewer_agent,
        round_reviewer_agent=None if round_reviewer_is_configured else round_reviewer_agent,
    )
    topology_failure = _topology_blocker(topology)
    if topology_failure is not None:
        return _write_round_payload(
            context,
            loop_dir=loop_dir,
            loop_id=loop_id,
            task_id=task_id,
            started_at=started_at,
            status='blocked',
            worker_agent=worker_agent,
            reviewer_agent=reviewer_agent,
            artifact_refs=artifact_refs,
            topology=topology,
            worker={},
            reviewer={},
            rework={},
            orchestrator={},
            round_reviewer={},
            round_result='blocked',
            round_result_source=str(topology_failure.get('source') or 'topology_not_ready'),
            failure=topology_failure,
            orchestration_bundle=orchestration_bundle_summary,
            node_id=node_id,
        )

    worker = _artifact_dict(node_artifacts.get('worker'))
    reviewer = _artifact_dict(node_artifacts.get('reviewer'))
    rework = _rework_artifacts(node_artifacts.get('rework'))
    orchestrator: dict[str, object] = {}
    round_reviewer = _artifact_dict(state_artifacts.get(ROUND_REVIEWER_FIELD))
    authority_update = _optional_artifact_dict(state_artifacts.get('authority_update'))
    project_root_test: dict[str, object] | None = None
    resume_progress = {'consumed_persisted_terminal': False}

    def pending_payload(pending: dict[str, object]) -> dict[str, object]:
        return _write_pending_payload(
            context,
            loop_dir=loop_dir,
            loop_id=loop_id,
            task_id=task_id,
            started_at=started_at,
            worker_agent=worker_agent,
            reviewer_agent=reviewer_agent,
            orchestrator_agent=orchestrator_agent,
            round_reviewer_agent=round_reviewer_agent,
            artifact_refs=artifact_refs,
            topology=topology,
            worker=worker,
            reviewer=reviewer,
            rework=rework,
            orchestrator=orchestrator,
            round_reviewer=round_reviewer,
            pending=pending,
            authority_update=_public_authority_update(authority_update),
            orchestration_bundle=orchestration_bundle_summary,
            node_id=node_id,
        )

    stage = 'worker_ask'
    try:
        if not worker:
            worker = _submit_and_watch(
                context,
                deps,
                loop_dir=loop_dir,
                loop_id=loop_id,
                target=worker_agent,
                sender=orchestrator_agent,
                purpose='worker',
                bundle_revision=bundle_revision,
                node_id=node_id,
                attempt=1,
                task_id=f'{loop_id}-worker',
                message=_worker_message(
                    loop_id=loop_id,
                    task_id=task_id,
                    task_text=task_text,
                    artifact_refs=artifact_refs,
                    node_id=node_id,
                    work_packet_ref=node_work_packet_ref,
                    work_packet=node_work_packet,
                ),
                timeout=timeout,
                defer_observation=bool(resume_progress['consumed_persisted_terminal']),
            )
            if worker.get('watch_source') == 'persisted_terminal':
                resume_progress['consumed_persisted_terminal'] = True
        worker_pending = _round_pending(worker)
        if worker_pending is not None:
            return pending_payload(worker_pending)
        worker_status_failure = _round_status_failure(worker)
        if worker_status_failure is not None:
            return _write_round_payload(
                context,
                loop_dir=loop_dir,
                loop_id=loop_id,
                task_id=task_id,
                started_at=started_at,
                status='blocked',
                worker_agent=worker_agent,
                reviewer_agent=reviewer_agent,
                artifact_refs=artifact_refs,
                topology=topology,
                worker=worker,
                reviewer=reviewer,
                rework=rework,
                orchestrator=orchestrator,
                round_reviewer=round_reviewer,
                round_result='blocked',
                round_result_source=str(worker_status_failure.get('source') or 'ask_job_incomplete'),
                failure=worker_status_failure,
                orchestration_bundle=orchestration_bundle_summary,
                node_id=node_id,
            )
        if project_root_authority_required and authority_update is None:
            promoted, authority_failure = _promote_project_root_authority(
                context,
                round_result='pass',
                worker_agent=worker_agent,
                task_text=task_text,
                worker=worker,
            )
            if authority_failure is not None:
                return _write_round_payload(
                    context,
                    loop_dir=loop_dir,
                    loop_id=loop_id,
                    task_id=task_id,
                    started_at=started_at,
                    status='blocked',
                    worker_agent=worker_agent,
                    reviewer_agent=reviewer_agent,
                    artifact_refs=artifact_refs,
                    topology=topology,
                    worker=worker,
                    reviewer=reviewer,
                    rework=rework,
                    orchestrator=orchestrator,
                    round_reviewer=round_reviewer,
                    round_result='blocked',
                    round_result_source=str(authority_failure.get('source') or 'round_authority'),
                    failure=authority_failure,
                    orchestration_bundle=orchestration_bundle_summary,
                    node_id=node_id,
            )
            authority_update = _merge_authority_updates(authority_update, promoted)
        stage = 'reviewer_ask'
        if not reviewer:
            reviewer = _submit_and_watch(
                context,
                deps,
                loop_dir=loop_dir,
                loop_id=loop_id,
                target=reviewer_agent,
                sender=worker_agent,
                purpose='reviewer',
                bundle_revision=bundle_revision,
                node_id=node_id,
                attempt=1,
                task_id=f'{loop_id}-reviewer',
                message=_reviewer_message(
                    loop_id=loop_id,
                    task_id=task_id,
                    task_text=task_text,
                    artifact_refs=artifact_refs,
                    node_id=node_id,
                    work_packet_ref=node_work_packet_ref,
                    work_packet=node_work_packet,
                    worker=worker,
                    authority_update=authority_update,
                ),
                timeout=timeout,
                defer_observation=bool(resume_progress['consumed_persisted_terminal']),
            )
            if reviewer.get('watch_source') == 'persisted_terminal':
                resume_progress['consumed_persisted_terminal'] = True
        reviewer_pending = _round_pending(reviewer)
        if reviewer_pending is not None:
            return pending_payload(reviewer_pending)
        if _reviewer_requires_rework(reviewer):
            stage = 'worker_rework_ask'
            worker_rework = rework.get('worker_rework') or {}
            if not worker_rework:
                worker_rework = _submit_and_watch(
                    context,
                    deps,
                    loop_dir=loop_dir,
                    loop_id=loop_id,
                    target=worker_agent,
                    sender=reviewer_agent,
                    purpose='worker_rework',
                    bundle_revision=bundle_revision,
                    node_id=node_id,
                    attempt=1,
                    task_id=f'{loop_id}-worker-rework',
                    message=_worker_rework_message(
                        loop_id=loop_id,
                        task_id=task_id,
                        task_text=task_text,
                        artifact_refs=artifact_refs,
                        node_id=node_id,
                        work_packet_ref=node_work_packet_ref,
                        work_packet=node_work_packet,
                        worker=worker,
                        reviewer=reviewer,
                    ),
                    timeout=timeout,
                    defer_observation=bool(resume_progress['consumed_persisted_terminal']),
                )
                rework['worker_rework'] = worker_rework
                if worker_rework.get('watch_source') == 'persisted_terminal':
                    resume_progress['consumed_persisted_terminal'] = True
            worker_rework_pending = _round_pending(worker_rework)
            if worker_rework_pending is not None:
                return pending_payload(worker_rework_pending)
            worker_rework_status_failure = _round_status_failure(worker_rework)
            if worker_rework_status_failure is not None:
                _restore_authority_update(
                    context,
                    authority_update,
                    worker_rework_status_failure,
                    reason='worker_rework_not_completed',
                )
                return _write_round_payload(
                    context,
                    loop_dir=loop_dir,
                    loop_id=loop_id,
                    task_id=task_id,
                    started_at=started_at,
                    status='blocked',
                    worker_agent=worker_agent,
                    reviewer_agent=reviewer_agent,
                    artifact_refs=artifact_refs,
                    topology=topology,
                    worker=worker,
                    reviewer=reviewer,
                    rework=rework,
                    orchestrator=orchestrator,
                    round_reviewer=round_reviewer,
                    round_result='blocked',
                    round_result_source=str(worker_rework_status_failure.get('source') or 'ask_job_incomplete'),
                    failure=worker_rework_status_failure,
                    authority_update=_public_authority_update(authority_update),
                    orchestration_bundle=orchestration_bundle_summary,
                    node_id=node_id,
                )
            if project_root_authority_required:
                promoted, authority_failure = _promote_project_root_authority(
                    context,
                    round_result='pass',
                    worker_agent=worker_agent,
                    task_text=task_text,
                    worker=worker_rework,
                    allow_noop_verified=authority_update is not None,
                )
                if authority_failure is not None:
                    _restore_authority_update(
                        context,
                        authority_update,
                        authority_failure,
                        reason='worker_rework_promotion_failed',
                    )
                    return _write_round_payload(
                        context,
                        loop_dir=loop_dir,
                        loop_id=loop_id,
                        task_id=task_id,
                        started_at=started_at,
                        status='blocked',
                        worker_agent=worker_agent,
                        reviewer_agent=reviewer_agent,
                        artifact_refs=artifact_refs,
                        topology=topology,
                        worker=worker,
                        reviewer=reviewer,
                        rework=rework,
                        orchestrator=orchestrator,
                        round_reviewer=round_reviewer,
                        round_result='blocked',
                        round_result_source=str(authority_failure.get('source') or 'round_authority'),
                        failure=authority_failure,
                        authority_update=_public_authority_update(authority_update),
                        orchestration_bundle=orchestration_bundle_summary,
                        node_id=node_id,
                    )
                authority_update = _merge_authority_updates(authority_update, promoted)
            stage = 'reviewer_recheck_ask'
            reviewer_recheck = rework.get('reviewer_recheck') or {}
            if not reviewer_recheck:
                reviewer_recheck = _submit_and_watch(
                    context,
                    deps,
                    loop_dir=loop_dir,
                    loop_id=loop_id,
                    target=reviewer_agent,
                    sender=worker_agent,
                    purpose='reviewer_recheck',
                    bundle_revision=bundle_revision,
                    node_id=node_id,
                    attempt=1,
                    task_id=f'{loop_id}-reviewer-recheck',
                    message=_reviewer_recheck_message(
                        loop_id=loop_id,
                        task_id=task_id,
                        task_text=task_text,
                        artifact_refs=artifact_refs,
                        node_id=node_id,
                        work_packet_ref=node_work_packet_ref,
                        work_packet=node_work_packet,
                        worker=worker,
                        reviewer=reviewer,
                        worker_rework=worker_rework,
                        authority_update=authority_update,
                    ),
                    timeout=timeout,
                    defer_observation=bool(resume_progress['consumed_persisted_terminal']),
                )
                rework['reviewer_recheck'] = reviewer_recheck
                if reviewer_recheck.get('watch_source') == 'persisted_terminal':
                    resume_progress['consumed_persisted_terminal'] = True
            reviewer_recheck_pending = _round_pending(reviewer_recheck)
            if reviewer_recheck_pending is not None:
                return pending_payload(reviewer_recheck_pending)
        stage = 'ccb_round_reviewer_ask'
        if not round_reviewer:
            round_reviewer = _submit_and_watch(
                context,
                deps,
                loop_dir=loop_dir,
                loop_id=loop_id,
                target=round_reviewer_agent,
                sender='system',
                purpose=ROUND_REVIEWER_FIELD,
                bundle_revision=bundle_revision,
                node_id='round',
                attempt=1,
                task_id=f'{loop_id}-round-reviewer',
                message=_round_reviewer_message(
                    loop_id=loop_id,
                    task_id=task_id,
                    task_text=task_text,
                    artifact_refs=artifact_refs,
                    worker=worker,
                    reviewer=reviewer,
                    rework=rework,
                    authority_update=authority_update,
                ),
                timeout=timeout,
                defer_observation=bool(resume_progress['consumed_persisted_terminal']),
            )
            if round_reviewer.get('watch_source') == 'persisted_terminal':
                resume_progress['consumed_persisted_terminal'] = True
        round_reviewer_pending = _round_pending(round_reviewer)
        if round_reviewer_pending is not None:
            return pending_payload(round_reviewer_pending)
        provisional_result, provisional_source, provisional_failure = _round_result(
            {'loop_run_status': 'ok', ROUND_REVIEWER_FIELD: round_reviewer}
        )
        if provisional_source in {'missing_round_reviewer_result', 'unknown_round_result'}:
            _append_event(
                loop_dir,
                loop_id=loop_id,
                kind='round_reviewer_result_correction_requested',
                payload={
                    'task_id': task_id,
                    'source': provisional_source,
                    'job_id': round_reviewer.get('job_id'),
                    'target': round_reviewer.get('target'),
                    'unknown_round_result': (
                        provisional_failure.get('unknown_round_result')
                        if isinstance(provisional_failure, dict)
                        else None
                    ),
                },
            )
            corrected_round_reviewer = _submit_and_watch(
                context,
                deps,
                loop_dir=loop_dir,
                loop_id=loop_id,
                target=round_reviewer_agent,
                sender='system',
                purpose=ROUND_REVIEWER_CORRECTION_PURPOSE,
                bundle_revision=bundle_revision,
                node_id='round',
                attempt=2,
                task_id=f'{loop_id}-round-reviewer-correction',
                message=_round_reviewer_correction_message(
                    task_id=task_id,
                    original=round_reviewer,
                    result_source=provisional_source,
                    failure=provisional_failure,
                ),
                timeout=timeout,
                defer_observation=bool(resume_progress['consumed_persisted_terminal']),
            )
            corrected_pending = _round_pending(corrected_round_reviewer)
            if corrected_pending is not None:
                return pending_payload(corrected_pending)
            corrected_round_reviewer['correction_source_job_id'] = round_reviewer.get('job_id')
            corrected_round_reviewer['correction_source_artifact'] = round_reviewer.get('artifact')
            corrected_round_reviewer['correction_source_round_result_source'] = provisional_source
            round_reviewer = corrected_round_reviewer
    except Exception as exc:
        failure = _ask_failure_record(exc, default_stage=stage)
        _restore_authority_update(context, authority_update, failure, reason='ask_failure_after_promotion')
        return _write_round_payload(
            context,
            loop_dir=loop_dir,
            loop_id=loop_id,
            task_id=task_id,
            started_at=started_at,
            status='blocked',
            worker_agent=worker_agent,
            reviewer_agent=reviewer_agent,
            artifact_refs=artifact_refs,
            topology=topology,
            worker=worker,
            reviewer=reviewer,
            rework=rework,
            orchestrator=orchestrator,
            round_reviewer=round_reviewer,
            round_result='blocked',
            round_result_source=str(failure.get('source') or 'ask_failure'),
            failure=failure,
            authority_update=_public_authority_update(authority_update),
            orchestration_bundle=orchestration_bundle_summary,
            node_id=node_id,
        )

    status_items = [worker, reviewer, *rework.values(), round_reviewer]
    status_pending = _round_pending(*status_items)
    if status_pending is not None:
        return pending_payload(status_pending)
    status = _round_status(*status_items)
    status_failure = _round_status_failure(*status_items)
    if status_failure is not None:
        _restore_authority_update(context, authority_update, status_failure, reason='ask_job_incomplete_after_promotion')
        return _write_round_payload(
            context,
            loop_dir=loop_dir,
            loop_id=loop_id,
            task_id=task_id,
            started_at=started_at,
            status='blocked',
            worker_agent=worker_agent,
            reviewer_agent=reviewer_agent,
            artifact_refs=artifact_refs,
            topology=topology,
            worker=worker,
            reviewer=reviewer,
            rework=rework,
            orchestrator=orchestrator,
            round_reviewer=round_reviewer,
            round_result='blocked',
            round_result_source=str(status_failure.get('source') or 'ask_job_incomplete'),
            failure=status_failure,
            authority_update=_public_authority_update(authority_update),
            orchestration_bundle=orchestration_bundle_summary,
            node_id=node_id,
        )
    round_result, round_result_source, failure = _round_result(
        {'loop_run_status': status, ROUND_REVIEWER_FIELD: round_reviewer}
    )
    if failure is not None:
        _restore_authority_update(context, authority_update, failure, reason='round_result_failure_after_promotion')
        status = 'blocked'
    elif round_result != 'pass':
        _restore_authority_update(
            context,
            authority_update,
            None,
            reason=f'non_pass_round_result:{round_result}',
        )
    if failure is None and round_result == 'pass':
        project_root_test, test_failure = _project_root_test_authority(
            context,
            loop_dir=loop_dir,
            task_text=task_text,
        )
        if test_failure is not None:
            _restore_authority_update(
                context,
                authority_update,
                test_failure,
                reason='project_root_test_failed',
            )
            status = 'blocked'
            round_result = 'blocked'
            round_result_source = str(test_failure['source'])
            failure = test_failure
    authority_update = _public_authority_update(authority_update)
    return _write_round_payload(
        context,
        loop_dir=loop_dir,
        loop_id=loop_id,
        task_id=task_id,
        started_at=started_at,
        status=status,
        worker_agent=worker_agent,
        reviewer_agent=reviewer_agent,
        orchestrator_agent=orchestrator_agent,
        round_reviewer_agent=round_reviewer_agent,
        artifact_refs=artifact_refs,
        topology=topology,
        worker=worker,
        reviewer=reviewer,
        rework=rework,
        orchestrator=orchestrator,
        round_reviewer=round_reviewer,
        round_result=round_result,
        round_result_source=round_result_source,
        failure=failure,
        authority_update=authority_update,
        project_root_test=project_root_test,
        orchestration_bundle=orchestration_bundle_summary,
        node_id=node_id,
    )


def release_ask_first_execution_round(context, round_payload: dict[str, object], services=None) -> dict[str, object]:
    deps = _deps(services)
    loop_id = str(round_payload.get('loop_id') or '').strip()
    if not loop_id:
        raise ValueError('ask-first release requires loop_id')
    release = deps.loop_topology(
        context,
        SimpleNamespace(action='release', loop_id=loop_id, policy='auto', idle_only=True, json_output=True),
    )
    round_payload.setdefault('topology', {})
    topology = round_payload['topology']
    if isinstance(topology, dict):
        topology['release'] = release
    round_path = _round_json_path(round_payload)
    if round_path is not None:
        atomic_write_json(round_path, round_payload)
    return release


def submit_or_recover_ask_once(
    context,
    *,
    loop_dir: Path,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    bundle_revision: int,
    node_id: str,
    attempt: int,
    task_id: str,
    message: str,
    allowed_chain_targets: tuple[str, ...] = (),
    bind_chain_workspace_tree: bool = False,
    services=None,
) -> dict[str, object]:
    """Submit once or consume persisted terminal state without live provider polling."""
    try:
        return _submit_and_watch(
            context,
            _deps(services),
            loop_dir=loop_dir,
            loop_id=loop_id,
            target=target,
            sender=sender,
            purpose=purpose,
            bundle_revision=bundle_revision,
            node_id=node_id,
            attempt=attempt,
            task_id=task_id,
            message=message,
            allowed_chain_targets=allowed_chain_targets,
            bind_chain_workspace_tree=bind_chain_workspace_tree,
            timeout=None,
            defer_observation=True,
        )
    except (_AskSubmissionError, _ImmaculateActivationError) as exc:
        freshness = getattr(exc, 'freshness', None)
        return {
            'target': target,
            'sender': RUNNER_ASK_SENDER,
            'logical_sender': sender,
            'purpose': purpose,
            'job_id': None,
            'status': 'failed',
            'reply': '',
            'terminal': True,
            'watch_source': 'submission_failure',
            'observation_error': str(exc),
            'failure_source': (
                'immaculate_activation_failed'
                if isinstance(exc, _ImmaculateActivationError)
                else 'ask_submission_failed'
            ),
            'failure_stage': exc.stage,
            'freshness': dict(freshness) if isinstance(freshness, dict) else None,
            'submission_identity': _submission_identity(
                bundle_revision=bundle_revision,
                node_id=node_id,
                purpose=_intent_purpose(purpose),
                attempt=attempt,
            ),
        }


def _deps(services):
    services = services or SimpleNamespace()
    return SimpleNamespace(
        loop_topology=getattr(services, 'loop_topology', loop_topology),
        plan_task=getattr(services, 'plan_task', plan_task),
        clear_agent_context=getattr(services, 'clear_agent_context', clear_agent_context),
        submit_ask=getattr(services, 'submit_ask', submit_ask),
        watch_ask_job=getattr(services, 'watch_ask_job', watch_ask_job),
        load_persisted_terminal_watch_payload=getattr(
            services,
            'load_persisted_terminal_watch_payload',
            load_persisted_terminal_watch_payload,
        ),
        effective_capacity_snapshot=getattr(
            services,
            'effective_capacity_snapshot',
            lambda context: compile_project_effective_capacity_snapshot(
                Path(context.project.project_root)
            ),
        ),
    )


def _agent_for_role(context, role_id: str, *, fallback: str) -> tuple[str, bool]:
    try:
        loaded = load_project_config(context.project.project_root, include_loop_overlays=False)
        config = loaded.config
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


def _apply_mount_topology(
    context,
    deps,
    *,
    loop_dir: Path,
    loop_id: str,
    worker_agent: str,
    reviewer_agent: str,
    round_reviewer_agent: str | None,
) -> dict[str, object]:
    proposal_path = loop_dir / 'ask_first_mount_topology.proposal.json'
    proposal = {
        'schema': 'ccb.loop.agent_mount_topology.v1',
        'release_policy': {'policy': 'auto', 'idle_only': True},
        'windows': [
            {
                'name': 'ccb-exec',
                'class': 'execution',
                'max_panes': 6,
                'layout_policy': 'append-or-create-window',
            }
        ],
        'agents': [
            {
                'id': worker_agent,
                'profile': WORKER_PROFILE,
                'desired_state': 'present',
                'window_name': 'ccb-exec',
                'pane_order': 0,
                'lifecycle': 'ephemeral',
                'release_policy': 'auto',
            },
            {
                'id': reviewer_agent,
                'profile': REVIEWER_PROFILE,
                'desired_state': 'present',
                'window_name': 'ccb-exec',
                'pane_order': 1,
                'lifecycle': 'ephemeral',
                'release_policy': 'auto',
            },
        ],
    }
    control_agents = ((round_reviewer_agent, 'ccb_round_reviewer', 0),)
    if any(agent_name for agent_name, _profile, _pane_order in control_agents):
        proposal['windows'].append(
            {
                'name': 'ccb-plan',
                'class': 'planning',
                'max_panes': 6,
                'layout_policy': 'append-or-create-window',
            }
        )
    for agent_name, profile, pane_order in control_agents:
        if not agent_name:
            continue
        proposal['agents'].append(
            {
                'id': agent_name,
                'profile': profile,
                'desired_state': 'present',
                'window_name': 'ccb-plan',
                'pane_order': pane_order,
                'lifecycle': 'ephemeral',
                'release_policy': 'auto',
            }
        )
    atomic_write_json(proposal_path, proposal)
    proposed: dict[str, object] = {}
    committed: dict[str, object] = {}
    try:
        proposed = deps.loop_topology(
            context,
            SimpleNamespace(
                action='propose',
                loop_id=loop_id,
                from_path=str(proposal_path),
                proposal_id='ask-first-execution',
                json_output=True,
            ),
        )
        committed = deps.loop_topology(
            context,
            SimpleNamespace(
                action='commit',
                loop_id=loop_id,
                proposal_id='ask-first-execution',
                apply=True,
                json_output=True,
            ),
        )
        status = deps.loop_topology(context, SimpleNamespace(action='status', loop_id=loop_id, json_output=True))
    except Exception as exc:
        status = _topology_status_after_failure(context, deps, loop_id=loop_id)
        return {
            'proposal_source_path': str(proposal_path),
            'propose': proposed,
            'commit': committed,
            'status': status,
            'failure': _failure_record(source='topology_apply_failed', stage='topology', exc=exc),
        }
    return {
        'proposal_source_path': str(proposal_path),
        'propose': proposed,
        'commit': committed,
        'status': status,
    }


def _topology_status_after_failure(context, deps, *, loop_id: str) -> dict[str, object]:
    try:
        status = deps.loop_topology(context, SimpleNamespace(action='status', loop_id=loop_id, json_output=True))
    except Exception as exc:
        return {
            'loop_topology_status': 'unknown',
            'status_failure': _failure_record(source='topology_status_failed', stage='topology', exc=exc),
        }
    return status if isinstance(status, dict) else {}


def _topology_blocker(topology: dict[str, object]) -> dict[str, object] | None:
    failure = topology.get('failure') if isinstance(topology.get('failure'), dict) else None
    if failure is not None:
        return dict(failure)
    status_payload = topology.get('status') if isinstance(topology.get('status'), dict) else {}
    status = str(status_payload.get('loop_topology_status') or '').strip()
    if status == 'ready':
        return None
    return {
        'source': 'topology_not_ready',
        'stage': 'topology',
        'reason': f'mount topology status {status or "missing"}; expected ready',
        'error': f'mount topology status {status or "missing"}; expected ready',
        'loop_topology_status': status or None,
        'desired_path': status_payload.get('desired_path'),
        'observed_path': status_payload.get('observed_path'),
        'topology_status': status_payload,
        'topology_drift': _topology_status_drift(status_payload),
        'retained': _topology_status_retained(status_payload),
    }


def _failure_record(*, source: str, stage: str, exc: Exception) -> dict[str, object]:
    return {
        'source': source,
        'stage': stage,
        'error_type': exc.__class__.__name__,
        'error': str(exc),
        'reason': str(exc),
    }


def _topology_status_drift(status_payload: dict[str, object]) -> object:
    observed = status_payload.get('observed') if isinstance(status_payload.get('observed'), dict) else {}
    return observed.get('drift')


def _topology_status_retained(status_payload: dict[str, object]) -> object:
    observed = status_payload.get('observed') if isinstance(status_payload.get('observed'), dict) else {}
    return observed.get('retained') or observed.get('retained_count')


def _ask_failure_record(exc: Exception, *, default_stage: str) -> dict[str, object]:
    source = 'ask_failure'
    if isinstance(exc, _ImmaculateActivationError):
        source = 'immaculate_activation_failed'
    elif isinstance(exc, _AskSubmissionError):
        source = 'ask_submission_failed'
    elif isinstance(exc, _AskWatchError):
        source = 'watch_failed'
    stage = str(getattr(exc, 'stage', '') or default_stage)
    failure = _failure_record(source=source, stage=stage, exc=exc)
    target = getattr(exc, 'target', None)
    job_id = getattr(exc, 'job_id', None)
    purpose = getattr(exc, 'purpose', None)
    if target:
        failure['target'] = str(target)
    if job_id:
        failure['job_id'] = str(job_id)
    if purpose:
        failure['purpose'] = str(purpose)
    freshness = getattr(exc, 'freshness', None)
    if isinstance(freshness, dict):
        failure['freshness'] = dict(freshness)
    return failure


def _write_round_payload(
    context,
    *,
    loop_dir: Path,
    loop_id: str,
    task_id: str,
    started_at: str,
    status: str,
    worker_agent: str,
    reviewer_agent: str,
    artifact_refs: dict[str, str],
    topology: dict[str, object],
    worker: dict[str, object],
    reviewer: dict[str, object],
    rework: dict[str, dict[str, object]],
    orchestrator: dict[str, object],
    round_reviewer: dict[str, object],
    round_result: str,
    round_result_source: str,
    orchestration_bundle: dict[str, object],
    node_id: str,
    failure: dict[str, object] | None = None,
    authority_update: dict[str, object] | None = None,
    project_root_test: dict[str, object] | None = None,
    orchestrator_agent: str | None = None,
    round_reviewer_agent: str | None = None,
) -> dict[str, object]:
    round_summary_path = loop_dir / 'round_summary.md'
    atomic_write_text(
        round_summary_path,
        _round_summary_text(
            loop_id=loop_id,
            task_id=task_id,
            result=round_result,
            result_source=round_result_source,
            artifact_refs=artifact_refs,
            topology=topology,
            worker=worker,
            reviewer=reviewer,
            rework=rework or {},
            orchestrator=orchestrator,
            round_reviewer=round_reviewer,
            failure=failure,
            authority_update=authority_update,
            project_root_test=project_root_test,
        ),
    )
    finished_at = _utc_now()
    node_record = _workgroup_record(
        node_id=node_id,
        worker_agent=worker_agent,
        reviewer_agent=reviewer_agent,
        worker=worker,
        reviewer=reviewer,
        rework=rework,
        round_result=round_result,
    )
    payload = {
        'schema': 'ccb.loop.workgroup_round_state.v1',
        'schema_version': 1,
        'record_type': 'ccb_loop_ask_first_execution_round',
        'workgroup_state_schema': 'ccb.loop.workgroup_round_state.v1',
        'loop_run_status': status,
        'dispatch_source': 'ask_first_mount_topology',
        'loop_id': loop_id,
        'task_id': task_id,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'started_at': started_at,
        'finished_at': finished_at,
        'profiles': {
            'worker': WORKER_PROFILE,
            'reviewer': REVIEWER_PROFILE,
        },
        'agents': {
            'worker': worker_agent,
            'reviewer': reviewer_agent,
            'orchestrator': orchestrator_agent or ORCHESTRATOR_TARGET,
            ROUND_REVIEWER_FIELD: round_reviewer_agent or ROUND_REVIEWER_TARGET,
        },
        'legacy_aliases': {
            LEGACY_ROUND_CHECKER_FIELD: {
                'field': ROUND_REVIEWER_FIELD,
                'target': round_reviewer_agent or ROUND_REVIEWER_TARGET,
                'purpose': 'compatibility_only',
            }
        },
        'artifact_refs': artifact_refs,
        'orchestration_bundle': orchestration_bundle,
        'bundle_revision': orchestration_bundle.get('bundle_revision'),
        'bundle_digest': orchestration_bundle.get('bundle_digest'),
        'controller_status': round_result,
        'topology': topology,
        'nodes': {node_id: node_record},
        'workgroups': {node_id: node_record},
        'worker': worker,
        'reviewer': reviewer,
        'rework': rework or {},
        'orchestrator': orchestrator,
        ROUND_REVIEWER_FIELD: round_reviewer,
        'round_result': round_result,
        'round_result_source': round_result_source,
        'paths': {
            'round': str(round_summary_path),
            'round_json': str(loop_dir / 'round.json'),
            'asks': str(loop_dir / 'asks.jsonl'),
            'events': str(loop_dir / 'events.jsonl'),
            'artifacts': str(loop_dir / 'artifacts'),
        },
    }
    if failure is not None:
        payload['failure'] = failure
    if authority_update is not None:
        payload['authority_update'] = authority_update
    if project_root_test is not None:
        payload['project_root_test'] = project_root_test
    atomic_write_json(loop_dir / 'round.json', payload)
    _clear_stage_state(loop_dir)
    event_payload = {'task_id': task_id, 'status': status, 'round_result': round_result}
    if failure is not None:
        event_payload['round_result_source'] = round_result_source
        event_payload['failure_stage'] = failure.get('stage')
    if authority_update is not None:
        event_payload['authority_update_source'] = authority_update.get('source')
    _append_event(loop_dir, loop_id=loop_id, kind='ask_first_round_finished', payload=event_payload)
    return payload


def _write_pending_payload(
    context,
    *,
    loop_dir: Path,
    loop_id: str,
    task_id: str,
    started_at: str,
    worker_agent: str,
    reviewer_agent: str,
    artifact_refs: dict[str, str],
    topology: dict[str, object],
    worker: dict[str, object],
    reviewer: dict[str, object],
    rework: dict[str, dict[str, object]],
    orchestrator: dict[str, object],
    round_reviewer: dict[str, object],
    pending: dict[str, object],
    orchestration_bundle: dict[str, object],
    node_id: str,
    authority_update: dict[str, object] | None = None,
    orchestrator_agent: str | None = None,
    round_reviewer_agent: str | None = None,
) -> dict[str, object]:
    pending_path = loop_dir / 'round.pending.json'
    state_path = _stage_state_path(loop_dir)
    observed_at = _utc_now()
    node_record = _workgroup_record(
        node_id=node_id,
        worker_agent=worker_agent,
        reviewer_agent=reviewer_agent,
        worker=worker,
        reviewer=reviewer,
        rework=rework,
        pending=pending,
    )
    current_artifacts = {
        'artifact_refs': artifact_refs,
        'orchestration_bundle': orchestration_bundle,
        'nodes': {node_id: node_record},
        'worker': worker,
        'reviewer': reviewer,
        'rework': rework or {},
        'orchestrator': orchestrator,
        ROUND_REVIEWER_FIELD: round_reviewer,
    }
    if authority_update is not None:
        current_artifacts['authority_update'] = authority_update
    state_payload = {
        'schema': 'ccb.loop.workgroup_round_state.v1',
        'schema_version': 1,
        'record_type': 'ccb_loop_ask_first_stage_state',
        'workgroup_state_schema': 'ccb.loop.workgroup_round_state.v1',
        'status': 'executing',
        'legacy_status': 'pending',
        'task_id': task_id,
        'loop_id': loop_id,
        'bundle_revision': orchestration_bundle.get('bundle_revision'),
        'bundle_digest': orchestration_bundle.get('bundle_digest'),
        'round_started_at': started_at,
        'updated_at': observed_at,
        'stage': pending.get('stage'),
        'target': pending.get('target'),
        'job_id': pending.get('job_id'),
        'purpose': pending.get('purpose'),
        'submitted_at': pending.get('submitted_at'),
        'current_artifacts': current_artifacts,
        'nodes': {node_id: node_record},
        'orchestration_bundle': orchestration_bundle,
        'pending': pending,
    }
    payload = {
        'schema': 'ccb.loop.workgroup_round_state.v1',
        'schema_version': 1,
        'record_type': 'ccb_loop_ask_first_execution_round',
        'workgroup_state_schema': 'ccb.loop.workgroup_round_state.v1',
        'loop_run_status': 'pending',
        'dispatch_source': 'ask_first_mount_topology',
        'loop_id': loop_id,
        'task_id': task_id,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'started_at': started_at,
        'observed_at': observed_at,
        'profiles': {
            'worker': WORKER_PROFILE,
            'reviewer': REVIEWER_PROFILE,
        },
        'agents': {
            'worker': worker_agent,
            'reviewer': reviewer_agent,
            'orchestrator': orchestrator_agent or ORCHESTRATOR_TARGET,
            ROUND_REVIEWER_FIELD: round_reviewer_agent or ROUND_REVIEWER_TARGET,
        },
        'legacy_aliases': {
            LEGACY_ROUND_CHECKER_FIELD: {
                'field': ROUND_REVIEWER_FIELD,
                'target': round_reviewer_agent or ROUND_REVIEWER_TARGET,
                'purpose': 'compatibility_only',
            }
        },
        'artifact_refs': artifact_refs,
        'orchestration_bundle': orchestration_bundle,
        'bundle_revision': orchestration_bundle.get('bundle_revision'),
        'bundle_digest': orchestration_bundle.get('bundle_digest'),
        'controller_status': 'executing',
        'topology': topology,
        'nodes': {node_id: node_record},
        'workgroups': {node_id: node_record},
        'worker': worker,
        'reviewer': reviewer,
        'rework': rework or {},
        'orchestrator': orchestrator,
        ROUND_REVIEWER_FIELD: round_reviewer,
        'pending': pending,
        'round_result': 'pending',
        'round_result_source': 'ask_job_pending',
        'paths': {
            'pending_json': str(pending_path),
            'stage_state': str(state_path),
            'asks': str(loop_dir / 'asks.jsonl'),
            'events': str(loop_dir / 'events.jsonl'),
            'artifacts': str(loop_dir / 'artifacts'),
        },
    }
    if authority_update is not None:
        payload['authority_update'] = authority_update
    atomic_write_json(state_path, state_payload)
    atomic_write_json(pending_path, payload)
    _append_event(
        loop_dir,
        loop_id=loop_id,
        kind='ask_first_round_pending',
        payload={
            'task_id': task_id,
            'status': 'pending',
            'round_result': 'pending',
            'pending_stage': pending.get('stage'),
            'job_id': pending.get('job_id'),
            'reason': pending.get('reason'),
        },
    )
    return payload


def _workgroup_record(
    *,
    node_id: str,
    worker_agent: str,
    reviewer_agent: str,
    worker: dict[str, object],
    reviewer: dict[str, object],
    rework: dict[str, dict[str, object]],
    pending: dict[str, object] | None = None,
    round_result: str | None = None,
) -> dict[str, object]:
    status = _node_status(
        worker=worker,
        reviewer=reviewer,
        rework=rework,
        pending=pending,
        round_result=round_result,
    )
    return {
        'node_id': node_id,
        'status': status,
        'worker_agent': worker_agent,
        'reviewer_agent': reviewer_agent,
        'worker': worker,
        'reviewer': reviewer,
        'rework': rework or {},
    }


def _node_status(
    *,
    worker: dict[str, object],
    reviewer: dict[str, object],
    rework: dict[str, dict[str, object]],
    pending: dict[str, object] | None,
    round_result: str | None,
) -> str:
    if round_result == 'pass':
        return 'integrated'
    if round_result in {'partial', 'replan_required', 'blocked'}:
        return 'blocked'
    pending = pending or {}
    purpose = str(pending.get('purpose') or '')
    submission_unknown = str(pending.get('source') or '') == 'ask_submission_unknown'
    if purpose in {'reviewer', 'reviewer_recheck'}:
        return 'reviewer_submission_unknown' if submission_unknown else 'reviewer_pending'
    if purpose in {'worker', 'worker_rework'}:
        return 'worker_submission_unknown' if submission_unknown else 'worker_pending'
    recheck = rework.get('reviewer_recheck') or {}
    if recheck:
        if str(recheck.get('status') or '') != 'completed':
            return 'review_failed'
        return 'reviewer_rework' if _reviewer_requires_rework(recheck) else 'review_passed'
    if reviewer:
        if str(reviewer.get('status') or '') != 'completed':
            return 'review_failed'
        return 'reviewer_rework' if _reviewer_requires_rework(reviewer) else 'review_passed'
    if worker:
        return 'worker_complete' if str(worker.get('status') or '') == 'completed' else 'worker_failed'
    return 'created'


def _stage_state_path(loop_dir: Path) -> Path:
    return loop_dir / 'ask_first_stage_state.json'


def _load_stage_state(loop_dir: Path, *, loop_id: str, task_id: str) -> dict[str, object]:
    path = _stage_state_path(loop_dir)
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise RuntimeError(f'ask-first stage state is not an object: {path}')
    if str(payload.get('loop_id') or '') != loop_id or str(payload.get('task_id') or '') != task_id:
        raise RuntimeError(f'ask-first stage state does not match loop/task: {path}')
    return payload


def _state_current_artifacts(stage_state: dict[str, object]) -> dict[str, object]:
    current = stage_state.get('current_artifacts') if isinstance(stage_state, dict) else {}
    return dict(current) if isinstance(current, dict) else {}


def _state_node_artifacts(stage_state: dict[str, object], *, node_id: str) -> dict[str, object]:
    nodes = stage_state.get('nodes')
    if isinstance(nodes, dict):
        node = nodes.get(node_id)
        if isinstance(node, dict):
            return dict(node)
    return {}


def _artifact_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not value:
        return {}
    if not bool(value.get('terminal')):
        return {}
    return dict(value)


def _optional_artifact_dict(value: object) -> dict[str, object] | None:
    return dict(value) if isinstance(value, dict) and value else None


def _rework_artifacts(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): dict(item)
        for key, item in value.items()
        if isinstance(item, dict) and item and bool(item.get('terminal'))
    }


def _clear_stage_state(loop_dir: Path) -> None:
    for path in (_stage_state_path(loop_dir), loop_dir / 'round.pending.json'):
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def _submission_intent_path(loop_dir: Path) -> Path:
    return loop_dir / 'ask_first_submission_intents.jsonl'


def _write_submission_intent(
    loop_dir: Path,
    *,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    intent_purpose: str,
    stage: str,
    ask_task_id: str,
    bundle_revision: int,
    node_id: str,
    attempt: int,
    job_id: str | None = None,
    status: str = 'prepared',
    freshness: dict[str, object] | None = None,
) -> dict[str, object]:
    path = _submission_intent_path(loop_dir)
    identity = _submission_identity(
        bundle_revision=bundle_revision,
        node_id=node_id,
        purpose=intent_purpose,
        attempt=attempt,
    )
    existing = _load_submission_intent(path, identity=identity)
    now = _utc_now()
    if status not in {'prepared', 'accepted', 'terminal', 'consumed', 'unknown'}:
        raise ValueError(f'unknown ask-first submission intent status: {status}')
    if not existing and status != 'prepared':
        raise RuntimeError(f'ask-first submission intent must start at prepared, not {status}')
    if existing:
        if str(existing.get('loop_id') or '') != loop_id:
            raise RuntimeError(f'ask-first submission intent does not match loop: {path}')
        if str(existing.get('target') or '') != target:
            raise RuntimeError('ask-first submission intent target changed for the same identity')
        existing_job_id = str(existing.get('job_id') or '').strip()
        if existing_job_id and job_id and existing_job_id != job_id:
            raise RuntimeError('ask-first submission intent job_id is already bound')
        if str(existing.get('status') or '') == status and (not job_id or existing_job_id == job_id):
            return existing
        allowed_next = {
            'prepared': {'accepted', 'unknown'},
            'accepted': {'terminal'},
            'terminal': {'consumed'},
            'unknown': set(),
            'consumed': set(),
        }
        current_status = str(existing.get('status') or '')
        if status not in allowed_next.get(current_status, set()):
            raise RuntimeError(
                f'invalid ask-first submission intent transition: {current_status or "missing"} -> {status}'
            )
    payload: dict[str, object] = {
        'schema_version': 1,
        'record_type': 'ccb_loop_ask_first_submission_intent',
        'intent_id': ':'.join(str(identity[key]) for key in ('bundle_revision', 'node_id', 'purpose', 'attempt')),
        'status': status,
        'loop_id': loop_id,
        **identity,
        'stage': stage,
        'target': target,
        'sender': RUNNER_ASK_SENDER,
        'logical_sender': sender,
        'ask_purpose': purpose,
        'ask_task_id': ask_task_id,
        'ts': now,
    }
    bound_job_id = job_id or (str(existing.get('job_id') or '').strip() if existing else '')
    if bound_job_id:
        payload['job_id'] = bound_job_id
    if freshness is not None:
        payload['freshness'] = freshness
    elif existing and isinstance(existing.get('freshness'), dict):
        payload['freshness'] = dict(existing['freshness'])
    _append_jsonl(path, payload)
    return payload


def _matching_submission_intent(
    loop_dir: Path,
    *,
    loop_id: str,
    target: str,
    bundle_revision: int,
    node_id: str,
    intent_purpose: str,
    attempt: int,
) -> dict[str, object] | None:
    path = _submission_intent_path(loop_dir)
    identity = _submission_identity(
        bundle_revision=bundle_revision,
        node_id=node_id,
        purpose=intent_purpose,
        attempt=attempt,
    )
    payload = _load_submission_intent(path, identity=identity)
    if not payload:
        return None
    if str(payload.get('loop_id') or '') != loop_id:
        raise RuntimeError(f'ask-first submission intent does not match loop: {path}')
    if str(payload.get('target') or '') != target:
        raise RuntimeError('ask-first submission intent target changed for the same identity')
    return payload


def _load_submission_intent(
    path: Path,
    *,
    identity: dict[str, object],
) -> dict[str, object]:
    if not path.is_file():
        return {}
    latest: dict[str, object] = {}
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        if not raw_line.strip():
            continue
        payload = json.loads(raw_line)
        if not isinstance(payload, dict):
            raise RuntimeError(f'ask-first submission intent line is not an object: {path}')
        if payload.get('record_type') != 'ccb_loop_ask_first_submission_intent':
            raise RuntimeError(f'unknown ask-first submission intent record_type: {path}')
        if all(payload.get(key) == value for key, value in identity.items()):
            latest = dict(payload)
    return latest


def _submission_identity(
    *,
    bundle_revision: int,
    node_id: str,
    purpose: str,
    attempt: int,
) -> dict[str, object]:
    if isinstance(bundle_revision, bool) or not isinstance(bundle_revision, int) or bundle_revision <= 0:
        raise ValueError('submission intent bundle_revision must be a positive integer')
    if purpose not in {'worker', 'reviewer', 'worker_rework', 'reviewer_recheck', 'round_reviewer'}:
        raise ValueError(f'unsupported submission intent purpose: {purpose}')
    if not node_id or (purpose == 'round_reviewer') != (node_id == 'round'):
        raise ValueError('submission intent node_id must be canonical, with round reserved for round review')
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError('submission intent attempt must be a positive integer')
    return {
        'bundle_revision': bundle_revision,
        'node_id': node_id,
        'purpose': purpose,
        'attempt': attempt,
    }


def _submission_unknown_result(
    loop_dir: Path,
    *,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    intent_purpose: str,
    stage: str,
    ask_task_id: str,
    bundle_revision: int,
    node_id: str,
    attempt: int,
    intent: dict[str, object],
) -> dict[str, object]:
    reason = (
        'submission intent exists without accepted job_id; previous runner may have exited during daemon submission. '
        'Operator must inspect persisted CCB job/message state before retrying this stage.'
    )
    _append_event(
        loop_dir,
        loop_id=loop_id,
        kind='ask_submission_unknown',
        payload={
            'purpose': purpose,
            'target': target,
            'stage': stage,
            'reason': reason,
        },
    )
    unknown = _write_submission_intent(
        loop_dir,
        loop_id=loop_id,
        target=target,
        sender=sender,
        purpose=purpose,
        intent_purpose=intent_purpose,
        stage=stage,
        ask_task_id=ask_task_id,
        bundle_revision=bundle_revision,
        node_id=node_id,
        attempt=attempt,
        status='unknown',
    )
    return {
        'target': target,
        'sender': RUNNER_ASK_SENDER,
        'logical_sender': sender,
        'purpose': purpose,
        'job_id': None,
        'submitted_at': intent.get('ts'),
        'status': 'submission_unknown',
        'reply': '',
        'terminal': False,
        'watch_source': 'submission_intent',
        'watch_observation': 'submission_unknown',
        'pending_source': 'ask_submission_unknown',
        'observation_error': reason,
        'intent_path': str(_submission_intent_path(loop_dir)),
        'freshness': unknown.get('freshness'),
        'submission_identity': _submission_identity(
            bundle_revision=bundle_revision,
            node_id=node_id,
            purpose=intent_purpose,
            attempt=attempt,
        ),
    }


def _prepare_immaculate_activation(
    context,
    deps,
    *,
    loop_dir: Path,
    loop_id: str,
    target: str,
    purpose: str,
    stage: str,
    ask_task_id: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        'schema_version': 1,
        'record_type': 'ccb_immaculate_activation_freshness',
        'loop_id': loop_id,
        'target': target,
        'purpose': purpose,
        'stage': stage,
        'ask_task_id': ask_task_id,
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
    else:
        payload['clear_summary'] = _compact_clear_summary(summary)
        payload['status'] = _clear_status_for_target(summary, target)
    _append_event(
        loop_dir,
        loop_id=loop_id,
        kind='immaculate_activation_freshness',
        payload=payload,
    )
    return payload


def _compact_clear_summary(summary: object) -> dict[str, object]:
    if not isinstance(summary, dict):
        return {'status': 'unknown', 'raw_type': type(summary).__name__}
    compact: dict[str, object] = {'status': summary.get('status')}
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


def _accepted_submission_persistence_unknown_result(
    loop_dir: Path,
    *,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    stage: str,
    job_id: str,
    submitted_at: str,
    reason: str,
) -> dict[str, object]:
    _append_event(
        loop_dir,
        loop_id=loop_id,
        kind='ask_submission_unknown',
        payload={
            'purpose': purpose,
            'target': target,
            'stage': stage,
            'job_id': job_id,
            'reason': reason,
        },
    )
    return {
        'target': target,
        'sender': RUNNER_ASK_SENDER,
        'logical_sender': sender,
        'purpose': purpose,
        'job_id': job_id,
        'submitted_at': submitted_at,
        'status': 'running',
        'reply': '',
        'terminal': False,
        'watch_source': 'submission_intent',
        'watch_observation': 'submission_unknown',
        'pending_source': 'ask_submission_unknown',
        'observation_error': reason,
        'intent_path': str(_submission_intent_path(loop_dir)),
    }


def _submit_and_watch(
    context,
    deps,
    *,
    loop_dir: Path,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    bundle_revision: int,
    node_id: str,
    attempt: int,
    task_id: str,
    message: str,
    timeout: float | None,
    allowed_chain_targets: tuple[str, ...] = (),
    bind_chain_workspace_tree: bool = False,
    defer_observation: bool = False,
) -> dict[str, object]:
    # The intent check and daemon submission are one exact-once critical section.
    # Direct `runner --once` callers are not covered by AutoRunnerLock.
    with file_lock(loop_dir / 'ask_first_submission.lock'):
        return _submit_and_watch_locked(
            context,
            deps,
            loop_dir=loop_dir,
            loop_id=loop_id,
            target=target,
            sender=sender,
            purpose=purpose,
            bundle_revision=bundle_revision,
            node_id=node_id,
            attempt=attempt,
            task_id=task_id,
            message=message,
            allowed_chain_targets=allowed_chain_targets,
            bind_chain_workspace_tree=bind_chain_workspace_tree,
            timeout=timeout,
            defer_observation=defer_observation,
        )


def _submit_and_watch_locked(
    context,
    deps,
    *,
    loop_dir: Path,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    bundle_revision: int,
    node_id: str,
    attempt: int,
    task_id: str,
    message: str,
    timeout: float | None,
    allowed_chain_targets: tuple[str, ...] = (),
    bind_chain_workspace_tree: bool = False,
    defer_observation: bool = False,
) -> dict[str, object]:
    stage = f'{purpose}_ask'
    intent_purpose = _intent_purpose(purpose)
    identity = _submission_identity(
        bundle_revision=bundle_revision,
        node_id=node_id,
        purpose=intent_purpose,
        attempt=attempt,
    )
    existing = _latest_submitted_ask(loop_dir, target=target, identity=identity)
    if existing is not None:
        result = _watch_or_recover_job(
            context,
            deps,
            loop_dir=loop_dir,
            loop_id=loop_id,
            target=target,
            sender=sender,
            purpose=purpose,
            stage=stage,
            job_id=str(existing['job_id']),
            submitted_at=str(existing.get('ts') or ''),
            timeout=timeout,
            allow_live_watch=False,
        )
        _settle_submission_intent(
            loop_dir,
            loop_id=loop_id,
            target=target,
            sender=sender,
            purpose=purpose,
            intent_purpose=intent_purpose,
            stage=stage,
            ask_task_id=task_id,
            identity=identity,
            job_id=str(existing['job_id']),
            result=result,
        )
        result['submission_identity'] = identity
        return result
    intent = _matching_submission_intent(
        loop_dir,
        loop_id=loop_id,
        target=target,
        bundle_revision=bundle_revision,
        node_id=node_id,
        intent_purpose=intent_purpose,
        attempt=attempt,
    )
    if intent is not None:
        intent_job_id = str(intent.get('job_id') or '').strip()
        if intent_job_id:
            result = _watch_or_recover_job(
                context,
                deps,
                loop_dir=loop_dir,
                loop_id=loop_id,
                target=target,
                sender=sender,
                purpose=purpose,
                stage=stage,
                job_id=intent_job_id,
                submitted_at=str(intent.get('ts') or ''),
                timeout=timeout,
                allow_live_watch=False,
            )
            if isinstance(intent.get('freshness'), dict):
                result['freshness'] = dict(intent['freshness'])
            _settle_submission_intent(
                loop_dir,
                loop_id=loop_id,
                target=target,
                sender=sender,
                purpose=purpose,
                intent_purpose=intent_purpose,
                stage=stage,
                ask_task_id=task_id,
                identity=identity,
                job_id=intent_job_id,
                result=result,
            )
            result['submission_identity'] = identity
            return result
        return _submission_unknown_result(
            loop_dir,
            loop_id=loop_id,
            target=target,
            sender=sender,
            purpose=purpose,
            intent_purpose=intent_purpose,
            stage=stage,
            ask_task_id=task_id,
            bundle_revision=bundle_revision,
            node_id=node_id,
            attempt=attempt,
            intent=intent,
        )
    freshness = _prepare_immaculate_activation(
        context,
        deps,
        loop_dir=loop_dir,
        loop_id=loop_id,
        target=target,
        purpose=purpose,
        stage=stage,
        ask_task_id=task_id,
    )
    if str(freshness.get('status') or '') != 'cleared':
        raise _ImmaculateActivationError(
            target=target,
            purpose=purpose,
            stage=stage,
            freshness=freshness,
        )
    _write_submission_intent(
        loop_dir,
        loop_id=loop_id,
        target=target,
        sender=sender,
        purpose=purpose,
        intent_purpose=intent_purpose,
        stage=stage,
        ask_task_id=task_id,
        bundle_revision=bundle_revision,
        node_id=node_id,
        attempt=attempt,
        freshness=freshness,
    )
    try:
        summary = deps.submit_ask(
            context,
            ParsedAskCommand(
                project=None,
                target=target,
                sender=RUNNER_ASK_SENDER,
                message=message,
                task_id=task_id,
                allowed_chain_targets=allowed_chain_targets,
                bind_chain_workspace_tree=bind_chain_workspace_tree,
            ),
        )
    except Exception as exc:
        raise _AskSubmissionError(target=target, purpose=purpose, stage=stage, error=str(exc)) from exc
    job = _single_job(summary.jobs, target=target)
    job_id = str(job['job_id'])
    try:
        accepted_intent = _write_submission_intent(
            loop_dir,
            loop_id=loop_id,
            target=target,
            sender=sender,
            purpose=purpose,
            intent_purpose=intent_purpose,
            stage=stage,
            ask_task_id=task_id,
            bundle_revision=bundle_revision,
            node_id=node_id,
            attempt=attempt,
            job_id=job_id,
            status='accepted',
            freshness=freshness,
        )
    except Exception as exc:
        return _accepted_submission_persistence_unknown_result(
            loop_dir,
            loop_id=loop_id,
            target=target,
            sender=sender,
            purpose=purpose,
            stage=stage,
            job_id=job_id,
            submitted_at='',
            reason=f'accepted job_id could not be persisted before ask log append: {exc}',
        )
    try:
        ask_record = _append_ask(
            loop_dir,
            loop_id=loop_id,
            target=target,
            sender=RUNNER_ASK_SENDER,
            purpose=purpose,
            job_id=job_id,
            identity=identity,
            freshness=freshness,
        )
    except Exception as exc:
        return _accepted_submission_persistence_unknown_result(
            loop_dir,
            loop_id=loop_id,
            target=target,
            sender=sender,
            purpose=purpose,
            stage=stage,
            job_id=job_id,
            submitted_at=str(accepted_intent.get('ts') or ''),
            reason=f'local ask log append failed after daemon accepted job: {exc}',
        )
    if defer_observation:
        reason = 'submitted after persisted terminal recovery; waiting for a later runner invocation'
        result = {
            'target': target,
            'sender': RUNNER_ASK_SENDER,
            'logical_sender': sender,
            'purpose': purpose,
            'job_id': job_id,
            'submitted_at': str(ask_record.get('ts') or ''),
            'status': 'running',
            'reply': '',
            'terminal': False,
            'watch_source': 'not_started',
            'watch_observation': 'deferred_after_persisted_recovery',
            'observation_error': reason,
            'freshness': freshness,
            'submission_identity': identity,
        }
        _append_event(
            loop_dir,
            loop_id=loop_id,
            kind='ask_pending',
            payload={
                'purpose': purpose,
                'target': target,
                'job_id': job_id,
                'status': 'running',
                'watch_observation': 'deferred_after_persisted_recovery',
                'reason': reason,
            },
        )
        return result
    result = _watch_or_recover_job(
        context,
        deps,
        loop_dir=loop_dir,
        loop_id=loop_id,
        target=target,
        sender=sender,
        purpose=purpose,
        stage=stage,
        job_id=job_id,
        submitted_at=str(ask_record.get('ts') or ''),
        timeout=timeout,
        allow_live_watch=not defer_observation,
    )
    result['freshness'] = freshness
    _settle_submission_intent(
        loop_dir,
        loop_id=loop_id,
        target=target,
        sender=sender,
        purpose=purpose,
        intent_purpose=intent_purpose,
        stage=stage,
        ask_task_id=task_id,
        identity=identity,
        job_id=job_id,
        result=result,
    )
    result['submission_identity'] = identity
    return result


def _intent_purpose(ask_purpose: str) -> str:
    if ask_purpose in {ROUND_REVIEWER_FIELD, ROUND_REVIEWER_CORRECTION_PURPOSE}:
        return 'round_reviewer'
    return ask_purpose


def _settle_submission_intent(
    loop_dir: Path,
    *,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    intent_purpose: str,
    stage: str,
    ask_task_id: str,
    identity: dict[str, object],
    job_id: str,
    result: dict[str, object],
) -> None:
    if not bool(result.get('terminal')):
        return
    latest = _load_submission_intent(_submission_intent_path(loop_dir), identity=identity)
    if str(latest.get('status') or '') == 'consumed':
        return
    common = {
        'loop_id': loop_id,
        'target': target,
        'sender': sender,
        'purpose': purpose,
        'intent_purpose': intent_purpose,
        'stage': stage,
        'ask_task_id': ask_task_id,
        'bundle_revision': int(identity['bundle_revision']),
        'node_id': str(identity['node_id']),
        'attempt': int(identity['attempt']),
        'job_id': job_id,
    }
    if str(latest.get('status') or '') == 'accepted':
        _write_submission_intent(loop_dir, status='terminal', **common)
    _write_submission_intent(loop_dir, status='consumed', **common)


def _watch_or_recover_job(
    context,
    deps,
    *,
    loop_dir: Path,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    stage: str,
    job_id: str,
    submitted_at: str,
    timeout: float | None,
    allow_live_watch: bool = True,
) -> dict[str, object]:
    persisted = _load_persisted_terminal(context, deps, job_id)
    if persisted is not None:
        return _ask_result_from_retry_aware_payload(
            context,
            deps,
            loop_dir,
            loop_id=loop_id,
            target=target,
            sender=sender,
            purpose=purpose,
            job_id=job_id,
            submitted_at=submitted_at,
            payload=persisted,
            watch_source='persisted_terminal',
            timeout=timeout,
            allow_live_watch=allow_live_watch,
        )
    if not allow_live_watch:
        result = {
            'target': target,
            'sender': RUNNER_ASK_SENDER,
            'logical_sender': sender,
            'purpose': purpose,
            'job_id': job_id,
            'submitted_at': submitted_at,
            'status': 'running',
            'reply': '',
            'terminal': False,
            'watch_source': 'persisted_terminal',
            'watch_observation': 'not_terminal',
        }
        _append_event(
            loop_dir,
            loop_id=loop_id,
            kind='ask_pending',
            payload={
                'purpose': purpose,
                'target': target,
                'job_id': job_id,
                'status': 'running',
                'watch_observation': 'persisted_terminal_not_found',
            },
        )
        return result
    try:
        batch = deps.watch_ask_job(
            context,
            job_id,
            StringIO(),
            timeout=_ask_first_watch_timeout(timeout),
            emit_output=False,
        )
    except Exception as exc:
        persisted = _load_persisted_terminal(context, deps, job_id)
        if persisted is not None:
            return _ask_result_from_retry_aware_payload(
                context,
                deps,
                loop_dir,
                loop_id=loop_id,
                target=target,
                sender=sender,
                purpose=purpose,
                job_id=job_id,
                submitted_at=submitted_at,
                payload=persisted,
                watch_source='persisted_terminal',
                observation_error=str(exc),
                timeout=timeout,
                allow_live_watch=allow_live_watch,
            )
        observation = 'timeout' if _is_watch_timeout(exc) else 'error'
        result = {
            'target': target,
            'sender': RUNNER_ASK_SENDER,
            'logical_sender': sender,
            'purpose': purpose,
            'job_id': job_id,
            'submitted_at': submitted_at,
            'status': 'running',
            'reply': '',
            'terminal': False,
            'watch_source': 'watch_ask_job',
            'watch_observation': observation,
            'observation_error': str(exc),
        }
        _append_event(
            loop_dir,
            loop_id=loop_id,
            kind='ask_pending',
            payload={
                'purpose': purpose,
                'target': target,
                'job_id': job_id,
                'status': 'running',
                'watch_observation': observation,
                'reason': str(exc),
            },
        )
        return result
    return _ask_result_from_retry_aware_payload(
        context,
        deps,
        loop_dir,
        loop_id=loop_id,
        target=target,
        sender=sender,
        purpose=purpose,
        job_id=job_id,
        submitted_at=submitted_at,
        payload=batch,
        watch_source='watch_ask_job',
        timeout=timeout,
        allow_live_watch=allow_live_watch,
    )


def _load_persisted_terminal(context, deps, job_id: str) -> dict[str, object] | None:
    payload = deps.load_persisted_terminal_watch_payload(context, job_id, cursor=0)
    if payload is None:
        return None
    return dict(payload)


def _ask_first_watch_timeout(timeout: float | None) -> float:
    if timeout is None:
        return 0.0
    return float(timeout)


def _ask_result_from_retry_aware_payload(
    context,
    deps,
    loop_dir: Path,
    *,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    job_id: str,
    submitted_at: str,
    payload,
    watch_source: str,
    timeout: float | None,
    allow_live_watch: bool,
    observation_error: str | None = None,
    retry_lineage: tuple[str, ...] = (),
) -> dict[str, object]:
    status = str(_payload_value(payload, 'status') or '').strip()
    retryable_terminal_status = status in {'failed', 'incomplete'}
    if not retryable_terminal_status:
        result = _ask_result_from_watch_payload(
            loop_dir,
            loop_id=loop_id,
            target=target,
            sender=sender,
            purpose=purpose,
            job_id=job_id,
            submitted_at=submitted_at,
            payload=payload,
            watch_source=watch_source,
            observation_error=observation_error,
        )
        if retry_lineage:
            result['retry_lineage'] = list(retry_lineage)
            result['retry_source_job_id'] = retry_lineage[0]
        return result
    successor = _retry_successor_job_id(context, job_id)
    if not successor or successor in retry_lineage or successor == job_id:
        return _ask_result_from_watch_payload(
            loop_dir,
            loop_id=loop_id,
            target=target,
            sender=sender,
            purpose=purpose,
            job_id=job_id,
            submitted_at=submitted_at,
            payload=payload,
            watch_source=watch_source,
            observation_error=observation_error,
        )
    result = _watch_or_recover_job(
        context,
        deps,
        loop_dir=loop_dir,
        loop_id=loop_id,
        target=target,
        sender=sender,
        purpose=purpose,
        stage=f'{purpose}_ask',
        job_id=successor,
        submitted_at=submitted_at,
        timeout=timeout,
        allow_live_watch=allow_live_watch,
    )
    lineage = [job_id, *retry_lineage]
    result['retry_source_job_id'] = job_id
    result['retry_successor_job_id'] = successor
    result['retry_lineage'] = lineage
    result['watch_source'] = str(result.get('watch_source') or watch_source)
    return result


def _retry_successor_job_id(context, job_id: str) -> str | None:
    project_root = Path(str(context.project.project_root))
    agents_dir = project_root / '.ccb' / 'agents'
    candidates: list[tuple[float, str]] = []
    for jobs_path in sorted(agents_dir.glob('*/jobs.jsonl')):
        try:
            raw_jobs = jobs_path.read_text(encoding='utf-8')
        except OSError:
            continue
        for raw_line in raw_jobs.splitlines():
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            options = record.get('provider_options')
            if not isinstance(options, dict):
                continue
            if str(options.get('retry_source_job_id') or '').strip() != job_id:
                continue
            successor = str(record.get('job_id') or '').strip()
            if not successor:
                continue
            timestamp = _job_record_timestamp(record)
            candidates.append((timestamp, successor))
    if not candidates:
        return None
    return sorted(candidates)[-1][1]


def _job_record_timestamp(record: dict[str, object]) -> float:
    for key in ('created_at', 'updated_at'):
        value = str(record.get(key) or '').strip()
        if not value:
            continue
        try:
            normalized = value.replace('Z', '+00:00')
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            continue
    return 0.0


def _ask_result_from_watch_payload(
    loop_dir: Path,
    *,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    job_id: str,
    submitted_at: str,
    payload,
    watch_source: str,
    observation_error: str | None = None,
) -> dict[str, object]:
    status = str(_payload_value(payload, 'status') or '').strip()
    reply = str(_payload_value(payload, 'reply') or '')
    terminal = bool(_payload_value(payload, 'terminal'))
    result = {
        'target': target,
        'sender': RUNNER_ASK_SENDER,
        'logical_sender': sender,
        'purpose': purpose,
        'job_id': job_id,
        'submitted_at': submitted_at,
        'status': status,
        'reply': reply,
        'terminal': terminal,
        'watch_source': watch_source,
    }
    visible_reply_source = _payload_value(payload, 'visible_reply_source')
    if visible_reply_source:
        result['visible_reply_source'] = str(visible_reply_source)
    chain_evidence = _payload_value(payload, 'chain_evidence')
    if isinstance(chain_evidence, list):
        result['chain_evidence'] = [dict(item) for item in chain_evidence if isinstance(item, dict)]
    if observation_error:
        result['observation_error'] = observation_error
    if not terminal:
        result['watch_observation'] = 'non_terminal'
        _append_event(
            loop_dir,
            loop_id=loop_id,
            kind='ask_pending',
            payload={'purpose': purpose, 'target': target, 'job_id': job_id, 'status': status or None},
        )
        return result
    artifact_path = loop_dir / 'artifacts' / f'{purpose}-reply.md'
    atomic_write_text(artifact_path, reply)
    result['artifact'] = str(artifact_path)
    _append_event(
        loop_dir,
        loop_id=loop_id,
        kind='ask_terminal',
        payload={'purpose': purpose, 'target': target, 'job_id': job_id, 'status': status, 'watch_source': watch_source},
    )
    return result


def _payload_value(payload, key: str):
    if isinstance(payload, dict):
        return payload.get(key)
    return getattr(payload, key, None)


def _latest_submitted_ask(
    loop_dir: Path,
    *,
    target: str,
    identity: dict[str, object],
) -> dict[str, object] | None:
    path = loop_dir / 'asks.jsonl'
    if not path.is_file():
        return None
    latest: dict[str, object] | None = None
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        if not raw_line.strip():
            continue
        record = json.loads(raw_line)
        if not isinstance(record, dict):
            continue
        if record.get('record_type') != 'ccb_loop_ask_first_ask':
            continue
        if str(record.get('target') or '') != target:
            continue
        if any(record.get(key) != value for key, value in identity.items()):
            continue
        if not str(record.get('job_id') or '').strip():
            continue
        status = str(record.get('status') or '').strip()
        if status and status != 'submitted':
            continue
        latest = dict(record)
    return latest


def _is_watch_timeout(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).strip().lower()
    return 'watch timed out' in text or 'timed out' in text or 'timeout' in text


def _worker_message(
    *,
    loop_id: str,
    task_id: str,
    task_text: str,
    artifact_refs: dict[str, str],
    node_id: str,
    work_packet_ref: str,
    work_packet: str,
) -> str:
    return (
        f'Loop: {loop_id}\n'
        'Role: worker\n'
        f'Task: {task_id}\n'
        f'Node: {node_id}\n'
        f'node_work_packet: {work_packet_ref}\n'
        f"task_packet: {artifact_refs.get('task_packet')}\n"
        f"execution_contract: {artifact_refs.get('execution_contract')}\n\n"
        'Canonical node work packet:\n'
        f'{work_packet}\n\n'
        'Task packet and execution contract evidence:\n'
        f'{task_text}\n\n'
        'Output requirements:\n'
        '- After completing the required verification, stop tool use and send one final answer.\n'
        '- Do not run optional final diff/status commands unless the execution contract explicitly requires them.\n'
        '- The next assistant response after the final required verification command must be the final answer, not a progress update.\n'
        '- Final answer must include: status: done|blocked|needs_rework\n'
        '- Final answer must include: changed_files: <paths or none>\n'
        '- Final answer must include: verification: <commands run and result>\n'
        '- Final answer must include: evidence: task_packet, execution_contract, and artifact refs if any\n'
        '- Do not leave the job at a progress update such as checking final diff or preparing summary.\n'
        '- no hidden fallback or scope shrinkage'
    )


def _reviewer_message(
    *,
    loop_id: str,
    task_id: str,
    task_text: str,
    artifact_refs: dict[str, str],
    node_id: str,
    work_packet_ref: str,
    work_packet: str,
    worker: dict[str, object],
    authority_update: dict[str, object] | None = None,
) -> str:
    authority_lines = _authority_update_evidence_lines(authority_update)
    return (
        f'Loop: {loop_id}\n'
        'Role: code_reviewer\n'
        f'Task: {task_id}\n'
        f'Node: {node_id}\n'
        f'node_work_packet: {work_packet_ref}\n'
        f"task_packet: {artifact_refs.get('task_packet')}\n"
        f"execution_contract: {artifact_refs.get('execution_contract')}\n"
        f'Worker job: {worker.get("job_id")}\n'
        f'Worker reply artifact: {worker.get("artifact")}\n\n'
        f'{authority_lines}'
        'Canonical node work packet:\n'
        f'{work_packet}\n\n'
        'Task packet and execution contract evidence:\n'
        f'{task_text}\n\n'
        'Required contract audit:\n'
        '- validate the task result against project-root evidence after script-owned promotion\n'
        '- explicitly check execution_contract before accepting the round\n'
        '- reject hidden fallback, scope shrink, and fake success\n'
        '- reject pass if required evidence is missing or only implied by provider reply text\n\n'
        'Output requirements:\n'
        '- status: pass|rework_required|blocked|non_converged\n'
        '- execution_contract audit: pass|fail with evidence refs\n'
        '- verification checks performed\n'
        '- concise risk notes'
    )


def _reviewer_requires_rework(reviewer: dict[str, object]) -> bool:
    reply = str(reviewer.get('reply') or '').lower()
    for raw_line in reply.splitlines():
        line = raw_line.strip().lstrip('-').strip()
        if not line.startswith('status:'):
            continue
        value = line.split(':', 1)[1].strip().split()[0].strip('`.,;')
        return value == 'rework_required'
    return False


def _worker_rework_message(
    *,
    loop_id: str,
    task_id: str,
    task_text: str,
    artifact_refs: dict[str, str],
    node_id: str,
    work_packet_ref: str,
    work_packet: str,
    worker: dict[str, object],
    reviewer: dict[str, object],
) -> str:
    return (
        f'Loop: {loop_id}\n'
        'Role: worker\n'
        'Purpose: bounded_rework\n'
        f'Task: {task_id}\n'
        f'Node: {node_id}\n'
        f'node_work_packet: {work_packet_ref}\n'
        f"task_packet: {artifact_refs.get('task_packet')}\n"
        f"execution_contract: {artifact_refs.get('execution_contract')}\n"
        f'Initial worker job: {worker.get("job_id")} status={worker.get("status")}\n'
        f'Reviewer rejection job: {reviewer.get("job_id")} status={reviewer.get("status")}\n'
        f'Reviewer rejection artifact: {reviewer.get("artifact")}\n\n'
        'Canonical node work packet:\n'
        f'{work_packet}\n\n'
        'Task packet and execution contract evidence:\n'
        f'{task_text}\n\n'
        'Output requirements:\n'
        '- status: done|blocked\n'
        '- address exactly the reviewer rejection evidence\n'
        '- cite task_packet, execution_contract, and reviewer rejection artifact\n'
        '- no hidden fallback or scope shrinkage'
    )


def _reviewer_recheck_message(
    *,
    loop_id: str,
    task_id: str,
    task_text: str,
    artifact_refs: dict[str, str],
    node_id: str,
    work_packet_ref: str,
    work_packet: str,
    worker: dict[str, object],
    reviewer: dict[str, object],
    worker_rework: dict[str, object],
    authority_update: dict[str, object] | None = None,
) -> str:
    authority_lines = _authority_update_evidence_lines(authority_update)
    return (
        f'Loop: {loop_id}\n'
        'Role: code_reviewer\n'
        'Purpose: bounded_rework_recheck\n'
        f'Task: {task_id}\n'
        f'Node: {node_id}\n'
        f'node_work_packet: {work_packet_ref}\n'
        f"task_packet: {artifact_refs.get('task_packet')}\n"
        f"execution_contract: {artifact_refs.get('execution_contract')}\n"
        f'Initial worker job: {worker.get("job_id")} status={worker.get("status")}\n'
        f'Initial reviewer job: {reviewer.get("job_id")} status={reviewer.get("status")}\n'
        f'Rework worker job: {worker_rework.get("job_id")} status={worker_rework.get("status")}\n'
        f'Rework artifact: {worker_rework.get("artifact")}\n\n'
        f'{authority_lines}'
        'Canonical node work packet:\n'
        f'{work_packet}\n\n'
        'Task packet and execution contract evidence:\n'
        f'{task_text}\n\n'
        'Output requirements:\n'
        '- status: pass|rework_required|blocked|non_converged\n'
        '- this is the only bounded rework recheck for the round\n'
        '- cite execution_contract and rework evidence before accepting'
    )


def _round_reviewer_message(
    *,
    loop_id: str,
    task_id: str,
    task_text: str,
    artifact_refs: dict[str, str],
    worker: dict[str, object],
    reviewer: dict[str, object],
    rework: dict[str, dict[str, object]],
    authority_update: dict[str, object] | None = None,
) -> str:
    rework_lines = _rework_evidence_lines(rework)
    authority_lines = _authority_update_evidence_lines(authority_update)
    expected_result_lines = _expected_round_result_lines(task_text)
    evidence_lines = ''.join(
        _round_reviewer_reply_evidence(label, evidence)
        for label, evidence in (
            ('Worker', worker),
            ('Reviewer', reviewer),
        )
    )
    return (
        'FINAL ANSWER FORMAT - parser enforced:\n'
        '- Do not describe what you are about to do.\n'
        '- Do not run tests, tools, shell commands, or verification steps; judge only the supplied evidence.\n'
        '- Do not write a preamble such as "I have reviewed the evidence".\n'
        '- Do not write a test-running preamble such as "Now let me run the tests".\n'
        '- The first non-empty line MUST be exactly one standalone machine field:\n'
        '  round result: <pass|partial|replan_required|blocked>\n'
        '- Do not write analysis, headings, greetings, or any other preamble before that line.\n'
        '- Do not wrap the machine line in Markdown fences, bullets, quotes, or backticks.\n'
        '- After that first line, provide evidence and audit details.\n'
        '- If the first non-empty line is not this field, the runner must block the round.\n'
        '- A later `round result: pass` is ignored by the runner and blocks the round.\n\n'
        f'{expected_result_lines}'
        f'Loop: {loop_id}\n'
        'Role: ccb_round_reviewer\n'
        f'Task: {task_id}\n'
        f"task_packet: {artifact_refs.get('task_packet')}\n"
        f"execution_contract: {artifact_refs.get('execution_contract')}\n"
        f'Worker job: {worker.get("job_id")} status={worker.get("status")}\n'
        f'Reviewer job: {reviewer.get("job_id")} status={reviewer.get("status")}\n\n'
        f'{rework_lines}'
        f'{authority_lines}'
        'Supplied round evidence artifacts:\n'
        f'{evidence_lines}'
        'Task packet and execution contract evidence:\n'
        f'{task_text}\n\n'
        'Repeat the machine output protocol:\n'
        '- Your first non-empty line must be `round result: <pass|partial|replan_required|blocked>`.\n'
        '- No preamble, no Markdown fence, no backticks around the machine line.\n\n'
        '- Do not run tests or tools before answering; use the worker/reviewer evidence already supplied.\n'
        '- If evidence is insufficient, the first line must be `round result: blocked`.\n\n'
        'Output requirements:\n'
        '- validate final result against project-root evidence, not isolated worker workspace evidence\n'
        '- verification performed against execution_contract\n'
        '- hidden fallback/scope shrink/fake success audit\n'
        '- evidence refs\n'
        '- recommended next owner'
    )


def _round_reviewer_reply_evidence(label: str, evidence: dict[str, object]) -> str:
    artifact = str(evidence.get('artifact') or '').strip()
    lines = [
        f'{label} reply artifact: {artifact or "<missing>"}',
    ]
    if artifact:
        try:
            text = Path(artifact).read_text(encoding='utf-8').strip()
        except OSError as exc:
            text = f'<artifact read failed: {exc.__class__.__name__}: {exc}>'
        truncated = len(text) > ROUND_REVIEWER_EVIDENCE_SNIPPET_LIMIT
        snippet = text[:ROUND_REVIEWER_EVIDENCE_SNIPPET_LIMIT] if text else '<empty>'
        suffix = ' (truncated)' if truncated else ''
        lines.extend([
            f'{label} reply content{suffix}:',
            '```text',
            snippet,
            '```',
        ])
    lines.append('')
    return '\n'.join(lines) + '\n'


def _round_reviewer_correction_message(
    *,
    task_id: str,
    original: dict[str, object],
    result_source: str,
    failure: dict[str, object] | None,
) -> str:
    unknown = ''
    if isinstance(failure, dict) and failure.get('unknown_round_result'):
        unknown = f"Unknown first-line value observed: {failure.get('unknown_round_result')}\n"
    first_line = _first_non_empty_reply_line(str(original.get('reply') or '')) or '<missing>'
    return (
        'Your previous ccb_round_reviewer reply could not be imported by the runner.\n'
        f'Task: {task_id}\n'
        f"Previous reviewer job: {original.get('job_id')}\n"
        f"Previous reply artifact: {original.get('artifact')}\n"
        f'Import failure: {result_source}\n'
        f'Previous first non-empty line: {first_line}\n'
        f'{unknown}'
        '\n'
        'Return a corrected machine-readable result for the same evidence.\n'
        'Do not run tests, tools, shell commands, CCB commands, or workflow wrappers.\n'
        'Do not infer from this correction request alone; use the evidence you already reviewed.\n'
        'If that evidence is insufficient, the first line must be exactly: round result: blocked\n'
        '\n'
        'FINAL ANSWER FORMAT - parser enforced:\n'
        '- The first non-empty line MUST be exactly one standalone machine field:\n'
        '  round result: <pass|partial|replan_required|blocked>\n'
        '- No preamble, heading, Markdown fence, bullet, quote, or backticks before that line.\n'
        '- A later machine line after prose is invalid and will still block the round.\n'
    )


def _expected_round_result_lines(task_text: str) -> str:
    expected = _expected_round_result(task_text)
    if expected is None:
        return ''
    return (
        f'Contract-declared expected round result: {expected}\n'
        f'- If the supplied evidence supports that contract expectation, your first line must be exactly: round result: {expected}\n'
        '- If the evidence does not support that expectation, your first line must still be one of: '
        'round result: pass|partial|replan_required|blocked\n\n'
    )


def _expected_round_result(task_text: str) -> str | None:
    allowed = {'pass', 'partial', 'replan_required', 'blocked'}
    keys = {'expected_round_result', 'expected_round_result_if_converged'}
    for raw_line in task_text.splitlines():
        line = raw_line.strip().lower().lstrip('-').strip()
        if ':' not in line:
            continue
        key, raw_value = line.split(':', 1)
        if key.strip() not in keys:
            continue
        value = _normalise_round_result_value(raw_value)
        if value in allowed:
            return value
    return None


def _rework_evidence_lines(rework: dict[str, dict[str, object]]) -> str:
    if not rework:
        return ''
    lines = ['Bounded rework evidence:']
    for purpose, evidence in rework.items():
        lines.append(
            f'- {purpose}: target={evidence.get("target")} job={evidence.get("job_id")} '
            f'status={evidence.get("status")} artifact={evidence.get("artifact")}'
        )
    lines.append('')
    return '\n'.join(lines)


def _authority_update_evidence_lines(authority_update: dict[str, object] | None) -> str:
    if not isinstance(authority_update, dict):
        return ''
    changed_files = authority_update.get('changed_files') if isinstance(authority_update.get('changed_files'), list) else []
    allowed_change_paths = (
        authority_update.get('allowed_change_paths')
        if isinstance(authority_update.get('allowed_change_paths'), list)
        else []
    )
    lines = [
        'Project-root authority evidence:',
        f'- source: {authority_update.get("source")}',
        f'- operation: {authority_update.get("operation")}',
        f'- worker_workspace: {authority_update.get("workspace_path")}',
        f'- project_root: {authority_update.get("project_root")}',
        f'- verified_project_root: {authority_update.get("verified_project_root")}',
    ]
    if changed_files:
        lines.append(f'- changed_files: {", ".join(str(path) for path in changed_files)}')
    if allowed_change_paths:
        lines.append(f'- allowed_change_paths: {", ".join(str(path) for path in allowed_change_paths)}')
    lines.append('')
    return '\n'.join(lines) + '\n'


def _round_summary_text(
    *,
    loop_id: str,
    task_id: str,
    result: str,
    result_source: str,
    artifact_refs: dict[str, str],
    topology: dict[str, object],
    worker: dict[str, object],
    reviewer: dict[str, object],
    rework: dict[str, dict[str, object]],
    orchestrator: dict[str, object],
    round_reviewer: dict[str, object],
    failure: dict[str, object] | None = None,
    authority_update: dict[str, object] | None = None,
    project_root_test: dict[str, object] | None = None,
) -> str:
    topology_status = topology.get('status') if isinstance(topology.get('status'), dict) else {}
    commit = topology.get('commit') if isinstance(topology.get('commit'), dict) else {}
    reconcile = commit.get('reconcile') if isinstance(commit, dict) and isinstance(commit.get('reconcile'), dict) else {}
    lines = [
        '# Round Summary',
        '',
        f'task_id: {task_id}',
        f'loop_id: {loop_id}',
        f'round result: {result}',
        f'round_result_source: {result_source}',
        f"task_packet: {artifact_refs.get('task_packet')}",
        f"execution_contract: {artifact_refs.get('execution_contract')}",
        '',
        '## Ask Evidence',
        '',
        f"- worker: {worker.get('target')} job={worker.get('job_id')} status={worker.get('status')} artifact={worker.get('artifact')}",
        f"- reviewer: {reviewer.get('target')} job={reviewer.get('job_id')} status={reviewer.get('status')} artifact={reviewer.get('artifact')}",
        f"- orchestrator: {orchestrator.get('target')} job={orchestrator.get('job_id')} status={orchestrator.get('status')} artifact={orchestrator.get('artifact')}",
        f"- ccb_round_reviewer: {round_reviewer.get('target')} job={round_reviewer.get('job_id')} status={round_reviewer.get('status')} artifact={round_reviewer.get('artifact')}",
        '',
        '## Topology Evidence',
        '',
        f"- desired: {commit.get('desired_path')}",
        f"- observed: {reconcile.get('observed_path') or topology_status.get('observed_path')}",
        f"- status: {topology_status.get('loop_topology_status')}",
        "- release policy: auto after round_summary import",
        '',
        ]
    if rework:
        insertion = 14
        lines[insertion:insertion] = [
            f"- {purpose}: {evidence.get('target')} job={evidence.get('job_id')} "
            f"status={evidence.get('status')} artifact={evidence.get('artifact')}"
            for purpose, evidence in rework.items()
        ]
    if authority_update is not None:
        changed_files = (
            authority_update.get('changed_files')
            if isinstance(authority_update.get('changed_files'), list)
            else []
        )
        allowed_change_paths = (
            authority_update.get('allowed_change_paths')
            if isinstance(authority_update.get('allowed_change_paths'), list)
            else []
        )
        lines.extend(
            [
                '## Authority Update',
                '',
                f"- source: {authority_update.get('source')}",
                f"- stage: {authority_update.get('stage')}",
                f"- operation: {authority_update.get('operation')}",
                f"- worker_agent: {authority_update.get('worker_agent')}",
                f"- workspace_mode: {authority_update.get('workspace_mode')}",
                f"- verified_project_root: {authority_update.get('verified_project_root')}",
            ]
        )
        if changed_files:
            lines.append(f"- changed_files: {', '.join(str(path) for path in changed_files)}")
        if allowed_change_paths:
            lines.append(f"- allowed_change_paths: {', '.join(str(path) for path in allowed_change_paths)}")
        lines.append('')
    if project_root_test is not None:
        lines.extend(
            [
                '## Project Root Test',
                '',
                f"- test_command: {project_root_test.get('test_command')}",
                f"- test_cwd: {project_root_test.get('test_cwd')}",
                f"- test_resolution_path: {project_root_test.get('test_resolution_path')}",
                f"- test_result: {project_root_test.get('test_result')}",
                f"- test_file_resolved_to_lab: {project_root_test.get('test_file_resolved_to_lab')}",
                f"- test_sys_path_project_first: {project_root_test.get('test_sys_path_project_first')}",
                '',
            ]
        )
    if failure is not None:
        changed_files = failure.get('changed_files') if isinstance(failure.get('changed_files'), list) else []
        deleted_files = failure.get('deleted_files') if isinstance(failure.get('deleted_files'), list) else []
        allowed_change_paths = (
            failure.get('allowed_change_paths')
            if isinstance(failure.get('allowed_change_paths'), list)
            else []
        )
        out_of_scope_files = (
            failure.get('out_of_scope_files')
            if isinstance(failure.get('out_of_scope_files'), list)
            else []
        )
        lines.extend(
            [
                '## Blocker Evidence',
                '',
                f"- source: {failure.get('source')}",
                f"- stage: {failure.get('stage')}",
                f"- error_type: {failure.get('error_type')}",
                f"- error: {failure.get('error')}",
                f"- loop_topology_status: {failure.get('loop_topology_status')}",
            ]
        )
        if changed_files:
            lines.append(f"- changed_files: {', '.join(str(path) for path in changed_files)}")
        if allowed_change_paths:
            lines.append(f"- allowed_change_paths: {', '.join(str(path) for path in allowed_change_paths)}")
        if out_of_scope_files:
            lines.append(f"- out_of_scope_files: {', '.join(str(path) for path in out_of_scope_files)}")
        if deleted_files:
            lines.append(f"- deleted_files: {', '.join(str(path) for path in deleted_files)}")
        if failure.get('changed_file_count') is not None:
            lines.append(f"- changed_file_count: {failure.get('changed_file_count')}")
        lines.append('')
    lines.extend(
        [
            '## Contract Audit',
            '',
            '- reviewer was instructed to check execution_contract explicitly',
            '- hidden fallback, scope shrink, and fake success are rejected conditions',
            '',
        ]
    )
    return '\n'.join(lines)


def _round_result(payload: dict[str, object]) -> tuple[str, str, dict[str, object] | None]:
    declared, unknown, source_field = _declared_round_result(payload)
    if declared is not None:
        reviewer = payload.get(ROUND_REVIEWER_FIELD) if isinstance(payload.get(ROUND_REVIEWER_FIELD), dict) else {}
        if source_field == LEGACY_ROUND_CHECKER_FIELD:
            source = 'round_checker_reply'
        elif str(reviewer.get('purpose') or '') == ROUND_REVIEWER_CORRECTION_PURPOSE:
            source = 'round_reviewer_correction_reply'
        else:
            source = 'round_reviewer_reply'
        return declared, source, None
    if unknown:
        return (
            'blocked',
            'unknown_round_result',
            {
                'source': 'unknown_round_result',
                'stage': 'round_result',
                'reason': f'unknown round result {unknown!r}',
                'error': f'unknown round result {unknown!r}',
                'unknown_round_result': unknown,
            },
        )
    if str(payload.get('loop_run_status') or '') == 'ok':
        return 'blocked', 'missing_round_reviewer_result', None
    return (
        'blocked',
        'loop_run_status',
        {
            'source': 'loop_run_status',
            'stage': 'round_status',
            'error': f"loop run status {payload.get('loop_run_status') or 'missing'}",
        },
    )


def _declared_round_result(payload: dict[str, object]) -> tuple[str | None, str | None, str]:
    reviewer = payload.get(ROUND_REVIEWER_FIELD) if isinstance(payload.get(ROUND_REVIEWER_FIELD), dict) else {}
    source_field = ROUND_REVIEWER_FIELD
    if not reviewer and isinstance(payload.get(LEGACY_ROUND_CHECKER_FIELD), dict):
        reviewer = payload[LEGACY_ROUND_CHECKER_FIELD]
        source_field = LEGACY_ROUND_CHECKER_FIELD
    reply = str(reviewer.get('reply') or '')
    mapping = {
        'pass': 'pass',
        'partial': 'partial',
        'replan_required': 'replan_required',
        'blocked': 'blocked',
        'global_blocker': 'blocked',
    }
    line = _first_non_empty_reply_line(reply)
    if line is None:
        return None, None, source_field
    value = _standalone_round_result_value_from_line(line)
    if value is None:
        return None, None, source_field
    if value not in mapping:
        return None, value, source_field
    return mapping[value], None, source_field


def _first_non_empty_reply_line(reply: str) -> str | None:
    for raw_line in reply.splitlines():
        line = raw_line.strip().lower()
        if line:
            return line
    return None


def _standalone_round_result_value_from_line(line: str) -> str | None:
    if line.startswith('round result:'):
        value_text = line.split(':', 1)[1].strip()
    elif line.startswith('round_result:'):
        value_text = line.split(':', 1)[1].strip()
    else:
        return None
    value = _normalise_round_result_value(value_text)
    return value if value_text == value else None


def _normalise_round_result_value(value_text: str) -> str:
    return value_text.strip().split()[0].strip('`"\'.,;:()[]{}')


def _round_status(*results: dict[str, object]) -> str:
    if all(str(result.get('status') or '') == 'completed' for result in results):
        return 'ok'
    return 'incomplete'


def _round_pending(*results: dict[str, object]) -> dict[str, object] | None:
    for result in results:
        if not result:
            continue
        if bool(result.get('terminal')):
            continue
        purpose = str(result.get('purpose') or 'ask').strip()
        status = str(result.get('status') or '').strip()
        reason = str(result.get('observation_error') or '').strip()
        if not reason:
            reason = f'{purpose} job status {status or "missing"} is not terminal'
        pending: dict[str, object] = {
            'source': str(result.get('pending_source') or 'ask_job_pending'),
            'stage': f'{purpose}_ask',
            'purpose': purpose,
            'reason': reason,
            'target': result.get('target'),
            'job_id': result.get('job_id'),
            'submitted_at': result.get('submitted_at'),
            'job_status': status or None,
        }
        for key in ('watch_source', 'watch_observation', 'visible_reply_source'):
            value = result.get(key)
            if value:
                pending[key] = value
        return pending
    return None


def _round_status_failure(*results: dict[str, object]) -> dict[str, object] | None:
    for result in results:
        status = str(result.get('status') or '').strip()
        if status == 'completed':
            continue
        purpose = str(result.get('purpose') or 'ask').strip()
        source = 'ask_job_incomplete'
        if str(result.get('watch_observation') or '') == 'error':
            source = 'watch_failed'
            reason = str(result.get('observation_error') or '').strip() or f'{purpose} watch failed'
        else:
            reason = f'{purpose} job status {status or "missing"}; expected completed'
        failure: dict[str, object] = {
            'source': source,
            'stage': f'{purpose}_ask',
            'reason': reason,
            'error': reason,
            'target': result.get('target'),
            'job_id': result.get('job_id'),
            'job_status': status or None,
        }
        for key in ('watch_source', 'watch_observation', 'visible_reply_source'):
            value = result.get(key)
            if value:
                failure[key] = value
        return failure
    return None


def _promote_project_root_authority(
    context,
    *,
    round_result: str,
    worker_agent: str,
    task_text: str,
    worker: dict[str, object] | None = None,
    allow_noop_verified: bool = False,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    if round_result != 'pass':
        return None, None
    binding_path = _workspace_binding_path(context, worker_agent)
    if not binding_path.is_file():
        configured_mode = _configured_workspace_mode(context, WORKER_PROFILE)
        if configured_mode == 'inplace':
            return None, None
        reason = (
            'round reviewer declared pass, but worker workspace binding is missing; '
            'configured non-inplace workers require script-owned project-root promotion evidence'
        )
        return None, {
            'source': 'workspace_binding_missing',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            'worker_agent': worker_agent,
            'workspace_mode_configured': configured_mode,
            'workspace_binding': str(binding_path),
            'project_root': str(context.project.project_root),
            'changed_files': [],
        }
    try:
        binding = json.loads(binding_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        reason = f'workspace binding {binding_path} is not valid JSON: {exc}'
        return None, {
            'source': 'workspace_binding_invalid',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            'worker_agent': worker_agent,
            'workspace_binding': str(binding_path),
        }
    except Exception as exc:
        return None, _failure_record(source='workspace_binding_unreadable', stage='round_authority', exc=exc)
    if not isinstance(binding, dict):
        reason = f'workspace binding {binding_path} is not an object'
        return None, {
            'source': 'workspace_binding_invalid',
            'stage': 'round_authority',
            'error': reason,
            'reason': reason,
            'workspace_binding': str(binding_path),
    }
    workspace_mode = str(binding.get('workspace_mode') or '').strip()
    if not workspace_mode:
        reason = f'workspace binding {binding_path} does not declare workspace_mode'
        return None, {
            'source': 'workspace_binding_invalid',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            'worker_agent': worker_agent,
            'workspace_binding': str(binding_path),
        }
    if workspace_mode == 'inplace':
        return None, None
    workspace_path = _workspace_path_from_binding(context, worker_agent, binding)
    if not str(binding.get('workspace_path') or '').strip():
        reason = f'workspace binding {binding_path} does not declare workspace_path'
        return None, {
            'source': 'workspace_binding_invalid',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            'worker_agent': worker_agent,
            'workspace_mode': workspace_mode,
            'workspace_binding': str(binding_path),
        }
    project_root = Path(context.project.project_root)
    if _same_resolved_path(workspace_path, project_root):
        return None, None
    allowed_change_paths = _declared_allowed_change_paths(task_text)
    changed_files = _changed_workspace_files(workspace_path, project_root)
    deleted_files = _deleted_workspace_files(workspace_path, project_root, workspace_mode=workspace_mode)
    ignored_control_changed_files: list[str] = []
    ignored_control_deleted_files: list[str] = []
    if workspace_mode == 'copy':
        changed_files, ignored_control_changed_files = _ignore_copy_workspace_control_drift(
            changed_files,
            allowed_change_paths,
        )
        deleted_files, ignored_control_deleted_files = _ignore_copy_workspace_control_drift(
            deleted_files,
            allowed_change_paths,
        )
    if len(changed_files) > MAX_PROMOTED_WORKSPACE_FILES:
        reason = (
            f'worker workspace changed more than {MAX_PROMOTED_WORKSPACE_FILES} files; '
            'script-owned promotion requires a smaller explicit delta'
        )
        return None, {
            'source': 'isolated_workspace_change_limit_exceeded',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            'worker_agent': worker_agent,
            'workspace_mode': workspace_mode or None,
            'workspace_path': str(workspace_path),
            'project_root': str(project_root),
            'workspace_binding': str(binding_path),
            'changed_files': changed_files[:MAX_PROMOTED_WORKSPACE_FILES],
            'changed_file_count': len(changed_files),
            'ignored_control_changed_files': ignored_control_changed_files,
            'ignored_control_deleted_files': ignored_control_deleted_files,
        }
    if deleted_files:
        reason = (
            'worker workspace deleted or renamed project-root files; direct_execution promotion only supports '
            'additions/modifications, so script-owned import cannot verify this pass'
        )
        return None, {
            'source': 'isolated_workspace_deletions_unsupported',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            'worker_agent': worker_agent,
            'workspace_mode': workspace_mode or None,
            'workspace_path': str(workspace_path),
            'project_root': str(project_root),
            'workspace_binding': str(binding_path),
            'changed_files': changed_files,
            'deleted_files': deleted_files,
            'ignored_control_changed_files': ignored_control_changed_files,
            'ignored_control_deleted_files': ignored_control_deleted_files,
        }
    if not changed_files:
        already_applied = _already_applied_declared_workspace_files(
            workspace_path,
            project_root,
            allowed_change_paths,
            worker,
        )
        if already_applied:
            return {
                'source': 'isolated_workspace_declared_changes_already_project_root',
                'stage': 'round_authority',
                'operation': 'verify_worker_declared_files_match_project_root',
                'worker_agent': worker_agent,
                'workspace_mode': workspace_mode or None,
                'workspace_path': str(workspace_path),
                'project_root': str(project_root),
                'workspace_binding': str(binding_path),
                'changed_files': already_applied,
                'allowed_change_paths': allowed_change_paths,
                'ignored_control_changed_files': ignored_control_changed_files,
                'ignored_control_deleted_files': ignored_control_deleted_files,
                'verified_project_root': True,
                '_project_root_rollback': {},
            }, None
        if allow_noop_verified:
            return {
                'source': 'isolated_workspace_changes_already_promoted',
                'stage': 'round_authority',
                'operation': 'verify_worker_workspace_matches_project_root',
                'worker_agent': worker_agent,
                'workspace_mode': workspace_mode or None,
                'workspace_path': str(workspace_path),
                'project_root': str(project_root),
                'workspace_binding': str(binding_path),
                'changed_files': [],
                'allowed_change_paths': allowed_change_paths,
                'ignored_control_changed_files': ignored_control_changed_files,
                'ignored_control_deleted_files': ignored_control_deleted_files,
                'verified_project_root': True,
                '_project_root_rollback': {},
            }, None
        reason = (
            'round reviewer declared pass from an isolated worker workspace, but no project-root effects were '
            'detected for script-owned promotion'
        )
        return None, {
            'source': 'isolated_workspace_no_project_root_effect',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            'worker_agent': worker_agent,
            'workspace_mode': workspace_mode or None,
            'workspace_path': str(workspace_path),
            'project_root': str(project_root),
            'workspace_binding': str(binding_path),
            'changed_files': [],
            'ignored_control_changed_files': ignored_control_changed_files,
            'ignored_control_deleted_files': ignored_control_deleted_files,
        }
    if not allowed_change_paths:
        reason = (
            'worker workspace has project-root deltas, but task packet/execution contract did not declare '
            'allowed_change_paths for script-owned isolated workspace promotion'
        )
        return None, {
            'source': 'isolated_workspace_change_scope_missing',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            'worker_agent': worker_agent,
            'workspace_mode': workspace_mode or None,
            'workspace_path': str(workspace_path),
            'project_root': str(project_root),
            'workspace_binding': str(binding_path),
            'changed_files': changed_files,
            'allowed_change_paths': [],
            'ignored_control_changed_files': ignored_control_changed_files,
            'ignored_control_deleted_files': ignored_control_deleted_files,
        }
    out_of_scope = [
        changed_file
        for changed_file in changed_files
        if not _path_allowed_by_scope(changed_file, allowed_change_paths)
    ]
    if out_of_scope:
        reason = 'worker workspace contains changes outside task-declared allowed_change_paths'
        return None, {
            'source': 'isolated_workspace_change_scope_violation',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            'worker_agent': worker_agent,
            'workspace_mode': workspace_mode or None,
            'workspace_path': str(workspace_path),
            'project_root': str(project_root),
            'workspace_binding': str(binding_path),
            'changed_files': changed_files,
            'allowed_change_paths': allowed_change_paths,
            'out_of_scope_files': out_of_scope,
            'ignored_control_changed_files': ignored_control_changed_files,
            'ignored_control_deleted_files': ignored_control_deleted_files,
        }
    rollback = _capture_project_files(project_root, changed_files)
    try:
        _copy_workspace_files(workspace_path, project_root, changed_files)
    except Exception as exc:
        _restore_project_files(project_root, rollback)
        failure = _failure_record(source='isolated_workspace_promotion_failed', stage='round_authority', exc=exc)
        failure.update(
            {
                'worker_agent': worker_agent,
                'workspace_mode': workspace_mode or None,
                'workspace_path': str(workspace_path),
                'project_root': str(project_root),
                'workspace_binding': str(binding_path),
                'changed_files': changed_files,
                'ignored_control_changed_files': ignored_control_changed_files,
                'ignored_control_deleted_files': ignored_control_deleted_files,
            }
        )
        return None, failure
    unapplied = _unapplied_workspace_files(workspace_path, project_root, changed_files)
    if unapplied:
        _restore_project_files(project_root, rollback)
        reason = (
            'round reviewer declared pass, but worker workspace changes could not be verified in the project root'
        )
        return None, {
            'source': 'isolated_workspace_changes_not_applied',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            'worker_agent': worker_agent,
            'workspace_mode': workspace_mode or None,
            'workspace_path': str(workspace_path),
            'project_root': str(project_root),
            'workspace_binding': str(binding_path),
            'changed_files': unapplied,
            'ignored_control_changed_files': ignored_control_changed_files,
            'ignored_control_deleted_files': ignored_control_deleted_files,
        }
    return {
        'source': 'isolated_workspace_changes_promoted',
        'stage': 'round_authority',
        'operation': 'copy_worker_workspace_files_to_project_root',
        'worker_agent': worker_agent,
        'workspace_mode': workspace_mode or None,
        'workspace_path': str(workspace_path),
        'project_root': str(project_root),
        'workspace_binding': str(binding_path),
        'changed_files': changed_files,
        'allowed_change_paths': allowed_change_paths,
        'ignored_control_changed_files': ignored_control_changed_files,
        'ignored_control_deleted_files': ignored_control_deleted_files,
        'verified_project_root': True,
        '_project_root_rollback': rollback,
    }, None


def _configured_workspace_mode(context, profile: str) -> str | None:
    try:
        config = load_project_config(Path(context.project.project_root), include_loop_overlays=False).config
    except Exception:
        return None
    loop_capacity = getattr(config, 'loop_capacity', None)
    profiles = getattr(loop_capacity, 'role_profiles', {}) if loop_capacity is not None else {}
    role_profile = profiles.get(profile) if isinstance(profiles, dict) else None
    workspace_mode = getattr(role_profile, 'workspace_mode', None)
    value = getattr(workspace_mode, 'value', workspace_mode)
    text = str(value or '').strip()
    return text or None


def _workspace_binding_path(context, agent_name: str) -> Path:
    candidates = _workspace_binding_candidate_paths(context, agent_name)
    for path in candidates:
        if path.is_file():
            return path
    if candidates:
        return candidates[0]
    workspaces_dir = getattr(context.paths, 'workspaces_dir', None)
    if workspaces_dir is None:
        workspaces_dir = Path(context.project.project_root) / '.ccb' / 'workspaces'
    return Path(workspaces_dir) / agent_name / '.ccb-workspace.json'


def _workspace_binding_candidate_paths(context, agent_name: str) -> list[Path]:
    candidates: list[Path] = []
    workspace_group = _agent_workspace_group(context, agent_name)
    if workspace_group:
        group_binding = getattr(context.paths, 'workspace_group_binding_path', None)
        if callable(group_binding):
            candidates.append(Path(group_binding(workspace_group)))
        else:
            workspaces_dir = getattr(context.paths, 'workspaces_dir', None)
            if workspaces_dir is None:
                workspaces_dir = Path(context.project.project_root) / '.ccb' / 'workspaces'
            candidates.append(Path(workspaces_dir) / 'groups' / workspace_group / '.ccb-workspace.json')
    workspace_binding = getattr(context.paths, 'workspace_binding_path', None)
    if callable(workspace_binding):
        candidates.append(Path(workspace_binding(agent_name)))
    else:
        workspaces_dir = getattr(context.paths, 'workspaces_dir', None)
        if workspaces_dir is None:
            workspaces_dir = Path(context.project.project_root) / '.ccb' / 'workspaces'
        candidates.append(Path(workspaces_dir) / agent_name / '.ccb-workspace.json')
    return _unique_path_candidates(candidates)


def _agent_workspace_group(context, agent_name: str) -> str | None:
    for path in _agent_spec_candidate_paths(context, agent_name):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        workspace_group = str(payload.get('workspace_group') or '').strip()
        if workspace_group:
            return workspace_group
    return None


def _agent_spec_candidate_paths(context, agent_name: str) -> list[Path]:
    candidates: list[Path] = []
    agent_spec_path = getattr(context.paths, 'agent_spec_path', None)
    if callable(agent_spec_path):
        candidates.append(Path(agent_spec_path(agent_name)))
    agent_anchor_dir = getattr(context.paths, 'agent_anchor_dir', None)
    if callable(agent_anchor_dir):
        candidates.append(Path(agent_anchor_dir(agent_name)) / 'agent.json')
    candidates.append(Path(context.project.project_root) / '.ccb' / 'agents' / agent_name / 'agent.json')
    return _unique_path_candidates(candidates)


def _unique_path_candidates(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _workspace_path_from_binding(context, agent_name: str, binding: dict[str, object]) -> Path:
    workspace_text = str(binding.get('workspace_path') or '').strip()
    if workspace_text:
        return Path(workspace_text)
    return _workspace_binding_path(context, agent_name).parent


def _changed_workspace_files(workspace_path: Path, project_root: Path) -> list[str]:
    if not workspace_path.is_dir():
        return []
    changed: list[str] = []
    for path in sorted(workspace_path.rglob('*')):
        try:
            relative = path.relative_to(workspace_path)
        except ValueError:
            continue
        if _ignore_workspace_relative(relative):
            continue
        project_path = project_root / relative
        if path.is_dir():
            continue
        if not project_path.is_file() or not filecmp.cmp(path, project_path, shallow=False):
            changed.append(relative.as_posix())
    return changed


def _deleted_workspace_files(workspace_path: Path, project_root: Path, *, workspace_mode: str | None = None) -> list[str]:
    if not workspace_path.is_dir():
        return []
    if str(workspace_mode or '').strip() == 'git-worktree':
        git_deleted = _git_workspace_deleted_files(workspace_path)
        if git_deleted is not None:
            return git_deleted
    deleted: list[str] = []
    for path in sorted(project_root.rglob('*')):
        try:
            relative = path.relative_to(project_root)
        except ValueError:
            continue
        if _ignore_workspace_relative(relative):
            continue
        if path.is_dir():
            continue
        if not (workspace_path / relative).exists():
            deleted.append(relative.as_posix())
    return deleted


def _git_workspace_deleted_files(workspace_path: Path) -> list[str] | None:
    try:
        completed = subprocess.run(
            ['git', 'status', '--porcelain=v1', '--untracked-files=no'],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    deleted: list[str] = []
    for raw_line in completed.stdout.splitlines():
        if len(raw_line) < 4:
            continue
        status = raw_line[:2]
        if 'D' not in status and 'R' not in status:
            continue
        path_text = raw_line[3:].strip()
        if ' -> ' in path_text:
            path_text = path_text.split(' -> ', 1)[0].strip()
        path_text = path_text.strip('"')
        try:
            relative = _safe_relative_path(path_text)
        except ValueError:
            deleted.append(path_text)
            continue
        if _ignore_workspace_relative(relative):
            continue
        deleted.append(relative.as_posix())
    return _unique_paths(deleted)


def _copy_workspace_files(workspace_path: Path, project_root: Path, changed_files: list[str]) -> None:
    for changed_file in changed_files:
        relative = _safe_relative_path(changed_file)
        source = workspace_path / relative
        destination = project_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _declared_allowed_change_paths(task_text: str) -> list[str]:
    return declared_allowed_change_paths(task_text)


def _split_declared_paths(value: str) -> list[str]:
    return split_declared_paths(value)


def _path_allowed_by_scope(changed_file: str, allowed_change_paths: list[str]) -> bool:
    return path_allowed_by_scope(changed_file, allowed_change_paths)


def _ignore_copy_workspace_control_drift(
    paths: list[str],
    allowed_change_paths: list[str],
) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    ignored: list[str] = []
    for path in paths:
        if _path_allowed_by_scope(path, allowed_change_paths):
            kept.append(path)
            continue
        try:
            relative = _safe_relative_path(path)
        except ValueError:
            kept.append(path)
            continue
        if _copy_workspace_control_drift_relative(relative):
            ignored.append(relative.as_posix())
            continue
        kept.append(path)
    return kept, ignored


def _already_applied_declared_workspace_files(
    workspace_path: Path,
    project_root: Path,
    allowed_change_paths: list[str],
    worker: dict[str, object] | None,
) -> list[str]:
    declared = _declared_worker_changed_files(worker)
    if not declared or not allowed_change_paths:
        return []
    verified: list[str] = []
    for changed_file in declared:
        try:
            relative = _safe_relative_path(changed_file)
        except ValueError:
            return []
        normalized = relative.as_posix()
        if not _path_allowed_by_scope(normalized, allowed_change_paths):
            return []
        source = workspace_path / relative
        destination = project_root / relative
        if not source.is_file() or not destination.is_file():
            return []
        if not filecmp.cmp(source, destination, shallow=False):
            return []
        verified.append(normalized)
    return _unique_paths(verified)


def _declared_worker_changed_files(worker: dict[str, object] | None) -> list[str]:
    if not isinstance(worker, dict):
        return []
    reply = str(worker.get('reply') or '')
    declared: list[str] = []
    for raw_line in reply.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        line = stripped.lstrip('-*').strip()
        lower = line.lower()
        for prefix in WORKER_CHANGED_FILE_PREFIXES:
            if lower.startswith(prefix):
                declared.extend(_split_declared_paths(line.split(':', 1)[1]))
                break
    return _unique_paths(declared)


def _copy_workspace_control_drift_relative(relative: Path) -> bool:
    parts = relative.parts
    if not parts:
        return False
    if parts[0] in {'logs', 'evidence'}:
        return True
    if relative.as_posix() == 'command_log.tsv':
        return True
    return len(parts) >= 3 and parts[:3] == ('docs', 'plantree', 'plans')


def _capture_project_files(project_root: Path, changed_files: list[str]) -> dict[str, bytes | None]:
    rollback: dict[str, bytes | None] = {}
    for changed_file in changed_files:
        relative = _safe_relative_path(changed_file)
        destination = project_root / relative
        rollback[relative.as_posix()] = destination.read_bytes() if destination.is_file() else None
    return rollback


def _restore_project_files(project_root: Path, rollback: dict[str, bytes | None]) -> None:
    for changed_file, content in rollback.items():
        relative = _safe_relative_path(changed_file)
        destination = project_root / relative
        if content is None:
            if destination.exists():
                destination.unlink()
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def _authority_update_rollback(authority_update: dict[str, object] | None) -> dict[str, bytes | None] | None:
    if not isinstance(authority_update, dict):
        return None
    rollback = authority_update.get('_project_root_rollback')
    if not isinstance(rollback, dict):
        return None
    return {
        str(key): (bytes(value) if isinstance(value, bytes) else None)
        for key, value in rollback.items()
    }


def _merge_authority_updates(
    previous: dict[str, object] | None,
    current: dict[str, object] | None,
) -> dict[str, object] | None:
    if not isinstance(previous, dict):
        return current
    if not isinstance(current, dict):
        return previous
    current_changed = current.get('changed_files') if isinstance(current.get('changed_files'), list) else []
    if not current_changed:
        return previous
    merged = dict(current)
    previous_changed = previous.get('changed_files') if isinstance(previous.get('changed_files'), list) else []
    merged['changed_files'] = _unique_paths([*previous_changed, *current_changed])
    previous_allowed = (
        previous.get('allowed_change_paths')
        if isinstance(previous.get('allowed_change_paths'), list)
        else []
    )
    current_allowed = (
        current.get('allowed_change_paths')
        if isinstance(current.get('allowed_change_paths'), list)
        else []
    )
    merged['allowed_change_paths'] = _unique_paths([*previous_allowed, *current_allowed])
    previous_rollback = previous.get('_project_root_rollback')
    current_rollback = current.get('_project_root_rollback')
    rollback = dict(current_rollback) if isinstance(current_rollback, dict) else {}
    if isinstance(previous_rollback, dict):
        rollback.update(previous_rollback)
    merged['_project_root_rollback'] = rollback
    previous_count = int(previous.get('promotion_count') or 1)
    current_count = int(current.get('promotion_count') or 1)
    merged['promotion_count'] = previous_count + current_count
    return merged


def _unique_paths(paths: list[object]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        text = str(path or '').strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _restore_authority_update(
    context,
    authority_update: dict[str, object] | None,
    failure: dict[str, object] | None,
    *,
    reason: str,
) -> bool:
    rollback = _authority_update_rollback(authority_update)
    if not rollback:
        return False
    _restore_project_files(Path(context.project.project_root), rollback)
    if isinstance(authority_update, dict):
        authority_update['authority_rollback'] = 'restored_project_root'
        authority_update['authority_rollback_reason'] = reason
    if isinstance(failure, dict):
        failure['authority_rollback'] = 'restored_project_root'
        failure['authority_rollback_reason'] = reason
    return True


def _public_authority_update(authority_update: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(authority_update, dict):
        return None
    public = dict(authority_update)
    public.pop('_project_root_rollback', None)
    return public


def _unapplied_workspace_files(workspace_path: Path, project_root: Path, changed_files: list[str]) -> list[str]:
    unapplied: list[str] = []
    for changed_file in changed_files:
        relative = _safe_relative_path(changed_file)
        source = workspace_path / relative
        destination = project_root / relative
        if not destination.is_file() or not filecmp.cmp(source, destination, shallow=False):
            unapplied.append(relative.as_posix())
    return unapplied


def _safe_relative_path(value: str) -> Path:
    return safe_relative_path(value)


def _ignore_workspace_relative(relative: Path) -> bool:
    parts = set(relative.parts)
    if parts.intersection({'.ccb', '.git', '.pytest_cache', '__pycache__'}):
        return True
    if relative.name == '.ccb-workspace.json':
        return True
    return relative.suffix in {'.pyc', '.pyo'}


def _same_resolved_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


def _project_root_test_authority(
    context,
    *,
    loop_dir: Path,
    task_text: str,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    test_command = _declared_project_root_test_command(task_text)
    if test_command is None:
        return None, None
    project_root = Path(context.project.project_root)
    resolution_path = loop_dir / 'project_root_test_resolution.json'
    evidence = _project_root_test_evidence(project_root, test_command, resolution_path)
    atomic_write_json(resolution_path, evidence)
    if not bool(evidence.get('test_file_resolved_to_lab')) or not bool(evidence.get('test_sys_path_project_first')):
        reason = 'project-root test command did not resolve to lab-local test authority'
        failure = {
            'source': 'project_root_test_resolution_failed',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            **evidence,
        }
        return evidence, failure
    if evidence.get('test_result') != 'pass':
        reason = 'project-root test command failed after workspace promotion'
        failure = {
            'source': 'project_root_test_failed',
            'stage': 'round_authority',
            'reason': reason,
            'error': reason,
            **evidence,
        }
        return evidence, failure
    return evidence, None


def _declared_project_root_test_command(task_text: str) -> str | None:
    for raw_line in task_text.splitlines():
        line = raw_line.strip().lstrip('-').strip()
        lower = line.lower()
        for prefix in TEST_COMMAND_PREFIXES:
            if not lower.startswith(prefix):
                continue
            command = line.split(':', 1)[1].strip().strip('`')
            return command or None
    return None


def _project_root_test_evidence(project_root: Path, test_command: str, resolution_path: Path) -> dict[str, object]:
    args = shlex.split(test_command)
    test_file = _resolve_project_root_test_file(project_root, args)
    test_file_resolved_to_lab = test_file is not None and _path_within(test_file, project_root)
    sys_path_project_first = _sys_path_project_first(project_root)
    returncode = None
    test_result = 'not_run'
    if args and test_file_resolved_to_lab and sys_path_project_first:
        run_args = _python_command_args(args)
        completed = subprocess.run(
            run_args,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        returncode = completed.returncode
        test_result = 'pass' if completed.returncode == 0 else 'fail'
    return {
        'test_command': test_command,
        'test_cwd': str(project_root),
        'test_resolution_path': str(resolution_path),
        'test_result': test_result,
        'test_file_resolved_to_lab': bool(test_file_resolved_to_lab),
        'test_sys_path_project_first': bool(sys_path_project_first),
        'test_file': str(test_file) if test_file is not None else None,
        'returncode': returncode,
    }


def _resolve_project_root_test_file(project_root: Path, args: list[str]) -> Path | None:
    return _resolve_unittest_file(project_root, args) or _resolve_pytest_file(project_root, args)


def _resolve_unittest_file(project_root: Path, args: list[str]) -> Path | None:
    if '-m' not in args or 'unittest' not in args:
        return None
    start_dir = 'tests'
    pattern = 'test*.py'
    for index, value in enumerate(args):
        if value in {'-s', '--start-directory'} and index + 1 < len(args):
            start_dir = args[index + 1]
        if value in {'-p', '--pattern'} and index + 1 < len(args):
            pattern = args[index + 1]
    candidates = sorted((project_root / start_dir).glob(pattern))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _resolve_pytest_file(project_root: Path, args: list[str]) -> Path | None:
    start_index = _pytest_args_start(args)
    if start_index is None:
        return None
    positional = False
    skip_next = False
    for token in args[start_index:]:
        if skip_next:
            skip_next = False
            continue
        if token == '--':
            positional = True
            continue
        if not positional and token.startswith('-'):
            skip_next = _pytest_option_requires_value(token)
            continue
        candidate = _pytest_file_candidate(project_root, token)
        if candidate is not None:
            return candidate
    return None


def _pytest_args_start(args: list[str]) -> int | None:
    if not args:
        return None
    executable = Path(args[0]).name
    if executable in {'pytest', 'py.test'}:
        return 1
    if executable in {'python', 'python3'} and len(args) >= 3 and args[1] == '-m' and args[2] == 'pytest':
        return 3
    return None


def _pytest_option_requires_value(token: str) -> bool:
    option = token.split('=', 1)[0]
    return '=' not in token and option in {
        '-c',
        '-k',
        '-m',
        '--basetemp',
        '--confcutdir',
        '--import-mode',
        '--junitxml',
        '--maxfail',
        '--rootdir',
        '--tb',
    }


def _pytest_file_candidate(project_root: Path, token: str) -> Path | None:
    value = token.split('::', 1)[0]
    if not value:
        return None
    candidate = Path(value)
    if '..' in candidate.parts or candidate.suffix != '.py':
        return None
    path = candidate if candidate.is_absolute() else project_root / candidate
    if not _path_within(path, project_root) or not path.is_file():
        return None
    return path


def _sys_path_project_first(project_root: Path) -> bool:
    script = (
        'import json,os,sys; '
        'print(json.dumps({"sys_path_0": os.path.abspath(sys.path[0] or os.getcwd())}))'
    )
    try:
        completed = subprocess.run(
            [sys.executable, '-c', script],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return False
    if completed.returncode != 0:
        return False
    try:
        payload = json.loads(completed.stdout.strip() or '{}')
    except json.JSONDecodeError:
        return False
    return _same_resolved_path(Path(str(payload.get('sys_path_0') or '.')), project_root)


def _python_command_args(args: list[str]) -> list[str]:
    return list(args)


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _single_job(jobs: tuple[dict, ...], *, target: str) -> dict:
    if len(jobs) != 1:
        raise RuntimeError(f'expected one ask job for {target}; got {len(jobs)}')
    job = dict(jobs[0])
    if not str(job.get('job_id') or ''):
        raise RuntimeError(f'ask job for {target} did not return job_id')
    return job


def _artifact_refs(record: dict[str, object]) -> dict[str, str]:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    refs: dict[str, str] = {}
    for kind in ('task_packet', 'execution_contract', 'orchestration_notes', 'orchestration_bundle'):
        artifact = artifacts.get(kind) if isinstance(artifacts, dict) else None
        if isinstance(artifact, dict) and str(artifact.get('path') or '').strip():
            refs[kind] = str(artifact['path'])
    return refs


def _node_work_packet(project_root: Path, node: dict[str, object]) -> tuple[str, str]:
    reference = str(node.get('work_packet_ref') or '').strip()
    if not reference:
        raise ValueError('orchestration bundle node work_packet_ref is missing')
    relative = safe_relative_path(reference)
    path = Path(project_root) / relative
    if not path.is_file():
        raise ValueError(f'orchestration bundle node work packet is missing: {reference}')
    text = path.read_text(encoding='utf-8').strip()
    if not text:
        raise ValueError(f'orchestration bundle node work packet is empty: {reference}')
    return relative.as_posix(), text


def _requires_project_root_authority(record: dict[str, object], task_text: str) -> bool:
    artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
    orchestration_notes = artifacts.get('orchestration_notes') if isinstance(artifacts, dict) else None
    if isinstance(orchestration_notes, dict):
        route = str(orchestration_notes.get('orchestrator_route') or orchestration_notes.get('route') or '').strip()
        if route:
            return route == 'direct_execution'
    for raw_line in task_text.splitlines():
        line = raw_line.strip().lower().lstrip('-').strip()
        if line.startswith('route:'):
            return line.split(':', 1)[1].strip().split()[0].strip('`.,;') == 'direct_execution'
    return False


def _task_record(payload: dict[str, object]) -> dict[str, object]:
    task = payload.get('task') if isinstance(payload.get('task'), dict) else None
    if task is None:
        raise RuntimeError('plan task-show did not return task record')
    return dict(task)


def _round_json_path(payload: dict[str, object]) -> Path | None:
    paths = payload.get('paths') if isinstance(payload.get('paths'), dict) else {}
    text = str(paths.get('round_json') or '').strip()
    return Path(text) if text else None


def _append_ask(
    loop_dir: Path,
    *,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    job_id: str,
    identity: dict[str, object],
    freshness: dict[str, object] | None = None,
) -> dict[str, object]:
    record = {
        'schema_version': 1,
        'record_type': 'ccb_loop_ask_first_ask',
        'ask_id': f'ask-{uuid4().hex[:12]}',
        'ts': _utc_now(),
        'loop_id': loop_id,
        'target': target,
        'sender': sender,
        'ask_purpose': purpose,
        'purpose': purpose,
        **identity,
        'job_id': job_id,
        'status': 'submitted',
    }
    if freshness is not None:
        record['freshness'] = freshness
    _append_jsonl(loop_dir / 'asks.jsonl', record)
    return record


def _append_event(loop_dir: Path, *, loop_id: str, kind: str, payload: dict[str, object]) -> None:
    _append_jsonl(
        loop_dir / 'events.jsonl',
        {
            'schema_version': 1,
            'record_type': 'ccb_loop_ask_first_event',
            'event_id': f'evt-{uuid4().hex[:12]}',
            'ts': _utc_now(),
            'loop_id': loop_id,
            'kind': kind,
            'actor': 'loop_runner',
            **payload,
        },
    )


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.write('\n')


def _ensure_loop_dirs(loop_dir: Path) -> None:
    for relative in ('artifacts', 'topology_proposals'):
        (loop_dir / relative).mkdir(parents=True, exist_ok=True)


def _loop_dir(context, loop_id: str) -> Path:
    return Path(context.paths.runtime_state_root) / 'runtime' / 'loops' / loop_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


__all__ = ['release_ask_first_execution_round', 'run_ask_first_execution_round']
