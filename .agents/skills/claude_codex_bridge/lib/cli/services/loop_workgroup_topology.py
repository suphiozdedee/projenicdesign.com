from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
import hashlib
from pathlib import Path
import re

from agents.models import AgentValidationError, normalize_agent_name

from .loop_effective_capacity import (
    compile_project_effective_capacity_snapshot,
    effective_capacity_digest,
    normalize_effective_capacity_snapshot,
)


WORKGROUP_MOUNT_DEMAND_SCHEMA = 'ccb.loop.workgroup_mount_demand.v1'
MOUNT_TOPOLOGY_SCHEMA = 'ccb.loop.agent_mount_topology.v1'
USER_INTERACTION_WINDOW = 'ccb-user'
PLANNING_WINDOW = 'ccb-plan'
EXECUTION_WINDOW_PREFIX = 'ccb-exec'
MAX_WORKGROUPS = 4
MAX_EXECUTION_WINDOW_PANES = 6

_BUNDLE_SCHEMA = 'ccb.loop.orchestration_bundle.v1'
_DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
_CONTROL_ORDER = ('task_detailer', 'orchestrator', 'ccb_round_reviewer')
_CONTROL_WINDOWS = {
    'task_detailer': USER_INTERACTION_WINDOW,
    'orchestrator': PLANNING_WINDOW,
    'ccb_round_reviewer': PLANNING_WINDOW,
}


def compile_workgroup_mount_demand(
    bundle: Mapping[str, object],
    *,
    loop_id: str,
    capacity_snapshot: object,
    active_node_ids: Iterable[str] | None = None,
    control_profiles: Iterable[str] = (),
    node_attempts: Mapping[str, int] | None = None,
    control_attempts: Mapping[str, int] | None = None,
) -> dict[str, object]:
    """Compile validated bundle nodes into mount-only physical demand."""
    loop_name = _name(loop_id, field='loop_id')
    capacity = normalize_effective_capacity_snapshot(capacity_snapshot)
    config_version = int(capacity['config_version'])
    if config_version not in {2, 3}:
        raise ValueError(f'workgroup mount demand does not support config version {config_version}')
    capacity_digest = effective_capacity_digest(capacity)
    nodes = _bundle_nodes(bundle, capacity=capacity, capacity_digest=capacity_digest)
    active_nodes = _active_nodes(nodes, active_node_ids=active_node_ids)
    controls = _control_profiles(control_profiles, capacity=capacity)
    if not active_nodes and not controls:
        raise ValueError('workgroup mount demand requires at least one active node or control profile')
    limits = capacity['limits']
    max_parallel = int(limits['max_parallel_workgroups'])
    if len(active_nodes) > max_parallel:
        raise ValueError(
            f'workgroup mount demand exceeds max_parallel_workgroups={max_parallel}: '
            f'requested {len(active_nodes)}; the controller cannot silently serialize the demand'
        )

    attempts = _attempts(node_attempts, allowed={str(node['node_id']) for node in nodes}, field='node_attempts')
    control_attempt_map = _attempts(
        control_attempts,
        allowed=set(_CONTROL_ORDER),
        field='control_attempts',
    )
    execution_max_panes = _execution_window_max_panes(capacity)
    pairs_per_window = execution_max_panes // 2
    if pairs_per_window < 1:
        raise ValueError('execution window capacity must fit one coder/code_reviewer pair')
    name_template = str(capacity['policies']['naming']['template'])
    lifetime = str(capacity['policies']['release']['default_lifetime'])
    release_policy = str(capacity['policies']['release']['policy'])

    control_agents: list[dict[str, object]] = []
    control_bindings: list[dict[str, object]] = []
    control_window_counts: Counter[str] = Counter()
    names: set[str] = set()
    for profile in controls:
        attempt = control_attempt_map.get(profile, 1)
        window_name = _CONTROL_WINDOWS[profile]
        pane_order = control_window_counts[window_name] + 1
        control_window_counts[window_name] += 1
        agent_name = _render_agent_name(
            name_template,
            loop_id=loop_name,
            node_id='control',
            profile=profile,
            index=1,
            attempt=attempt,
        )
        _add_unique_name(names, agent_name)
        profile_record = capacity['dynamic_profiles'][profile]
        control_agents.append(
            _agent_record(
                name=agent_name,
                profile=profile,
                profile_record=profile_record,
                loop_id=loop_name,
                node_id='control',
                attempt=attempt,
                lifetime=lifetime,
                release_policy=release_policy,
                window_name=window_name,
                pane_order=pane_order,
                workspace_group=None,
            )
        )
        control_bindings.append(
            {
                'profile': profile,
                'agent': agent_name,
                'attempt': attempt,
                'window_name': window_name,
                'pane_order': pane_order,
            }
        )

    bindings: list[dict[str, object]] = []
    workgroup_nodes: list[dict[str, object]] = []
    execution_windows: list[str] = []
    for pair_index, node in enumerate(active_nodes):
        node_id = str(node['node_id'])
        workgroup_id = str(node['workgroup_id'])
        attempt = attempts.get(node_id, 1)
        page = pair_index // pairs_per_window
        window_name = _execution_window_name(page)
        if window_name not in execution_windows:
            execution_windows.append(window_name)
        pane_base = (pair_index % pairs_per_window) * 2
        workspace_group = _generated_name(f'loop-{loop_name}-{node_id}')
        worker_profile = _resolve_profile('coder', capacity=capacity)
        reviewer_profile = _resolve_profile('code_reviewer', capacity=capacity)
        worker_name = _render_agent_name(
            name_template,
            loop_id=loop_name,
            node_id=node_id,
            profile=worker_profile,
            index=pair_index + 1,
            attempt=attempt,
        )
        reviewer_name = _render_agent_name(
            name_template,
            loop_id=loop_name,
            node_id=node_id,
            profile=reviewer_profile,
            index=pair_index + 1,
            attempt=attempt,
        )
        _add_unique_name(names, worker_name)
        _add_unique_name(names, reviewer_name)
        worker = _agent_record(
            name=worker_name,
            profile=worker_profile,
            profile_record=capacity['dynamic_profiles'][worker_profile],
            loop_id=loop_name,
            node_id=node_id,
            attempt=attempt,
            lifetime=lifetime,
            release_policy=release_policy,
            window_name=window_name,
            pane_order=pane_base,
            workspace_group=workspace_group,
        )
        reviewer = _agent_record(
            name=reviewer_name,
            profile=reviewer_profile,
            profile_record=capacity['dynamic_profiles'][reviewer_profile],
            loop_id=loop_name,
            node_id=node_id,
            attempt=attempt,
            lifetime=lifetime,
            release_policy=release_policy,
            window_name=window_name,
            pane_order=pane_base + 1,
            workspace_group=workspace_group,
        )
        workgroup_nodes.append({'id': node_id, 'agents': [worker, reviewer]})
        bindings.append(
            {
                'node_id': node_id,
                'workgroup_id': workgroup_id,
                'attempt': attempt,
                'workspace_group': workspace_group,
                'worker_profile': worker_profile,
                'reviewer_profile': reviewer_profile,
                'worker_agent': worker_name,
                'reviewer_agent': reviewer_name,
                'window_name': window_name,
                'pane_orders': {'coder': pane_base, 'code_reviewer': pane_base + 1},
            }
        )

    all_agents = control_agents + [agent for node in workgroup_nodes for agent in node['agents']]
    profile_counts = Counter(str(agent['profile']) for agent in all_agents)
    _validate_physical_capacity(profile_counts, capacity=capacity)
    physical_count = len(all_agents)
    max_dynamic = int(limits['max_active_dynamic_agents'])
    if physical_count > max_dynamic:
        raise ValueError(
            f'workgroup mount demand exceeds max_active_dynamic_agents={max_dynamic}: '
            f'requested {physical_count} including {len(control_agents)} control roles'
        )

    windows = _windows(
        control_agents=control_agents,
        execution_windows=execution_windows,
        execution_max_panes=execution_max_panes,
    )
    topology_nodes = []
    if control_agents:
        topology_nodes.append({'id': 'control', 'agents': control_agents})
    topology_nodes.extend(workgroup_nodes)
    mount_topology = {
        'schema': MOUNT_TOPOLOGY_SCHEMA,
        'record_type': 'ccb_loop_agent_mount_topology_plan',
        'loop_id': loop_name,
        'owner': {'kind': 'loop', 'loop_id': loop_name},
        'capacity_digest': capacity_digest,
        'windows': windows,
        'nodes': topology_nodes,
        'release_policy': {'policy': release_policy, 'idle_only': True},
    }
    return {
        'schema': WORKGROUP_MOUNT_DEMAND_SCHEMA,
        'record_type': 'ccb_loop_workgroup_mount_demand',
        'loop_id': loop_name,
        'config_version': config_version,
        'capacity_digest': capacity_digest,
        'workgroup_count': len(nodes),
        'active_workgroup_count': len(active_nodes),
        'max_workgroups': int(limits['max_workgroups']),
        'max_parallel_workgroups': max_parallel,
        'max_active_dynamic_agents': max_dynamic,
        'control_agent_count': len(control_agents),
        'physical_agent_count': physical_count,
        'profile_counts': dict(sorted(profile_counts.items())),
        'bindings': bindings,
        'control_bindings': control_bindings,
        'mount_topology': mount_topology,
    }


def compile_project_workgroup_mount_demand(
    project_root: Path,
    bundle: Mapping[str, object],
    *,
    loop_id: str,
    active_node_ids: Iterable[str] | None = None,
    control_profiles: Iterable[str] = (),
    node_attempts: Mapping[str, int] | None = None,
    control_attempts: Mapping[str, int] | None = None,
) -> dict[str, object]:
    return compile_workgroup_mount_demand(
        bundle,
        loop_id=loop_id,
        capacity_snapshot=compile_project_effective_capacity_snapshot(project_root),
        active_node_ids=active_node_ids,
        control_profiles=control_profiles,
        node_attempts=node_attempts,
        control_attempts=control_attempts,
    )


def _bundle_nodes(
    bundle: Mapping[str, object],
    *,
    capacity: dict[str, object],
    capacity_digest: str,
) -> list[dict[str, object]]:
    if not isinstance(bundle, Mapping):
        raise ValueError('orchestration bundle must be an object')
    if str(bundle.get('schema') or '') != _BUNDLE_SCHEMA:
        raise ValueError(f'orchestration bundle schema must be {_BUNDLE_SCHEMA}')
    supplied_digest = str(bundle.get('capacity_digest') or '')
    if not _DIGEST_RE.fullmatch(supplied_digest):
        raise ValueError('orchestration bundle capacity_digest must use sha256:<64 lowercase hex>')
    if supplied_digest != capacity_digest:
        raise ValueError(
            f'orchestration bundle capacity_digest is stale: expected {capacity_digest}, got {supplied_digest}'
        )
    raw_nodes = bundle.get('nodes')
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError('orchestration bundle must contain one to four nodes')
    max_workgroups = int(capacity['limits']['max_workgroups'])
    if len(raw_nodes) > max_workgroups:
        raise ValueError(
            f'orchestration bundle exceeds max_workgroups={max_workgroups}: requested {len(raw_nodes)}'
        )
    if len(raw_nodes) > MAX_WORKGROUPS:
        raise ValueError(f'orchestration bundle cannot exceed {MAX_WORKGROUPS} workgroups')
    selection = bundle.get('selection')
    selected_count = selection.get('workgroup_count') if isinstance(selection, Mapping) else None
    if selected_count != len(raw_nodes):
        raise ValueError('orchestration bundle selection.workgroup_count must equal node count')
    nodes: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, raw_node in enumerate(raw_nodes, start=1):
        if not isinstance(raw_node, Mapping):
            raise ValueError(f'orchestration bundle node #{index} must be an object')
        node = dict(raw_node)
        node_id = _name(node.get('node_id'), field=f'nodes[{index}].node_id')
        if node_id in seen:
            raise ValueError(f'duplicate orchestration bundle node_id: {node_id}')
        seen.add(node_id)
        if str(node.get('worker_profile') or '') != 'coder':
            raise ValueError(f'orchestration bundle node {node_id} worker_profile must be coder')
        if str(node.get('reviewer_profile') or '') != 'code_reviewer':
            raise ValueError(f'orchestration bundle node {node_id} reviewer_profile must be code_reviewer')
        node['node_id'] = node_id
        node['workgroup_id'] = _name(node.get('workgroup_id'), field=f'{node_id}.workgroup_id')
        nodes.append(node)
    nodes.sort(key=lambda item: (int(item.get('integration_order') or 0), str(item['node_id'])))
    return nodes


def _active_nodes(
    nodes: list[dict[str, object]],
    *,
    active_node_ids: Iterable[str] | None,
) -> list[dict[str, object]]:
    ordered_ids = [str(node['node_id']) for node in nodes]
    requested = ordered_ids if active_node_ids is None else [str(item or '').strip() for item in active_node_ids]
    if len(requested) != len(set(requested)):
        raise ValueError('workgroup mount demand active_node_ids must be unique')
    unknown = sorted(set(requested) - set(ordered_ids))
    if unknown:
        raise ValueError(f'workgroup mount demand references unknown active node: {unknown[0]}')
    requested_set = set(requested)
    return [node for node in nodes if str(node['node_id']) in requested_set]


def _control_profiles(
    raw_profiles: Iterable[str],
    *,
    capacity: dict[str, object],
) -> tuple[str, ...]:
    requested = [str(item or '').strip() for item in raw_profiles]
    if len(requested) != len(set(requested)):
        raise ValueError('control_profiles must not contain duplicates')
    unknown = sorted(set(requested) - set(_CONTROL_ORDER))
    if unknown:
        raise ValueError(f'unsupported activation control profile: {unknown[0]}')
    dynamic = capacity['dynamic_profiles']
    missing = sorted(set(requested) - set(dynamic))
    if missing:
        raise ValueError(f'effective capacity missing activation control profile: {missing[0]}')
    return tuple(profile for profile in _CONTROL_ORDER if profile in requested)


def _attempts(
    value: Mapping[str, int] | None,
    *,
    allowed: set[str],
    field: str,
) -> dict[str, int]:
    attempts: dict[str, int] = {}
    for raw_name, raw_attempt in dict(value or {}).items():
        name = str(raw_name or '').strip()
        if name not in allowed:
            raise ValueError(f'{field} contains unknown key: {name}')
        if isinstance(raw_attempt, bool) or not isinstance(raw_attempt, int) or raw_attempt <= 0:
            raise ValueError(f'{field}.{name} must be a positive integer')
        attempts[name] = raw_attempt
    return attempts


def _validate_physical_capacity(
    profile_counts: Mapping[str, int],
    *,
    capacity: dict[str, object],
) -> None:
    dynamic = capacity['dynamic_profiles']
    for profile, count in sorted(profile_counts.items()):
        record = dynamic.get(profile)
        if not isinstance(record, Mapping):
            raise ValueError(f'effective capacity missing dynamic profile: {profile}')
        maximum = int(record['max_instances'])
        if count > maximum:
            raise ValueError(
                f'workgroup mount demand profile {profile} exceeds max_instances={maximum}: requested {count}'
            )


def _resolve_profile(logical_profile: str, *, capacity: dict[str, object]) -> str:
    dynamic = capacity['dynamic_profiles']
    if logical_profile in dynamic:
        return logical_profile
    if int(capacity['config_version']) == 2:
        alias = str(capacity['profile_aliases'].get(logical_profile) or '')
        if alias in dynamic:
            return alias
    raise ValueError(f'effective capacity missing dynamic profile: {logical_profile}')


def _agent_record(
    *,
    name: str,
    profile: str,
    profile_record: Mapping[str, object],
    loop_id: str,
    node_id: str,
    attempt: int,
    lifetime: str,
    release_policy: str,
    window_name: str,
    pane_order: int,
    workspace_group: str | None,
) -> dict[str, object]:
    record = {
        'id': name,
        'profile': profile,
        'role': profile_record['role_id'],
        'provider': profile_record['provider'],
        'model': profile_record.get('model'),
        'workspace_mode': profile_record['workspace_mode'],
        'desired_state': 'present',
        'lifecycle': 'immaculate',
        'lifetime': lifetime,
        'release_policy': str(profile_record.get('release_policy') or release_policy),
        'loop_id': loop_id,
        'node_id': node_id,
        'attempt': attempt,
        'window_name': window_name,
        'pane_order': pane_order,
        'placement': {
            'mode': 'mount_only',
            'window_name': window_name,
            'pane_order': pane_order,
        },
    }
    if workspace_group is not None:
        record['workspace_group'] = workspace_group
    return record


def _windows(
    *,
    control_agents: list[dict[str, object]],
    execution_windows: list[str],
    execution_max_panes: int,
) -> list[dict[str, object]]:
    active_control_windows = {str(agent['window_name']) for agent in control_agents}
    windows: list[dict[str, object]] = []
    for name, window_class in (
        (USER_INTERACTION_WINDOW, 'user'),
        (PLANNING_WINDOW, 'planning'),
    ):
        if name in active_control_windows:
            windows.append(
                {
                    'name': name,
                    'class': window_class,
                    'max_panes': MAX_EXECUTION_WINDOW_PANES,
                    'layout_policy': 'append-or-create-window',
                }
            )
    windows.extend(
        {
            'name': name,
            'class': 'execution',
            'max_panes': execution_max_panes,
            'layout_policy': 'append-or-create-window',
        }
        for name in execution_windows
    )
    return windows


def _execution_window_max_panes(capacity: dict[str, object]) -> int:
    if int(capacity['config_version']) == 2:
        return MAX_EXECUTION_WINDOW_PANES
    policy = str(capacity['policies']['execution_windows']['policy'])
    match = re.search(r'(?:^|:)max_panes=([0-9]+)(?:$|:)', policy)
    if match is None:
        raise ValueError('effective capacity execution window policy must declare max_panes')
    value = int(match.group(1))
    if value < 1 or value > MAX_EXECUTION_WINDOW_PANES:
        raise ValueError(
            f'effective capacity execution max_panes must be between 1 and {MAX_EXECUTION_WINDOW_PANES}'
        )
    return value


def _execution_window_name(page: int) -> str:
    return EXECUTION_WINDOW_PREFIX if page == 0 else f'{EXECUTION_WINDOW_PREFIX}-{page + 1}'


def _render_agent_name(
    template: str,
    *,
    loop_id: str,
    node_id: str,
    profile: str,
    index: int,
    attempt: int,
) -> str:
    try:
        rendered = template.format(
            loop_id=loop_id,
            node_id=node_id,
            profile=profile,
            index=index,
            attempt=attempt,
        )
    except Exception as exc:
        raise ValueError(f'workgroup name_template is invalid: {exc}') from exc
    if attempt > 1 and '{attempt}' not in template:
        rendered = f'{rendered}-attempt-{attempt}'
    return _generated_name(rendered)


def _generated_name(rendered: str) -> str:
    try:
        return normalize_agent_name(rendered)
    except AgentValidationError as exc:
        if len(rendered) <= 32 or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]+', rendered):
            raise ValueError(f'generated agent name is invalid: {exc}') from exc
    digest = hashlib.sha256(rendered.encode('utf-8')).hexdigest()[:8]
    prefix = rendered[:23].rstrip('-_')
    return _name(f'{prefix}-{digest}', field='compacted generated agent name')


def _add_unique_name(names: set[str], name: str) -> None:
    if name in names:
        raise ValueError(f'workgroup name_template produced duplicate agent name: {name}')
    names.add(name)


def _name(value: object, *, field: str) -> str:
    try:
        return normalize_agent_name(str(value or ''))
    except AgentValidationError as exc:
        raise ValueError(f'{field} is invalid: {exc}') from exc


__all__ = [
    'MOUNT_TOPOLOGY_SCHEMA',
    'WORKGROUP_MOUNT_DEMAND_SCHEMA',
    'compile_project_workgroup_mount_demand',
    'compile_workgroup_mount_demand',
]
