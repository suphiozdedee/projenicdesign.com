from __future__ import annotations

import hashlib
from itertools import combinations
import json
from pathlib import Path
import re
from typing import Iterable

from agents.models_runtime.names import AgentValidationError, normalize_agent_name

from .loop_execution_scope import (
    declared_allowed_change_paths,
    normalize_scope_paths,
    path_allowed_by_scope,
    safe_relative_path,
    scopes_overlap,
)
from .loop_effective_capacity import (
    effective_capacity_digest,
    normalize_effective_capacity_snapshot,
)


ORCHESTRATION_BUNDLE_CANDIDATE_SCHEMA = 'ccb.loop.orchestration_bundle_candidate.v1'
ORCHESTRATION_BUNDLE_SCHEMA = 'ccb.loop.orchestration_bundle.v1'
MAX_WORKGROUPS = 4
DEFAULT_MAX_NODE_REWORK_ROUNDS = 1
_SEGMENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
_TASK_INPUT_ARTIFACTS = (
    'task_packet',
    'execution_contract',
    'detail_design',
    'detail_summary',
    'detail_packet',
    'orchestration_notes',
)
_CANDIDATE_ROOT_KEYS = frozenset(
    {'schema', 'task_id', 'bundle_revision', 'selection', 'nodes', 'integration', 'policy'}
)
_SELECTION_KEYS = frozenset(
    {'workgroup_count', 'complexity', 'cutability', 'execution_shape', 'rationale'}
)
_SELECTION_COMPLEXITIES = frozenset({'atomic', 'bounded', 'complex', 'very_complex'})
_SELECTION_CUTABILITIES = frozenset({'none', 'limited', 'high'})
_SELECTION_SHAPES = frozenset({'single_unit', 'parallel', 'serial', 'mixed_dag'})
_CANDIDATE_NODE_KEYS = frozenset(
    {
        'node_id',
        'workgroup_id',
        'worker_profile',
        'reviewer_profile',
        'depends_on',
        'parallel_group',
        'work_packet',
        'allowed_paths',
        'acceptance_refs',
        'verification_refs',
        'integration_order',
    }
)
_INTEGRATION_KEYS = frozenset({'verification_refs', 'project_root_verification_refs'})
_POLICY_KEYS = frozenset({'max_node_rework_rounds', 'on_required_node_failure', 'on_structural_failure'})
_NORMALIZED_ROOT_KEYS = frozenset(
    {
        'schema',
        'task_id',
        'task_revision',
        'task_digest',
        'capacity_digest',
        'bundle_revision',
        'selection',
        'nodes',
        'integration',
        'policy',
    }
)
_NORMALIZED_NODE_KEYS = frozenset(
    {
        'node_id',
        'workgroup_id',
        'worker_profile',
        'reviewer_profile',
        'depends_on',
        'parallel_group',
        'work_packet_ref',
        'work_packet_sha256',
        'allowed_paths',
        'acceptance_refs',
        'verification_refs',
        'integration_order',
    }
)


def build_single_node_candidate(
    record: dict[str, object],
    *,
    project_root: Path,
) -> dict[str, object]:
    task_id = _task_id(record)
    artifacts = _artifact_records(record)
    task_packet = _required_artifact_ref(artifacts, 'task_packet')
    execution_contract = _required_artifact_ref(artifacts, 'execution_contract')
    task_text = _artifact_text(project_root, artifacts, ('task_packet', 'execution_contract'))
    allowed_paths = declared_allowed_change_paths(task_text)
    return {
        'schema': ORCHESTRATION_BUNDLE_CANDIDATE_SCHEMA,
        'task_id': task_id,
        'bundle_revision': _compatibility_bundle_revision(record),
        'selection': {
            'workgroup_count': 1,
            'complexity': 'atomic',
            'cutability': 'none',
            'execution_shape': 'single_unit',
            'rationale': 'The task is one tightly coupled implementation unit.',
        },
        'nodes': [
            {
                'node_id': 'node-001',
                'workgroup_id': 'wg-001',
                'worker_profile': 'coder',
                'reviewer_profile': 'code_reviewer',
                'depends_on': [],
                'parallel_group': 'wave-1',
                'work_packet': (
                    f'Execute task {task_id} using {task_packet} and {execution_contract}. '
                    'Stay within the declared allowed paths and return the required verification evidence.'
                ),
                'allowed_paths': allowed_paths,
                'acceptance_refs': [execution_contract],
                'verification_refs': [execution_contract],
                'integration_order': 10,
            }
        ],
        'integration': {
            'verification_refs': [execution_contract],
            'project_root_verification_refs': [execution_contract],
        },
        'policy': {
            'max_node_rework_rounds': DEFAULT_MAX_NODE_REWORK_ROUNDS,
            'on_required_node_failure': 'partial_or_blocked',
            'on_structural_failure': 'replan_required',
        },
    }


def _compatibility_bundle_revision(record: dict[str, object]) -> int:
    artifacts = _artifact_records(record)
    existing = artifacts.get('orchestration_bundle')
    if not isinstance(existing, dict):
        return 1
    current_revision = _positive_int(existing.get('bundle_revision'), field_name='bundle_revision')
    current_task_revision = task_revision(record)
    current_task_digest = task_input_digest(record)
    if (
        existing.get('task_revision') == current_task_revision
        and str(existing.get('task_digest') or '') == current_task_digest
    ):
        return current_revision
    return current_revision + 1


def normalize_bundle_candidate(
    candidate: object,
    *,
    record: dict[str, object],
    project_root: Path,
    capacity_snapshot: object,
) -> tuple[dict[str, object], dict[str, str]]:
    if not isinstance(candidate, dict):
        raise ValueError('orchestration bundle candidate must be a JSON object')
    _reject_unknown_keys(candidate, _CANDIDATE_ROOT_KEYS, field_name='orchestration_bundle')
    if str(candidate.get('schema') or '') != ORCHESTRATION_BUNDLE_CANDIDATE_SCHEMA:
        raise ValueError(f'orchestration bundle candidate schema must be {ORCHESTRATION_BUNDLE_CANDIDATE_SCHEMA}')
    missing_root_fields = sorted(_CANDIDATE_ROOT_KEYS - set(candidate))
    if missing_root_fields:
        raise ValueError(f'orchestration bundle candidate missing fields: {", ".join(missing_root_fields)}')
    task_id = _task_id(record)
    candidate_task_id = str(candidate.get('task_id') or '').strip()
    if candidate_task_id != task_id:
        raise ValueError(f'orchestration bundle task_id mismatch: expected {task_id}, got {candidate_task_id or "missing"}')
    bundle_revision = _positive_int(candidate.get('bundle_revision'), field_name='bundle_revision')
    capacity = normalize_effective_capacity_snapshot(capacity_snapshot)
    capacity_limits = capacity['limits']
    max_workgroups = int(capacity_limits['max_workgroups'])
    nodes_raw = candidate.get('nodes')
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise ValueError('orchestration bundle nodes must be a non-empty list')
    if len(nodes_raw) > max_workgroups:
        raise ValueError(f'orchestration bundle exceeds max_workgroups={max_workgroups}: requested {len(nodes_raw)}')
    selection = _normalize_selection(candidate.get('selection'), node_count=len(nodes_raw))

    artifacts = _artifact_records(record)
    declared_paths = declared_allowed_change_paths(
        _artifact_text(project_root, artifacts, ('task_packet', 'execution_contract'))
    )
    known_refs = {
        str(artifact.get('path') or '').strip()
        for artifact in artifacts.values()
        if isinstance(artifact, dict) and str(artifact.get('path') or '').strip()
    }
    task_root = safe_relative_path(str(record.get('task_root') or ''))
    normalized_nodes: list[dict[str, object]] = []
    work_packets: dict[str, str] = {}
    node_ids: set[str] = set()
    workgroup_ids: set[str] = set()
    integration_orders: set[int] = set()

    for index, raw_node in enumerate(nodes_raw):
        field_name = f'nodes[{index}]'
        if not isinstance(raw_node, dict):
            raise ValueError(f'{field_name} must be an object')
        _reject_unknown_keys(raw_node, _CANDIDATE_NODE_KEYS, field_name=field_name)
        node_id = _agent_name_segment(raw_node.get('node_id'), field_name=f'{field_name}.node_id')
        workgroup_id = _agent_name_segment(raw_node.get('workgroup_id'), field_name=f'{field_name}.workgroup_id')
        if node_id in node_ids:
            raise ValueError(f'duplicate orchestration bundle node_id: {node_id}')
        if workgroup_id in workgroup_ids:
            raise ValueError(f'duplicate orchestration bundle workgroup_id: {workgroup_id}')
        node_ids.add(node_id)
        workgroup_ids.add(workgroup_id)
        worker_profile = str(raw_node.get('worker_profile') or '').strip()
        reviewer_profile = str(raw_node.get('reviewer_profile') or '').strip()
        if worker_profile != 'coder':
            raise ValueError(f'{field_name}.worker_profile must be coder in bundle v1')
        if reviewer_profile != 'code_reviewer':
            raise ValueError(f'{field_name}.reviewer_profile must be code_reviewer in bundle v1')
        depends_on = sorted(_segment_list(raw_node.get('depends_on'), field_name=f'{field_name}.depends_on'))
        if node_id in depends_on:
            raise ValueError(f'{field_name}.depends_on cannot reference itself')
        parallel_group = _segment(raw_node.get('parallel_group'), field_name=f'{field_name}.parallel_group')
        raw_work_packet = raw_node.get('work_packet')
        if not isinstance(raw_work_packet, str):
            raise ValueError(f'{field_name}.work_packet must be a string')
        work_packet = raw_work_packet.strip()
        if not work_packet:
            raise ValueError(f'{field_name}.work_packet must be non-empty')
        if len(work_packet.encode('utf-8')) > 65536:
            raise ValueError(f'{field_name}.work_packet exceeds 65536 bytes')
        allowed_paths = sorted(
            normalize_scope_paths(raw_node.get('allowed_paths'), field_name=f'{field_name}.allowed_paths')
        )
        if len(nodes_raw) > 1 and not allowed_paths:
            raise ValueError(f'{field_name}.allowed_paths must be non-empty for multi-workgroup execution')
        if declared_paths:
            outside = [path for path in allowed_paths if not path_allowed_by_scope(path, declared_paths)]
            if outside:
                raise ValueError(f'{field_name}.allowed_paths exceed execution contract scope: {", ".join(outside)}')
        elif allowed_paths:
            raise ValueError(f'{field_name}.allowed_paths require execution_contract allowed_change_paths')
        acceptance_refs = sorted(_artifact_ref_list(
            raw_node.get('acceptance_refs'),
            field_name=f'{field_name}.acceptance_refs',
            known_refs=known_refs,
        ))
        verification_refs = sorted(_artifact_ref_list(
            raw_node.get('verification_refs'),
            field_name=f'{field_name}.verification_refs',
            known_refs=known_refs,
        ))
        integration_order = _positive_int(raw_node.get('integration_order'), field_name=f'{field_name}.integration_order')
        if integration_order in integration_orders:
            raise ValueError(f'duplicate orchestration bundle integration_order: {integration_order}')
        integration_orders.add(integration_order)
        packet_ref = (task_root / 'orchestration' / 'work-packets' / f'{node_id}.md').as_posix()
        packet_text = work_packet.rstrip() + '\n'
        work_packets[packet_ref] = packet_text
        normalized_nodes.append(
            {
                'node_id': node_id,
                'workgroup_id': workgroup_id,
                'worker_profile': worker_profile,
                'reviewer_profile': reviewer_profile,
                'depends_on': depends_on,
                'parallel_group': parallel_group,
                'work_packet_ref': packet_ref,
                'work_packet_sha256': _sha256_text(packet_text),
                'allowed_paths': allowed_paths,
                'acceptance_refs': acceptance_refs,
                'verification_refs': verification_refs,
                'integration_order': integration_order,
            }
        )

    normalized_nodes.sort(key=lambda node: (int(node['integration_order']), str(node['node_id'])))
    _validate_dependencies(normalized_nodes)
    _validate_parallel_scopes(normalized_nodes)
    _validate_capacity(normalized_nodes, capacity)
    integration = _normalize_integration(candidate.get('integration'), known_refs=known_refs)
    policy = _normalize_policy(candidate.get('policy'))
    normalized = {
        'schema': ORCHESTRATION_BUNDLE_SCHEMA,
        'task_id': task_id,
        'task_revision': task_revision(record),
        'task_digest': task_input_digest(record),
        'capacity_digest': effective_capacity_digest(capacity),
        'bundle_revision': bundle_revision,
        'selection': selection,
        'nodes': normalized_nodes,
        'integration': integration,
        'policy': policy,
    }
    return normalized, work_packets


def load_task_orchestration_bundle(
    project_root: Path,
    record: dict[str, object],
    *,
    capacity_snapshot: object,
) -> tuple[dict[str, object], dict[str, object]]:
    artifacts = _artifact_records(record)
    artifact = artifacts.get('orchestration_bundle')
    if not isinstance(artifact, dict):
        raise ValueError('direct execution requires orchestration_bundle artifact')
    if str(artifact.get('authority_status') or '') == 'superseded':
        raise ValueError('orchestration_bundle authority is superseded and cannot dispatch workers')
    path_text = str(artifact.get('path') or '').strip()
    if not path_text:
        raise ValueError('orchestration_bundle artifact missing path')
    path = _project_file(project_root, path_text)
    text = path.read_text(encoding='utf-8')
    actual_sha = _sha256_text(text)
    expected_sha = str(artifact.get('sha256') or '').strip()
    if expected_sha and actual_sha != expected_sha:
        raise ValueError('orchestration_bundle artifact sha256 mismatch')
    try:
        bundle = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f'orchestration_bundle artifact is not valid JSON: {exc}') from exc
    validate_normalized_bundle(
        bundle,
        record=record,
        project_root=project_root,
        capacity_snapshot=capacity_snapshot,
    )
    expected_bundle_digest = str(artifact.get('bundle_digest') or '').strip()
    actual_bundle_digest = bundle_digest(bundle)
    if expected_bundle_digest and expected_bundle_digest != actual_bundle_digest:
        raise ValueError('orchestration_bundle artifact bundle_digest mismatch')
    artifact_task_digest = str(artifact.get('task_digest') or '').strip()
    if artifact_task_digest and artifact_task_digest != str(bundle.get('task_digest') or ''):
        raise ValueError('orchestration_bundle artifact task_digest mismatch')
    artifact_task_revision = artifact.get('task_revision')
    if artifact_task_revision is not None and artifact_task_revision != bundle.get('task_revision'):
        raise ValueError('orchestration_bundle artifact task_revision mismatch')
    artifact_capacity_digest = str(artifact.get('capacity_digest') or '').strip()
    if artifact_capacity_digest and artifact_capacity_digest != str(bundle.get('capacity_digest') or ''):
        raise ValueError('orchestration_bundle artifact capacity_digest mismatch')
    return dict(bundle), dict(artifact)


def validate_normalized_bundle(
    bundle: object,
    *,
    record: dict[str, object],
    project_root: Path,
    capacity_snapshot: object,
) -> None:
    if not isinstance(bundle, dict):
        raise ValueError('orchestration_bundle artifact must be a JSON object')
    _reject_unknown_keys(bundle, _NORMALIZED_ROOT_KEYS, field_name='orchestration_bundle')
    if str(bundle.get('schema') or '') != ORCHESTRATION_BUNDLE_SCHEMA:
        raise ValueError(f'orchestration_bundle schema must be {ORCHESTRATION_BUNDLE_SCHEMA}')
    if str(bundle.get('task_id') or '') != _task_id(record):
        raise ValueError('orchestration_bundle task_id does not match task')
    if bundle.get('task_revision') != task_revision(record):
        raise ValueError('orchestration_bundle task_revision is stale')
    if str(bundle.get('task_digest') or '') != task_input_digest(record):
        raise ValueError('orchestration_bundle task_digest is stale')
    capacity = normalize_effective_capacity_snapshot(capacity_snapshot)
    if str(bundle.get('capacity_digest') or '') != effective_capacity_digest(capacity):
        raise ValueError('orchestration_bundle capacity_digest is stale')
    _positive_int(bundle.get('bundle_revision'), field_name='bundle_revision')
    nodes = bundle.get('nodes')
    max_workgroups = int(capacity['limits']['max_workgroups'])
    if not isinstance(nodes, list) or not nodes or len(nodes) > max_workgroups:
        raise ValueError(f'orchestration_bundle nodes must contain 1..{max_workgroups} entries')
    if _normalize_selection(bundle.get('selection'), node_count=len(nodes)) != bundle.get('selection'):
        raise ValueError('orchestration_bundle selection is not canonically normalized')
    artifacts = _artifact_records(record)
    declared_paths = declared_allowed_change_paths(
        _artifact_text(project_root, artifacts, ('task_packet', 'execution_contract'))
    )
    known_refs = _known_artifact_refs(record)
    task_root = safe_relative_path(str(record.get('task_root') or ''))
    if not task_root.parts:
        raise ValueError('orchestration_bundle task_root must be non-empty')
    node_ids: set[str] = set()
    workgroup_ids: set[str] = set()
    integration_orders: set[int] = set()
    normalized_nodes: list[dict[str, object]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError(f'nodes[{index}] must be an object')
        field_name = f'nodes[{index}]'
        _reject_unknown_keys(node, _NORMALIZED_NODE_KEYS, field_name=field_name)
        missing_node_fields = sorted(_NORMALIZED_NODE_KEYS - set(node))
        if missing_node_fields:
            raise ValueError(f'{field_name} missing fields: {", ".join(missing_node_fields)}')
        node_id = _agent_name_segment(node.get('node_id'), field_name=f'{field_name}.node_id')
        workgroup_id = _agent_name_segment(node.get('workgroup_id'), field_name=f'{field_name}.workgroup_id')
        if node_id in node_ids:
            raise ValueError(f'duplicate orchestration bundle node_id: {node_id}')
        if workgroup_id in workgroup_ids:
            raise ValueError(f'duplicate orchestration bundle workgroup_id: {workgroup_id}')
        node_ids.add(node_id)
        workgroup_ids.add(workgroup_id)
        if str(node.get('worker_profile') or '') != 'coder':
            raise ValueError(f'{field_name}.worker_profile must be coder in bundle v1')
        if str(node.get('reviewer_profile') or '') != 'code_reviewer':
            raise ValueError(f'{field_name}.reviewer_profile must be code_reviewer in bundle v1')
        depends_on = sorted(_segment_list(node.get('depends_on'), field_name=f'{field_name}.depends_on'))
        if node_id in depends_on:
            raise ValueError(f'{field_name}.depends_on cannot reference itself')
        parallel_group = _segment(node.get('parallel_group'), field_name=f'{field_name}.parallel_group')
        packet_ref = str(node.get('work_packet_ref') or '').strip()
        expected_packet_ref = (task_root / 'orchestration' / 'work-packets' / f'{node_id}.md').as_posix()
        if packet_ref != expected_packet_ref:
            raise ValueError(f'{field_name}.work_packet_ref must be {expected_packet_ref}')
        packet_path = _project_file(project_root, packet_ref)
        packet_text = packet_path.read_text(encoding='utf-8')
        packet_sha = str(node.get('work_packet_sha256') or '')
        if _sha256_text(packet_text) != packet_sha:
            raise ValueError(f'{field_name}.work_packet_sha256 mismatch')
        allowed_paths = sorted(
            normalize_scope_paths(node.get('allowed_paths'), field_name=f'{field_name}.allowed_paths')
        )
        if len(nodes) > 1 and not allowed_paths:
            raise ValueError(f'{field_name}.allowed_paths must be non-empty for multi-workgroup execution')
        if declared_paths:
            outside = [path for path in allowed_paths if not path_allowed_by_scope(path, declared_paths)]
            if outside:
                raise ValueError(f'{field_name}.allowed_paths exceed execution contract scope: {", ".join(outside)}')
        elif allowed_paths:
            raise ValueError(f'{field_name}.allowed_paths require execution_contract allowed_change_paths')
        acceptance_refs = sorted(
            _artifact_ref_list(
                node.get('acceptance_refs'),
                field_name=f'{field_name}.acceptance_refs',
                known_refs=known_refs,
            )
        )
        verification_refs = sorted(
            _artifact_ref_list(
                node.get('verification_refs'),
                field_name=f'{field_name}.verification_refs',
                known_refs=known_refs,
            )
        )
        integration_order = _positive_int(
            node.get('integration_order'),
            field_name=f'{field_name}.integration_order',
        )
        if integration_order in integration_orders:
            raise ValueError(f'duplicate orchestration bundle integration_order: {integration_order}')
        integration_orders.add(integration_order)
        canonical_node = {
            'node_id': node_id,
            'workgroup_id': workgroup_id,
            'worker_profile': 'coder',
            'reviewer_profile': 'code_reviewer',
            'depends_on': depends_on,
            'parallel_group': parallel_group,
            'work_packet_ref': packet_ref,
            'work_packet_sha256': packet_sha,
            'allowed_paths': allowed_paths,
            'acceptance_refs': acceptance_refs,
            'verification_refs': verification_refs,
            'integration_order': integration_order,
        }
        if canonical_node != node:
            raise ValueError(f'{field_name} is not canonically normalized')
        normalized_nodes.append(canonical_node)
    if normalized_nodes != sorted(
        normalized_nodes,
        key=lambda node: (int(node['integration_order']), str(node['node_id'])),
    ):
        raise ValueError('orchestration_bundle nodes are not in canonical integration order')
    _validate_dependencies(normalized_nodes)
    _validate_parallel_scopes(normalized_nodes)
    _validate_capacity(normalized_nodes, capacity)
    if _normalize_integration(bundle.get('integration'), known_refs=known_refs) != bundle.get('integration'):
        raise ValueError('orchestration_bundle integration is not canonically normalized')
    if _normalize_policy(bundle.get('policy')) != bundle.get('policy'):
        raise ValueError('orchestration_bundle policy is not canonically normalized')


def task_input_digest(record: dict[str, object]) -> str:
    artifacts = _artifact_records(record)
    payload = [
        {
            'kind': kind,
            'path': str(artifacts[kind].get('path') or ''),
            'sha256': str(artifacts[kind].get('sha256') or ''),
        }
        for kind in _TASK_INPUT_ARTIFACTS
        if isinstance(artifacts.get(kind), dict)
    ]
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def task_revision(record: dict[str, object]) -> int:
    value = record.get('task_revision', 1)
    return _positive_int(value, field_name='task_revision')


def bundle_text(bundle: dict[str, object]) -> str:
    return json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + '\n'


def bundle_digest(bundle: dict[str, object]) -> str:
    return _sha256_text(bundle_text(bundle))


def bundle_summary(bundle: dict[str, object], artifact: dict[str, object] | None = None) -> dict[str, object]:
    nodes = bundle.get('nodes') if isinstance(bundle.get('nodes'), list) else []
    return {
        'schema': bundle.get('schema'),
        'bundle_revision': bundle.get('bundle_revision'),
        'bundle_digest': str((artifact or {}).get('bundle_digest') or bundle_digest(bundle)),
        'task_revision': bundle.get('task_revision'),
        'task_digest': bundle.get('task_digest'),
        'capacity_digest': bundle.get('capacity_digest'),
        'selection': bundle.get('selection'),
        'source': (artifact or {}).get('bundle_source'),
        'node_count': len(nodes),
        'node_ids': [str(node.get('node_id') or '') for node in nodes if isinstance(node, dict)],
        'artifact_path': (artifact or {}).get('path'),
    }


def _normalize_selection(value: object, *, node_count: int) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError('selection must be an object')
    _reject_unknown_keys(value, _SELECTION_KEYS, field_name='selection')
    missing = sorted(_SELECTION_KEYS - set(value))
    if missing:
        raise ValueError(f'selection missing fields: {", ".join(missing)}')
    workgroup_count = _positive_int(value.get('workgroup_count'), field_name='selection.workgroup_count')
    if not 1 <= workgroup_count <= MAX_WORKGROUPS:
        raise ValueError(f'selection.workgroup_count must be between 1 and {MAX_WORKGROUPS}')
    if workgroup_count != node_count:
        raise ValueError('selection.workgroup_count must equal node count')
    complexity = _enum_value(
        value.get('complexity'),
        allowed=_SELECTION_COMPLEXITIES,
        field_name='selection.complexity',
    )
    cutability = _enum_value(
        value.get('cutability'),
        allowed=_SELECTION_CUTABILITIES,
        field_name='selection.cutability',
    )
    execution_shape = _enum_value(
        value.get('execution_shape'),
        allowed=_SELECTION_SHAPES,
        field_name='selection.execution_shape',
    )
    rationale = str(value.get('rationale') or '').strip()
    if not rationale or '\n' in rationale or '\r' in rationale or len(rationale) > 500:
        raise ValueError('selection.rationale must be a non-empty single line of at most 500 characters')
    return {
        'workgroup_count': workgroup_count,
        'complexity': complexity,
        'cutability': cutability,
        'execution_shape': execution_shape,
        'rationale': rationale,
    }


def _enum_value(value: object, *, allowed: frozenset[str], field_name: str) -> str:
    text = str(value or '').strip()
    if text not in allowed:
        raise ValueError(f'{field_name} must be one of: {", ".join(sorted(allowed))}')
    return text


def _normalize_integration(value: object, *, known_refs: set[str]) -> dict[str, list[str]]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('integration must be an object')
    _reject_unknown_keys(value, _INTEGRATION_KEYS, field_name='integration')
    return {
        'verification_refs': sorted(_artifact_ref_list(
            value.get('verification_refs', []),
            field_name='integration.verification_refs',
            known_refs=known_refs,
        )),
        'project_root_verification_refs': sorted(_artifact_ref_list(
            value.get('project_root_verification_refs', []),
            field_name='integration.project_root_verification_refs',
            known_refs=known_refs,
        )),
    }


def _normalize_policy(value: object) -> dict[str, object]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('policy must be an object')
    _reject_unknown_keys(value, _POLICY_KEYS, field_name='policy')
    rework = value.get('max_node_rework_rounds', DEFAULT_MAX_NODE_REWORK_ROUNDS)
    if isinstance(rework, bool) or not isinstance(rework, int) or not 0 <= rework <= 2:
        raise ValueError('policy.max_node_rework_rounds must be an integer from 0 to 2')
    required_failure = str(value.get('on_required_node_failure') or 'partial_or_blocked').strip()
    structural_failure = str(value.get('on_structural_failure') or 'replan_required').strip()
    if required_failure != 'partial_or_blocked':
        raise ValueError('policy.on_required_node_failure must be partial_or_blocked in bundle v1')
    if structural_failure != 'replan_required':
        raise ValueError('policy.on_structural_failure must be replan_required in bundle v1')
    return {
        'max_node_rework_rounds': rework,
        'on_required_node_failure': required_failure,
        'on_structural_failure': structural_failure,
    }


def _validate_dependencies(nodes: Iterable[dict[str, object]]) -> None:
    nodes_by_id = {str(node.get('node_id') or ''): node for node in nodes}
    if len(nodes_by_id) == 0:
        raise ValueError('orchestration_bundle requires at least one node')
    for node_id, node in nodes_by_id.items():
        if not _SEGMENT_RE.fullmatch(node_id):
            raise ValueError(f'invalid orchestration bundle node_id: {node_id!r}')
        depends_on = node.get('depends_on')
        if not isinstance(depends_on, list):
            raise ValueError(f'node {node_id} depends_on must be a list')
        missing = sorted({str(item) for item in depends_on} - set(nodes_by_id))
        if missing:
            raise ValueError(f'node {node_id} depends_on unknown nodes: {", ".join(missing)}')
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError(f'orchestration bundle dependency cycle includes {node_id}')
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in nodes_by_id[node_id].get('depends_on') or []:
            visit(str(dependency))
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in sorted(nodes_by_id):
        visit(node_id)


def _validate_parallel_scopes(nodes: list[dict[str, object]]) -> None:
    dependencies = {
        str(node.get('node_id') or ''): {str(item) for item in node.get('depends_on') or []}
        for node in nodes
    }

    def reaches(start: str, target: str) -> bool:
        pending = list(dependencies.get(start, set()))
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(dependencies.get(current, set()))
        return False

    for left_index, left in enumerate(nodes):
        left_id = str(left.get('node_id') or '')
        for right in nodes[left_index + 1:]:
            right_id = str(right.get('node_id') or '')
            if reaches(left_id, right_id) or reaches(right_id, left_id):
                continue
            for left_scope in left.get('allowed_paths') or []:
                for right_scope in right.get('allowed_paths') or []:
                    if scopes_overlap(str(left_scope), str(right_scope)):
                        raise ValueError(
                            f'parallel nodes {left_id} and {right_id} have overlapping allowed paths: '
                            f'{left_scope} <-> {right_scope}'
                        )


def _validate_capacity(
    nodes: list[dict[str, object]],
    capacity: dict[str, object],
) -> None:
    limits = capacity['limits']
    dynamic_profiles = capacity['dynamic_profiles']
    aliases = capacity['profile_aliases']
    max_parallel = int(limits['max_parallel_workgroups'])
    max_agents = int(limits['max_active_dynamic_agents'])
    frontier = _widest_dependency_frontier(nodes)
    if len(frontier) > max_parallel:
        raise ValueError(
            f'orchestration bundle exceeds max_parallel_workgroups={max_parallel}: '
            f'dependency frontier requests {len(frontier)} ({", ".join(str(node["node_id"]) for node in frontier)})'
        )
    requested: dict[str, int] = {}
    for node in frontier:
        for key in ('worker_profile', 'reviewer_profile'):
            logical_profile = str(node.get(key) or '')
            profile = str(aliases.get(logical_profile) or logical_profile)
            profile_record = dynamic_profiles.get(profile)
            if not isinstance(profile_record, dict):
                raise ValueError(
                    f'orchestration bundle requires effective dynamic profile: {logical_profile}'
                )
            requested[profile] = requested.get(profile, 0) + 1
    for profile, count in sorted(requested.items()):
        maximum = int(dynamic_profiles[profile]['max_instances'])
        if count > maximum:
            raise ValueError(
                f'orchestration bundle profile {profile} exceeds max_instances={maximum}: '
                f'dependency frontier requests {count}'
            )
    requested_agents = sum(requested.values())
    if requested_agents > max_agents:
        raise ValueError(
            'orchestration bundle exceeds max_active_dynamic_agents='
            f'{max_agents}: dependency frontier requests {requested_agents}'
        )


def _widest_dependency_frontier(nodes: list[dict[str, object]]) -> list[dict[str, object]]:
    nodes_by_id = {str(node['node_id']): node for node in nodes}
    dependencies = {
        node_id: {str(item) for item in node.get('depends_on') or []}
        for node_id, node in nodes_by_id.items()
    }

    def reaches(start: str, target: str) -> bool:
        pending = list(dependencies.get(start, set()))
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(dependencies.get(current, set()))
        return False

    ordered = [nodes_by_id[node_id] for node_id in sorted(nodes_by_id)]
    for size in range(len(ordered), 0, -1):
        for candidate in combinations(ordered, size):
            ids = [str(node['node_id']) for node in candidate]
            if all(
                not reaches(left, right) and not reaches(right, left)
                for left, right in combinations(ids, 2)
            ):
                return list(candidate)
    return []


def _artifact_ref_list(value: object, *, field_name: str, known_refs: set[str]) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f'{field_name} must be a non-empty list')
    refs: list[str] = []
    for index, item in enumerate(value):
        ref = str(item or '').strip()
        if not ref:
            raise ValueError(f'{field_name}[{index}] must be non-empty')
        path_part = ref.split('#', 1)[0]
        safe_relative_path(path_part)
        if path_part not in known_refs:
            raise ValueError(f'{field_name}[{index}] must reference an imported task artifact: {path_part}')
        if ref not in refs:
            refs.append(ref)
    return refs


def _segment_list(value: object, *, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f'{field_name} must be a list')
    result: list[str] = []
    for index, item in enumerate(value):
        segment = _segment(item, field_name=f'{field_name}[{index}]')
        if segment in result:
            raise ValueError(f'{field_name} contains duplicate value: {segment}')
        result.append(segment)
    return result


def _segment(value: object, *, field_name: str) -> str:
    text = str(value or '').strip()
    if not _SEGMENT_RE.fullmatch(text):
        raise ValueError(f'{field_name} must match {_SEGMENT_RE.pattern}: {text!r}')
    return text


def _agent_name_segment(value: object, *, field_name: str) -> str:
    try:
        return normalize_agent_name(str(value or ''))
    except AgentValidationError as exc:
        raise ValueError(f'{field_name} is invalid: {exc}') from exc


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{field_name} must be a positive integer')
    return value


def _artifact_records(record: dict[str, object]) -> dict[str, dict[str, object]]:
    artifacts = record.get('artifacts')
    if not isinstance(artifacts, dict):
        return {}
    return {str(kind): dict(value) for kind, value in artifacts.items() if isinstance(value, dict)}


def _known_artifact_refs(record: dict[str, object]) -> set[str]:
    return {
        str(artifact.get('path') or '').strip()
        for artifact in _artifact_records(record).values()
        if str(artifact.get('path') or '').strip()
    }


def _required_artifact_ref(artifacts: dict[str, dict[str, object]], kind: str) -> str:
    artifact = artifacts.get(kind)
    ref = str(artifact.get('path') or '').strip() if isinstance(artifact, dict) else ''
    if not ref:
        raise ValueError(f'orchestration bundle requires task artifact: {kind}')
    safe_relative_path(ref)
    return ref


def _artifact_text(project_root: Path, artifacts: dict[str, dict[str, object]], kinds: Iterable[str]) -> str:
    sections: list[str] = []
    for kind in kinds:
        artifact = artifacts.get(kind)
        ref = str(artifact.get('path') or '').strip() if isinstance(artifact, dict) else ''
        if not ref:
            continue
        sections.append(_project_file(project_root, ref).read_text(encoding='utf-8'))
    return '\n'.join(sections)


def _project_file(project_root: Path, value: str) -> Path:
    relative = safe_relative_path(value)
    path = (project_root / relative).resolve()
    root = project_root.resolve()
    if path == root or root not in path.parents or not path.is_file():
        raise ValueError(f'orchestration bundle reference is not a project file: {value}')
    return path


def _task_id(record: dict[str, object]) -> str:
    return _segment(record.get('task_id'), field_name='task_id')


def _reject_unknown_keys(value: dict[str, object], allowed: frozenset[str], *, field_name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f'{field_name} contains unknown fields: {", ".join(unknown)}')


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


__all__ = [
    'DEFAULT_MAX_NODE_REWORK_ROUNDS',
    'MAX_WORKGROUPS',
    'ORCHESTRATION_BUNDLE_CANDIDATE_SCHEMA',
    'ORCHESTRATION_BUNDLE_SCHEMA',
    'build_single_node_candidate',
    'bundle_digest',
    'bundle_summary',
    'bundle_text',
    'load_task_orchestration_bundle',
    'normalize_bundle_candidate',
    'task_input_digest',
    'task_revision',
    'validate_normalized_bundle',
]
