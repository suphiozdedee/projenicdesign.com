from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from agents.config_loader import load_project_config
from agents.config_loader_runtime.role_lookup import RoleLookupError, load_installed_role_manifest, normalize_role_id
from agents.models import AgentValidationError, LoopRoleProfileSpec, WorkspaceMode, normalize_agent_name
from agents.store import AgentRuntimeStore
from storage.atomic import atomic_write_json

from .agent_status_diagnostics import (
    agent_kind,
    dispatch_state,
    failed_apply,
    normalize_apply_payload,
    ownership_class,
    pane_identity_source,
)
from .daemon import ping_local_state
from .reload import reload_config
from .reload_apply_diagnostics import reload_apply_summary

ACTIVE_STATES = frozenset({'visible', 'hidden', 'parked'})
REMOVE_POLICIES = frozenset({'auto', 'hide', 'park', 'unload', 'kill'})


def agent_lifecycle(context, command) -> dict[str, object]:
    action = str(getattr(command, 'action', '') or '').strip().lower()
    if action == 'status':
        return _status(context, command)
    if action == 'show':
        return _show(context, command)
    if action == 'add':
        return _add(context, command)
    if action == 'move':
        return _move(context, command)
    if action in {'hide', 'park', 'resume'}:
        return _transition(context, command)
    if action in {'remove', 'release'}:
        return _remove(context, command)
    raise ValueError(f'unsupported agent lifecycle action: {action}')


def add_lifecycle_agents(context, commands: Iterable[object], *, action: str = 'agent-add-batch') -> list[dict[str, object]]:
    staged: list[tuple[str, dict[str, object]]] = []
    previous_by_name: dict[str, dict[str, object] | None] = {}
    try:
        for command in tuple(commands):
            name, previous, payload = _stage_add(context, command)
            previous_by_name[name] = previous
            staged.append((name, payload))
        if not staged:
            return []
        apply = _apply_reload_if_mounted(context, action=action)
        results: list[dict[str, object]] = []
        for name, payload in staged:
            payload['apply'] = apply
            _update_apply_evidence(context, payload)
            _write_state(context, name, payload)
            results.append(dict(payload, action='add'))
    except Exception:
        for name, previous in previous_by_name.items():
            _restore_state(context, name, previous)
        raise
    for name, payload in staged:
        _append_event(
            context,
            {
                'event': 'add',
                'agent': name,
                'lifecycle_state': payload['lifecycle_state'],
            },
        )
    return results


def _status(context, command) -> dict[str, object]:
    role_class_filter = _optional_text(getattr(command, 'role_class', None))
    records = []
    dynamic = _load_dynamic_records(context)
    for record in dynamic:
        if role_class_filter and str(record.get('role_class') or '') != role_class_filter:
            continue
        records.append(_status_record(record, source='dynamic'))
    loaded = load_project_config(context.project.project_root, include_loop_overlays=False)
    dynamic_names = {str(record.get('agent') or '') for record in dynamic}
    for name, spec in sorted(loaded.config.agents.items()):
        if name in dynamic_names:
            continue
        role_class = _infer_role_class(getattr(spec, 'role', None))
        if role_class_filter and role_class != role_class_filter:
            continue
        records.append(
            {
                'agent': name,
                'source': 'configured',
                'agent_kind': 'static',
                'ownership_class': 'static_configured',
                'role': getattr(spec, 'role', None),
                'provider': getattr(spec, 'provider', None),
                'profile': None,
                'role_class': role_class,
                'lifecycle_state': 'configured',
                'visibility_state': 'visible' if name in loaded.config.default_agents else 'configured',
                'dispatch_disabled': bool(getattr(spec, 'dispatch_disabled', False)),
                'dispatch_state': dispatch_state(bool(getattr(spec, 'dispatch_disabled', False))),
                'pane_id': None,
                'pane_identity_source': 'missing',
                'apply_status': None,
                'apply_plan_class': None,
                'apply_stage': None,
                'failed_apply': False,
                'retained_busy': False,
                'ask_target': name,
                'state_path': None,
            }
        )
    return {
        'agent_lifecycle_status': 'ok',
        'action': 'status',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'agent_count': len(records),
        'agents': records,
        'runtime_agents_root': str(_agents_root(context)),
    }


def _show(context, command) -> dict[str, object]:
    name = _normalize_name(getattr(command, 'agent_name', None), field_name='agent')
    record = _load_record(_state_path(context, name))
    if record is not None:
        record = dict(record)
        record['agent_lifecycle_status'] = 'ok'
        record['action'] = 'show'
        record['source'] = 'dynamic'
        record['state_path'] = str(_state_path(context, name))
        record.update(_diagnostic_fields(record, source='dynamic'))
        return record
    loaded = load_project_config(context.project.project_root, include_loop_overlays=False)
    spec = loaded.config.agents.get(name)
    if spec is None:
        raise ValueError(f'agent {name!r} not found')
    return {
        'agent_lifecycle_status': 'ok',
        'action': 'show',
        'source': 'configured',
        'agent_kind': 'static',
        'ownership_class': 'static_configured',
        'agent': name,
        'role': getattr(spec, 'role', None),
        'provider': getattr(spec, 'provider', None),
        'model': getattr(spec, 'model', None),
        'workspace_mode': getattr(getattr(spec, 'workspace_mode', None), 'value', None),
        'role_class': _infer_role_class(getattr(spec, 'role', None)),
        'lifecycle_state': 'configured',
        'visibility_state': 'visible' if name in loaded.config.default_agents else 'configured',
        'dispatch_disabled': bool(getattr(spec, 'dispatch_disabled', False)),
        'dispatch_state': dispatch_state(bool(getattr(spec, 'dispatch_disabled', False))),
        'pane_id': None,
        'pane_identity_source': 'missing',
        'apply_status': None,
        'apply_plan_class': None,
        'apply_stage': None,
        'failed_apply': False,
        'retained_busy': False,
        'ask_target': name,
    }


def _add(context, command) -> dict[str, object]:
    name, previous, payload = _stage_add(context, command)
    try:
        payload['apply'] = _apply_reload_if_mounted(context, action='agent-add')
        _update_apply_evidence(context, payload)
    except Exception:
        _restore_state(context, name, previous)
        raise
    _write_state(context, name, payload)
    _append_event(
        context,
        {'event': 'add', 'agent': name, 'lifecycle_state': payload['lifecycle_state']},
    )
    return dict(payload, action='add')


def _stage_add(context, command) -> tuple[str, dict[str, object] | None, dict[str, object]]:
    name = _normalize_name(getattr(command, 'agent_name', None), field_name='agent')
    loaded = load_project_config(context.project.project_root, include_loop_overlays=True)
    if name in loaded.config.agents:
        raise ValueError(f'agent {name!r} already exists or is active')
    previous = _load_record(_state_path(context, name))
    profile = _profile_spec(loaded.config, getattr(command, 'profile', None))
    spec = _resolve_dynamic_spec(name, command, profile=profile)
    visibility = _normalize_visibility(getattr(command, 'visibility', None), default='hidden')
    role_class = _infer_role_class(spec['role'])
    payload = {
        'schema_version': 1,
        'record_type': 'ccb_dynamic_agent_lifecycle',
        'agent_lifecycle_status': 'active',
        'agent': name,
        'profile': spec.get('profile'),
        'role': spec['role'],
        'provider': spec['provider'],
        'model': spec.get('model'),
        'thinking': spec.get('thinking'),
        'workspace_mode': spec.get('workspace_mode') or WorkspaceMode.INPLACE.value,
        'workspace_group': spec.get('workspace_group'),
        'workspace_root': None,
        'workspace_path': None,
        'startup_args': list(spec.get('startup_args') or ()),
        'provider_profile': dict(spec.get('provider_profile') or {}),
        'target': '.',
        'labels': ['ccb-dynamic', f'role-class:{role_class}'],
        'description': 'CCB dynamic agent',
        'role_class': role_class,
        'lifecycle_state': visibility,
        'visibility_state': 'visible' if visibility == 'visible' else visibility,
        'dispatch_disabled': visibility == 'parked',
        'window_name': _optional_text(getattr(command, 'window_name', None)),
        'window_class': _optional_text(getattr(command, 'window_class', None)),
        'loop_id': _optional_text(getattr(command, 'loop_id', None)),
        'node_id': _optional_text(getattr(command, 'node_id', None)),
        'placement': _placement_record(command),
        'lifetime': _optional_text(getattr(command, 'lifetime', None)) or 'session',
        'created_at': str((previous or {}).get('created_at') or _utc_now()),
        'created_sequence': _created_sequence(context, previous),
        'updated_at': _utc_now(),
        'created_by': 'ccb agent add',
        'last_reason': 'agent add',
        'ask_target': name,
        'state_path': str(_state_path(context, name)),
        'events_path': str(_events_path(context)),
    }
    _write_state(context, name, payload)
    _update_resolved_placement(context, payload)
    _write_state(context, name, payload)
    return name, previous, payload


def _remove(context, command) -> dict[str, object]:
    batch_names = tuple(str(item) for item in tuple(getattr(command, 'agent_names', ()) or ()))
    if batch_names:
        return _remove_many(context, command, batch_names)
    name = _normalize_name(getattr(command, 'agent_name', None), field_name='agent')
    requested_action = str(getattr(command, 'action', None) or 'remove').strip().lower()
    state_path = _state_path(context, name)
    previous = _load_record(state_path)
    if previous is None:
        raise ValueError(f'agent {name!r} is not a dynamic agent')
    policy = str(getattr(command, 'policy', None) or 'auto').strip().lower()
    if policy not in REMOVE_POLICIES:
        raise ValueError(f'unsupported remove policy: {policy}')
    if policy == 'kill' and (not bool(getattr(command, 'force', False)) or not _optional_text(getattr(command, 'reason', None))):
        raise ValueError('agent remove --policy kill requires --force and --reason')
    resolved_policy = _resolve_remove_policy(policy, role_class=str(previous.get('role_class') or 'unknown'))
    if resolved_policy in {'unload', 'kill'} and bool(getattr(command, 'idle_only', False)):
        gate = _release_gate(context, name)
        if bool(gate.get('retained')):
            retained = dict(previous)
            retained['agent_lifecycle_status'] = 'retained_busy'
            retained['requested_action'] = requested_action
            retained['requested_policy'] = policy
            retained['resolved_policy'] = resolved_policy
            retained['retained_busy'] = True
            retained['retain_reason'] = gate.get('reason')
            retained['runtime_state'] = gate.get('runtime_state')
            retained['queue_depth'] = gate.get('queue_depth')
            retained['updated_at'] = _utc_now()
            _write_state(context, name, retained)
            _append_event(context, {'event': f'{requested_action}-retained', 'agent': name, 'policy': resolved_policy})
            return dict(retained, action=requested_action, state_path=str(state_path), events_path=str(_events_path(context)))
    next_state = _state_for_policy(resolved_policy)
    payload = dict(previous)
    payload.update(
        {
            'agent_lifecycle_status': 'removed' if next_state == 'unloaded' else 'active',
            'requested_action': requested_action,
            'requested_policy': policy,
            'resolved_policy': resolved_policy,
            'previous_state': previous.get('lifecycle_state'),
            'lifecycle_state': next_state,
            'visibility_state': 'hidden' if next_state in {'hidden', 'parked'} else 'unloaded',
            'dispatch_disabled': next_state == 'parked',
            'retained_busy': False,
            'summary_policy': _optional_text(getattr(command, 'summary_policy', None)),
            'force': bool(getattr(command, 'force', False)),
            'reason': _optional_text(getattr(command, 'reason', None)),
            'updated_at': _utc_now(),
            'last_reason': _optional_text(getattr(command, 'reason', None)) or f'agent remove policy {resolved_policy}',
            'state_path': str(state_path),
            'events_path': str(_events_path(context)),
        }
    )
    _write_state(context, name, payload)
    _append_event(context, {'event': requested_action, 'agent': name, 'policy': resolved_policy, 'next_state': next_state})
    try:
        payload['apply'] = _apply_reload_if_mounted(
            context,
            action='agent-remove' if next_state == 'unloaded' else f'agent-{resolved_policy}',
        )
        if next_state == 'unloaded':
            _update_apply_evidence(context, payload)
        else:
            _update_transition_apply_evidence(context, payload)
    except Exception:
        _restore_state(context, name, previous)
        raise
    _write_state(context, name, payload)
    return dict(payload, action=requested_action)


def _remove_many(context, command, names: tuple[str, ...]) -> dict[str, object]:
    requested_action = str(getattr(command, 'action', None) or 'remove').strip().lower()
    command_label = f'agent {requested_action} --agents'
    normalized = _normalize_agent_names(names, command=command_label)
    policy = str(getattr(command, 'policy', None) or 'auto').strip().lower()
    if policy not in REMOVE_POLICIES:
        raise ValueError(f'unsupported remove policy: {policy}')
    if policy == 'kill' and (not bool(getattr(command, 'force', False)) or not _optional_text(getattr(command, 'reason', None))):
        raise ValueError('agent remove --policy kill requires --force and --reason')

    previous_by_name: dict[str, dict[str, object]] = {}
    resolved_by_name: dict[str, str] = {}
    next_state_by_name: dict[str, str] = {}
    for name in normalized:
        previous = _load_record(_state_path(context, name))
        if previous is None:
            raise ValueError(f'agent {name!r} is not a dynamic agent')
        previous_state = str(previous.get('lifecycle_state') or '')
        if previous_state not in ACTIVE_STATES:
            raise ValueError(f'agent {name!r} is not active; current lifecycle_state={previous_state or "<empty>"}')
        resolved_policy = _resolve_remove_policy(policy, role_class=str(previous.get('role_class') or 'unknown'))
        resolved_by_name[name] = resolved_policy
        next_state_by_name[name] = _state_for_policy(resolved_policy)
        previous_by_name[name] = previous

    if bool(getattr(command, 'idle_only', False)):
        retained: dict[str, dict[str, object]] = {}
        for name in normalized:
            if resolved_by_name[name] not in {'unload', 'kill'}:
                continue
            gate = _release_gate(context, name)
            if bool(gate.get('retained')):
                retained[name] = gate
        if retained:
            records = []
            for name in normalized:
                record = dict(previous_by_name[name])
                if name in retained:
                    record['agent_lifecycle_status'] = 'retained_busy'
                    record['requested_action'] = requested_action
                    record['requested_policy'] = policy
                    record['resolved_policy'] = resolved_by_name[name]
                    record['retained_busy'] = True
                    record['retain_reason'] = retained[name].get('reason')
                    record['runtime_state'] = retained[name].get('runtime_state')
                    record['queue_depth'] = retained[name].get('queue_depth')
                    record['updated_at'] = _utc_now()
                    _write_state(context, name, record)
                    _append_event(context, {'event': f'{requested_action}-retained', 'agent': name, 'policy': resolved_by_name[name]})
                records.append(_status_record(record, source='dynamic'))
            return {
                'agent_lifecycle_status': 'retained_busy',
                'action': requested_action,
                'requested_policy': policy,
                'resolved_policies': dict(resolved_by_name),
                'retained_agents': sorted(retained),
                'agent_count': len(records),
                'agents': records,
                'project_id': context.project.project_id,
                'project_root': str(context.project.project_root),
                'runtime_agents_root': str(_agents_root(context)),
            }

    payloads: dict[str, dict[str, object]] = {}
    try:
        for name in normalized:
            previous = previous_by_name[name]
            resolved_policy = resolved_by_name[name]
            next_state = next_state_by_name[name]
            state_path = _state_path(context, name)
            payload = dict(previous)
            payload.update(
                {
                    'agent_lifecycle_status': 'removed' if next_state == 'unloaded' else 'active',
                    'requested_action': requested_action,
                    'requested_policy': policy,
                    'resolved_policy': resolved_policy,
                    'previous_state': previous.get('lifecycle_state'),
                    'lifecycle_state': next_state,
                    'visibility_state': 'hidden' if next_state in {'hidden', 'parked'} else 'unloaded',
                    'dispatch_disabled': next_state == 'parked',
                    'retained_busy': False,
                    'summary_policy': _optional_text(getattr(command, 'summary_policy', None)),
                    'force': bool(getattr(command, 'force', False)),
                    'reason': _optional_text(getattr(command, 'reason', None)),
                    'updated_at': _utc_now(),
                    'last_reason': _optional_text(getattr(command, 'reason', None))
                    or f'agent {requested_action} batch policy {resolved_policy}',
                    'state_path': str(state_path),
                    'events_path': str(_events_path(context)),
                }
            )
            _write_state(context, name, payload)
            payloads[name] = payload
        for name in normalized:
            _append_event(
                context,
                {
                    'event': requested_action,
                    'agent': name,
                    'policy': resolved_by_name[name],
                    'next_state': next_state_by_name[name],
                    'batch_agents': list(normalized),
                },
            )
        apply = _apply_reload_if_mounted(
            context,
            action=_batch_remove_apply_action(requested_action, next_state_by_name),
        )
        for name in normalized:
            payload = payloads[name]
            payload['apply'] = apply
            if next_state_by_name[name] == 'unloaded':
                _update_apply_evidence(context, payload)
            else:
                _update_transition_apply_evidence(context, payload)
            _write_state(context, name, payload)
    except Exception:
        for name, previous in previous_by_name.items():
            _restore_state(context, name, previous)
        raise

    records = [_status_record(payloads[name], source='dynamic') for name in normalized]
    unloaded = [name for name in normalized if next_state_by_name[name] == 'unloaded']
    return {
        'agent_lifecycle_status': 'removed' if len(unloaded) == len(normalized) else 'active',
        'action': requested_action,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'agent_count': len(records),
        'removed_agents': unloaded,
        'updated_agents': list(normalized),
        'requested_policy': policy,
        'resolved_policies': dict(resolved_by_name),
        'apply': apply,
        'agents': records,
        'runtime_agents_root': str(_agents_root(context)),
    }


def _move(context, command) -> dict[str, object]:
    batch_names = tuple(str(item) for item in tuple(getattr(command, 'agent_names', ()) or ()))
    if batch_names:
        return _move_many(context, command, batch_names)
    name = _normalize_name(getattr(command, 'agent_name', None), field_name='agent')
    state_path = _state_path(context, name)
    previous = _load_record(state_path)
    if previous is None:
        raise ValueError(f'agent {name!r} is not a dynamic agent')
    previous_state = str(previous.get('lifecycle_state') or '')
    if previous_state not in ACTIVE_STATES:
        raise ValueError(f'agent {name!r} is not active; current lifecycle_state={previous_state or "<empty>"}')
    placement = _placement_record(command)
    if str(placement.get('mode') or '') == 'auto':
        raise ValueError('agent move requires --window, --window-class, --loop-id, or --node-id')
    payload = dict(previous)
    payload.update(
        {
            'requested_action': 'move',
            'previous_window_name': _optional_text(dict(previous.get('placement') or {}).get('window_name'))
            or _optional_text(previous.get('resolved_window_name'))
            or _optional_text(previous.get('window_name')),
            'window_name': placement.get('window_name'),
            'window_class': placement.get('window_class'),
            'loop_id': placement.get('loop_id'),
            'node_id': placement.get('node_id'),
            'placement': placement,
            'reason': _optional_text(getattr(command, 'reason', None)),
            'updated_at': _utc_now(),
            'last_reason': _optional_text(getattr(command, 'reason', None)) or 'agent move',
            'state_path': str(state_path),
            'events_path': str(_events_path(context)),
        }
    )
    _write_state(context, name, payload)
    _update_resolved_placement(context, payload)
    target_window = _optional_text(payload.get('resolved_window_name')) or _optional_text(
        dict(payload.get('placement') or {}).get('window_name')
    )
    placement_sequence = _next_placement_sequence(context, target_window=target_window, exclude_agent=name)
    payload['placement_sequence'] = placement_sequence
    placement_with_sequence = dict(payload.get('placement') or {})
    placement_with_sequence['placement_sequence'] = placement_sequence
    payload['placement'] = placement_with_sequence
    _write_state(context, name, payload)
    _append_event(
        context,
        {
            'event': 'move',
            'agent': name,
            'previous_window_name': payload.get('previous_window_name'),
            'target_window_name': payload.get('resolved_window_name') or dict(payload.get('placement') or {}).get('window_name'),
        },
    )
    try:
        payload['apply'] = _apply_reload_if_mounted(context, action='agent-move')
        _update_move_apply_evidence(context, payload)
    except Exception:
        _restore_state(context, name, previous)
        raise
    _write_state(context, name, payload)
    return dict(payload, action='move')


def _move_many(context, command, names: tuple[str, ...]) -> dict[str, object]:
    normalized = _normalize_agent_names(names, command='agent move --agents')
    placement = _placement_record(command)
    if str(placement.get('mode') or '') == 'auto':
        raise ValueError('agent move requires --window, --window-class, --loop-id, or --node-id')
    previous_by_name: dict[str, dict[str, object]] = {}
    payloads: dict[str, dict[str, object]] = {}
    for name in normalized:
        previous = _load_record(_state_path(context, name))
        if previous is None:
            raise ValueError(f'agent {name!r} is not a dynamic agent')
        previous_state = str(previous.get('lifecycle_state') or '')
        if previous_state not in ACTIVE_STATES:
            raise ValueError(f'agent {name!r} is not active; current lifecycle_state={previous_state or "<empty>"}')
        previous_by_name[name] = previous
    explicit_target_window = _optional_text(placement.get('window_name'))
    base_sequence = _next_placement_sequence_for_agents(
        context,
        target_window=explicit_target_window,
        exclude_agents=set(normalized),
    )
    try:
        for index, name in enumerate(normalized):
            previous = previous_by_name[name]
            payload = dict(previous)
            placement_with_sequence = dict(placement)
            placement_sequence = base_sequence + index
            placement_with_sequence['placement_sequence'] = placement_sequence
            payload.update(
                {
                    'requested_action': 'move',
                    'previous_window_name': _optional_text(dict(previous.get('placement') or {}).get('window_name'))
                    or _optional_text(previous.get('resolved_window_name'))
                    or _optional_text(previous.get('window_name')),
                    'window_name': explicit_target_window,
                    'window_class': placement.get('window_class'),
                    'loop_id': placement.get('loop_id'),
                    'node_id': placement.get('node_id'),
                    'placement': placement_with_sequence,
                    'placement_sequence': placement_sequence,
                    'reason': _optional_text(getattr(command, 'reason', None)),
                    'updated_at': _utc_now(),
                    'last_reason': _optional_text(getattr(command, 'reason', None)) or 'agent move batch',
                    'state_path': str(_state_path(context, name)),
                    'events_path': str(_events_path(context)),
                }
            )
            if explicit_target_window is not None:
                payload['resolved_window_name'] = explicit_target_window
            _write_state(context, name, payload)
            payloads[name] = payload
        if explicit_target_window is None:
            for name in normalized:
                payload = payloads[name]
                _update_resolved_placement(context, payload)
                if _optional_text(payload.get('resolved_window_name')) is None:
                    raise ValueError(f'agent move --agents could not resolve target window for {name!r}')
                _write_state(context, name, payload)
        for name, payload in payloads.items():
            _append_event(
                context,
                {
                    'event': 'move',
                    'agent': name,
                    'previous_window_name': payload.get('previous_window_name'),
                    'target_window_name': payload.get('resolved_window_name')
                    or dict(payload.get('placement') or {}).get('window_name'),
                    'batch_agents': list(normalized),
                },
            )
        apply = _apply_reload_if_mounted(context, action='agent-move-batch')
        for name in normalized:
            payload = payloads[name]
            payload['apply'] = apply
            _update_move_apply_evidence(context, payload)
            _write_state(context, name, payload)
    except Exception:
        for name, previous in previous_by_name.items():
            _restore_state(context, name, previous)
        raise
    records = [_status_record(payloads[name], source='dynamic') for name in normalized]
    target_windows = tuple(
        dict.fromkeys(
            _optional_text(payloads[name].get('resolved_window_name'))
            or _optional_text(dict(payloads[name].get('placement') or {}).get('window_name'))
            or ''
            for name in normalized
        )
    )
    target_windows = tuple(name for name in target_windows if name)
    return {
        'agent_lifecycle_status': 'active',
        'action': 'move',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'agent_count': len(records),
        'moved_agents': list(normalized),
        'target_window_name': target_windows[0] if len(target_windows) == 1 else None,
        'target_window_names': list(target_windows),
        'apply': apply,
        'agents': records,
        'runtime_agents_root': str(_agents_root(context)),
    }


def _transition(context, command) -> dict[str, object]:
    action = str(getattr(command, 'action', '') or '').strip().lower()
    batch_names = tuple(str(item) for item in tuple(getattr(command, 'agent_names', ()) or ()))
    if batch_names:
        return _transition_many(context, command, batch_names)
    name = _normalize_name(getattr(command, 'agent_name', None), field_name='agent')
    state_path = _state_path(context, name)
    previous = _load_record(state_path)
    if previous is None:
        raise ValueError(f'agent {name!r} is not a dynamic agent')
    previous_state = str(previous.get('lifecycle_state') or '')
    if previous_state not in ACTIVE_STATES:
        raise ValueError(f'agent {name!r} is not active; current lifecycle_state={previous_state or "<empty>"}')
    next_state = _transition_next_state(action, command)
    payload = dict(previous)
    payload.update(
        {
            'agent_lifecycle_status': 'active',
            'requested_action': action,
            'previous_state': previous_state,
            'lifecycle_state': next_state,
            'visibility_state': 'visible' if next_state == 'visible' else 'hidden',
            'dispatch_disabled': next_state == 'parked',
            'reason': _optional_text(getattr(command, 'reason', None)),
            'updated_at': _utc_now(),
            'last_reason': _optional_text(getattr(command, 'reason', None)) or f'agent {action}',
            'state_path': str(state_path),
            'events_path': str(_events_path(context)),
        }
    )
    _write_state(context, name, payload)
    _append_event(context, {'event': action, 'agent': name, 'previous_state': previous_state, 'next_state': next_state})
    try:
        payload['apply'] = _apply_reload_if_mounted(context, action=f'agent-{action}')
        _update_transition_apply_evidence(context, payload)
    except Exception:
        _restore_state(context, name, previous)
        raise
    _write_state(context, name, payload)
    return dict(payload, action=action)


def _transition_many(context, command, names: tuple[str, ...]) -> dict[str, object]:
    action = str(getattr(command, 'action', '') or '').strip().lower()
    normalized = _normalize_agent_names(names, command=f'agent {action} --agents')
    next_state = _transition_next_state(action, command)
    previous_by_name: dict[str, dict[str, object]] = {}
    payloads: dict[str, dict[str, object]] = {}
    for name in normalized:
        previous = _load_record(_state_path(context, name))
        if previous is None:
            raise ValueError(f'agent {name!r} is not a dynamic agent')
        previous_state = str(previous.get('lifecycle_state') or '')
        if previous_state not in ACTIVE_STATES:
            raise ValueError(f'agent {name!r} is not active; current lifecycle_state={previous_state or "<empty>"}')
        previous_by_name[name] = previous
    try:
        for name in normalized:
            previous = previous_by_name[name]
            state_path = _state_path(context, name)
            previous_state = str(previous.get('lifecycle_state') or '')
            payload = dict(previous)
            payload.update(
                {
                    'agent_lifecycle_status': 'active',
                    'requested_action': action,
                    'previous_state': previous_state,
                    'lifecycle_state': next_state,
                    'visibility_state': 'visible' if next_state == 'visible' else 'hidden',
                    'dispatch_disabled': next_state == 'parked',
                    'reason': _optional_text(getattr(command, 'reason', None)),
                    'updated_at': _utc_now(),
                    'last_reason': _optional_text(getattr(command, 'reason', None)) or f'agent {action} batch',
                    'state_path': str(state_path),
                    'events_path': str(_events_path(context)),
                }
            )
            _write_state(context, name, payload)
            payloads[name] = payload
        for name in normalized:
            _append_event(
                context,
                {
                    'event': action,
                    'agent': name,
                    'previous_state': previous_by_name[name].get('lifecycle_state'),
                    'next_state': next_state,
                    'batch_agents': list(normalized),
                },
            )
        apply = _apply_reload_if_mounted(context, action=f'agent-{action}-batch')
        for name in normalized:
            payload = payloads[name]
            payload['apply'] = apply
            _update_transition_apply_evidence(context, payload)
            _write_state(context, name, payload)
    except Exception:
        for name, previous in previous_by_name.items():
            _restore_state(context, name, previous)
        raise
    records = [_status_record(payloads[name], source='dynamic') for name in normalized]
    return {
        'agent_lifecycle_status': 'active',
        'action': action,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'agent_count': len(records),
        'transitioned_agents': list(normalized),
        'lifecycle_state': next_state,
        'dispatch_disabled': next_state == 'parked',
        'apply': apply,
        'agents': records,
        'runtime_agents_root': str(_agents_root(context)),
    }


def _status_record(record: dict[str, object], *, source: str) -> dict[str, object]:
    placement = record.get('placement') if isinstance(record.get('placement'), dict) else {}
    payload = {
        'agent': record.get('agent'),
        'source': source,
        'role': record.get('role'),
        'provider': record.get('provider'),
        'profile': record.get('profile'),
        'role_class': record.get('role_class'),
        'lifecycle_state': record.get('lifecycle_state'),
        'visibility_state': record.get('visibility_state'),
        'resolved_window_name': record.get('resolved_window_name')
        or placement.get('window_name')
        or record.get('window_name'),
        'pane_id': _optional_text(record.get('pane_id')) or _optional_text(placement.get('pane_id')),
        'ask_target': record.get('ask_target') or record.get('agent'),
        'state_path': record.get('state_path'),
    }
    payload.update(_diagnostic_fields(record, source=source))
    return payload


def _diagnostic_fields(record: dict[str, object], *, source: str) -> dict[str, object]:
    placement = record.get('placement') if isinstance(record.get('placement'), dict) else {}
    apply_payload = normalize_apply_payload(record)
    dispatch_disabled = bool(record.get('dispatch_disabled'))
    return {
        'agent_kind': agent_kind(source),
        'ownership_class': ownership_class(source=source, lifetime=record.get('lifetime')),
        'dispatch_disabled': dispatch_disabled,
        'dispatch_state': dispatch_state(dispatch_disabled),
        'pane_identity_source': pane_identity_source(
            record=_optional_text(record.get('pane_id')) or _optional_text(placement.get('pane_id'))
        ),
        'apply_status': apply_payload.get('apply_status'),
        'apply_plan_class': apply_payload.get('plan_class'),
        'apply_stage': apply_payload.get('stage'),
        'failed_apply': failed_apply(apply_payload),
        'retained_busy': bool(record.get('retained_busy')),
    }


def _resolve_dynamic_spec(name: str, command, *, profile: LoopRoleProfileSpec | None) -> dict[str, object]:
    profile_name = _optional_text(getattr(command, 'profile', None))
    role = _optional_text(getattr(command, 'role', None))
    provider = _optional_text(getattr(command, 'provider', None))
    model = _optional_text(getattr(command, 'model', None))
    thinking = _optional_text(getattr(command, 'thinking', None))
    workspace_mode = _optional_text(getattr(command, 'workspace_mode', None))
    workspace_group = _optional_text(getattr(command, 'workspace_group', None))
    if profile is not None:
        role = _merge_value('role', role, profile.role)
        provider = _merge_value('provider', provider, profile.provider)
        model = _merge_value('model', model, profile.model)
        thinking = _merge_value('thinking', thinking, profile.thinking)
        workspace_mode = _merge_value('workspace_mode', workspace_mode, profile.workspace_mode.value)
        workspace_group = _merge_value('workspace_group', workspace_group, profile.workspace_group)
        startup_args = tuple(profile.startup_args)
        provider_profile = profile.provider_profile.to_record()
    else:
        startup_args = ()
        provider_profile = {}
    if role is None:
        raise ValueError('agent add requires --role or --profile')
    if provider is None:
        raise ValueError('agent add requires provider in name:provider, --provider, or --profile')
    try:
        role = normalize_role_id(role)
        load_installed_role_manifest(role)
    except RoleLookupError as exc:
        raise ValueError(str(exc)) from exc
    try:
        normalized_name = normalize_agent_name(name)
    except AgentValidationError as exc:
        raise ValueError(f'agent name is invalid: {exc}') from exc
    if normalized_name != name:
        raise ValueError(f'agent name normalized unexpectedly: {normalized_name}')
    if workspace_mode is not None:
        try:
            WorkspaceMode(workspace_mode)
        except ValueError as exc:
            raise ValueError(f'unsupported workspace_mode: {workspace_mode}') from exc
    return {
        'profile': profile_name,
        'role': role,
        'provider': provider,
        'model': model,
        'thinking': thinking,
        'workspace_mode': workspace_mode or WorkspaceMode.INPLACE.value,
        'workspace_group': workspace_group,
        'startup_args': startup_args,
        'provider_profile': provider_profile,
    }


def _merge_value(field: str, explicit: str | None, profile_value: object | None) -> str | None:
    value = _optional_text(profile_value)
    if explicit is None:
        return value
    if value is not None and explicit != value:
        raise ValueError(f'agent add {field} conflicts with profile value')
    return explicit


def _profile_spec(config, profile_name: object) -> LoopRoleProfileSpec | None:
    profile = _optional_text(profile_name)
    if profile is None:
        return None
    profiles = config.loop_capacity.role_profiles
    found = profiles.get(profile)
    if found is None:
        known = ', '.join(sorted(profiles)) or '<none>'
        raise ValueError(f'unknown agent profile {profile!r}; configured profiles: {known}')
    active_count = sum(
        1
        for record in _load_dynamic_records_from_project(config)
        if record.get('profile') == profile and str(record.get('lifecycle_state') or '') in ACTIVE_STATES
    )
    if active_count >= found.max_instances:
        raise ValueError(f'agent profile {profile} exceeds max_instances={found.max_instances}')
    return found


def _load_dynamic_records_from_project(config) -> tuple[dict[str, object], ...]:
    source_path = getattr(config, 'source_path', None)
    if not source_path:
        return ()
    project_root = Path(source_path).parent.parent
    agents_dir = project_root / '.ccb' / 'runtime' / 'agents'
    if not agents_dir.is_dir():
        return ()
    records = []
    for path in sorted(agents_dir.glob('*/lifecycle.json')):
        record = _load_record(path)
        if record is not None:
            records.append(record)
    return tuple(records)


def _resolve_remove_policy(policy: str, *, role_class: str) -> str:
    if policy != 'auto':
        return policy
    if role_class == 'short_lived_execution':
        return 'unload'
    if role_class == 'diagnostic':
        return 'unload'
    return 'park'


def _state_for_policy(policy: str) -> str:
    if policy == 'hide':
        return 'hidden'
    if policy == 'park':
        return 'parked'
    if policy in {'unload', 'kill'}:
        return 'unloaded'
    raise ValueError(f'unsupported resolved policy: {policy}')


def _release_gate(context, name: str) -> dict[str, object]:
    local = ping_local_state(context)
    if str(getattr(local, 'mount_state', '') or '') != 'mounted' or not bool(getattr(local, 'socket_connectable', False)):
        return {'retained': False, 'runtime_state': 'unmounted', 'queue_depth': 0}
    runtime = AgentRuntimeStore(context.paths).load_best_effort(name)
    runtime_state = _runtime_state_value(runtime)
    queue_depth = _safe_int(getattr(runtime, 'queue_depth', 0) if runtime is not None else 0)
    reason = ''
    if runtime_state in {'busy', 'starting', 'stopping'}:
        reason = f'runtime_state={runtime_state}'
    elif queue_depth > 0:
        reason = f'queue_depth={queue_depth}'
    return {
        'retained': bool(reason),
        'reason': reason,
        'runtime_state': runtime_state,
        'queue_depth': queue_depth,
    }


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


def _created_sequence(context, previous: dict[str, object] | None) -> int:
    previous_sequence = _optional_int((previous or {}).get('created_sequence'))
    if previous_sequence is not None:
        return previous_sequence
    highest = 0
    for record in _load_dynamic_records(context):
        sequence = _optional_int(record.get('created_sequence'))
        if sequence is not None:
            highest = max(highest, sequence)
    return highest + 1


def _next_placement_sequence(context, *, target_window: str | None, exclude_agent: str) -> int:
    return _next_placement_sequence_for_agents(context, target_window=target_window, exclude_agents={exclude_agent})


def _next_placement_sequence_for_agents(context, *, target_window: str | None, exclude_agents: set[str]) -> int:
    highest = 0
    for record in _load_dynamic_records(context):
        if str(record.get('agent') or '') in exclude_agents:
            continue
        if target_window is not None and _record_window_name(record) != target_window:
            continue
        sequence = _optional_int(record.get('placement_sequence'))
        if sequence is None:
            sequence = _optional_int(record.get('created_sequence'))
        if sequence is not None:
            highest = max(highest, sequence)
    return highest + 1


def _record_window_name(record: dict[str, object]) -> str | None:
    placement = record.get('placement') if isinstance(record.get('placement'), dict) else {}
    return (
        _optional_text(record.get('resolved_window_name'))
        or _optional_text(record.get('window_name'))
        or _optional_text(dict(placement).get('window_name'))
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _infer_role_class(role: object) -> str:
    text = str(role or '').lower()
    if any(token in text for token in ('ccb_frontdesk', 'ccb_planner')):
        return 'long_lived_interactive'
    if any(token in text for token in ('ccb_orchestrator', 'ccb_task_detailer', 'ccb_round_reviewer')):
        return 'short_lived_execution'
    if any(token in text for token in ('coder', 'worker', 'checker', 'reviewer')):
        return 'short_lived_execution'
    if any(token in text for token in ('frontdesk', 'frontend', 'planner', 'orchestrator', 'round_checker', 'broker')):
        return 'long_lived_interactive'
    if any(token in text for token in ('monitor', 'diagnostic', 'recovery')):
        return 'diagnostic'
    return 'unknown'


def _normalize_visibility(value: object, *, default: str) -> str:
    text = _optional_text(value) or default
    if text not in ACTIVE_STATES:
        raise ValueError(f'visibility must be one of: {", ".join(sorted(ACTIVE_STATES))}')
    return text


def _normalize_resume_visibility(value: object) -> str:
    text = _optional_text(value) or 'hidden'
    if text not in {'visible', 'hidden'}:
        raise ValueError('agent resume visibility must be one of: hidden, visible')
    return text


def _transition_next_state(action: str, command) -> str:
    if action == 'hide':
        return 'hidden'
    if action == 'park':
        return 'parked'
    if action == 'resume':
        return _normalize_resume_visibility(getattr(command, 'visibility', None))
    raise ValueError(f'unsupported agent lifecycle transition: {action}')


def _normalize_name(value: object, *, field_name: str) -> str:
    try:
        return normalize_agent_name(str(value or ''))
    except AgentValidationError as exc:
        raise ValueError(f'{field_name} is invalid: {exc}') from exc


def _normalize_agent_names(values: tuple[str, ...], *, command: str) -> tuple[str, ...]:
    names = tuple(_normalize_name(value, field_name='agent') for value in values)
    deduped = tuple(dict.fromkeys(names))
    if not deduped:
        raise ValueError(f'{command} requires at least one agent')
    if len(deduped) != len(names):
        raise ValueError(f'{command} cannot contain duplicate agent names')
    return deduped


def _batch_remove_apply_action(requested_action: str, next_state_by_name: dict[str, str]) -> str:
    if any(state == 'unloaded' for state in next_state_by_name.values()):
        return f'agent-{requested_action}-batch'
    states = set(next_state_by_name.values())
    if states == {'parked'}:
        return 'agent-park-batch'
    if states == {'hidden'}:
        return 'agent-hide-batch'
    return f'agent-{requested_action}-batch'


def _optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _placement_record(command) -> dict[str, object]:
    window_name = _optional_text(getattr(command, 'window_name', None))
    window_class = _optional_text(getattr(command, 'window_class', None))
    loop_id = _optional_text(getattr(command, 'loop_id', None))
    node_id = _optional_text(getattr(command, 'node_id', None))
    mode = 'auto'
    if window_name is not None:
        mode = 'window'
    elif loop_id is not None or node_id is not None:
        mode = 'execution_node'
    elif window_class is not None:
        mode = 'window_class'
    return {
        'mode': mode,
        'window_name': window_name,
        'window_class': window_class,
        'loop_id': loop_id,
        'node_id': node_id,
        'layout_policy': 'append-or-create-window',
    }


def _agents_root(context) -> Path:
    return Path(context.paths.runtime_state_root) / 'runtime' / 'agents'


def _state_dir(context, name: str) -> Path:
    return _agents_root(context) / name


def _state_path(context, name: str) -> Path:
    return _state_dir(context, name) / 'lifecycle.json'


def _events_path(context) -> Path:
    return _agents_root(context) / 'events.jsonl'


def _load_dynamic_records(context) -> tuple[dict[str, object], ...]:
    root = _agents_root(context)
    if not root.is_dir():
        return ()
    records = []
    for path in sorted(root.glob('*/lifecycle.json')):
        record = _load_record(path)
        if record is not None:
            record.setdefault('state_path', str(path))
            records.append(record)
    return tuple(records)


def _load_record(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        raise ValueError(f'{path}: expected JSON object')
    return dict(payload)


def _write_state(context, name: str, payload: dict[str, object]) -> None:
    _ensure_runtime_root(context)
    atomic_write_json(_state_path(context, name), payload)


def _restore_state(context, name: str, previous: dict[str, object] | None) -> None:
    if previous is None:
        try:
            _state_path(context, name).unlink()
        except FileNotFoundError:
            pass
        return
    _write_state(context, name, previous)


def _append_event(context, payload: dict[str, object]) -> None:
    _ensure_runtime_root(context)
    event = {
        'schema_version': 1,
        'record_type': 'ccb_dynamic_agent_event',
        'created_at': _utc_now(),
        'project_id': context.project.project_id,
        **payload,
    }
    path = _events_path(context)
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
        raise RuntimeError(_reload_failure_message(action, payload))
    return reload_apply_summary(payload, action=action)


def _reload_failure_message(action: str, payload: dict[str, object]) -> str:
    status = str(payload.get('status') or 'unknown')
    stage = _optional_text(payload.get('stage'))
    plan_class = _optional_text(payload.get('plan_class'))
    diagnostics = dict(payload.get('diagnostics') or {})
    namespace_patch = dict(payload.get('namespace_patch') or {})
    runtime_mount = dict(payload.get('runtime_mount') or {})
    namespace_diagnostics = dict(namespace_patch.get('diagnostics') or {})
    runtime_diagnostics = dict(runtime_mount.get('diagnostics') or {})
    reason = _optional_text(diagnostics.get('reason')) or _optional_text(namespace_diagnostics.get('reason')) or _optional_text(
        runtime_diagnostics.get('reason')
    )
    message = _optional_text(diagnostics.get('message')) or _optional_text(namespace_diagnostics.get('message')) or _optional_text(
        runtime_diagnostics.get('message')
    )
    error = _optional_text(namespace_diagnostics.get('error')) or _optional_text(runtime_diagnostics.get('error'))
    parts = [f'agent lifecycle {action} reload failed: {status}']
    if stage:
        parts.append(f'stage={stage}')
    if plan_class:
        parts.append(f'plan_class={plan_class}')
    if reason:
        parts.append(f'reason={reason}')
    if message:
        parts.append(f'message={message}')
    if error:
        parts.append(f'error={error}')
    return '; '.join(parts)


def _update_apply_evidence(context, payload: dict[str, object]) -> None:
    apply = dict(payload.get('apply') or {})
    if str(apply.get('apply_status') or '') != 'applied':
        return
    agent = str(payload.get('agent') or '')
    if str(payload.get('lifecycle_state') or '') == 'unloaded':
        _update_unload_apply_evidence(payload, agent=agent, apply=apply)
        return
    panes = dict(apply.get('namespace_agent_panes') or {})
    pane_id = _optional_text(panes.get(agent))
    window_name = _window_for_agent(context, agent)
    payload['pane_id'] = pane_id
    payload['applied'] = {
        'status': 'applied',
        'action': apply.get('action'),
        'plan_class': apply.get('plan_class'),
        'window_name': window_name,
        'pane_id': pane_id,
        'published_graph_version': apply.get('published_graph_version'),
        'runtime_mount_status': apply.get('runtime_mount_status'),
    }
    placement = dict(payload.get('placement') or {})
    if window_name is not None:
        placement['window_name'] = window_name
    if pane_id is not None:
        placement['pane_id'] = pane_id
    payload['placement'] = placement
    if window_name is not None:
        payload['resolved_window_name'] = window_name


def _update_unload_apply_evidence(payload: dict[str, object], *, agent: str, apply: dict[str, object]) -> None:
    removed_agents = dict(apply.get('namespace_removed_agents') or {})
    removed_pane_id = _optional_text(removed_agents.get(agent))
    previous_pane_id = _optional_text(payload.get('pane_id')) or removed_pane_id
    if previous_pane_id is not None:
        payload['last_pane_id'] = previous_pane_id
    payload['pane_id'] = None
    payload['applied'] = {
        'status': 'unloaded',
        'action': apply.get('action'),
        'plan_class': apply.get('plan_class'),
        'window_name': _optional_text(dict(payload.get('placement') or {}).get('window_name'))
        or _optional_text(payload.get('window_name')),
        'pane_id': None,
        'removed_pane_id': removed_pane_id,
        'published_graph_version': apply.get('published_graph_version'),
        'runtime_mount_status': apply.get('runtime_mount_status'),
        'unloaded_agents': list(apply.get('unloaded_agents') or ()),
        'runtime_authority_stopped_agents': list(apply.get('runtime_authority_stopped_agents') or ()),
    }
    placement = dict(payload.get('placement') or {})
    if previous_pane_id is not None:
        placement['last_pane_id'] = previous_pane_id
    placement['pane_id'] = None
    payload['placement'] = placement


def _update_transition_apply_evidence(context, payload: dict[str, object]) -> None:
    apply = dict(payload.get('apply') or {})
    if str(apply.get('apply_status') or '') != 'applied':
        return
    agent = str(payload.get('agent') or '')
    pane_id = _optional_text(payload.get('pane_id')) or _optional_text(dict(payload.get('placement') or {}).get('pane_id'))
    window_name = _window_for_agent(context, agent) or _optional_text(dict(payload.get('placement') or {}).get('window_name'))
    payload['applied'] = {
        'status': 'transitioned',
        'action': apply.get('action'),
        'plan_class': apply.get('plan_class'),
        'window_name': window_name,
        'pane_id': pane_id,
        'lifecycle_state': payload.get('lifecycle_state'),
        'dispatch_disabled': bool(payload.get('dispatch_disabled')),
        'published_graph_version': apply.get('published_graph_version'),
        'runtime_mount_status': apply.get('runtime_mount_status'),
    }
    placement = dict(payload.get('placement') or {})
    if window_name is not None:
        placement['window_name'] = window_name
    if pane_id is not None:
        placement['pane_id'] = pane_id
    payload['placement'] = placement
    if window_name is not None:
        payload['resolved_window_name'] = window_name


def _update_move_apply_evidence(context, payload: dict[str, object]) -> None:
    apply = dict(payload.get('apply') or {})
    if str(apply.get('apply_status') or '') != 'applied':
        return
    agent = str(payload.get('agent') or '')
    moved = dict(apply.get('namespace_moved_agents') or {})
    moved_windows = dict(apply.get('namespace_moved_agent_windows') or {})
    preserved_after = dict(apply.get('namespace_preserved_after') or {})
    pane_id = _optional_text(moved.get(agent)) or _optional_text(preserved_after.get(agent)) or _optional_text(payload.get('pane_id'))
    window_name = _optional_text(moved_windows.get(agent)) or _window_for_agent(context, agent) or _optional_text(
        dict(payload.get('placement') or {}).get('window_name')
    )
    payload['pane_id'] = pane_id
    payload['applied'] = {
        'status': 'moved' if str(apply.get('plan_class') or '') == 'move_agent' else 'transitioned',
        'action': apply.get('action'),
        'plan_class': apply.get('plan_class'),
        'previous_window_name': payload.get('previous_window_name'),
        'window_name': window_name,
        'pane_id': pane_id,
        'published_graph_version': apply.get('published_graph_version'),
        'runtime_mount_status': apply.get('runtime_mount_status'),
        'runtime_authority_moved_agents': list(apply.get('runtime_authority_moved_agents') or ()),
    }
    placement = dict(payload.get('placement') or {})
    if window_name is not None:
        placement['window_name'] = window_name
    if pane_id is not None:
        placement['pane_id'] = pane_id
    payload['placement'] = placement
    if window_name is not None:
        payload['resolved_window_name'] = window_name


def _window_for_agent(context, agent: str) -> str | None:
    try:
        config = load_project_config(context.project.project_root).config
    except Exception:
        return None
    for window in tuple(config.windows or ()):
        if agent in tuple(window.agent_names or ()):
            return str(window.name)
    return None


def _update_resolved_placement(context, payload: dict[str, object]) -> None:
    agent = str(payload.get('agent') or '')
    if not agent:
        return
    placement = dict(payload.get('placement') or {})
    if str(placement.get('mode') or 'auto') == 'auto':
        return
    window_name = _window_for_agent(context, agent)
    if window_name is None:
        return
    payload['resolved_window_name'] = window_name
    placement['window_name'] = window_name
    payload['placement'] = placement


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


__all__ = ['add_lifecycle_agents', 'agent_lifecycle']
