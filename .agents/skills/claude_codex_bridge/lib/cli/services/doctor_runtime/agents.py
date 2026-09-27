from __future__ import annotations

from pathlib import Path

from provider_execution.capabilities import execution_restore_capability

from ..provider_binding import binding_status

_MAILBOX_CONSISTENCY_FIELDS = (
    ('mailbox_state', lambda record: _mailbox_state_value(record)),
    ('queue_depth', lambda record: getattr(record, 'queue_depth', None)),
    ('pending_reply_count', lambda record: getattr(record, 'pending_reply_count', None)),
    ('active_inbound_event_id', lambda record: getattr(record, 'active_inbound_event_id', None)),
    ('head_inbound_event_id', lambda record: getattr(record, 'head_inbound_event_id', None)),
    ('head_event_type', lambda record: getattr(record, 'head_event_type', None)),
    ('head_status', lambda record: getattr(record, 'head_status', None)),
    ('head_message_id', lambda record: getattr(record, 'head_message_id', None)),
    ('head_attempt_id', lambda record: getattr(record, 'head_attempt_id', None)),
    ('head_payload_ref', lambda record: getattr(record, 'head_payload_ref', None)),
    ('last_inbound_started_at', lambda record: getattr(record, 'last_inbound_started_at', None)),
    ('last_inbound_finished_at', lambda record: getattr(record, 'last_inbound_finished_at', None)),
    ('lease_version', lambda record: getattr(record, 'lease_version', None)),
)


def agent_summaries(
    context,
    *,
    config,
    stores: dict[str, object],
    catalog,
    execution_registry,
    errors: list[str],
) -> list[dict]:
    agents: list[dict] = []
    mailbox_kernel = stores.get('mailbox_kernel')
    for agent_name, spec in sorted(config.agents.items()):
        runtime = stores['runtime'].load_best_effort(agent_name)
        mailbox, mailbox_error = mailbox_summary_for_agent(
            agent_name=agent_name,
            mailbox_store=stores['mailbox'],
            errors=errors,
        )
        mailbox_consistency = mailbox_summary_consistency_for_agent(
            agent_name=agent_name,
            mailbox=mailbox,
            mailbox_error=mailbox_error,
            mailbox_kernel=mailbox_kernel,
            errors=errors,
        )
        latest_snapshot = latest_snapshot_for_agent(
            context,
            agent_name=agent_name,
            runtime=runtime,
            snapshot_store=stores['snapshot'],
            errors=errors,
        )
        agents.append(
            agent_summary(
                context,
                agent_name=agent_name,
                spec=spec,
                runtime=runtime,
                mailbox=mailbox,
                mailbox_consistency=mailbox_consistency,
                latest_snapshot=latest_snapshot,
                catalog=catalog,
                execution_registry=execution_registry,
            )
        )
    return agents


def mailbox_summary_for_agent(
    *,
    agent_name: str,
    mailbox_store,
    errors: list[str],
) -> tuple[object | None, str | None]:
    try:
        return mailbox_store.load(agent_name), None
    except Exception as exc:
        detail = f'mailbox_store:{agent_name}:{exc}'
        errors.append(detail)
        return None, detail


def mailbox_summary_consistency_for_agent(
    *,
    agent_name: str,
    mailbox,
    mailbox_error: str | None,
    mailbox_kernel,
    errors: list[str],
) -> dict[str, object]:
    if mailbox_kernel is None:
        return {
            'status': None,
            'mismatches': (),
            'error': None,
            'projected': None,
        }
    try:
        projected = mailbox_kernel.project_mailbox_summary(
            agent_name,
            updated_at=_mailbox_projection_timestamp(mailbox),
            prior=mailbox,
        )
    except Exception as exc:
        detail = f'mailbox_projection:{agent_name}:{exc}'
        errors.append(detail)
        return {
            'status': 'error',
            'mismatches': ('projection_failed',),
            'error': detail,
            'projected': None,
        }
    projected_payload = _mailbox_projected_payload(projected)
    if mailbox_error is not None:
        return {
            'status': 'error',
            'mismatches': ('summary_unreadable',),
            'error': mailbox_error,
            'projected': projected_payload,
        }
    if mailbox is None:
        mismatches = ('summary_missing',) if _mailbox_projection_is_material(projected) else ()
        return {
            'status': 'mismatch' if mismatches else 'ok',
            'mismatches': mismatches,
            'error': None,
            'projected': projected_payload,
        }
    mismatches = tuple(
        field_name
        for field_name, getter in _MAILBOX_CONSISTENCY_FIELDS
        if getter(mailbox) != getter(projected)
    )
    return {
        'status': 'mismatch' if mismatches else 'ok',
        'mismatches': mismatches,
        'error': None,
        'projected': projected_payload,
    }


def latest_snapshot_for_agent(
    context,
    *,
    agent_name: str,
    runtime,
    snapshot_store,
    errors: list[str],
):
    if runtime is None:
        return None
    jobs_path = context.paths.job_store_path(agent_name)
    if not jobs_path.exists():
        return None
    from jobs.store import JobStore

    try:
        job_store = JobStore(context.paths)
        jobs = job_store.list_agent(agent_name)
    except Exception as exc:
        errors.append(f'job_store:{agent_name}:{exc}')
        return None
    if not jobs:
        return None
    return snapshot_store.load(jobs[-1].job_id)


def agent_summary(
    context,
    *,
    agent_name: str,
    spec,
    runtime,
    mailbox,
    mailbox_consistency,
    latest_snapshot,
    catalog,
    execution_registry,
) -> dict:
    workspace_path = resolved_workspace_path(context, agent_name=agent_name, runtime=runtime)
    runtime_ref = getattr(runtime, 'runtime_ref', None)
    session_ref = runtime_session_ref(runtime)
    manifest = catalog.resolve_completion_manifest(spec.provider, spec.runtime_mode)
    capability = execution_restore_capability(execution_registry.get(spec.provider), provider=spec.provider)
    switch = session_switch_summary(runtime, provider=spec.provider)
    return {
        'agent_name': agent_name,
        'provider': spec.provider,
        'runtime_mode': spec.runtime_mode.value,
        'workspace_path': workspace_path,
        'workspace_mode': spec.workspace_mode.value,
        'branch_name': None,
        'completion_family': manifest.completion_family.value,
        'completion_confidence': snapshot_confidence(latest_snapshot),
        'last_completion_reason': snapshot_reason(latest_snapshot),
        'queue_depth': getattr(runtime, 'queue_depth', 0) if runtime is not None else 0,
        'health': getattr(runtime, 'health', 'stopped') if runtime is not None else 'stopped',
        'runtime_ref': runtime_ref,
        'session_ref': session_ref,
        'backend_type': getattr(runtime, 'backend_type', spec.runtime_mode.value) if runtime is not None else spec.runtime_mode.value,
        'binding_status': binding_status(runtime_ref, session_ref, workspace_path),
        'binding_source': getattr(getattr(runtime, 'binding_source', None), 'value', 'provider-session') if runtime is not None else 'provider-session',
        'terminal': getattr(runtime, 'terminal_backend', None) if runtime is not None else None,
        'tmux_socket_name': getattr(runtime, 'tmux_socket_name', None) if runtime is not None else None,
        'tmux_socket_path': getattr(runtime, 'tmux_socket_path', None) if runtime is not None else None,
        'pane_id': getattr(runtime, 'pane_id', None) if runtime is not None else None,
        'active_pane_id': getattr(runtime, 'active_pane_id', None) if runtime is not None else None,
        'pane_title_marker': getattr(runtime, 'pane_title_marker', None) if runtime is not None else None,
        'pane_state': getattr(runtime, 'pane_state', None) if runtime is not None else None,
        'execution_resume_supported': capability['resume_supported'],
        'execution_restore_mode': capability['restore_mode'],
        'execution_restore_reason': capability['restore_reason'],
        'execution_restore_detail': capability['restore_detail'],
        'session_switch_state': switch.get('state'),
        'session_switch_reason': switch.get('reason'),
        'session_switch_committed': switch.get('committed'),
        'session_switch_candidate_id': switch.get('candidate_session_id'),
        'session_switch_candidate_path': switch.get('candidate_session_path'),
        'mailbox_summary_version': getattr(mailbox, 'summary_version', None) if mailbox is not None else None,
        'mailbox_summary_source': getattr(mailbox, 'summary_source', None) if mailbox is not None else None,
        'mailbox_summary_refreshed_at': getattr(mailbox, 'summary_refreshed_at', None) if mailbox is not None else None,
        'mailbox_state': getattr(getattr(mailbox, 'mailbox_state', None), 'value', None) if mailbox is not None else None,
        'mailbox_queue_depth': getattr(mailbox, 'queue_depth', None) if mailbox is not None else None,
        'mailbox_pending_reply_count': getattr(mailbox, 'pending_reply_count', None) if mailbox is not None else None,
        'mailbox_active_inbound_event_id': getattr(mailbox, 'active_inbound_event_id', None) if mailbox is not None else None,
        'mailbox_head_inbound_event_id': getattr(mailbox, 'head_inbound_event_id', None) if mailbox is not None else None,
        'mailbox_head_event_type': getattr(mailbox, 'head_event_type', None) if mailbox is not None else None,
        'mailbox_head_status': getattr(mailbox, 'head_status', None) if mailbox is not None else None,
        'mailbox_consistency_status': mailbox_consistency.get('status'),
        'mailbox_consistency_mismatches': tuple(mailbox_consistency.get('mismatches') or ()),
        'mailbox_consistency_error': mailbox_consistency.get('error'),
        'mailbox_consistency_projected': mailbox_consistency.get('projected'),
    }


def resolved_workspace_path(context, *, agent_name: str, runtime) -> str:
    if runtime is not None and runtime.workspace_path:
        return runtime.workspace_path
    return str(context.paths.workspace_path(agent_name))


def runtime_session_ref(runtime) -> str | None:
    if runtime is None:
        return None
    return runtime.session_id or runtime.session_ref or runtime.session_file


def snapshot_confidence(latest_snapshot):
    if latest_snapshot is None or latest_snapshot.latest_decision.confidence is None:
        return None
    return latest_snapshot.latest_decision.confidence.value


def snapshot_reason(latest_snapshot):
    if latest_snapshot is None:
        return None
    return latest_snapshot.latest_decision.reason


def session_switch_summary(runtime, *, provider: str) -> dict[str, object]:
    if runtime is None or str(provider or '').strip().lower() != 'codex':
        return {}
    runtime_root = str(getattr(runtime, 'runtime_root', None) or '').strip()
    if not runtime_root:
        return {}
    try:
        from provider_backends.codex.session_switch.diagnostics import read_diagnostics

        record = read_diagnostics(Path(runtime_root))
    except Exception:
        return {}
    if not record:
        return {}
    candidate = record.get('candidate')
    candidate_id = None
    candidate_path = None
    if isinstance(candidate, dict):
        candidate_id = candidate.get('session_id')
        candidate_path = candidate.get('session_path')
    return {
        'state': record.get('state'),
        'reason': record.get('reason'),
        'committed': record.get('committed'),
        'candidate_session_id': candidate_id,
        'candidate_session_path': candidate_path,
    }


def _mailbox_projection_timestamp(mailbox) -> str:
    return (
        getattr(mailbox, 'summary_refreshed_at', None)
        or getattr(mailbox, 'updated_at', None)
        or '1970-01-01T00:00:00Z'
    )


def _mailbox_state_value(record) -> str | None:
    return getattr(getattr(record, 'mailbox_state', None), 'value', None)


def _mailbox_projection_is_material(record) -> bool:
    if record is None:
        return False
    return any(
        (
            getattr(record, 'queue_depth', 0),
            getattr(record, 'pending_reply_count', 0),
            getattr(record, 'active_inbound_event_id', None),
            getattr(record, 'head_inbound_event_id', None),
            getattr(record, 'last_inbound_started_at', None),
            getattr(record, 'last_inbound_finished_at', None),
            getattr(record, 'lease_version', 0),
            _mailbox_state_value(record) not in (None, 'idle'),
        )
    )


def _mailbox_projected_payload(record) -> dict[str, object] | None:
    if record is None:
        return None
    return {
        'mailbox_state': _mailbox_state_value(record),
        'queue_depth': getattr(record, 'queue_depth', None),
        'pending_reply_count': getattr(record, 'pending_reply_count', None),
        'active_inbound_event_id': getattr(record, 'active_inbound_event_id', None),
        'head_inbound_event_id': getattr(record, 'head_inbound_event_id', None),
        'head_event_type': getattr(record, 'head_event_type', None),
        'head_status': getattr(record, 'head_status', None),
        'head_message_id': getattr(record, 'head_message_id', None),
        'head_attempt_id': getattr(record, 'head_attempt_id', None),
        'head_payload_ref': getattr(record, 'head_payload_ref', None),
        'last_inbound_started_at': getattr(record, 'last_inbound_started_at', None),
        'last_inbound_finished_at': getattr(record, 'last_inbound_finished_at', None),
        'lease_version': getattr(record, 'lease_version', None),
    }


__all__ = ['agent_summaries']
