from __future__ import annotations

from collections.abc import Mapping

from ccbd.services.runtime_recovery_policy import herdr_recovery_policy, normalized_runtime_health
from provider_runtime.session_payload import (
    namespace_restore_token_present,
    redacted_namespace_ref,
    redacted_restore_tokens,
)

from .recovery_context import RecoveryContext
from .store import SupervisionEvent


def append_recovery_event(
    ctx: RecoveryContext,
    *,
    event_kind: str,
    occurred_at: str,
    runtime,
    prior_health: str,
    result_health: str,
    details: dict[str, object] | None = None,
) -> None:
    public_details = _recovery_event_details(
        event_kind=event_kind,
        runtime=runtime,
        prior_health=prior_health,
        result_health=result_health,
        details=details,
    )
    ctx.event_store.append(
        SupervisionEvent(
            event_kind=event_kind,
            project_id=ctx.project_id,
            agent_name=ctx.agent_name,
            occurred_at=occurred_at,
            daemon_generation=runtime.daemon_generation,
            desired_state=runtime.desired_state,
            reconcile_state=runtime.reconcile_state,
            prior_health=prior_health,
            result_health=result_health,
            runtime_state=runtime.state.value,
            runtime_ref=runtime.runtime_ref,
            session_ref=runtime.session_ref,
            details=public_details,
        )
    )


def _recovery_event_details(
    *,
    event_kind: str,
    runtime,
    prior_health: str,
    result_health: str,
    details: dict[str, object] | None,
) -> dict[str, object]:
    redacted_details = redacted_restore_tokens(details or {})
    public_details = redacted_details if isinstance(redacted_details, dict) else {}
    ledger = _herdr_recovery_evidence_ledger(
        event_kind=event_kind,
        runtime=runtime,
        prior_health=prior_health,
        result_health=result_health,
        details=public_details,
    )
    if ledger is not None:
        public_details['recovery_evidence_ledger'] = ledger
    return public_details


def _herdr_recovery_evidence_ledger(
    *,
    event_kind: str,
    runtime,
    prior_health: str,
    result_health: str,
    details: Mapping[str, object],
) -> dict[str, object] | None:
    policy = herdr_recovery_policy(runtime)
    if policy is None:
        return None
    backend_ref = _runtime_backend_ref(runtime)
    namespace_ref = _runtime_namespace_ref(runtime, backend_ref=backend_ref)
    pane_ref = _runtime_pane_ref(runtime, backend_ref=backend_ref)
    restore_token_present = namespace_restore_token_present(namespace_ref) or bool(
        getattr(runtime, 'namespace_restore_token_present', False)
    )
    ledger: dict[str, object] = {
        'backend_impl': 'herdr',
        'owner': policy['owner'],
        'herdr_auto_restore_mode': policy['herdr_auto_restore_mode'],
        'probation_seconds': policy['probation_seconds'],
        'circuit_threshold': policy['circuit_threshold'],
        'restore_token_present': restore_token_present,
        'prior_health': prior_health,
        'result_health': result_health,
        'runtime_health': normalized_runtime_health(runtime) or result_health,
        'event_kind': event_kind,
        'action': _recovery_action(
            event_kind=event_kind,
            prior_health=prior_health,
            result_health=result_health,
            details=details,
        ),
        'reason': _recovery_reason(prior_health=prior_health, result_health=result_health, details=details),
    }
    redacted_namespace = redacted_namespace_ref(namespace_ref)
    if redacted_namespace is not None:
        ledger['namespace_ref'] = redacted_namespace
    if isinstance(pane_ref, Mapping):
        ledger['pane_ref'] = dict(pane_ref)
    agent_state_ref = str(getattr(runtime, 'herdr_agent_state_ref', '') or '').strip()
    if not agent_state_ref and isinstance(backend_ref, Mapping):
        agent_state_ref = str(backend_ref.get('herdr_agent_state_ref') or '').strip()
    if agent_state_ref:
        ledger['herdr_agent_state_ref'] = agent_state_ref
    return ledger


def _runtime_backend_ref(runtime) -> Mapping[str, object] | None:
    backend_ref = getattr(runtime, 'provider_runtime_backend_ref', None)
    return backend_ref if isinstance(backend_ref, Mapping) else None


def _runtime_namespace_ref(runtime, *, backend_ref: Mapping[str, object] | None):
    namespace_ref = getattr(runtime, 'namespace_ref', None)
    if isinstance(namespace_ref, Mapping):
        return namespace_ref
    if backend_ref is not None:
        candidate = backend_ref.get('namespace_ref')
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _runtime_pane_ref(runtime, *, backend_ref: Mapping[str, object] | None):
    pane_ref = getattr(runtime, 'pane_ref', None)
    if isinstance(pane_ref, Mapping):
        return pane_ref
    if backend_ref is not None:
        candidate = backend_ref.get('pane_ref')
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _recovery_action(
    *,
    event_kind: str,
    prior_health: str,
    result_health: str,
    details: Mapping[str, object],
) -> str:
    action = str(details.get('action') or '').strip()
    if action:
        return action
    if result_health == 'recovery-circuit-open':
        return 'circuit_open'
    if event_kind in {'recover_failed', 'recover_blocked'}:
        return 'blocked'
    if event_kind in {'recover_started', 'recover_probing', 'recover_succeeded'}:
        return _recovery_action_for_health(prior_health)
    return 'observe'


def _recovery_action_for_health(health: str) -> str:
    return {
        'process-dead': 'provider_restart',
        'namespace-crashed': 'namespace_recover',
        'daemon-unavailable': 'daemon_recover',
    }.get(str(health or '').strip().lower(), 'pane_recover')


def _recovery_reason(
    *,
    prior_health: str,
    result_health: str,
    details: Mapping[str, object],
) -> str:
    reason = str(details.get('reason') or '').strip()
    if reason:
        return reason
    return result_health or prior_health or 'unknown'


__all__ = ['append_recovery_event']
