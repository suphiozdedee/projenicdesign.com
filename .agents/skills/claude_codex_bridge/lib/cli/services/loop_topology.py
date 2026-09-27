from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from agents.config_loader import load_project_config
from agents.models import AgentValidationError, LoopCapacityConfig, normalize_agent_name
from storage.atomic import atomic_write_json

from .agent_lifecycle import add_lifecycle_agents, agent_lifecycle
from .layout import layout_command
from .loop_effective_capacity import (
    compile_project_effective_capacity_snapshot,
    effective_capacity_digest,
)

TOPOLOGY_SCHEMA = 'ccb.loop.agent_mount_topology.v1'
PROPOSAL_SCHEMA = 'ccb.loop.agent_mount_topology.proposal.v1'
OBSERVED_SCHEMA = 'ccb.loop.agent_mount_topology.observed.v1'
LEGACY_TOPOLOGY_SCHEMA = 'ccb.loop.agent_topology.v1'
LEGACY_PROPOSAL_SCHEMA = 'ccb.loop.agent_topology.proposal.v1'
LEGACY_OBSERVED_SCHEMA = 'ccb.loop.agent_topology.observed.v1'
DESIRED_STATES = frozenset({'present', 'hidden', 'parked', 'absent'})
ACTIVE_DESIRED_STATES = frozenset({'present', 'hidden', 'parked'})
RELEASE_POLICIES = frozenset({'auto', 'hide', 'park', 'unload'})
DRAINED_RELEASE_STATES = frozenset({'parked'})
LEGACY_DISPATCH_EDGE_TYPES = frozenset({'ask', 'ask_after'})
LEGACY_TOPOLOGY_PROFILES = frozenset({
    'worker',
    'checker',
    'round_checker',
    'planner',
    'round_reviewer',
    'ccb_worker',
    'ccb_checker',
    'ccb_round_checker',
})
USER_INTERACTION_WINDOW = 'ccb-user'
PLANNING_WINDOW = 'ccb-plan'
EXECUTION_WINDOW_PREFIX = 'ccb-exec'
TOPOLOGY_PROFILE_WINDOWS = {
    'ccb_frontdesk': USER_INTERACTION_WINDOW,
    'task_detailer': USER_INTERACTION_WINDOW,
    'ccb_task_detailer': USER_INTERACTION_WINDOW,
    'ccb_planner': PLANNING_WINDOW,
    'orchestrator': PLANNING_WINDOW,
    'ccb_orchestrator': PLANNING_WINDOW,
    'ccb_round_reviewer': PLANNING_WINDOW,
}
EXECUTION_PROFILES = frozenset({'coder', 'code_reviewer'})
MAX_TOPOLOGY_PANES_PER_WINDOW = 6


def loop_topology(context, command) -> dict[str, object]:
    action = str(getattr(command, 'action', '') or '').strip().lower()
    if action == 'propose':
        return _propose(context, command)
    if action == 'validate':
        return _validate_command(context, command)
    if action == 'commit':
        return _commit(context, command)
    if action == 'reconcile':
        return _reconcile(context, command)
    if action == 'status':
        return _status(context, command)
    if action == 'release':
        return _release(context, command)
    raise ValueError(f'unsupported loop topology action: {action}')


def _propose(context, command) -> dict[str, object]:
    loop_id = _normalize_loop_id(getattr(command, 'loop_id', None))
    source_path = Path(str(getattr(command, 'from_path', '') or '')).expanduser()
    if not source_path.is_absolute():
        source_path = context.project.project_root / source_path
    proposal = _load_json_object(source_path)
    proposal_id = _proposal_id(getattr(command, 'proposal_id', None), proposal)
    proposal = _normalize_proposal(proposal, loop_id=loop_id, proposal_id=proposal_id, source_path=source_path)
    validation = _validate_topology(context, proposal, loop_id=loop_id)
    path = _proposal_path(context, loop_id, proposal_id)
    atomic_write_json(path, proposal)
    _append_event(context, loop_id, {'event': 'proposal', 'proposal_id': proposal_id, 'agent_count': validation['agent_count']})
    return {
        'loop_topology_status': 'proposed',
        'loop_id': loop_id,
        'proposal_id': proposal_id,
        'proposal_path': str(path),
        'source_path': str(source_path),
        'validation': validation,
    }


def _validate_command(context, command) -> dict[str, object]:
    loop_id = _normalize_loop_id(getattr(command, 'loop_id', None))
    proposal_id = _normalize_proposal_id(getattr(command, 'proposal_id', None))
    proposal = _load_json_object(_proposal_path(context, loop_id, proposal_id))
    validation = _validate_topology(context, proposal, loop_id=loop_id)
    return {
        'loop_topology_status': 'valid',
        'loop_id': loop_id,
        'proposal_id': proposal_id,
        'proposal_path': str(_proposal_path(context, loop_id, proposal_id)),
        'validation': validation,
    }


def _commit(context, command) -> dict[str, object]:
    loop_id = _normalize_loop_id(getattr(command, 'loop_id', None))
    proposal_id = _normalize_proposal_id(getattr(command, 'proposal_id', None))
    proposal = _load_json_object(_proposal_path(context, loop_id, proposal_id))
    validation = _validate_topology(context, proposal, loop_id=loop_id)
    previous = _load_desired_optional(context, loop_id)
    previous_revision = int((previous or {}).get('revision') or 0)
    base_revision = proposal.get('base_revision')
    if base_revision is not None and int(base_revision) != previous_revision:
        raise ValueError(
            f'topology proposal base_revision={base_revision} does not match desired revision={previous_revision}'
        )
    revision = previous_revision + 1
    desired = {
        'schema': TOPOLOGY_SCHEMA,
        'record_type': 'ccb_loop_agent_mount_topology_desired',
        'topology_status': 'committed',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'loop_id': loop_id,
        'revision': revision,
        'base_revision': previous_revision,
        'proposal_id': proposal_id,
        'committed_at': _utc_now(),
        'agents': proposal.get('agents') or [],
        'windows': proposal.get('windows') or [],
        'nodes': proposal.get('nodes') or [],
        'release_policy': proposal.get('release_policy') or {'policy': 'auto', 'idle_only': True},
        'validation': validation,
    }
    if 'capacity_digest' in proposal:
        desired['capacity_digest'] = proposal.get('capacity_digest')
    if 'owner' in proposal:
        desired['owner'] = proposal.get('owner')
    if 'edges' in proposal:
        desired['edges'] = proposal.get('edges') or []
    if 'artifacts' in proposal:
        desired['artifacts'] = proposal.get('artifacts') or {}
    if 'gates' in proposal:
        desired['gates'] = proposal.get('gates') or []
    for compatibility_field in (
        'dispatch_compatibility',
        'compatibility_mode',
        'legacy_dispatch_compatibility',
    ):
        if compatibility_field in proposal:
            desired[compatibility_field] = proposal.get(compatibility_field)
    atomic_write_json(_desired_path(context, loop_id), desired)
    _append_event(
        context,
        loop_id,
        {'event': 'commit', 'proposal_id': proposal_id, 'revision': revision, 'agent_count': validation['agent_count']},
    )
    payload: dict[str, object] = {
        'loop_topology_status': 'committed',
        'loop_id': loop_id,
        'proposal_id': proposal_id,
        'revision': revision,
        'desired_path': str(_desired_path(context, loop_id)),
        'validation': validation,
    }
    if bool(getattr(command, 'apply', False)):
        payload['reconcile'] = _reconcile_desired(context, loop_id, desired=desired)
    return payload


def _reconcile(context, command) -> dict[str, object]:
    loop_id = _normalize_loop_id(getattr(command, 'loop_id', None))
    desired = _load_desired_optional(context, loop_id)
    if desired is None:
        return {
            'loop_topology_status': 'missing',
            'loop_id': loop_id,
            'desired_path': str(_desired_path(context, loop_id)),
            'observed_path': str(_observed_path(context, loop_id)),
            'actions': [],
            'drift': {'missing_desired': True},
        }
    return _reconcile_desired(context, loop_id, desired=desired)


def _status(context, command) -> dict[str, object]:
    loop_id = _normalize_loop_id(getattr(command, 'loop_id', None))
    desired_path = _desired_read_path(context, loop_id)
    observed_path = _observed_read_path(context, loop_id)
    desired = _load_json_optional(desired_path)
    observed = _load_json_optional(observed_path)
    proposals = [
        path.stem
        for path in sorted(_proposal_dir(context, loop_id).glob('*.json'))
        if path.is_file()
    ]
    status = 'missing' if desired is None else 'ready'
    if desired is not None and observed is None:
        status = 'desired_pending'
    elif desired is not None and observed is not None:
        desired_revision = int(desired.get('revision') or 0)
        observed_revision = int(observed.get('desired_revision') or 0)
        observed_status = str(observed.get('last_reconcile_status') or '')
        if observed_revision == desired_revision and observed_status == 'failed':
            status = 'failed'
        elif observed_revision == desired_revision and observed_status == 'release_incomplete':
            status = 'release_incomplete'
        elif observed_revision == desired_revision and observed_status == 'retained_busy':
            status = 'retained_busy'
        elif observed_revision == desired_revision and str(desired.get('topology_status') or '') == 'released':
            status = 'released'
        elif observed_revision < desired_revision:
            status = 'drift'
    return {
        'loop_topology_status': status,
        'loop_id': loop_id,
        'desired_path': str(desired_path),
        'observed_path': str(observed_path),
        'proposal_dir': str(_proposal_dir(context, loop_id)),
        'proposal_ids': proposals,
        'desired': _status_summary(desired),
        'observed': _status_summary(observed),
    }


def _release(context, command) -> dict[str, object]:
    loop_id = _normalize_loop_id(getattr(command, 'loop_id', None))
    desired = _load_desired_optional(context, loop_id)
    if desired is None:
        desired = {
            'schema': TOPOLOGY_SCHEMA,
            'record_type': 'ccb_loop_agent_mount_topology_desired',
            'topology_status': 'released',
            'project_id': context.project.project_id,
            'project_root': str(context.project.project_root),
            'loop_id': loop_id,
            'revision': 1,
            'base_revision': 0,
            'proposal_id': None,
            'committed_at': _utc_now(),
            'agents': [],
            'windows': [],
            'nodes': [],
            'release_policy': _release_policy_record(command),
            'validation': {'agent_count': 0, 'present_agent_count': 0, 'profile_counts': {}, 'edge_count': 0},
        }
    elif str(desired.get('topology_status') or '') == 'released' and not _desired_agents(
        desired,
        loop_id=loop_id,
    ):
        desired = json.loads(json.dumps(desired))
        desired['release_policy'] = _release_policy_record(command)
    else:
        desired = json.loads(json.dumps(desired))
        desired['schema'] = TOPOLOGY_SCHEMA
        desired['record_type'] = 'ccb_loop_agent_mount_topology_desired'
        desired['revision'] = int(desired.get('revision') or 0) + 1
        desired['base_revision'] = int(desired.get('revision') or 1) - 1
        desired['topology_status'] = 'released'
        desired['committed_at'] = _utc_now()
        desired['release_policy'] = _release_policy_record(command)
        _mark_all_agents_absent(desired)
    atomic_write_json(_desired_path(context, loop_id), desired)
    try:
        payload = _reconcile_desired(context, loop_id, desired=desired)
    except Exception:
        if not _retry_release_after_failed_reconcile(context, loop_id):
            raise
        payload = _reconcile_desired(context, loop_id, desired=desired)
    payload = _prune_released_authority(context, loop_id, desired=desired, payload=payload)
    payload = _prune_drained_release_authority(context, loop_id, payload=payload)
    payload = _mark_release_residue(context, loop_id, payload=payload)
    if payload.get('retained_count', 0) != 0:
        payload['loop_topology_status'] = 'retained_busy'
    elif payload.get('release_incomplete_count', 0) != 0:
        payload['loop_topology_status'] = 'release_incomplete'
    else:
        payload['loop_topology_status'] = 'released'
    _append_event(
        context,
        loop_id,
        {
            'event': 'release',
            'revision': desired.get('revision'),
            'retained_count': payload.get('retained_count', 0),
            'released_count': payload.get('released_count', 0),
            'drained_count': payload.get('drained_count', 0),
            'release_incomplete_count': payload.get('release_incomplete_count', 0),
        },
    )
    return payload


def _retry_release_after_failed_reconcile(context, loop_id: str) -> bool:
    observed = _load_json_optional(_observed_path(context, loop_id))
    if not isinstance(observed, Mapping):
        return False
    if str(observed.get('last_reconcile_status') or '') != 'failed':
        return False
    if int(observed.get('retained_count') or 0) != 0:
        return False
    blockers = _release_residue_blockers(observed)
    return bool(blockers)


def _reconcile_desired(context, loop_id: str, *, desired: Mapping[str, object]) -> dict[str, object]:
    loaded = load_project_config(context.project.project_root, include_loop_overlays=True)
    desired_agents = _desired_agents(desired, loop_id=loop_id)
    dynamic = _dynamic_records(context)
    dynamic_by_name = {str(record.get('agent') or ''): record for record in dynamic}
    wanted = {
        str(agent['id']): agent
        for agent in desired_agents
        if str(agent.get('desired_state') or 'present') in ACTIVE_DESIRED_STATES
    }
    absent = {str(agent['id']): agent for agent in desired_agents if str(agent.get('desired_state') or '') == 'absent'}
    actions: list[dict[str, object]] = []
    retained: list[dict[str, object]] = []
    default_release_policy = _topology_release_policy(desired)

    try:
        _validate_topology(
            context,
            desired,
            loop_id=loop_id,
            allow_stale_capacity=str(desired.get('topology_status') or '') == 'released',
        )
        pending_adds: list[tuple[Mapping[str, object], str]] = []
        pending_alignments: list[tuple[str, Mapping[str, object], Mapping[str, object], str]] = []
        for name, agent in wanted.items():
            record = dynamic_by_name.get(name)
            target_state = str(agent.get('desired_state') or 'present')
            if record is None or str(record.get('lifecycle_state') or '') == 'unloaded':
                pending_adds.append((agent, target_state))
            else:
                pending_alignments.append((name, agent, record, target_state))

        release_groups: dict[str, list[str]] = {}
        for name, record in sorted(dynamic_by_name.items()):
            if str(record.get('loop_id') or '') != loop_id:
                continue
            if name in wanted:
                continue
            if str(record.get('lifecycle_state') or '') == 'unloaded':
                continue
            target = absent.get(name)
            policy = _agent_release_policy(target, default_policy=default_release_policy)
            release_groups.setdefault(policy, []).append(name)
        for policy, names in sorted(release_groups.items()):
            batch_actions = _release_agents(context, tuple(names), policy=policy)
            actions.extend(batch_actions)
            for action in batch_actions:
                if str(action.get('status') or '') == 'retained_busy':
                    retained.append({'agent': action.get('agent'), 'reason': action.get('reason')})

        if pending_adds and not retained:
            actions.extend(_add_agents(context, tuple(pending_adds)))
            for agent, _target_state in pending_adds:
                name = str(agent.get('id') or '')
                dynamic_by_name[name] = _dynamic_record(context, name) or {}
        elif pending_adds:
            for agent, target_state in pending_adds:
                actions.append(
                    {
                        'action': 'add',
                        'agent': str(agent.get('id') or ''),
                        'status': 'deferred_retained_busy',
                        'target_state': target_state,
                        'apply_status': 'not_applied',
                    }
                )

        for _name, agent, record, target_state in pending_alignments:
            actions.extend(_align_agent(context, agent, record=record, target_state=target_state))

        for window_name in _target_windows(desired_agents):
            actions.append(_arrange_window(context, window_name))
    except Exception as exc:
        _write_observed(
            context,
            loop_id,
            desired=desired,
            desired_agents=desired_agents,
            actions=actions,
            retained=retained,
            loaded=loaded,
            status='failed',
            error=str(exc),
        )
        _append_event(
            context,
            loop_id,
            {
                'event': 'reconcile-failed',
                'revision': int(desired.get('revision') or 0),
                'agent_count': len(desired_agents),
                'retained_count': len(retained),
                'error': str(exc),
            },
        )
        raise

    return _write_observed(
        context,
        loop_id,
        desired=desired,
        desired_agents=desired_agents,
        actions=actions,
        retained=retained,
        loaded=loaded,
        status='retained_busy' if retained else 'reconciled',
    )


def _write_observed(
    context,
    loop_id: str,
    *,
    desired: Mapping[str, object],
    desired_agents: list[dict[str, object]],
    actions: list[dict[str, object]],
    retained: list[dict[str, object]],
    loaded,
    status: str,
    error: str | None = None,
) -> dict[str, object]:
    observed_agents = []
    for agent in desired_agents:
        name = str(agent.get('id') or '')
        record = _dynamic_record(context, name)
        observed_agents.append(_observed_agent(agent, record=record))
    released_agents = _released_agent_ids(observed_agents, actions)
    retained_agents = sorted(
        str(item.get('agent') or '')
        for item in retained
        if isinstance(item, Mapping) and str(item.get('agent') or '')
    )
    retain_reasons = {
        str(item.get('agent') or ''): item.get('reason')
        for item in retained
        if isinstance(item, Mapping) and str(item.get('agent') or '') and item.get('reason') is not None
    }
    observed = {
        'schema': OBSERVED_SCHEMA,
        'record_type': 'ccb_loop_agent_mount_topology_observed',
        'last_reconcile_status': status,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'loop_id': loop_id,
        'desired_revision': int(desired.get('revision') or 0),
        'reconciled_at': _utc_now(),
        'agents': observed_agents,
        'windows': list(desired.get('windows') or []),
        'actions': actions,
        'retained': retained,
        'retained_agents': retained_agents,
        'retain_reasons': retain_reasons,
        'retained_count': len(retained),
        'released_count': len(released_agents),
        'released_agents': released_agents,
        'drift': _drift_for(desired_agents, observed_agents),
        'config_source_kind': loaded.source_kind,
        'config_source': str(loaded.source_path) if loaded.source_path is not None else None,
    }
    if 'edges' in desired:
        observed['edges'] = list(desired.get('edges') or [])
    if 'capacity_digest' in desired:
        observed['capacity_digest'] = desired.get('capacity_digest')
    if 'owner' in desired:
        observed['owner'] = desired.get('owner')
    if error is not None:
        observed['error'] = error
    atomic_write_json(_observed_path(context, loop_id), observed)
    if status != 'failed':
        _append_event(
            context,
            loop_id,
            {
                'event': 'reconcile',
                'revision': observed['desired_revision'],
                'agent_count': len(observed_agents),
                'retained_count': len(retained),
            },
        )
    return {
        'loop_topology_status': observed['last_reconcile_status'],
        'loop_id': loop_id,
        'desired_revision': observed['desired_revision'],
        'desired_path': str(_desired_path(context, loop_id)),
        'observed_path': str(_observed_path(context, loop_id)),
        'agent_count': len(observed_agents),
        'retained_count': len(retained),
        'retained_agents': observed['retained_agents'],
        'retained': observed['retained'],
        'retain_reasons': observed['retain_reasons'],
        'released_count': observed['released_count'],
        'released_agents': observed['released_agents'],
        'actions': actions,
        'observed': observed,
    }


def _add_agent(context, agent: Mapping[str, object], *, target_state: str) -> dict[str, object]:
    return _add_agents(context, ((agent, target_state),))[0]


def _add_agents(context, agents: tuple[tuple[Mapping[str, object], str], ...]) -> list[dict[str, object]]:
    if not agents:
        return []
    commands = tuple(_add_agent_command(agent, target_state=target_state) for agent, target_state in agents)
    payloads = add_lifecycle_agents(
        context,
        commands,
        action='topology-agent-add-batch' if len(commands) > 1 else 'topology-agent-add',
    )
    results: list[dict[str, object]] = []
    target_states = {str(agent.get('id') or ''): target_state for agent, target_state in agents}
    for payload in payloads:
        name = str(payload.get('agent') or '')
        results.append(
            {
                'action': 'add',
                'agent': name,
                'status': payload.get('agent_lifecycle_status'),
                'target_state': target_states.get(name, 'present'),
                'apply_status': _apply_status(payload),
            }
        )
    return results


def _add_agent_command(agent: Mapping[str, object], *, target_state: str) -> SimpleNamespace:
    name = str(agent.get('id') or '')
    return SimpleNamespace(
        action='add',
        agent_name=name,
        provider=str(agent.get('provider') or ''),
        profile=str(agent.get('profile') or ''),
        role=_optional_text(agent.get('role')),
        model=_optional_text(agent.get('model')),
        thinking=_optional_text(agent.get('thinking')),
        workspace_mode=None,
        workspace_group=_optional_text(agent.get('workspace_group')),
        window_name=_optional_text(agent.get('window_name')),
        window_class=_optional_text(agent.get('window_class')),
        loop_id=_optional_text(agent.get('loop_id')),
        node_id=_optional_text(agent.get('node_id')),
        lifetime=_lifetime_for_agent(agent),
        visibility='parked' if target_state == 'parked' else ('hidden' if target_state == 'hidden' else 'visible'),
        json_output=True,
    )


def _align_agent(
    context,
    agent: Mapping[str, object],
    *,
    record: Mapping[str, object],
    target_state: str,
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    name = str(agent.get('id') or '')
    current_window = _optional_text(record.get('resolved_window_name')) or _optional_text(record.get('window_name'))
    target_window = _optional_text(agent.get('window_name'))
    if target_window and current_window != target_window:
        command = SimpleNamespace(
            action='move',
            agent_name=name,
            agent_names=(),
            window_name=target_window,
            window_class=_optional_text(agent.get('window_class')),
            loop_id=_optional_text(agent.get('loop_id')),
            node_id=_optional_text(agent.get('node_id')),
            reason='topology reconcile',
            json_output=True,
        )
        payload = agent_lifecycle(context, command)
        actions.append(
            {
                'action': 'move',
                'agent': name,
                'status': payload.get('agent_lifecycle_status'),
                'target_window': target_window,
                'apply_status': _apply_status(payload),
            }
        )
    current_state = str(record.get('lifecycle_state') or '')
    desired_action = _transition_action(current_state, target_state)
    if desired_action is not None:
        command = SimpleNamespace(
            action=desired_action,
            agent_name=name,
            agent_names=(),
            visibility='visible' if target_state == 'present' else 'hidden',
            reason='topology reconcile',
            json_output=True,
        )
        payload = agent_lifecycle(context, command)
        actions.append(
            {
                'action': desired_action,
                'agent': name,
                'status': payload.get('agent_lifecycle_status'),
                'target_state': target_state,
                'apply_status': _apply_status(payload),
            }
        )
    if not actions:
        actions.append({'action': 'noop', 'agent': name, 'status': 'ok', 'target_state': target_state})
    return actions


def _release_agent(context, name: str, *, policy: str) -> dict[str, object]:
    if policy not in RELEASE_POLICIES:
        raise ValueError(f'unsupported topology release policy: {policy}')
    command = SimpleNamespace(
        action='release',
        agent_name=name,
        agent_names=(),
        policy=policy,
        idle_only=True,
        summary_policy=None,
        reason='topology release',
        json_output=True,
    )
    payload = agent_lifecycle(context, command)
    status = str(payload.get('agent_lifecycle_status') or '')
    retain_reason = payload.get('retain_reason') or payload.get('reason')
    return {
        'action': 'release',
        'agent': name,
        'status': status,
        'policy': command.policy,
        'reason': retain_reason,
        'retain_reason': retain_reason if status == 'retained_busy' else None,
        'apply_status': _apply_status(payload),
    }


def _release_agents(context, names: tuple[str, ...], *, policy: str) -> list[dict[str, object]]:
    if policy not in RELEASE_POLICIES:
        raise ValueError(f'unsupported topology release policy: {policy}')
    command = SimpleNamespace(
        action='release',
        agent_name=None,
        agent_names=names,
        policy=policy,
        idle_only=True,
        summary_policy=None,
        reason='topology release',
        json_output=True,
    )
    payload = agent_lifecycle(context, command)
    retained_agents = set(str(item) for item in tuple(payload.get('retained_agents') or ()))
    if retained_agents:
        actions: list[dict[str, object]] = []
        for name in names:
            if name not in retained_agents:
                continue
            retain_reason = _retained_reason(context, payload, name)
            actions.append(
                {
                    'action': 'release',
                    'agent': name,
                    'status': 'retained_busy',
                    'policy': policy,
                    'reason': retain_reason,
                    'retain_reason': retain_reason,
                    'apply_status': _apply_status(payload),
                }
            )
        for name in names:
            if name not in retained_agents:
                actions.append(_release_agent(context, name, policy=policy))
        return actions
    records_by_name = {
        str(record.get('agent') or ''): record
        for record in tuple(payload.get('agents') or ())
        if isinstance(record, Mapping)
    }
    actions: list[dict[str, object]] = []
    for name in names:
        record = records_by_name.get(name, {})
        retained = name in retained_agents or bool(record.get('retained_busy'))
        status = 'retained_busy' if retained else str(payload.get('agent_lifecycle_status') or '')
        if not status:
            status = str(record.get('agent_lifecycle_status') or '') or 'ok'
        retain_reason = record.get('retain_reason') or payload.get('reason')
        actions.append(
            {
                'action': 'release',
                'agent': name,
                'status': status,
                'policy': policy,
                'reason': retain_reason,
                'retain_reason': retain_reason if retained else None,
                'apply_status': _apply_status(payload),
            }
        )
    return actions


def _retained_reason(context, payload: Mapping[str, object], name: str) -> object:
    for record in tuple(payload.get('agents') or ()):
        if not isinstance(record, Mapping):
            continue
        if str(record.get('agent') or '') == name:
            reason = record.get('retain_reason') or payload.get('reason')
            if reason is not None:
                return reason
    record = _dynamic_record(context, name)
    if record is not None:
        reason = record.get('retain_reason') or payload.get('reason')
        if reason is not None:
            return reason
    return payload.get('reason')


def _arrange_window(context, window_name: str) -> dict[str, object]:
    command = SimpleNamespace(action='arrange', window_name=window_name, timeout_s=5.0, json_output=True)
    payload = layout_command(context, command)
    action = {
        'action': 'reflow',
        'window_name': window_name,
        'status': payload.get('arrange_status') or payload.get('layout_status'),
        'reason': payload.get('reason'),
    }
    if 'reflowed_windows' in payload:
        action['reflowed_windows'] = list(payload.get('reflowed_windows') or ())
    if 'reflow_errors' in payload:
        action['reflow_errors'] = dict(payload.get('reflow_errors') or {})
    preserved_agent_panes = _layout_agent_panes(payload)
    if preserved_agent_panes:
        action['preserved_agent_panes'] = preserved_agent_panes
    if 'moved_agents' in payload:
        action['moved_agents'] = list(payload.get('moved_agents') or ())
    return action


def _layout_agent_panes(payload: Mapping[str, object]) -> dict[str, str]:
    panes: dict[str, str] = {}
    for raw_window in tuple(payload.get('windows') or ()):
        if not isinstance(raw_window, Mapping):
            continue
        for raw_agent in tuple(raw_window.get('agents') or ()):
            if not isinstance(raw_agent, Mapping):
                continue
            name = str(raw_agent.get('agent') or '').strip()
            pane_id = str(raw_agent.get('pane_id') or '').strip()
            if name and pane_id:
                panes[name] = pane_id
    return dict(sorted(panes.items()))


def _transition_action(current_state: str, target_state: str) -> str | None:
    if target_state == 'present':
        return None if current_state == 'visible' else 'resume'
    if target_state == 'hidden':
        return None if current_state == 'hidden' else 'hide'
    if target_state == 'parked':
        return None if current_state == 'parked' else 'park'
    return None


def _validate_topology(
    context,
    payload: Mapping[str, object],
    *,
    loop_id: str,
    allow_stale_capacity: bool = False,
) -> dict[str, object]:
    loaded = load_project_config(context.project.project_root, include_loop_overlays=False)
    config_version = int(loaded.config.version)
    snapshot = None
    allowed_legacy_profiles: frozenset[str] = frozenset()
    if config_version == 2:
        snapshot = compile_project_effective_capacity_snapshot(context.project.project_root)
        allowed_legacy_profiles = frozenset(
            str(profile)
            for logical, profile in snapshot['profile_aliases'].items()
            if logical in {'coder', 'code_reviewer'} and str(profile) in LEGACY_TOPOLOGY_PROFILES
        )
    agents = _desired_agents(
        payload,
        validate=True,
        loop_id=loop_id,
        allowed_legacy_profiles=allowed_legacy_profiles,
    )
    windows = _window_dicts(payload)
    _validate_mount_placement(agents, windows=windows)
    default_release_policy = _topology_release_policy(payload)
    for agent in agents:
        _agent_release_policy(agent, default_policy=default_release_policy)
    profile_counts = Counter(str(agent.get('profile') or '') for agent in agents if str(agent.get('desired_state') or 'present') in ACTIVE_DESIRED_STATES)
    capacity_digest = None
    if config_version == 3:
        _validate_mount_owner(payload, loop_id=loop_id, required=not allow_stale_capacity)
        snapshot = compile_project_effective_capacity_snapshot(context.project.project_root)
        capacity_digest = effective_capacity_digest(snapshot)
        supplied_digest = str(payload.get('capacity_digest') or '')
        if not supplied_digest and not allow_stale_capacity:
            raise ValueError('Config V3 mount topology requires canonical capacity_digest')
        if supplied_digest and supplied_digest != capacity_digest and not allow_stale_capacity:
            raise ValueError(
                f'Config V3 mount topology capacity_digest is stale: expected {capacity_digest}, got {supplied_digest}'
            )
        max_nodes = int(snapshot['limits']['max_active_dynamic_agents'])
        known_profiles = snapshot['dynamic_profiles']
        if sum(profile_counts.values()) > max_nodes:
            raise ValueError(
                f'loop topology exceeds max_active_dynamic_agents={max_nodes}: requested {sum(profile_counts.values())}'
            )
    else:
        _validate_mount_owner(payload, loop_id=loop_id, required=False)
        capacity = loaded.config.loop_capacity or LoopCapacityConfig()
        if agents and not capacity.enabled:
            raise ValueError('loop topology requires [loop.capacity].enabled = true')
        if sum(profile_counts.values()) > capacity.max_nodes:
            raise ValueError(
                f'loop topology exceeds max_nodes={capacity.max_nodes}: requested {sum(profile_counts.values())}'
            )
        known_profiles = capacity.role_profiles
    for profile, count in sorted(profile_counts.items()):
        spec = known_profiles.get(profile)
        if spec is None:
            known = ', '.join(sorted(known_profiles)) or '<none>'
            raise ValueError(f'unknown loop topology profile {profile!r}; configured profiles: {known}')
        maximum = int(spec['max_instances']) if isinstance(spec, Mapping) else int(spec.max_instances)
        if count > maximum:
            raise ValueError(f'loop topology profile {profile} exceeds max_instances={maximum}: requested {count}')
    _validate_no_mount_dispatch_dsl(payload)
    _validate_edges(payload, agents, configured_agents=set(getattr(loaded.config, 'agents', {}) or {}))
    validation = {
        'topology_validation_status': 'valid',
        'loop_id': loop_id,
        'config_version': config_version,
        'agent_count': len(agents),
        'present_agent_count': sum(profile_counts.values()),
        'profile_counts': dict(sorted(profile_counts.items())),
        'window_count': len(windows),
        'edge_count': len(tuple(payload.get('edges') or ())),
        'config_source_kind': loaded.source_kind,
        'config_source': str(loaded.source_path) if loaded.source_path is not None else None,
    }
    if capacity_digest is not None:
        validation['capacity_digest'] = capacity_digest
    return validation


def _desired_agents(
    payload: Mapping[str, object],
    *,
    validate: bool = False,
    loop_id: str | None = None,
    allowed_legacy_profiles: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    agents: list[dict[str, object]] = []
    seen: set[str] = set()
    seen_nodes: set[str] = set()
    for node_index, node in enumerate(_node_dicts(payload), start=1):
        node_id = _node_id(node, fallback=f'node{node_index}')
        if validate and node_id in seen_nodes:
            raise ValueError(f'duplicate topology node id: {node_id}')
        seen_nodes.add(node_id)
        for agent_index, raw_agent in enumerate(_agent_dicts(node), start=1):
            agent = dict(raw_agent)
            profile = _normalize_named_value(agent.get('profile'), field_name='profile')
            name = agent.get('id') or agent.get('name')
            if name is None:
                if loop_id is None:
                    raise ValueError('topology agent requires id when loop_id is not available')
                name = f'loop-{loop_id}-{profile}-{agent_index}'
            name = _normalize_named_value(name, field_name='agent id')
            if name in seen:
                raise ValueError(f'duplicate topology agent id: {name}')
            seen.add(name)
            state = str(agent.get('desired_state') or agent.get('state') or 'present').strip().lower()
            if state not in DESIRED_STATES:
                raise ValueError(f'topology agent {name} has unsupported desired_state: {state}')
            agent['id'] = name
            agent['profile'] = profile
            agent['desired_state'] = state
            explicit_loop_id = _optional_text(agent.get('loop_id'))
            if validate and explicit_loop_id is not None and loop_id is not None and explicit_loop_id != loop_id:
                raise ValueError(
                    f'topology agent {name} loop_id={explicit_loop_id} does not match topology loop_id={loop_id}'
                )
            agent['loop_id'] = str(explicit_loop_id or loop_id or '')
            agent['node_id'] = node_id
            lifecycle = _optional_text(agent.get('lifecycle'))
            if lifecycle is not None:
                agent['lifecycle'] = lifecycle
            if _optional_text(agent.get('lifetime')) is None:
                agent['lifetime'] = _lifetime_for_lifecycle(lifecycle)
            profile_key = _topology_profile_key(agent)
            if (
                validate
                and profile_key in LEGACY_TOPOLOGY_PROFILES
                and profile_key not in allowed_legacy_profiles
            ):
                raise ValueError(
                    f'legacy workflow profile alias {profile!r} is not supported in topology; '
                    'use ccb_frontdesk, task_detailer, ccb_task_detailer, ccb_planner, orchestrator, ccb_orchestrator, '
                    'ccb_round_reviewer, coder, or code_reviewer'
                )
            explicit_window = _optional_text(agent.get('window_name'))
            if explicit_window is not None:
                agent['window_name'] = explicit_window
            agents.append(agent)
    _assign_default_windows(agents)
    return agents


def _assign_default_windows(agents: list[dict[str, object]]) -> None:
    execution_index = 0
    for agent in agents:
        if _optional_text(agent.get('window_name')) is not None:
            continue
        if _optional_text(agent.get('window_class')) is not None:
            continue
        profile = _topology_profile_key(agent)
        window_name = TOPOLOGY_PROFILE_WINDOWS.get(profile)
        if window_name is not None:
            agent['window_name'] = window_name
            continue
        if profile not in EXECUTION_PROFILES:
            raise ValueError(
                f'topology agent {agent.get("id")} profile {profile!r} requires explicit window_name/window_class '
                'or one of: ccb_frontdesk, ccb_task_detailer, ccb_planner, ccb_orchestrator, '
                'ccb_round_reviewer, coder, code_reviewer'
            )
        agent['window_name'] = _execution_window_name(execution_index)
        if str(agent.get('desired_state') or 'present') in ACTIVE_DESIRED_STATES:
            execution_index += 1


def _topology_profile_key(agent: Mapping[str, object]) -> str:
    return str(agent.get('profile') or '').strip().lower().replace('-', '_')


def _execution_window_name(index: int) -> str:
    page = (max(0, int(index)) // MAX_TOPOLOGY_PANES_PER_WINDOW) + 1
    return EXECUTION_WINDOW_PREFIX if page == 1 else f'{EXECUTION_WINDOW_PREFIX}-{page}'


def _validate_edges(
    payload: Mapping[str, object],
    agents: Iterable[Mapping[str, object]],
    *,
    configured_agents: set[str],
) -> None:
    edges = tuple(payload.get('edges') or ())
    if not edges:
        return
    if not _legacy_dispatch_compatibility(payload):
        raise ValueError(
            'mount topology does not accept communication or dispatch edges; '
            'use ask-first orchestration, or set dispatch_compatibility="legacy" for legacy graph dispatch'
        )
    agent_ids = {str(agent.get('id') or '') for agent in agents}
    known_endpoints = agent_ids | configured_agents | {'user'}
    edge_ids: set[str] = set()
    dependencies: dict[str, set[str]] = {}
    for index, raw_edge in enumerate(edges, start=1):
        if not isinstance(raw_edge, Mapping):
            raise ValueError(f'topology edge #{index} must be an object')
        edge_id = str(raw_edge.get('id') or f'edge{index}').strip()
        if not edge_id:
            raise ValueError(f'topology edge #{index} id cannot be empty')
        if edge_id in edge_ids:
            raise ValueError(f'duplicate topology edge id: {edge_id}')
        edge_ids.add(edge_id)
        edge_type = str(raw_edge.get('type') or '').strip().lower()
        if not edge_type:
            raise ValueError(f'topology edge {edge_id} requires type')
        if edge_type not in LEGACY_DISPATCH_EDGE_TYPES:
            supported = ', '.join(sorted(LEGACY_DISPATCH_EDGE_TYPES))
            raise ValueError(f'unsupported topology edge type {edge_type!r} for edge {edge_id}; supported: {supported}')
        for endpoint in ('from', 'to'):
            value = str(raw_edge.get(endpoint) or '').strip()
            if value and value not in known_endpoints:
                raise ValueError(f'topology edge {edge_id} {endpoint} references unknown agent: {value}')
        dependencies[edge_id] = {str(item).strip() for item in tuple(raw_edge.get('after') or ()) if str(item).strip()}
    for edge_id, after in dependencies.items():
        missing = sorted(item for item in after if item not in edge_ids)
        if missing:
            raise ValueError(f'topology edge {edge_id} depends on unknown edge: {missing[0]}')
        if edge_id in after:
            raise ValueError(f'topology edge {edge_id} cannot depend on itself')
    _assert_acyclic(dependencies)


def _assert_acyclic(dependencies: Mapping[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(edge_id: str) -> None:
        if edge_id in visited:
            return
        if edge_id in visiting:
            raise ValueError(f'topology edge dependency cycle detected at {edge_id}')
        visiting.add(edge_id)
        for parent in dependencies.get(edge_id, set()):
            visit(parent)
        visiting.remove(edge_id)
        visited.add(edge_id)

    for edge_id in dependencies:
        visit(edge_id)


def _validate_no_mount_dispatch_dsl(payload: Mapping[str, object]) -> None:
    if _legacy_dispatch_compatibility(payload):
        return
    if payload.get('artifacts'):
        raise ValueError(
            'mount topology does not accept topology dispatch artifacts; '
            'use task/round artifact imports, or set dispatch_compatibility="legacy" for legacy graph dispatch'
        )
    if tuple(payload.get('gates') or ()):
        raise ValueError(
            'mount topology does not accept topology dispatch gates; '
            'use ask-first orchestration, or set dispatch_compatibility="legacy" for legacy graph dispatch'
        )


def _validate_mount_owner(payload: Mapping[str, object], *, loop_id: str, required: bool) -> None:
    raw_owner = payload.get('owner')
    if raw_owner is None:
        if required:
            raise ValueError('Config V3 mount topology requires loop-scoped owner')
        return
    if not isinstance(raw_owner, Mapping):
        raise ValueError('mount topology owner must be an object')
    if set(raw_owner) != {'kind', 'loop_id'}:
        raise ValueError('mount topology owner fields must be exactly kind and loop_id')
    if str(raw_owner.get('kind') or '') != 'loop':
        raise ValueError('mount topology owner kind must be loop')
    owner_loop_id = _normalize_named_value(raw_owner.get('loop_id'), field_name='mount topology owner loop_id')
    if owner_loop_id != loop_id:
        raise ValueError(
            f'mount topology owner loop_id={owner_loop_id} does not match topology loop_id={loop_id}'
        )


def _normalize_proposal(
    payload: Mapping[str, object],
    *,
    loop_id: str,
    proposal_id: str,
    source_path: Path,
) -> dict[str, object]:
    proposal = dict(payload)
    proposal['schema'] = PROPOSAL_SCHEMA
    proposal['record_type'] = 'ccb_loop_agent_mount_topology_proposal'
    proposal['loop_id'] = loop_id
    proposal['proposal_id'] = proposal_id
    proposal['proposed_at'] = str(proposal.get('proposed_at') or _utc_now())
    proposal['source_path'] = str(source_path)
    proposal.setdefault('agents', [])
    proposal.setdefault('windows', [])
    proposal.setdefault('nodes', [])
    return proposal


def _node_dicts(payload: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    nodes = payload.get('nodes') or []
    if not isinstance(nodes, list):
        raise ValueError('topology nodes must be a list')
    top_level_agents = payload.get('agents') or []
    if nodes:
        if top_level_agents and not isinstance(top_level_agents, list):
            raise ValueError('mount topology agents must be a list')
        return tuple(_node_object(raw_node, index) for index, raw_node in enumerate(nodes, start=1))
    if not isinstance(top_level_agents, list):
        raise ValueError('mount topology agents must be a list')
    if top_level_agents:
        return ({'id': 'mount', 'agents': [dict(agent) if isinstance(agent, Mapping) else agent for agent in top_level_agents]},)
    return ()


def _node_object(raw_node: object, index: int) -> dict[str, object]:
    if not isinstance(raw_node, Mapping):
        raise ValueError(f'topology node #{index} must be an object')
    return dict(raw_node)


def _window_dicts(payload: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    raw_windows = payload.get('windows') or []
    if not isinstance(raw_windows, list):
        raise ValueError('mount topology windows must be a list')
    result = []
    seen: set[str] = set()
    for index, raw_window in enumerate(raw_windows, start=1):
        if not isinstance(raw_window, Mapping):
            raise ValueError(f'mount topology window #{index} must be an object')
        window = dict(raw_window)
        name = _normalize_named_value(window.get('name'), field_name='window name')
        if name in seen:
            raise ValueError(f'duplicate mount topology window name: {name}')
        seen.add(name)
        window['name'] = name
        max_panes = window.get('max_panes')
        if max_panes is not None:
            try:
                normalized_max = int(max_panes)
            except (TypeError, ValueError) as exc:
                raise ValueError(f'mount topology window {name} max_panes must be an integer') from exc
            if normalized_max < 1 or normalized_max > MAX_TOPOLOGY_PANES_PER_WINDOW:
                raise ValueError(
                    f'mount topology window {name} max_panes must be between 1 and {MAX_TOPOLOGY_PANES_PER_WINDOW}'
                )
            window['max_panes'] = normalized_max
        result.append(window)
    return tuple(result)


def _validate_mount_placement(agents: Iterable[Mapping[str, object]], *, windows: tuple[dict[str, object], ...]) -> None:
    known_windows = {str(window.get('name') or '') for window in windows}
    window_limits = {
        str(window.get('name') or ''): int(window.get('max_panes') or MAX_TOPOLOGY_PANES_PER_WINDOW)
        for window in windows
    }
    active_counts: Counter[str] = Counter()
    pane_orders: dict[str, set[int]] = {}
    for agent in agents:
        if str(agent.get('desired_state') or 'present') not in ACTIVE_DESIRED_STATES:
            continue
        window_name = str(agent.get('window_name') or '').strip()
        if not window_name:
            continue
        if known_windows and window_name not in known_windows:
            raise ValueError(f'topology agent {agent.get("id")} references unknown mount topology window: {window_name}')
        active_counts[window_name] += 1
        pane_order = agent.get('pane_order')
        if pane_order is not None:
            try:
                normalized_order = int(pane_order)
            except (TypeError, ValueError) as exc:
                raise ValueError(f'topology agent {agent.get("id")} pane_order must be an integer') from exc
            if normalized_order < 0:
                raise ValueError(f'topology agent {agent.get("id")} pane_order must be >= 0')
            pane_limit = window_limits.get(window_name, MAX_TOPOLOGY_PANES_PER_WINDOW)
            if normalized_order >= pane_limit:
                raise ValueError(
                    f'topology agent {agent.get("id")} pane_order={normalized_order} '
                    f'exceeds window {window_name} max_panes={pane_limit}'
                )
            seen_orders = pane_orders.setdefault(window_name, set())
            if normalized_order in seen_orders:
                raise ValueError(f'duplicate pane_order {normalized_order} in mount topology window {window_name}')
            seen_orders.add(normalized_order)
    for window_name, count in sorted(active_counts.items()):
        limit = window_limits.get(window_name, MAX_TOPOLOGY_PANES_PER_WINDOW)
        if count > limit:
            raise ValueError(f'mount topology window {window_name} exceeds max_panes={limit}: requested {count}')


def _mark_all_agents_absent(payload: dict[str, object]) -> None:
    top_level_agents = payload.get('agents') or []
    if isinstance(top_level_agents, list):
        for raw_agent in top_level_agents:
            if isinstance(raw_agent, dict):
                raw_agent['desired_state'] = 'absent'
    nodes = payload.get('nodes') or []
    if not isinstance(nodes, list):
        return
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            continue
        agents = raw_node.get('agents') or []
        if not isinstance(agents, list):
            continue
        for raw_agent in agents:
            if isinstance(raw_agent, dict):
                raw_agent['desired_state'] = 'absent'


def _prune_released_authority(
    context,
    loop_id: str,
    *,
    desired: Mapping[str, object],
    payload: Mapping[str, object],
) -> dict[str, object]:
    released_names = frozenset(str(item) for item in tuple(payload.get('released_agents') or ()) if str(item))
    return _prune_authority_agent_records(
        context,
        loop_id,
        desired=desired,
        payload=payload,
        agent_names=released_names,
    )


def _prune_drained_release_authority(context, loop_id: str, *, payload: Mapping[str, object]) -> dict[str, object]:
    observed = json.loads(json.dumps(payload.get('observed') or {}))
    drained_names = frozenset(_drained_release_agent_ids(observed))
    if not drained_names:
        return dict(payload)
    desired = _load_desired_optional(context, loop_id)
    if desired is None:
        return dict(payload)
    updated = _prune_authority_agent_records(
        context,
        loop_id,
        desired=desired,
        payload=payload,
        agent_names=drained_names,
    )
    observed = json.loads(json.dumps(updated.get('observed') or {}))
    drained_agents = sorted(drained_names)
    drain_reasons = {name: 'parked_after_release' for name in drained_agents}
    observed['drained_agents'] = drained_agents
    observed['drained_count'] = len(drained_agents)
    observed['drain_reasons'] = drain_reasons
    atomic_write_json(_observed_path(context, loop_id), observed)
    updated['observed'] = observed
    updated['drained_agents'] = drained_agents
    updated['drained_count'] = len(drained_agents)
    updated['drain_reasons'] = drain_reasons
    return updated


def _prune_authority_agent_records(
    context,
    loop_id: str,
    *,
    desired: Mapping[str, object],
    payload: Mapping[str, object],
    agent_names: frozenset[str],
) -> dict[str, object]:
    pruned_names = frozenset(str(item) for item in agent_names if str(item))
    if not pruned_names:
        return dict(payload)
    pruned_desired = json.loads(json.dumps(desired))
    _remove_agent_records(pruned_desired, released_names=pruned_names)
    _refresh_desired_validation(pruned_desired, loop_id=loop_id)
    atomic_write_json(_desired_path(context, loop_id), pruned_desired)

    observed = json.loads(json.dumps(payload.get('observed') or {}))
    observed_agents = [
        agent
        for agent in tuple(observed.get('agents') or ())
        if isinstance(agent, Mapping) and str(agent.get('id') or '') not in pruned_names
    ]
    desired_agents = _desired_agents(pruned_desired, loop_id=loop_id)
    observed['agents'] = observed_agents
    observed['drift'] = _drift_for(desired_agents, observed_agents)
    atomic_write_json(_observed_path(context, loop_id), observed)

    updated = dict(payload)
    updated['observed'] = observed
    updated['agent_count'] = len(observed_agents)
    return updated


def _mark_release_residue(context, loop_id: str, *, payload: Mapping[str, object]) -> dict[str, object]:
    updated = dict(payload)
    if int(updated.get('retained_count') or 0) > 0:
        return updated
    observed = json.loads(json.dumps(updated.get('observed') or {}))
    blockers = _release_residue_blockers(observed)
    if not blockers:
        observed['release_incomplete_agents'] = []
        observed['release_incomplete_count'] = 0
        observed['release_incomplete_profile_counts'] = {}
        atomic_write_json(_observed_path(context, loop_id), observed)
        updated['observed'] = observed
        updated['release_incomplete_agents'] = []
        updated['release_incomplete_count'] = 0
        updated['release_incomplete_profile_counts'] = {}
        return updated
    names = sorted(blockers)
    profile_counts = Counter(
        str(item.get('profile') or '')
        for item in blockers.values()
        if str(item.get('profile') or '')
    )
    release_profile_counts = dict(sorted(profile_counts.items()))
    observed['last_reconcile_status'] = 'release_incomplete'
    observed['release_incomplete_agents'] = names
    observed['release_incomplete_count'] = len(names)
    observed['release_incomplete_profile_counts'] = release_profile_counts
    observed['release_blockers'] = blockers
    atomic_write_json(_observed_path(context, loop_id), observed)
    updated['observed'] = observed
    updated['release_incomplete_agents'] = names
    updated['release_incomplete_count'] = len(names)
    updated['release_incomplete_profile_counts'] = release_profile_counts
    updated['release_blockers'] = blockers
    return updated


def _release_residue_blockers(observed: Mapping[str, object]) -> dict[str, dict[str, object]]:
    blockers: dict[str, dict[str, object]] = {}
    for raw_agent in tuple(observed.get('agents') or ()):
        if not isinstance(raw_agent, Mapping):
            continue
        name = str(raw_agent.get('id') or '')
        desired_state = str(raw_agent.get('desired_state') or '')
        observed_state = str(raw_agent.get('observed_state') or '')
        if not name or desired_state != 'absent' or observed_state == 'released':
            continue
        if _is_drained_release_agent(raw_agent):
            continue
        blockers[name] = {
            'desired_state': desired_state,
            'lifecycle_state': raw_agent.get('lifecycle_state'),
            'observed_state': observed_state,
            'profile': raw_agent.get('profile'),
            'reason': 'active_after_release',
        }
    return dict(sorted(blockers.items()))


def _drained_release_agent_ids(observed: Mapping[str, object]) -> list[str]:
    return sorted(
        str(agent.get('id') or '')
        for agent in tuple(observed.get('agents') or ())
        if isinstance(agent, Mapping) and _is_drained_release_agent(agent)
    )


def _is_drained_release_agent(agent: Mapping[str, object]) -> bool:
    return (
        str(agent.get('desired_state') or '') == 'absent'
        and str(agent.get('observed_state') or '') in DRAINED_RELEASE_STATES
        and str(agent.get('lifecycle_state') or '') in DRAINED_RELEASE_STATES
    )


def _remove_agent_records(payload: dict[str, object], *, released_names: frozenset[str]) -> None:
    top_level_agents = payload.get('agents')
    if isinstance(top_level_agents, list):
        payload['agents'] = [
            raw_agent
            for raw_agent in top_level_agents
            if _raw_agent_name(raw_agent) not in released_names
        ]
    nodes = payload.get('nodes')
    if not isinstance(nodes, list):
        return
    pruned_nodes = []
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            pruned_nodes.append(raw_node)
            continue
        node = dict(raw_node)
        agents = node.get('agents')
        if isinstance(agents, list):
            node['agents'] = [
                raw_agent
                for raw_agent in agents
                if _raw_agent_name(raw_agent) not in released_names
            ]
        if node.get('agents'):
            pruned_nodes.append(node)
    payload['nodes'] = pruned_nodes


def _raw_agent_name(raw_agent: object) -> str | None:
    if not isinstance(raw_agent, Mapping):
        return None
    value = raw_agent.get('id') or raw_agent.get('name')
    if value is None:
        return None
    return str(value).strip() or None


def _refresh_desired_validation(payload: dict[str, object], *, loop_id: str) -> None:
    agents = _desired_agents(payload, loop_id=loop_id)
    profile_counts = Counter(
        str(agent.get('profile') or '')
        for agent in agents
        if str(agent.get('desired_state') or 'present') in ACTIVE_DESIRED_STATES
    )
    validation = dict(payload.get('validation') or {})
    validation['agent_count'] = len(agents)
    validation['present_agent_count'] = sum(profile_counts.values())
    validation['profile_counts'] = dict(sorted(profile_counts.items()))
    payload['validation'] = validation


def _agent_dicts(node: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    agents = node.get('agents') or []
    if not isinstance(agents, list):
        raise ValueError(f'topology node {_node_id(node, fallback="<unknown>")} agents must be a list')
    result = []
    for index, raw_agent in enumerate(agents, start=1):
        if not isinstance(raw_agent, Mapping):
            raise ValueError(f'topology node {_node_id(node, fallback="<unknown>")} agent #{index} must be an object')
        result.append(dict(raw_agent))
    return tuple(result)


def _node_id(node: Mapping[str, object], *, fallback: str) -> str:
    return _normalize_named_value(node.get('id') or fallback, field_name='node id')


def _target_windows(agents: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
    names = []
    seen = set()
    for agent in agents:
        if str(agent.get('desired_state') or '') == 'absent':
            continue
        window = str(agent.get('window_name') or '').strip()
        if window and window not in seen:
            seen.add(window)
            names.append(window)
    return tuple(names)


def _dynamic_records(context) -> tuple[dict[str, object], ...]:
    root = Path(context.paths.runtime_state_root) / 'runtime' / 'agents'
    if not root.is_dir():
        return ()
    records = []
    for path in sorted(root.glob('*/lifecycle.json')):
        payload = _load_json_optional(path)
        if isinstance(payload, dict):
            records.append(payload)
    return tuple(records)


def _dynamic_record(context, name: str) -> dict[str, object] | None:
    return _load_json_optional(Path(context.paths.runtime_state_root) / 'runtime' / 'agents' / name / 'lifecycle.json')


def _observed_agent(agent: Mapping[str, object], *, record: Mapping[str, object] | None) -> dict[str, object]:
    desired_state = str(agent.get('desired_state') or 'present')
    lifecycle_state = str((record or {}).get('lifecycle_state') or '')
    if record is None:
        observed_state = 'released' if desired_state == 'absent' else 'missing'
    elif lifecycle_state == 'unloaded':
        observed_state = 'released'
    elif lifecycle_state == 'parked':
        observed_state = 'parked'
    elif lifecycle_state == 'hidden':
        observed_state = 'hidden'
    else:
        observed_state = 'present'
    return {
        'id': agent.get('id'),
        'profile': agent.get('profile'),
        'role': agent.get('role') or (record or {}).get('role'),
        'provider': agent.get('provider') or (record or {}).get('provider'),
        'model': agent.get('model') or (record or {}).get('model'),
        'thinking': agent.get('thinking') or (record or {}).get('thinking'),
        'provider_profile': agent.get('provider_profile') or (record or {}).get('provider_profile'),
        'lifecycle': agent.get('lifecycle'),
        'lifetime': agent.get('lifetime') or (record or {}).get('lifetime'),
        'release_policy': agent.get('release_policy'),
        'node_id': agent.get('node_id'),
        'window_name': agent.get('window_name'),
        'pane_order': agent.get('pane_order'),
        'desired_state': desired_state,
        'observed_state': observed_state,
        'lifecycle_state': lifecycle_state or None,
        'ask_target': (record or {}).get('ask_target') or agent.get('id'),
    }


def _drift_for(desired_agents: Iterable[Mapping[str, object]], observed_agents: Iterable[Mapping[str, object]]) -> dict[str, object]:
    mismatched = []
    for observed in observed_agents:
        desired = str(observed.get('desired_state') or '')
        state = str(observed.get('observed_state') or '')
        ok = False
        if desired == 'absent':
            ok = state in {'released', 'parked', 'hidden'}
        elif desired == 'present':
            ok = state == 'present'
        elif desired in {'hidden', 'parked'}:
            ok = state == desired
        if not ok:
            mismatched.append({'agent': observed.get('id'), 'desired_state': desired, 'observed_state': state})
    return {'mismatched_agents': mismatched, 'agent_count': len(tuple(desired_agents))}


def _released_agent_ids(
    observed_agents: Iterable[Mapping[str, object]],
    actions: Iterable[Mapping[str, object]],
) -> list[str]:
    released = {
        str(agent.get('id') or '')
        for agent in observed_agents
        if str(agent.get('observed_state') or '') == 'released'
    }
    released.update(
        str(action.get('agent') or '')
        for action in actions
        if str(action.get('action') or '') == 'release'
        and str(action.get('status') or '') == 'removed'
    )
    return sorted(name for name in released if name)


def _status_summary(payload: Mapping[str, object] | None) -> dict[str, object] | None:
    if payload is None:
        return None
    return {
        'revision': payload.get('revision') or payload.get('desired_revision'),
        'status': payload.get('topology_status') or payload.get('last_reconcile_status'),
        'agent_count': len(tuple(payload.get('agents') or _desired_agents(payload, loop_id=str(payload.get('loop_id') or '')))),
        'updated_at': payload.get('committed_at') or payload.get('reconciled_at'),
    }


def _release_policy_record(command) -> dict[str, object]:
    policy = str(getattr(command, 'policy', None) or 'auto').strip().lower()
    if policy not in {'auto', 'idle-only'}:
        raise ValueError(f'unsupported topology release policy: {policy}')
    return {'policy': policy, 'idle_only': True}


def _topology_release_policy(payload: Mapping[str, object]) -> str:
    return _normalize_release_policy_value(payload.get('release_policy'), default='auto')


def _agent_release_policy(agent: Mapping[str, object] | None, *, default_policy: str) -> str:
    if agent is None:
        return default_policy
    return _normalize_release_policy_value(agent.get('release_policy'), default=default_policy)


def _lifetime_for_agent(agent: Mapping[str, object]) -> str:
    lifetime = _optional_text(agent.get('lifetime'))
    if lifetime is not None:
        return lifetime
    return _lifetime_for_lifecycle(_optional_text(agent.get('lifecycle')))


def _lifetime_for_lifecycle(lifecycle: str | None) -> str:
    normalized = str(lifecycle or '').strip().lower().replace('-', '_')
    if normalized in {'resident', 'long_lived', 'loop', 'current_loop', 'semi_resident'}:
        return 'current_loop'
    if normalized in {'ephemeral', 'round', 'current_round'}:
        return 'current_round'
    return 'current_loop'


def _legacy_dispatch_compatibility(payload: Mapping[str, object]) -> bool:
    return (
        str(payload.get('dispatch_compatibility') or '').strip().lower() == 'legacy'
        or str(payload.get('compatibility_mode') or '').strip().lower() == 'legacy_dispatch'
        or payload.get('legacy_dispatch_compatibility') is True
    )


def _normalize_release_policy_value(value: object, *, default: str) -> str:
    raw = value
    if isinstance(raw, Mapping):
        raw = raw.get('policy')
    policy = str(raw or default).strip().lower()
    if policy == 'idle-only':
        return 'auto'
    if policy not in RELEASE_POLICIES:
        raise ValueError(f'unsupported topology release policy: {policy}')
    return policy


def _apply_status(payload: Mapping[str, object]) -> str | None:
    apply = payload.get('apply')
    if isinstance(apply, Mapping):
        return str(apply.get('apply_status') or '') or None
    return None


def _proposal_id(value: object, payload: Mapping[str, object]) -> str:
    text = _optional_text(value)
    if text is not None:
        return _normalize_proposal_id(text)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:12]
    return f'proposal-{digest}'


def _normalize_loop_id(value: object) -> str:
    return _normalize_named_value(value, field_name='loop_id')


def _normalize_proposal_id(value: object) -> str:
    return _normalize_named_value(value, field_name='proposal_id')


def _normalize_named_value(value: object, *, field_name: str) -> str:
    try:
        return normalize_agent_name(str(value or ''))
    except AgentValidationError as exc:
        raise ValueError(f'{field_name} is invalid: {exc}') from exc


def _normalize_optional_name(value: object, *, field_name: str) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    return _normalize_named_value(text, field_name=field_name)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _load_json_object(path: Path) -> dict[str, object]:
    payload = _load_json_optional(path)
    if payload is None:
        raise ValueError(f'{path}: file not found')
    if not isinstance(payload, dict):
        raise ValueError(f'{path}: expected JSON object')
    return payload


def _load_json_optional(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        raise ValueError(f'{path}: expected JSON object')
    return dict(payload)


def _loop_dir(context, loop_id: str) -> Path:
    return Path(context.paths.runtime_state_root) / 'runtime' / 'loops' / loop_id


def _proposal_dir(context, loop_id: str) -> Path:
    return _loop_dir(context, loop_id) / 'topology_proposals'


def _proposal_path(context, loop_id: str, proposal_id: str) -> Path:
    return _proposal_dir(context, loop_id) / f'{proposal_id}.json'


def _load_desired_optional(context, loop_id: str) -> dict[str, object] | None:
    return _load_json_optional(_desired_read_path(context, loop_id))


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


def _events_path(context, loop_id: str) -> Path:
    return _loop_dir(context, loop_id) / 'agent_mount_topology.events.jsonl'


def _append_event(context, loop_id: str, payload: Mapping[str, object]) -> None:
    event = {
        'schema_version': 1,
        'record_type': 'ccb_loop_agent_mount_topology_event',
        'created_at': _utc_now(),
        'project_id': context.project.project_id,
        **dict(payload),
    }
    path = _events_path(context, loop_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
        handle.write('\n')


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


__all__ = ['loop_topology']
