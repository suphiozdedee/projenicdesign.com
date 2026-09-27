from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from agents.config_loader import load_project_config
from agents.models import AgentValidationError, LoopCapacityConfig, LoopRoleProfileSpec, normalize_agent_name
from agents.store import AgentRuntimeStore
from storage.atomic import atomic_write_json

from .daemon import ping_local_state
from .reload import reload_config
from .reload_apply_diagnostics import reload_apply_summary
from .loop_workgroup_topology import (
    compile_project_workgroup_mount_demand,
    compile_workgroup_mount_demand,
)


def loop_capacity(context, command) -> dict[str, object]:
    action = str(command.action or '').strip().lower()
    if action == 'ensure':
        return _ensure_capacity(context, command)
    if action == 'status':
        return _status_capacity(context, command)
    if action == 'release':
        return _release_capacity(context, command)
    raise ValueError(f'unsupported loop capacity action: {action}')


def _ensure_capacity(context, command) -> dict[str, object]:
    loop_id = _normalize_loop_id(command.loop_id)
    loaded = load_project_config(context.project.project_root, include_loop_overlays=False)
    if int(loaded.config.version) == 3:
        raise RuntimeError(
            'Config V3 workgroup capacity must use compile_project_workgroup_mount_demand; '
            'loop capacity ensure --profile is V2 physical-capacity compatibility only'
        )
    capacity = loaded.config.loop_capacity or LoopCapacityConfig()
    if not capacity.enabled:
        raise RuntimeError('loop capacity is disabled; configure [loop.capacity].enabled = true')
    requests = _normalize_requests(command.profile_counts)
    if not requests:
        raise ValueError('loop capacity ensure requires at least one --profile <name>=<count>')
    _validate_requests(capacity, requests)

    previous = _load_state(_state_path(context, loop_id))
    created_at = str(previous.get('created_at') or _utc_now()) if previous else _utc_now()
    updated_at = _utc_now()
    agents = _planned_agents(capacity, loop_id=loop_id, requests=requests)
    _validate_agent_name_conflicts(loaded.config, agents)
    payload = {
        'schema_version': 1,
        'record_type': 'ccb_loop_capacity_state',
        'loop_capacity_status': 'ensured',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'loop_id': loop_id,
        'created_at': created_at,
        'updated_at': updated_at,
        'released_at': None,
        'config_source_kind': loaded.source_kind,
        'config_source': str(loaded.source_path) if loaded.source_path is not None else None,
        'capacity': _capacity_record(capacity),
        'requests': [{'profile': profile, 'count': count} for profile, count in requests],
        'agents': agents,
        'agent_count': len(agents),
        'state_path': str(_state_path(context, loop_id)),
        'events_path': str(_events_path(context, loop_id)),
    }
    _write_state(context, loop_id, payload)
    _append_event(context, loop_id, {'event': 'ensure', 'loop_id': loop_id, 'agent_count': len(agents)})
    try:
        payload['apply'] = _apply_reload_if_mounted(context, action='ensure')
    except Exception as exc:
        _restore_state(context, loop_id, previous)
        _append_event(
            context,
            loop_id,
            {'event': 'ensure-apply-failed', 'loop_id': loop_id, 'agent_count': len(agents), 'error': str(exc)},
        )
        raise
    _write_state(context, loop_id, payload)
    return dict(payload)


def _status_capacity(context, command) -> dict[str, object]:
    loop_id = _normalize_loop_id(command.loop_id)
    state_path = _state_path(context, loop_id)
    state = _load_state(state_path)
    if not state:
        return {
            'loop_capacity_status': 'missing',
            'loop_id': loop_id,
            'project_id': context.project.project_id,
            'project_root': str(context.project.project_root),
            'state_path': str(state_path),
            'events_path': str(_events_path(context, loop_id)),
            'agents': [],
            'agent_count': 0,
        }
    payload = dict(state)
    payload['state_path'] = str(state_path)
    payload['events_path'] = str(_events_path(context, loop_id))
    payload['agent_count'] = len(tuple(payload.get('agents') or ()))
    return payload


def _release_capacity(context, command) -> dict[str, object]:
    policy = _normalize_release_policy(command)
    loop_id = _normalize_loop_id(command.loop_id)
    state_path = _state_path(context, loop_id)
    state = _load_state(state_path)
    if not state:
        payload = {
            'loop_capacity_status': 'missing',
            'loop_id': loop_id,
            'project_id': context.project.project_id,
            'project_root': str(context.project.project_root),
            'state_path': str(state_path),
            'events_path': str(_events_path(context, loop_id)),
            'agents': [],
            'agent_count': 0,
            'released_count': 0,
            'release_policy': policy,
            'idle_only': True,
        }
        _append_event(
            context,
            loop_id,
            {'event': 'release-missing', 'loop_id': loop_id, 'agent_count': 0, 'release_policy': policy},
        )
        return payload

    previous = json.loads(json.dumps(state))
    released_at = _utc_now()
    release_gates = _release_gates(context, tuple(state.get('agents') or ()))
    agents = []
    retained = []
    for raw_agent in tuple(state.get('agents') or ()):
        if not isinstance(raw_agent, dict):
            continue
        agent = dict(raw_agent)
        if str(agent.get('state') or '') == 'released':
            agents.append(agent)
            continue
        gate = release_gates.get(str(agent.get('name') or '')) or {}
        if bool(gate.get('retained')):
            agent['state'] = 'retained'
            agent['retained_at'] = released_at
            agent['retain_reason'] = str(gate.get('reason') or 'busy')
            agent['runtime_state'] = gate.get('runtime_state')
            agent['queue_depth'] = gate.get('queue_depth')
            retained.append(
                {
                    'name': agent.get('name'),
                    'reason': agent['retain_reason'],
                    'runtime_state': agent.get('runtime_state'),
                    'queue_depth': agent.get('queue_depth'),
                }
            )
        else:
            agent['state'] = 'released'
            agent['released_at'] = released_at
            for key in ('retained_at', 'retain_reason', 'runtime_state', 'queue_depth'):
                agent.pop(key, None)
        agents.append(agent)
    retained_count = len(retained)
    state['loop_capacity_status'] = 'ensured' if retained_count else 'released'
    state['updated_at'] = released_at
    state['released_at'] = None if retained_count else released_at
    state['agents'] = agents
    state['agent_count'] = len(agents)
    state['released_count'] = sum(1 for agent in agents if str(agent.get('state') or '') == 'released')
    state['retained_count'] = retained_count
    state['retained'] = retained
    state['release_policy'] = policy
    state['idle_only'] = True
    state['state_path'] = str(state_path)
    state['events_path'] = str(_events_path(context, loop_id))
    _write_state(context, loop_id, state)
    _append_event(
        context,
        loop_id,
        {
            'event': 'release',
            'loop_id': loop_id,
            'agent_count': len(agents),
            'released_count': state['released_count'],
            'retained_count': retained_count,
            'release_policy': policy,
        },
    )
    try:
        state['apply'] = _apply_reload_if_mounted(context, action='release')
    except Exception as exc:
        _restore_state(context, loop_id, previous)
        _append_event(
            context,
            loop_id,
            {'event': 'release-apply-failed', 'loop_id': loop_id, 'agent_count': len(agents), 'error': str(exc)},
        )
        raise
    _write_state(context, loop_id, state)
    return dict(state)


def _normalize_release_policy(command) -> str:
    policy = str(getattr(command, 'policy', None) or 'auto').strip().lower()
    if policy not in {'auto', 'idle-only'}:
        raise ValueError(f'unsupported loop capacity release policy: {policy}')
    if bool(getattr(command, 'idle_only', False)):
        return 'idle-only'
    return policy


def _release_gates(context, raw_agents: tuple[object, ...]) -> dict[str, dict[str, object]]:
    local = ping_local_state(context)
    if str(getattr(local, 'mount_state', '') or '') != 'mounted' or not bool(getattr(local, 'socket_connectable', False)):
        return {}
    store = AgentRuntimeStore(context.paths)
    gates: dict[str, dict[str, object]] = {}
    for raw_agent in raw_agents:
        if not isinstance(raw_agent, dict):
            continue
        name = str(raw_agent.get('name') or '').strip()
        if not name:
            continue
        runtime = store.load_best_effort(name)
        runtime_state = _runtime_state_value(runtime)
        queue_depth = _safe_int(getattr(runtime, 'queue_depth', 0) if runtime is not None else 0)
        reason = ''
        if runtime_state in {'busy', 'starting', 'stopping'}:
            reason = f'runtime_state={runtime_state}'
        elif queue_depth > 0:
            reason = f'queue_depth={queue_depth}'
        gates[name] = {
            'retained': bool(reason),
            'reason': reason,
            'runtime_state': runtime_state,
            'queue_depth': queue_depth,
        }
    return gates


def _runtime_state_value(runtime) -> str:
    if runtime is None:
        return 'missing'
    value = getattr(runtime, 'state', None)
    return str(getattr(value, 'value', value) or 'unknown')


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_loop_id(value: object) -> str:
    try:
        return normalize_agent_name(str(value or ''))
    except AgentValidationError as exc:
        raise ValueError(f'loop_id is invalid: {exc}') from exc


def _normalize_requests(raw_requests: object) -> tuple[tuple[str, int], ...]:
    normalized: list[tuple[str, int]] = []
    seen: set[str] = set()
    for raw_profile, raw_count in tuple(raw_requests or ()):
        try:
            profile = normalize_agent_name(str(raw_profile or ''))
        except AgentValidationError as exc:
            raise ValueError(f'loop capacity profile is invalid: {exc}') from exc
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'loop capacity profile {profile} count must be a positive integer') from exc
        if count <= 0:
            raise ValueError(f'loop capacity profile {profile} count must be a positive integer')
        if profile in seen:
            raise ValueError(f'duplicate loop capacity profile request: {profile}')
        seen.add(profile)
        normalized.append((profile, count))
    return tuple(normalized)


def _validate_requests(capacity: LoopCapacityConfig, requests: tuple[tuple[str, int], ...]) -> None:
    total = sum(count for _profile, count in requests)
    if total > capacity.max_nodes:
        raise ValueError(f'loop capacity request exceeds max_nodes={capacity.max_nodes}: requested {total}')
    for profile_name, count in requests:
        profile = capacity.role_profiles.get(profile_name)
        if profile is None:
            known = ', '.join(sorted(capacity.role_profiles)) or '<none>'
            raise ValueError(f'unknown loop role profile {profile_name!r}; configured profiles: {known}')
        if count > profile.max_instances:
            raise ValueError(
                f'loop role profile {profile_name} request exceeds max_instances={profile.max_instances}: requested {count}'
            )


def _planned_agents(
    capacity: LoopCapacityConfig,
    *,
    loop_id: str,
    requests: tuple[tuple[str, int], ...],
) -> list[dict[str, object]]:
    agents: list[dict[str, object]] = []
    names: set[str] = set()
    for profile_name, count in requests:
        profile = capacity.role_profiles[profile_name]
        for index in range(1, count + 1):
            name = _render_agent_name(capacity.name_template, loop_id=loop_id, profile=profile_name, index=index)
            if name in names:
                raise ValueError(f'loop capacity name_template produced duplicate agent name: {name}')
            names.add(name)
            agents.append(
                _agent_record(
                    name,
                    profile_name=profile_name,
                    profile=profile,
                    capacity=capacity,
                    loop_id=loop_id,
                    node_index=index,
                    created_sequence=len(agents) + 1,
                )
            )
    return agents


def _validate_agent_name_conflicts(config, agents: list[dict[str, object]]) -> None:
    configured = set(getattr(config, 'agents', {}) or {})
    conflicts = sorted(str(agent.get('name') or '') for agent in agents if str(agent.get('name') or '') in configured)
    if conflicts:
        raise ValueError(f'loop capacity generated agent conflicts with configured agent: {conflicts[0]}')


def _render_agent_name(template: str, *, loop_id: str, profile: str, index: int) -> str:
    try:
        rendered = template.format(loop_id=loop_id, profile=profile, index=index)
    except Exception as exc:
        raise ValueError(f'loop.capacity.name_template is invalid: {exc}') from exc
    try:
        return normalize_agent_name(rendered)
    except AgentValidationError as exc:
        raise ValueError(f'loop.capacity.name_template rendered invalid agent name {rendered!r}: {exc}') from exc


def _agent_record(
    name: str,
    *,
    profile_name: str,
    profile: LoopRoleProfileSpec,
    capacity: LoopCapacityConfig,
    loop_id: str,
    node_index: int,
    created_sequence: int,
) -> dict[str, object]:
    node_id = f'node{node_index}'
    window_name = _execution_node_window_name(loop_id=loop_id, node_id=node_id)
    return {
        'name': name,
        'profile': profile_name,
        'role': profile.role,
        'provider': profile.provider,
        'model': profile.model,
        'thinking': profile.thinking,
        'workspace_mode': profile.workspace_mode.value,
        'workspace_group': profile.workspace_group,
        'startup_args': list(profile.startup_args),
        'provider_profile': profile.provider_profile.to_record(),
        'reuse': profile.reuse,
        'lifetime': capacity.default_lifetime,
        'loop_id': loop_id,
        'node_id': node_id,
        'node_index': node_index,
        'created_sequence': created_sequence,
        'window_name': window_name,
        'placement': {
            'mode': 'execution_node',
            'loop_id': loop_id,
            'node_id': node_id,
            'window_name': window_name,
            'layout_policy': 'append-or-create-window',
        },
        'state': 'planned',
    }


def _execution_node_window_name(*, loop_id: str, node_id: str) -> str:
    return f'node-{_window_slug(loop_id)}-{_window_slug(node_id)}'


def _window_slug(value: str) -> str:
    text = str(value or '').strip().replace('_', '-')
    cleaned = ''.join(ch if ch.isalnum() or ch == '-' else '-' for ch in text)
    cleaned = '-'.join(part for part in cleaned.split('-') if part)
    if not cleaned:
        cleaned = 'window'
    if not cleaned[0].isalpha():
        cleaned = f'w-{cleaned}'
    return cleaned


def _capacity_record(capacity: LoopCapacityConfig) -> dict[str, object]:
    return {
        'enabled': capacity.enabled,
        'max_nodes': capacity.max_nodes,
        'default_lifetime': capacity.default_lifetime,
        'name_template': capacity.name_template,
        'reuse': capacity.reuse,
        'profiles': sorted(capacity.role_profiles),
    }


def _state_dir(context, loop_id: str) -> Path:
    return Path(context.paths.runtime_state_root) / 'runtime' / 'loops' / loop_id


def _state_path(context, loop_id: str) -> Path:
    return _state_dir(context, loop_id) / 'capacity.json'


def _events_path(context, loop_id: str) -> Path:
    return _state_dir(context, loop_id) / 'events.jsonl'


def _write_state(context, loop_id: str, payload: dict[str, object]) -> None:
    _ensure_runtime_root(context)
    atomic_write_json(_state_path(context, loop_id), payload)


def _restore_state(context, loop_id: str, previous: dict[str, object] | None) -> None:
    if previous is None:
        try:
            _state_path(context, loop_id).unlink()
        except FileNotFoundError:
            pass
        return
    _write_state(context, loop_id, previous)


def _load_state(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        raise ValueError(f'{path}: expected JSON object')
    return dict(payload)


def _append_event(context, loop_id: str, payload: dict[str, object]) -> None:
    _ensure_runtime_root(context)
    event = {
        'schema_version': 1,
        'record_type': 'ccb_loop_capacity_event',
        'created_at': _utc_now(),
        'project_id': context.project.project_id,
        **payload,
    }
    path = _events_path(context, loop_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
        handle.write('\n')


def _ensure_runtime_root(context) -> None:
    ensure_runtime_state_root = getattr(context.paths, 'ensure_runtime_state_root', None)
    if callable(ensure_runtime_state_root):
        ensure_runtime_state_root()


def _apply_reload_if_mounted(context, *, action: str) -> dict[str, object]:
    local = ping_local_state(context)
    if str(getattr(local, 'mount_state', '') or '') != 'mounted' or not bool(getattr(local, 'socket_connectable', False)):
        return {
            'apply_status': 'deferred_until_start',
            'action': action,
            'mount_state': getattr(local, 'mount_state', None),
            'reason': getattr(local, 'reason', None),
        }
    payload = reload_config(context, SimpleNamespace(dry_run=False))
    status = str(payload.get('status') or '')
    if status not in {'ok', 'noop', 'published'}:
        raise RuntimeError(f'loop capacity {action} reload failed: {status or "unknown"}')
    return reload_apply_summary(payload, action=action)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


__all__ = [
    'compile_project_workgroup_mount_demand',
    'compile_workgroup_mount_demand',
    'loop_capacity',
]
