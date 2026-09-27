from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from agents.models import AgentValidationError, normalize_agent_name
from cli.models import ParsedAskCommand
from storage.atomic import atomic_write_json, atomic_write_text

from .ask import submit_ask, watch_ask_job
from .loop_capacity import loop_capacity
from .plan_tasks import plan_task, task_execution_text


def loop_run_once(context, command, services=None) -> dict[str, object]:
    deps = _deps(services)
    loop_id, task, task_id = _resolve_run_request(context, command, deps)
    orchestrator = normalize_agent_name(str(command.orchestrator or 'orchestrator'))
    round_checker = normalize_agent_name(str(getattr(command, 'round_checker', None) or 'round_checker'))
    worker_profile = normalize_agent_name(str(command.worker_profile or 'worker'))
    reviewer_profile = normalize_agent_name(str(command.reviewer_profile or 'code_reviewer'))
    started_at = _utc_now()

    loop_dir = _loop_dir(context, loop_id)
    _ensure_loop_dirs(loop_dir)
    _write_breadcrumb(
        loop_dir,
        loop_id=loop_id,
        task_label=task_id or 'run-once',
        phase='capacity',
        owner='loop_runner',
        next_step='ensure capacity',
    )
    _append_event(loop_dir, loop_id=loop_id, kind='loop_run_started', payload={'task': task, 'task_id': task_id})

    ensure_payload = deps.loop_capacity(
        context,
        SimpleNamespace(
            action='ensure',
            loop_id=loop_id,
            profile_counts=((worker_profile, 1), (reviewer_profile, 1)),
        ),
    )
    _require_applied_capacity(ensure_payload, action='ensure')
    worker_agent = _agent_for_profile(ensure_payload, worker_profile)
    reviewer_agent = _agent_for_profile(ensure_payload, reviewer_profile)
    _write_breadcrumb(
        loop_dir,
        loop_id=loop_id,
        task_label=task_id or 'run-once',
        phase='execution',
        owner=orchestrator,
        next_step='worker ask',
    )

    release_payload: dict[str, object] | None = None
    worker_result: dict[str, object] | None = None
    reviewer_result: dict[str, object] | None = None
    orchestrator_result: dict[str, object] | None = None
    round_checker_result: dict[str, object] | None = None
    failure: dict[str, object] | None = None
    release_error: dict[str, object] | None = None
    status = 'failed'
    try:
        worker_result = _submit_and_watch(
            context,
            deps,
            loop_dir=loop_dir,
            loop_id=loop_id,
            target=worker_agent,
            sender=orchestrator,
            purpose='worker',
            task_id=f'{loop_id}-worker',
            message=_worker_message(loop_id=loop_id, task=task),
            timeout=getattr(command, 'timeout_s', None),
            node_id='node-worker',
        )
        reviewer_result = _submit_and_watch(
            context,
            deps,
            loop_dir=loop_dir,
            loop_id=loop_id,
            target=reviewer_agent,
            sender=orchestrator,
            purpose='reviewer',
            task_id=f'{loop_id}-reviewer',
            message=_reviewer_message(loop_id=loop_id, task=task, worker=worker_result),
            timeout=getattr(command, 'timeout_s', None),
            node_id='node-reviewer',
        )
        orchestrator_result = _submit_and_watch(
            context,
            deps,
            loop_dir=loop_dir,
            loop_id=loop_id,
            target=orchestrator,
            sender='system',
            purpose='aggregate',
            task_id=f'{loop_id}-aggregate',
            message=_aggregate_message(loop_id=loop_id, task=task, worker=worker_result, reviewer=reviewer_result),
            timeout=getattr(command, 'timeout_s', None),
            node_id='node-orchestrator',
        )
        _write_breadcrumb(
            loop_dir,
            loop_id=loop_id,
            task_label=task_id or 'run-once',
            phase='round_checking',
            owner=round_checker,
            next_step='round checker ask',
        )
        round_checker_result = _submit_and_watch(
            context,
            deps,
            loop_dir=loop_dir,
            loop_id=loop_id,
            target=round_checker,
            sender='system',
            purpose='round_checker',
            task_id=f'{loop_id}-round-checker',
            message=_round_checker_message(
                loop_id=loop_id,
                task=task,
                worker=worker_result,
                reviewer=reviewer_result,
                orchestrator=orchestrator_result,
            ),
            timeout=getattr(command, 'timeout_s', None),
            node_id='node-round-checker',
        )
        status = _round_status(worker_result, reviewer_result, orchestrator_result, round_checker_result)
        _write_breadcrumb(
            loop_dir,
            loop_id=loop_id,
            task_label=task_id or 'run-once',
            phase='release',
            owner='loop_runner',
            next_step='release capacity',
        )
    except Exception as exc:
        status = 'failed'
        failure = _failure_record(stage='execution', exc=exc)
        _append_event(loop_dir, loop_id=loop_id, kind='loop_run_failed', payload=failure)
    finally:
        try:
            release_payload = deps.loop_capacity(
                context,
                SimpleNamespace(action='release', loop_id=loop_id, policy='auto', idle_only=False),
            )
        except Exception as exc:
            release_error = _failure_record(stage='release', exc=exc)
            _append_event(loop_dir, loop_id=loop_id, kind='loop_release_failed', payload=release_error)
            if status == 'ok':
                status = 'release_failed'

    finished_at = _utc_now()
    payload = {
        'schema_version': 1,
        'record_type': 'ccb_loop_run_once_round',
        'loop_run_status': status,
        'loop_id': loop_id,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'started_at': started_at,
        'finished_at': finished_at,
        'task': task,
        'task_id': task_id,
        'orchestrator': orchestrator,
        'profiles': {
            'worker': worker_profile,
            'reviewer': reviewer_profile,
        },
        'agents': {
            'worker': worker_agent,
            'reviewer': reviewer_agent,
            'orchestrator': orchestrator,
            'round_checker': round_checker,
        },
        'worker': worker_result,
        'reviewer': reviewer_result,
        'aggregation': orchestrator_result,
        'round_checker': round_checker_result,
        'capacity': {
            'ensure': _capacity_summary(ensure_payload),
            'release': _capacity_summary(release_payload or {}),
        },
        'paths': {
            'round': str(loop_dir / 'round.json'),
            'asks': str(loop_dir / 'asks.jsonl'),
            'events': str(loop_dir / 'events.jsonl'),
            'breadcrumb': str(loop_dir / 'breadcrumb.md'),
            'artifacts': str(loop_dir / 'artifacts'),
        },
    }
    if failure is not None:
        payload['failure'] = failure
    if release_error is not None:
        payload['release_error'] = release_error
    atomic_write_json(loop_dir / 'round.json', payload)
    _append_event(loop_dir, loop_id=loop_id, kind='loop_run_finished', payload={'status': status})
    _write_breadcrumb(
        loop_dir,
        loop_id=loop_id,
        task_label=task_id or 'run-once',
        phase='done' if status == 'ok' else 'blocked',
        owner='loop_runner',
        next_step='round complete' if status == 'ok' else 'inspect round.json and release state',
        blocked='none' if status == 'ok' else status,
    )
    return payload


def _deps(services):
    return SimpleNamespace(
        loop_capacity=getattr(services, 'loop_capacity', loop_capacity),
        plan_task=getattr(services, 'plan_task', plan_task),
        submit_ask=getattr(services, 'submit_ask', submit_ask),
        watch_ask_job=getattr(services, 'watch_ask_job', watch_ask_job),
    )


def _resolve_run_request(context, command, deps) -> tuple[str, str, str | None]:
    task_id = str(getattr(command, 'task_id', None) or '').strip() or None
    if task_id is None:
        loop_id = _normalize_loop_id(command.loop_id)
        task = str(command.task or '').strip()
        if not task:
            raise ValueError('loop run-once --task cannot be empty')
        return loop_id, task, None
    show = deps.plan_task(context, SimpleNamespace(action='task-show', task_id=task_id))
    record = show.get('task') if isinstance(show.get('task'), dict) else {}
    current_loop = str(record.get('current_loop') or '').strip()
    loop_id = _normalize_loop_id(getattr(command, 'loop_id', None) or current_loop or f'lp{uuid4().hex[:6]}')
    deps.plan_task(context, SimpleNamespace(action='task-bind-loop', task_id=task_id, loop_id=loop_id))
    return loop_id, task_execution_text(context, task_id), task_id


def _normalize_loop_id(value: object) -> str:
    try:
        return normalize_agent_name(str(value or ''))
    except AgentValidationError as exc:
        raise ValueError(f'loop_id is invalid: {exc}') from exc


def _loop_dir(context, loop_id: str) -> Path:
    return Path(context.paths.runtime_state_root) / 'runtime' / 'loops' / loop_id


def _ensure_loop_dirs(loop_dir: Path) -> None:
    for relative in ('artifacts', 'nodes'):
        (loop_dir / relative).mkdir(parents=True, exist_ok=True)


def _require_applied_capacity(payload: dict[str, object], *, action: str) -> None:
    apply = payload.get('apply')
    if not isinstance(apply, dict) or str(apply.get('apply_status') or '') != 'applied':
        status = str(apply.get('apply_status') or 'unknown') if isinstance(apply, dict) else 'missing'
        raise RuntimeError(f'loop run-once requires mounted daemon; capacity {action} apply_status={status}')


def _agent_for_profile(payload: dict[str, object], profile: str) -> str:
    matches = [
        str(agent.get('name') or '')
        for agent in tuple(payload.get('agents') or ())
        if isinstance(agent, dict) and str(agent.get('profile') or '') == profile
    ]
    matches = [item for item in matches if item]
    if len(matches) != 1:
        raise RuntimeError(f'loop capacity returned {len(matches)} agents for profile {profile}; expected 1')
    return matches[0]


def _submit_and_watch(
    context,
    deps,
    *,
    loop_dir: Path,
    loop_id: str,
    target: str,
    sender: str,
    purpose: str,
    task_id: str,
    message: str,
    timeout: float | None,
    node_id: str,
) -> dict[str, object]:
    summary = deps.submit_ask(
        context,
        ParsedAskCommand(
            project=None,
            target=target,
            sender=sender,
            message=message,
            task_id=task_id,
        ),
    )
    job = _single_job(summary.jobs, target=target)
    job_id = str(job['job_id'])
    _append_ask(loop_dir, loop_id=loop_id, target=target, purpose=purpose, job_id=job_id, node_id=node_id)
    batch = deps.watch_ask_job(context, job_id, StringIO(), timeout=timeout, emit_output=False)
    result = {
        'target': target,
        'sender': sender,
        'purpose': purpose,
        'node_id': node_id,
        'job_id': job_id,
        'status': batch.status,
        'reply': batch.reply,
        'terminal': bool(batch.terminal),
    }
    artifact_path = loop_dir / 'artifacts' / f'{purpose}-reply.md'
    atomic_write_text(artifact_path, batch.reply or '')
    result['artifact'] = str(artifact_path)
    _append_event(
        loop_dir,
        loop_id=loop_id,
        kind='ask_terminal',
        payload={'purpose': purpose, 'target': target, 'job_id': job_id, 'status': batch.status},
    )
    return result


def _single_job(jobs: tuple[dict, ...], *, target: str) -> dict:
    if len(jobs) != 1:
        raise RuntimeError(f'expected one ask job for {target}; got {len(jobs)}')
    job = dict(jobs[0])
    if not str(job.get('job_id') or ''):
        raise RuntimeError(f'ask job for {target} did not return job_id')
    return job


def _worker_message(*, loop_id: str, task: str) -> str:
    return (
        f'Loop: {loop_id}\n'
        'Role: worker\n'
        f'Task: {task}\n\n'
        'Output requirements:\n'
        '- status: done|blocked|needs_rework\n'
        '- concise work summary\n'
        '- evidence or artifact refs\n'
        '- no hidden fallback or scope shrinkage'
    )


def _reviewer_message(*, loop_id: str, task: str, worker: dict[str, object]) -> str:
    return (
        f'Loop: {loop_id}\n'
        'Role: code_reviewer\n'
        f'Task: {task}\n'
        f'Worker job: {worker.get("job_id")}\n'
        f'Worker reply artifact: {worker.get("artifact")}\n\n'
        'Output requirements:\n'
        '- status: pass|rework_required|blocked|non_converged\n'
        '- verification checks performed\n'
        '- fallback/degradation audit\n'
        '- concise risk notes'
    )


def _aggregate_message(*, loop_id: str, task: str, worker: dict[str, object], reviewer: dict[str, object]) -> str:
    return (
        f'Loop: {loop_id}\n'
        'Role: orchestrator\n'
        f'Task: {task}\n'
        f'Worker job: {worker.get("job_id")} status={worker.get("status")}\n'
        f'Reviewer job: {reviewer.get("job_id")} status={reviewer.get("status")}\n\n'
        'Output requirements:\n'
        '- round status: pass|rework|blocked|non_converged\n'
        '- completed nodes\n'
        '- evidence refs\n'
        '- release readiness\n'
        '- next handoff'
    )


def _round_checker_message(
    *,
    loop_id: str,
    task: str,
    worker: dict[str, object],
    reviewer: dict[str, object],
    orchestrator: dict[str, object],
) -> str:
    return (
        f'Loop: {loop_id}\n'
        'Role: round_checker\n'
        f'Task: {task}\n'
        f'Worker job: {worker.get("job_id")} status={worker.get("status")}\n'
        f'Reviewer job: {reviewer.get("job_id")} status={reviewer.get("status")}\n'
        f'Orchestrator job: {orchestrator.get("job_id")} status={orchestrator.get("status")}\n\n'
        'Output requirements:\n'
        '- round result: pass|rework_node|partial|replan_required|global_blocker\n'
        '- verification performed against planner contract\n'
        '- hidden degradation audit\n'
        '- evidence refs\n'
        '- recommended next owner'
    )


def _round_status(*results: dict[str, object] | None) -> str:
    if all(result is not None and str(result.get('status') or '') == 'completed' for result in results):
        return 'ok'
    return 'incomplete'


def _capacity_summary(payload: dict[str, object]) -> dict[str, object]:
    apply = payload.get('apply')
    return {
        'loop_capacity_status': payload.get('loop_capacity_status'),
        'agent_count': payload.get('agent_count'),
        'released_count': payload.get('released_count'),
        'retained_count': payload.get('retained_count'),
        'release_policy': payload.get('release_policy'),
        'idle_only': payload.get('idle_only'),
        'apply': dict(apply) if isinstance(apply, dict) else apply,
    }


def _failure_record(*, stage: str, exc: Exception) -> dict[str, object]:
    return {
        'stage': stage,
        'error_type': exc.__class__.__name__,
        'error': str(exc),
    }


def _append_ask(loop_dir: Path, *, loop_id: str, target: str, purpose: str, job_id: str, node_id: str) -> None:
    _append_jsonl(
        loop_dir / 'asks.jsonl',
        {
            'schema_version': 1,
            'record_type': 'ccb_loop_ask',
            'ask_id': f'ask-{uuid4().hex[:12]}',
            'ts': _utc_now(),
            'loop_id': loop_id,
            'target': target,
            'purpose': purpose,
            'job_id': job_id,
            'node_id': node_id,
            'status': 'submitted',
        },
    )


def _append_event(loop_dir: Path, *, loop_id: str, kind: str, payload: dict[str, object]) -> None:
    _append_jsonl(
        loop_dir / 'events.jsonl',
        {
            'schema_version': 1,
            'record_type': 'ccb_loop_event',
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


def _write_breadcrumb(
    loop_dir: Path,
    *,
    loop_id: str,
    task_label: str = 'run-once',
    phase: str,
    owner: str,
    next_step: str,
    blocked: str = 'none',
) -> None:
    atomic_write_text(
        loop_dir / 'breadcrumb.md',
        '\n'.join(
            [
                f'Loop: {loop_id}',
                f'Task: {task_label}',
                f'Phase: {phase}',
                f'Owner: {owner}',
                f'Next: {next_step}',
                f'Blocked: {blocked}',
                'Needs user: no',
                f'Updated: {_utc_now()}',
                '',
            ]
        ),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


__all__ = ['loop_run_once']
