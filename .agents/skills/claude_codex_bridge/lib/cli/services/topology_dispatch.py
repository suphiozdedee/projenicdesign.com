"""Legacy bounded compatibility for explicitly opted-in topology dispatch experiments.

This module is not the Decision 020 ask-first runner mainline; ``loop runner
--once`` must not use it to discover topology dispatch or execute topology
graph edges.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import re
from uuid import uuid4

from cli.models import ParsedAskCommand
from storage.atomic import atomic_write_json, atomic_write_text

from .plan_tasks import task_execution_text

TOPOLOGY_DISPATCH_SCHEMA = 'ccb.loop.topology_dispatch.v1'
SUPPORTED_EDGE_TYPES = frozenset({'ask', 'ask_after'})
READY_OBSERVED_STATES = frozenset({'present'})
READY_LIFECYCLE_STATES = frozenset({'', 'visible'})
PSEUDO_SENDERS = frozenset({'user', 'system'})


def find_first_topology_dispatch_task(context) -> dict[str, object] | None:
    plantree_root = Path(context.project.project_root) / 'docs' / 'plantree' / 'plans'
    if not plantree_root.is_dir():
        return None
    candidates: list[dict[str, object]] = []
    for index_path in sorted(plantree_root.glob('*/tasks/index.json')):
        tasks_root = index_path.parent
        index = _load_json_object(index_path)
        for record in tuple(index.get('tasks') or ()):
            if not isinstance(record, dict):
                continue
            current_loop = str(record.get('current_loop') or '').strip()
            if str(record.get('status') or '').strip().lower() != 'running':
                continue
            next_owner = str(record.get('next_owner') or '').strip().lower()
            if next_owner and next_owner != 'orchestrator':
                continue
            if not current_loop or not topology_has_dispatch_graph(context, current_loop):
                continue
            candidates.append(
                {
                    'record': record,
                    'index': index,
                    'tasks_root': tasks_root,
                    'runner_action': 'execute',
                    'runner_reason': 'topology_dispatch_graph',
                    'next_owner': 'orchestrator',
                }
            )
    if not candidates:
        return None
    return candidates[0]


def topology_has_dispatch_graph(context, loop_id: str) -> bool:
    desired = _load_json_optional(_desired_read_path(context, loop_id))
    if desired is None:
        return False
    return bool(_edge_dicts(desired))


def maybe_run_topology_dispatch(
    context,
    command,
    deps,
    *,
    task: dict[str, object],
    loop_id: str,
) -> dict[str, object] | None:
    desired = _load_json_optional(_desired_read_path(context, loop_id))
    if desired is None:
        return None
    edges = _edge_dicts(desired)
    if not edges:
        return None
    observed = _load_json_object(_observed_read_path(context, loop_id))
    return _run_topology_dispatch(
        context,
        command,
        deps,
        task=task,
        loop_id=loop_id,
        desired=desired,
        observed=observed,
        edges=edges,
    )


def _run_topology_dispatch(
    context,
    command,
    deps,
    *,
    task: dict[str, object],
    loop_id: str,
    desired: Mapping[str, object],
    observed: Mapping[str, object],
    edges: tuple[dict[str, object], ...],
) -> dict[str, object]:
    record = task['record'] if isinstance(task.get('record'), dict) else {}
    task_id = str(record.get('task_id') or '')
    task_text = task_execution_text(context, task_id) if task_id else str(record.get('title') or '')
    loop_dir = _loop_dir(context, loop_id)
    (loop_dir / 'artifacts').mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    dispatch_path = loop_dir / 'topology_dispatch.json'
    events_path = loop_dir / 'topology_dispatch.events.jsonl'
    round_path = loop_dir / 'round.json'

    agent_map = _ready_agent_map(desired=desired, observed=observed)
    ordered_edges = _validate_dispatch_plan(edges, agent_map=agent_map)
    dispatch: dict[str, object] = {
        'schema': TOPOLOGY_DISPATCH_SCHEMA,
        'record_type': 'ccb_loop_topology_dispatch',
        'dispatch_status': 'running',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'loop_id': loop_id,
        'task_id': task_id,
        'started_at': started_at,
        'desired_revision': int(desired.get('revision') or 0),
        'observed_revision': int(observed.get('desired_revision') or 0),
        'edge_count': len(ordered_edges),
        'edges': [],
        'paths': {
            'desired': str(_desired_read_path(context, loop_id)),
            'observed': str(_observed_read_path(context, loop_id)),
            'dispatch': str(dispatch_path),
            'events': str(events_path),
            'round': str(round_path),
            'artifacts': str(loop_dir / 'artifacts'),
        },
    }
    atomic_write_json(dispatch_path, dispatch)
    _append_event(
        events_path,
        loop_id=loop_id,
        kind='topology_dispatch_started',
        payload={'edge_count': len(ordered_edges)},
    )

    results: list[dict[str, object]] = []
    completed: dict[str, dict[str, object]] = {}
    failure: dict[str, object] | None = None
    for edge in ordered_edges:
        edge_id = str(edge['id'])
        try:
            result = _dispatch_edge(
                context,
                command,
                deps,
                loop_dir=loop_dir,
                loop_id=loop_id,
                task_id=task_id,
                task_text=task_text,
                edge=edge,
                agent_map=agent_map,
                previous_results=completed,
                desired=desired,
            )
        except Exception as exc:
            result = _edge_failure(edge, exc)
            failure = result
            results.append(result)
            _append_event(
                events_path,
                loop_id=loop_id,
                kind='topology_edge_failed',
                payload={'edge_id': edge_id, 'error': str(exc)},
            )
            dispatch['edges'] = results
            dispatch['dispatch_status'] = 'failed'
            dispatch['failure'] = failure
            dispatch['finished_at'] = _utc_now()
            atomic_write_json(dispatch_path, dispatch)
            raise RuntimeError(f"topology dispatch edge '{edge_id}' failed: {exc}") from exc
        results.append(result)
        completed[edge_id] = result
        _append_event(
            events_path,
            loop_id=loop_id,
            kind='topology_edge_terminal',
            payload={'edge_id': edge_id, 'target': result.get('target'), 'status': result.get('status')},
        )
        dispatch['edges'] = results
        atomic_write_json(dispatch_path, dispatch)
        if str(result.get('status') or '') != 'completed':
            break

    finished_at = _utc_now()
    dispatch_status = (
        'ok'
        if len(results) == len(ordered_edges)
        and all(str(item.get('status') or '') == 'completed' for item in results)
        else 'incomplete'
    )
    dispatch['dispatch_status'] = dispatch_status
    dispatch['finished_at'] = finished_at
    atomic_write_json(dispatch_path, dispatch)
    round_checker = _round_reviewer_result(results, agent_map=agent_map)
    round_payload = {
        'schema_version': 1,
        'record_type': 'ccb_loop_topology_dispatch_round',
        'dispatch_source': 'topology_graph',
        'loop_run_status': 'ok' if dispatch_status == 'ok' else dispatch_status,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'loop_id': loop_id,
        'task_id': task_id,
        'started_at': started_at,
        'finished_at': finished_at,
        'task': task_text,
        'topology_dispatch': dispatch,
        'round_checker': round_checker,
        'paths': {
            'round': str(round_path),
            'dispatch': str(dispatch_path),
            'events': str(events_path),
            'artifacts': str(loop_dir / 'artifacts'),
        },
    }
    if failure is not None:
        round_payload['failure'] = failure
    atomic_write_json(round_path, round_payload)
    _append_event(events_path, loop_id=loop_id, kind='topology_dispatch_finished', payload={'status': dispatch_status})
    return round_payload


def _ready_agent_map(*, desired: Mapping[str, object], observed: Mapping[str, object]) -> dict[str, dict[str, object]]:
    desired_revision = int(desired.get('revision') or 0)
    observed_revision = int(observed.get('desired_revision') or 0)
    status = str(observed.get('last_reconcile_status') or '').strip()
    if status != 'reconciled':
        raise RuntimeError(f'topology dispatch requires reconciled observed topology; status={status or "missing"}')
    if observed_revision != desired_revision:
        raise RuntimeError(
            f'topology dispatch observed revision {observed_revision} '
            f'does not match desired revision {desired_revision}'
        )
    drift = observed.get('drift') if isinstance(observed.get('drift'), Mapping) else {}
    mismatched = tuple(drift.get('mismatched_agents') or ())
    if mismatched:
        names = ', '.join(
            str(item.get('agent') or item)
            for item in mismatched
            if isinstance(item, Mapping)
        ) or str(mismatched)
        raise RuntimeError(f'topology dispatch requires zero observed drift; mismatched_agents={names}')
    agents: dict[str, dict[str, object]] = {}
    for raw in tuple(observed.get('agents') or ()):
        if not isinstance(raw, Mapping):
            continue
        agent = dict(raw)
        agent_id = str(agent.get('id') or '').strip()
        if agent_id:
            agents[agent_id] = agent
    return agents


def _validate_dispatch_plan(
    edges: tuple[dict[str, object], ...],
    *,
    agent_map: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    edge_ids: set[str] = set()
    dependencies: dict[str, set[str]] = {}
    for edge in edges:
        edge_id = str(edge.get('id') or '').strip()
        if not edge_id:
            raise RuntimeError('topology dispatch edge id cannot be empty')
        if edge_id in edge_ids:
            raise RuntimeError(f'duplicate topology dispatch edge id: {edge_id}')
        edge_ids.add(edge_id)
        edge_type = str(edge.get('type') or '').strip()
        if edge_type not in SUPPORTED_EDGE_TYPES:
            supported = ', '.join(sorted(SUPPORTED_EDGE_TYPES))
            raise RuntimeError(
                f"topology dispatch edge '{edge_id}' has unsupported type '{edge_type}'; supported: {supported}"
            )
        after = set(_edge_after(edge))
        if edge_type == 'ask_after' and not after:
            raise RuntimeError(f"topology dispatch edge '{edge_id}' type ask_after requires after")
        dependencies[edge_id] = after
        source = _required_endpoint(edge, 'from')
        target = _required_endpoint(edge, 'to')
        if source not in PSEUDO_SENDERS:
            _require_agent_ready(agent_map, source, edge_id=edge_id, endpoint='source')
        _require_agent_ready(agent_map, target, edge_id=edge_id, endpoint='target')
    for edge_id, after in dependencies.items():
        missing = sorted(item for item in after if item not in edge_ids)
        if missing:
            raise RuntimeError(f"topology dispatch edge '{edge_id}' depends on unknown edge '{missing[0]}'")
        if edge_id in after:
            raise RuntimeError(f"topology dispatch edge '{edge_id}' cannot depend on itself")
    _assert_acyclic(dependencies)
    ordered = tuple(
        sorted(edges, key=lambda item: (_edge_order(item), int(item.get('_index') or 0), str(item.get('id') or '')))
    )
    completed: set[str] = set()
    for edge in ordered:
        edge_id = str(edge['id'])
        for dependency in _edge_after(edge):
            if dependency not in completed:
                raise RuntimeError(
                    f"topology dispatch edge '{edge_id}' depends on unfinished edge '{dependency}'; "
                    'order must place dependencies first'
                )
        completed.add(edge_id)
    return ordered


def _dispatch_edge(
    context,
    command,
    deps,
    *,
    loop_dir: Path,
    loop_id: str,
    task_id: str,
    task_text: str,
    edge: Mapping[str, object],
    agent_map: Mapping[str, Mapping[str, object]],
    previous_results: Mapping[str, Mapping[str, object]],
    desired: Mapping[str, object],
) -> dict[str, object]:
    edge_id = str(edge['id'])
    source = _required_endpoint(edge, 'from')
    target_id = _required_endpoint(edge, 'to')
    target = _ask_target(agent_map[target_id], fallback=target_id)
    sender = 'system' if source in PSEUDO_SENDERS else _ask_target(agent_map[source], fallback=source)
    message = _edge_message(
        loop_id=loop_id,
        task_text=task_text,
        edge=edge,
        target_agent=agent_map[target_id],
        previous_results=previous_results,
        desired=desired,
    )
    summary = deps.submit_ask(
        context,
        ParsedAskCommand(
            project=None,
            target=target,
            sender=sender,
            message=message,
            task_id=f'{loop_id}-{_safe_name(edge_id)}',
            artifact_request=True,
        ),
    )
    job = _single_job(summary.jobs, target=target)
    job_id = str(job['job_id'])
    _append_ask(
        loop_dir,
        loop_id=loop_id,
        target=target,
        purpose=edge_id,
        job_id=job_id,
        node_id=str(edge.get('node_id') or 'topology'),
    )
    batch = deps.watch_ask_job(
        context,
        job_id,
        StringIO(),
        timeout=getattr(command, 'timeout_s', None),
        emit_output=False,
    )
    reply = str(getattr(batch, 'reply', '') or '')
    artifact_path = loop_dir / 'artifacts' / f'{_safe_name(edge_id)}-reply.md'
    atomic_write_text(artifact_path, reply)
    return {
        'edge_id': edge_id,
        'type': str(edge.get('type') or ''),
        'order': edge.get('order'),
        'after': _edge_after(edge),
        'from': source,
        'to': target_id,
        'sender': sender,
        'target': target,
        'task_id': task_id,
        'job_id': job_id,
        'status': str(getattr(batch, 'status', '') or job.get('status') or ''),
        'terminal': bool(getattr(batch, 'terminal', False)),
        'reply': reply,
        'artifact': str(artifact_path),
        'input_artifact': edge.get('input_artifact'),
        'requested_output_artifact': edge.get('output_artifact'),
    }


def _edge_message(
    *,
    loop_id: str,
    task_text: str,
    edge: Mapping[str, object],
    target_agent: Mapping[str, object],
    previous_results: Mapping[str, Mapping[str, object]],
    desired: Mapping[str, object],
) -> str:
    edge_id = str(edge.get('id') or '')
    profile = str(target_agent.get('profile') or '').strip()
    role = _role_marker(profile)
    after = _edge_after(edge)
    upstream = [
        f"- {item}: job={previous_results[item].get('job_id')} artifact={previous_results[item].get('artifact')}"
        for item in after
        if item in previous_results
    ]
    artifacts = desired.get('artifacts') if isinstance(desired.get('artifacts'), Mapping) else {}
    return '\n'.join(
        [
            f'Loop: {loop_id}',
            f'Role: {role}',
            f'Topology edge: {edge_id}',
            f"Edge type: {edge.get('type')}",
            f"From: {edge.get('from')}",
            f"To: {edge.get('to')}",
            f"Input artifact: {edge.get('input_artifact') or 'none'}",
            f"Output artifact request: {edge.get('output_artifact') or 'host-recorded reply artifact'}",
            f'Graph artifacts: {dict(artifacts)}',
            '',
            'Task:',
            task_text,
            '',
            'Upstream edge results:',
            *(upstream or ['- none']),
            '',
            'Host rules:',
            '- Treat this as a topology-dispatched ask edge.',
            '- Return concise status, evidence, and artifact refs.',
            '- Do not mutate CCB task status, runtime authority, tmux state, or topology files.',
        ]
    )


def _role_marker(profile: str) -> str:
    normalized = profile.strip().lower().replace('-', '_')
    if normalized in {'coder', 'code_reviewer', 'ccb_round_reviewer'}:
        return normalized
    if normalized == 'ccb_orchestrator':
        return 'ccb_orchestrator'
    return normalized or 'topology_agent'


def _round_reviewer_result(
    results: list[dict[str, object]],
    *,
    agent_map: Mapping[str, Mapping[str, object]],
) -> dict[str, object] | None:
    for result in reversed(results):
        target_id = str(result.get('to') or '')
        profile = str((agent_map.get(target_id) or {}).get('profile') or '').strip().lower().replace('-', '_')
        if profile == 'ccb_round_reviewer':
            return result
    return results[-1] if results else None


def _require_agent_ready(
    agent_map: Mapping[str, Mapping[str, object]],
    agent_id: str,
    *,
    edge_id: str,
    endpoint: str,
) -> None:
    agent = agent_map.get(agent_id)
    if agent is None:
        raise RuntimeError(f"topology dispatch edge '{edge_id}' {endpoint} agent '{agent_id}' is not ready: missing")
    observed_state = str(agent.get('observed_state') or '').strip()
    lifecycle_state = str(agent.get('lifecycle_state') or '').strip()
    if observed_state not in READY_OBSERVED_STATES or lifecycle_state not in READY_LIFECYCLE_STATES:
        raise RuntimeError(
            f"topology dispatch edge '{edge_id}' {endpoint} agent '{agent_id}' is not ready: "
            f'observed_state={observed_state or "missing"} lifecycle_state={lifecycle_state or "missing"}'
        )


def _assert_acyclic(dependencies: Mapping[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(edge_id: str) -> None:
        if edge_id in visited:
            return
        if edge_id in visiting:
            raise RuntimeError(f'topology dispatch edge dependency cycle detected at {edge_id}')
        visiting.add(edge_id)
        for parent in dependencies.get(edge_id, set()):
            visit(parent)
        visiting.remove(edge_id)
        visited.add(edge_id)

    for edge_id in dependencies:
        visit(edge_id)


def _edge_dicts(payload: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    edges = []
    for index, raw in enumerate(tuple(payload.get('edges') or ()), start=1):
        if not isinstance(raw, Mapping):
            raise RuntimeError(f'topology dispatch edge #{index} must be an object')
        edge = dict(raw)
        edge['id'] = str(edge.get('id') or f'edge{index}').strip()
        edge['_index'] = index
        edges.append(edge)
    return tuple(edges)


def _edge_after(edge: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in tuple(edge.get('after') or ()) if str(item).strip())


def _edge_order(edge: Mapping[str, object]) -> int:
    try:
        return int(edge.get('order') or 0)
    except (TypeError, ValueError):
        raise RuntimeError(f"topology dispatch edge '{edge.get('id')}' order must be an integer")


def _required_endpoint(edge: Mapping[str, object], name: str) -> str:
    value = str(edge.get(name) or '').strip()
    if not value:
        raise RuntimeError(f"topology dispatch edge '{edge.get('id')}' requires {name}")
    return value


def _ask_target(agent: Mapping[str, object], *, fallback: str) -> str:
    return str(agent.get('ask_target') or fallback).strip() or fallback


def _edge_failure(edge: Mapping[str, object], exc: Exception) -> dict[str, object]:
    return {
        'edge_id': str(edge.get('id') or ''),
        'type': str(edge.get('type') or ''),
        'order': edge.get('order'),
        'after': _edge_after(edge),
        'from': edge.get('from'),
        'to': edge.get('to'),
        'status': 'failed',
        'error_type': exc.__class__.__name__,
        'error': str(exc),
    }


def _single_job(jobs: tuple[dict, ...], *, target: str) -> dict:
    if len(jobs) != 1:
        raise RuntimeError(f'expected one ask job for {target}; got {len(jobs)}')
    job = dict(jobs[0])
    if not str(job.get('job_id') or ''):
        raise RuntimeError(f'ask job for {target} did not return job_id')
    return job


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
            'dispatch_source': 'topology_graph',
        },
    )


def _append_event(path: Path, *, loop_id: str, kind: str, payload: dict[str, object]) -> None:
    _append_jsonl(
        path,
        {
            'schema_version': 1,
            'record_type': 'ccb_loop_topology_dispatch_event',
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


def _safe_name(value: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '-', str(value or '').strip()).strip('.-')
    return safe or 'edge'


def _desired_path(context, loop_id: str) -> Path:
    return _loop_dir(context, loop_id) / 'agent_mount_topology.desired.json'


def _legacy_desired_path(context, loop_id: str) -> Path:
    return _loop_dir(context, loop_id) / 'agent_topology.desired.json'


def _desired_read_path(context, loop_id: str) -> Path:
    path = _desired_path(context, loop_id)
    if path.is_file():
        return path
    legacy_path = _legacy_desired_path(context, loop_id)
    if legacy_path.is_file():
        return legacy_path
    return path


def _observed_path(context, loop_id: str) -> Path:
    return _loop_dir(context, loop_id) / 'agent_mount_topology.observed.json'


def _legacy_observed_path(context, loop_id: str) -> Path:
    return _loop_dir(context, loop_id) / 'agent_topology.observed.json'


def _observed_read_path(context, loop_id: str) -> Path:
    path = _observed_path(context, loop_id)
    if path.is_file():
        return path
    legacy_path = _legacy_observed_path(context, loop_id)
    if legacy_path.is_file():
        return legacy_path
    return path


def _loop_dir(context, loop_id: str) -> Path:
    return Path(context.paths.runtime_state_root) / 'runtime' / 'loops' / loop_id


def _load_json_optional(path: Path) -> dict[str, object] | None:
    try:
        return _load_json_object(path)
    except FileNotFoundError:
        return None


def _load_json_object(path: Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f'expected JSON object: {path}')
    return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


# Legacy opt-in topology dispatch helpers, not mainline runner authority.
__all__ = ['find_first_topology_dispatch_task', 'maybe_run_topology_dispatch', 'topology_has_dispatch_graph']
