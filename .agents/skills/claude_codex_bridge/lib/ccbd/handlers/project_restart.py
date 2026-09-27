from __future__ import annotations

from pathlib import Path

from agents.models import normalize_agent_name
from provider_core.registry import build_default_session_binding_map
from provider_core.session_binding_evidence_runtime.loading import (
    binding_search_roots,
    load_provider_session,
)
from rolepacks.runtime_lookup import load_installed_role, tree_digest
from rolepacks.sources import installed_role_metadata

from ccbd.services.start_policy import recovery_start_options


RESTART_PANES_REASON = 'manual_restart_panes'
RESTART_AGENT_REASON = 'manual_restart_agent'


def build_project_restart_agent_handler(app):
    def handle(payload: dict) -> dict:
        raw_name = str(payload.get('agent_name') or '').strip()
        if not raw_name:
            return _restart_failed_payload(
                app,
                agent_name='',
                reason='missing_agent',
                error='restart requires exactly one agent_name',
            )
        if raw_name.lower() == 'all':
            return _restart_failed_payload(
                app,
                agent_name='all',
                reason='restart_all_unsupported',
                error='restart all is not supported; restart exactly one configured agent',
            )
        agent_name = normalize_agent_name(raw_name)
        if agent_name not in set(_configured_agent_names(app)):
            return _restart_failed_payload(
                app,
                agent_name=agent_name,
                reason='unknown_agent',
                error=f'unknown restart target: {agent_name}',
            )

        busy_gate = _restart_busy_gate(app, agent_name=agent_name)
        blockers = tuple(busy_gate.get('blockers') or ())
        if blockers:
            return {
                'status': 'blocked',
                'restart_status': 'blocked',
                'agent_name': agent_name,
                'restartable_agents': list(_configured_agent_names(app)),
                'busy_gate': busy_gate,
                'blockers': list(blockers),
                'recreate_reason': RESTART_AGENT_REASON,
            }

        old_runtime = _runtime_evidence(app.registry.get(agent_name))
        try:
            lock = getattr(app, 'start_maintenance_lock', None)
            if lock is None:
                results = restart_project_agent_panes_in_place(app, agent_names=(agent_name,))
            else:
                with lock:
                    results = restart_project_agent_panes_in_place(app, agent_names=(agent_name,))
        except Exception as exc:
            return {
                'status': 'failed',
                'restart_status': 'failed',
                'agent_name': agent_name,
                'restartable_agents': list(_configured_agent_names(app)),
                'reason': 'restart_exception',
                'error': str(exc),
                'busy_gate': busy_gate,
                'old_runtime': old_runtime,
                'new_runtime': _runtime_evidence(app.registry.get(agent_name)),
                'recreate_reason': RESTART_AGENT_REASON,
            }

        result = dict(results[0]) if results else {'agent': agent_name, 'status': 'failed', 'reason': 'no_result'}
        restarted = str(result.get('status') or '') == 'restarted'
        deferred = str(result.get('status') or '') in {'deferred', 'unsupported'}
        if deferred:
            return {
                'status': 'unsupported',
                'restart_status': 'deferred',
                'agent_name': agent_name,
                'restartable_agents': list(_configured_agent_names(app)),
                'reason': str(result.get('reason') or 'deferred_to_provider_runtime_on_herdr'),
                'busy_gate': busy_gate,
                'old_runtime': old_runtime,
                'new_runtime': _runtime_evidence(app.registry.get(agent_name)),
                'result': result,
                'recreate_reason': RESTART_AGENT_REASON,
            }
        return {
            'status': 'ok' if restarted else 'failed',
            'restart_status': 'ok' if restarted else 'failed',
            'agent_name': agent_name,
            'restartable_agents': list(_configured_agent_names(app)),
            'reason': '' if restarted else str(result.get('reason') or result.get('status') or 'restart_failed'),
            'busy_gate': busy_gate,
            'old_runtime': old_runtime,
            'new_runtime': _runtime_evidence(app.registry.get(agent_name)),
            'result': result,
            'recreate_reason': RESTART_AGENT_REASON,
        }

    return handle


def build_project_restart_panes_handler(app):
    def handle(payload: dict) -> tuple[dict, object]:
        del payload
        agent_names = tuple(app.config.agents)
        namespace = _load_project_namespace_or_none(app)
        if _namespace_is_herdr(namespace):
            results = _herdr_restart_deferred_results(agent_names, namespace=namespace)
            restart_evidence = _herdr_restart_deferred_evidence(namespace, result_count=len(results))
            return {
                'status': 'unsupported',
                'restart_status': 'deferred',
                'agent_names': list(agent_names),
                'restart_mode': 'provider_runtime_required',
                'reason': 'deferred_to_provider_runtime_on_herdr',
                'backend_impl': 'herdr',
                'namespace_backend_family': getattr(namespace, 'namespace_backend_family', 'herdr-native'),
                'restart_evidence': restart_evidence,
                'results': list(results),
                'recreate_reason': RESTART_PANES_REASON,
            }, lambda: None

        def _after_response() -> None:
            try:
                with app.start_maintenance_lock:
                    restart_project_agent_panes_in_place(app, agent_names=agent_names)
            except Exception:
                # Keep ccbd alive; the supervision loop can repair failed panes later.
                return

        return {
            'status': 'scheduled',
            'agent_names': list(agent_names),
            'restart_mode': 'in_place',
            'recreate_reason': RESTART_PANES_REASON,
        }, _after_response

    return handle


def _restart_failed_payload(app, *, agent_name: str, reason: str, error: str) -> dict[str, object]:
    return {
        'status': 'failed',
        'restart_status': 'failed',
        'agent_name': agent_name,
        'restartable_agents': list(_configured_agent_names(app)),
        'reason': reason,
        'error': error,
    }


def _configured_agent_names(app) -> tuple[str, ...]:
    return tuple(str(name) for name in getattr(getattr(app, 'config', None), 'agents', {}) or {})


def _restart_busy_gate(app, *, agent_name: str) -> dict[str, object]:
    runtime = app.registry.get(agent_name)
    runtime_state = _runtime_state(runtime)
    runtime_queue_depth = _safe_int(getattr(runtime, 'queue_depth', 0) if runtime is not None else 0)
    queue_agent = _queue_agent_summary(app, agent_name=agent_name)
    queue_depth = _safe_int(queue_agent.get('queue_depth'))
    pending_reply_count = _safe_int(queue_agent.get('pending_reply_count'))
    active_inbound_event_id = _clean(queue_agent.get('active_inbound_event_id'))
    active_job_id = _active_job_id(app, agent_name=agent_name)
    callback_blockers = _pending_callback_blockers(app, agent_name=agent_name)

    blockers: list[dict[str, object]] = []
    if runtime_state in {'busy', 'starting', 'stopping'}:
        blockers.append({'reason': 'runtime_active', 'detail': f'state={runtime_state}'})
    if active_job_id:
        blockers.append({'reason': 'active_job', 'detail': f'job_id={active_job_id}'})
    if runtime_queue_depth > 0:
        blockers.append({'reason': 'runtime_queue_depth', 'detail': str(runtime_queue_depth)})
    if queue_depth > 0:
        blockers.append({'reason': 'queue_depth', 'detail': str(queue_depth)})
    if pending_reply_count > 0:
        blockers.append({'reason': 'pending_reply_delivery', 'detail': str(pending_reply_count)})
    if active_inbound_event_id:
        blockers.append({'reason': 'active_inbound_delivery', 'detail': active_inbound_event_id})
    blockers.extend(callback_blockers)

    return {
        'passed': not blockers,
        'runtime_state': runtime_state,
        'runtime_queue_depth': runtime_queue_depth,
        'queue_depth': queue_depth,
        'pending_reply_count': pending_reply_count,
        'active_job_id': active_job_id,
        'active_inbound_event_id': active_inbound_event_id,
        'pending_callback_count': len(callback_blockers),
        'blockers': blockers,
    }


def _queue_agent_summary(app, *, agent_name: str) -> dict[str, object]:
    dispatcher = getattr(app, 'dispatcher', None)
    queue = getattr(dispatcher, 'queue', None)
    if not callable(queue):
        return {}
    try:
        payload = queue(agent_name)
    except Exception:
        return {}
    agent = payload.get('agent') if isinstance(payload, dict) else None
    return dict(agent or {}) if isinstance(agent, dict) else {}


def _active_job_id(app, *, agent_name: str) -> str | None:
    state = getattr(getattr(app, 'dispatcher', None), '_state', None)
    active_job = getattr(state, 'active_job', None)
    if not callable(active_job):
        return None
    try:
        return _clean(active_job(agent_name))
    except Exception:
        return None


def _pending_callback_blockers(app, *, agent_name: str) -> tuple[dict[str, object], ...]:
    bureau = getattr(getattr(app, 'dispatcher', None), '_message_bureau', None)
    pending_callback_edges = getattr(bureau, 'pending_callback_edges', None)
    if not callable(pending_callback_edges):
        return ()
    blockers: list[dict[str, object]] = []
    try:
        edges = pending_callback_edges()
    except Exception:
        return ()
    for edge in edges:
        parent_agent = _clean(getattr(edge, 'parent_agent', None))
        callback_target = _clean(getattr(edge, 'callback_target_agent', None))
        if agent_name not in {parent_agent, callback_target}:
            continue
        state = getattr(getattr(edge, 'state', None), 'value', getattr(edge, 'state', None))
        edge_id = _clean(getattr(edge, 'edge_id', None)) or '<unknown>'
        detail = f'edge={edge_id} state={state}'
        child_job_id = _clean(getattr(edge, 'child_job_id', None))
        if child_job_id:
            detail += f' child_job={child_job_id}'
        blockers.append({'reason': 'pending_chain_continuation', 'detail': detail})
    return tuple(blockers)


def _runtime_evidence(runtime) -> dict[str, object]:
    if runtime is None:
        return {
            'state': 'missing',
            'health': 'missing',
            'pane_id': None,
            'active_pane_id': None,
            'runtime_ref': None,
            'session_ref': None,
            'runtime_pid': None,
            'restart_count': 0,
            'recovery_failure_count': 0,
        }
    return {
        'state': _runtime_state(runtime),
        'health': getattr(runtime, 'health', None),
        'pane_id': getattr(runtime, 'pane_id', None),
        'active_pane_id': getattr(runtime, 'active_pane_id', None),
        'runtime_ref': getattr(runtime, 'runtime_ref', None),
        'session_ref': getattr(runtime, 'session_ref', None),
        'runtime_pid': getattr(runtime, 'runtime_pid', None),
        'restart_count': _safe_int(getattr(runtime, 'restart_count', 0)),
        'recovery_failure_count': _safe_int(getattr(runtime, 'recovery_failure_count', 0)),
    }


def _runtime_state(runtime) -> str:
    if runtime is None:
        return 'missing'
    return str(getattr(getattr(runtime, 'state', None), 'value', getattr(runtime, 'state', 'unknown')) or 'unknown')


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _clean(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def restart_project_agent_panes_in_place(app, *, agent_names: tuple[str, ...]) -> tuple[dict[str, object], ...]:
    namespace = app.project_namespace.load()
    if namespace is None:
        raise RuntimeError('project namespace is not mounted')
    if _namespace_is_herdr(namespace):
        return _herdr_restart_deferred_results(agent_names, namespace=namespace)
    results: list[dict[str, object]] = []
    for agent_name in agent_names:
        results.append(_restart_agent_pane(app, agent_name=str(agent_name)))
    return tuple(results)


def _load_project_namespace_or_none(app):
    loader = getattr(getattr(app, 'project_namespace', None), 'load', None)
    if not callable(loader):
        return None
    try:
        return loader()
    except Exception:
        return None


def _namespace_is_herdr(namespace) -> bool:
    if namespace is None:
        return False
    return (
        str(getattr(namespace, 'backend_impl', '') or '').strip() == 'herdr'
        or str(getattr(namespace, 'namespace_backend_family', '') or '').strip() == 'herdr-native'
    )


def _herdr_restart_deferred_results(agent_names: tuple[str, ...], *, namespace) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            'agent': str(agent_name),
            'status': 'deferred',
            'reason': 'deferred_to_provider_runtime_on_herdr',
            'backend_impl': 'herdr',
            'namespace_backend_family': getattr(namespace, 'namespace_backend_family', 'herdr-native'),
            'restart_mode': 'provider_runtime_required',
            'restart_evidence': _herdr_restart_deferred_evidence(namespace, agent_name=str(agent_name)),
        }
        for agent_name in agent_names
    )


def _herdr_restart_deferred_evidence(
    namespace,
    *,
    agent_name: str | None = None,
    result_count: int | None = None,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        'backend_impl': 'herdr',
        'namespace_backend_family': getattr(namespace, 'namespace_backend_family', 'herdr-native'),
        'restart_surface': 'provider_runtime_required',
        'restart_status': 'deferred',
        'reason': 'deferred_to_provider_runtime_on_herdr',
        'respawn_evidence': 'not_attempted',
        'session_binding_evidence': 'not_attempted',
    }
    if agent_name is not None:
        evidence['agent'] = agent_name
    if result_count is not None:
        evidence['result_count'] = result_count
    return evidence


def _restart_agent_pane(app, *, agent_name: str) -> dict[str, object]:
    runtime = app.registry.get(agent_name)
    session = _load_agent_provider_session(app, agent_name=agent_name, runtime=runtime)
    pane_id = _restart_pane_id(runtime=runtime, session=session)
    if session is None:
        return {'agent': agent_name, 'status': 'skipped', 'reason': 'session_missing'}
    role_restart_block = _role_restart_blocked(session=session)
    if role_restart_block is not None:
        return {'agent': agent_name, **role_restart_block}
    if not pane_id:
        return {'agent': agent_name, 'status': 'skipped', 'reason': 'pane_missing'}
    supervisor = getattr(app, 'runtime_supervisor', None)
    start = getattr(supervisor, 'start', None)
    if not callable(start):
        return {
            'agent': agent_name,
            'status': 'failed',
            'reason': 'runtime_supervisor_missing',
            'pane_id': pane_id,
        }
    restore, auto_permission = _restart_start_options(app)
    try:
        summary = start(
            agent_names=(agent_name,),
            restore=restore,
            auto_permission=auto_permission,
            cleanup_tmux_orphans=False,
            interactive_tmux_layout=True,
            restart_agent_panes={agent_name: pane_id},
            recreate_reason=RESTART_AGENT_REASON,
        )
    except Exception as exc:
        return {
            'agent': agent_name,
            'status': 'failed',
            'reason': f'restart_start_failed: {exc}',
            'pane_id': pane_id,
        }
    result = _find_start_result(summary, agent_name)
    if result is None:
        return {
            'agent': agent_name,
            'status': 'failed',
            'reason': 'restart_start_result_missing',
            'pane_id': pane_id,
        }
    result_status = str(getattr(result, 'action', '') or '').strip().lower()
    result_health = str(getattr(result, 'health', '') or '').strip().lower()
    if result_status in {'failed', 'blocked', 'degraded'} or result_health in {'failed', 'degraded'}:
        return {
            'agent': agent_name,
            'status': 'failed',
            'reason': f'restart_start_{result_status or result_health or "unknown"}',
            'pane_id': pane_id,
        }
    refreshed = app.registry.get(agent_name)
    return {
        'agent': agent_name,
        'status': 'restarted',
        'pane_id': str(getattr(refreshed, 'pane_id', None) or pane_id),
        'action': str(getattr(result, 'action', '') or '').strip() or 'relaunched',
    }


def _restart_start_options(app) -> tuple[bool, bool]:
    store = getattr(app, 'start_policy_store', None)
    try:
        policy = store.load() if store is not None else None
    except Exception:
        policy = None
    if policy is None:
        # Match the normal `ccb start` defaults when no recovery policy exists.
        return True, True
    return recovery_start_options(policy)


def _find_start_result(summary, agent_name: str):
    for result in tuple(getattr(summary, 'agent_results', ()) or ()):
        if str(getattr(result, 'agent_name', '') or '').strip() == agent_name:
            return result
    return None


def _restart_pane_id(*, runtime, session) -> str | None:
    for candidate in (
        getattr(runtime, 'pane_id', None),
        getattr(runtime, 'active_pane_id', None),
        getattr(session, 'pane_id', None),
    ):
        text = str(candidate or '').strip()
        if text.startswith('%'):
            return text
    return None


def _load_agent_provider_session(app, *, agent_name: str, runtime):
    spec = app.config.agents.get(agent_name)
    if spec is None:
        return None
    provider = str(getattr(spec, 'provider', '') or '').strip().lower()
    if not provider:
        return None
    adapter = _session_binding_adapter(app, provider)
    if adapter is None:
        return None
    workspace_path = _workspace_path(app, agent_name=agent_name, runtime=runtime)
    return load_provider_session(
        adapter=adapter,
        provider=provider,
        agent_name=agent_name,
        roots=binding_search_roots(workspace_path=workspace_path, project_root=Path(app.project_root)),
        ensure_usable=False,
        session_is_usable_fn=lambda _session: True,
    )


def _session_binding_adapter(app, provider: str):
    bindings = getattr(getattr(app, 'runtime_service', None), '_session_bindings', None)
    if not isinstance(bindings, dict):
        bindings = build_default_session_binding_map(include_optional=True)
    return bindings.get(provider)


def _workspace_path(app, *, agent_name: str, runtime) -> Path:
    text = str(getattr(runtime, 'workspace_path', '') or '').strip()
    if text:
        return Path(text)
    try:
        return Path(app.paths.workspace_path(agent_name))
    except Exception:
        return Path(app.project_root)


def _role_restart_blocked(*, session) -> dict[str, str] | None:
    evidence = _session_role_evidence(session=session)
    if evidence is None:
        return None
    role = _load_installed_role_safe(evidence.get('id', ''))
    if role is None:
        return {
            'status': 'failed',
            'reason': 'role_not_installed',
            'detail': f'role_id={evidence.get("id")}',
        }
    current_digest = _installed_role_digest(role)
    launch_digest = evidence.get('digest') or ''
    if not launch_digest:
        return None
    if launch_digest != current_digest:
        return {
            'status': 'failed',
            'reason': 'role_digest_changed_fresh_restart_unsupported',
            'detail': (
                f'role_id={evidence.get("id")} '
                f'launch_version={evidence.get("version")} launch_digest={launch_digest} '
                f'current_version={role.version} current_digest={current_digest}'
            ),
        }
    return None


def _session_role_evidence(*, session) -> dict[str, str] | None:
    data = getattr(session, 'data', None)
    if not isinstance(data, dict):
        return None
    role_id = _clean(data.get('ccb_role_id'))
    if not role_id:
        return None
    return {
        'id': role_id,
        'version': _clean(data.get('ccb_role_version')) or '',
        'digest': _clean(data.get('ccb_role_digest')) or '',
    }


def _load_installed_role_safe(role_id: str):
    try:
        return load_installed_role(role_id)
    except Exception:
        return None


def _installed_role_digest(role) -> str:
    metadata = installed_role_metadata(role.id)
    digest = str(metadata.get('digest') or '').strip()
    if digest:
        return digest
    return f'sha256:{tree_digest(role.root)}'


__all__ = [
    'RESTART_AGENT_REASON',
    'RESTART_PANES_REASON',
    'build_project_restart_agent_handler',
    'build_project_restart_panes_handler',
    'restart_project_agent_panes_in_place',
]
