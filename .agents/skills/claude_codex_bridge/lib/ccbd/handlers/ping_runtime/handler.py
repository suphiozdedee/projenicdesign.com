from __future__ import annotations

from time import monotonic

from .payloads import build_agent_payload, build_ccbd_payload
from .summaries import (
    load_namespace_event_summary,
    load_namespace_summary,
    load_restore_summary,
    load_start_policy_summary,
)


def build_ping_handler(
    *,
    project_id: str,
    config,
    paths,
    registry,
    health_monitor,
    execution_state_store=None,
    execution_registry=None,
    restore_report_store=None,
    namespace_state_store=None,
    namespace_event_store=None,
    start_policy_store=None,
    metrics=None,
    serving_identity_getter=None,
):
    def handle(payload: dict) -> dict:
        started = monotonic()
        target = str(payload.get('target') or '').strip().lower()
        try:
            daemon_health = getattr(health_monitor, 'local_daemon_health', health_monitor.daemon_health)
            inspection = daemon_health()
            if target in {'', 'ccbd'}:
                execution_summary = execution_state_store.summary() if execution_state_store is not None else {}
                restore_summary = load_restore_summary(restore_report_store)
                namespace_summary = load_namespace_summary(namespace_state_store)
                namespace_event_summary = load_namespace_event_summary(namespace_event_store)
                start_policy_summary = load_start_policy_summary(start_policy_store)
                serving_identity = (
                    serving_identity_getter()
                    if callable(serving_identity_getter)
                    else {}
                )
                response = build_ccbd_payload(
                    project_id=project_id,
                    config=config,
                    paths=paths,
                    inspection=inspection,
                    execution_summary=execution_summary,
                    restore_summary=restore_summary,
                    namespace_summary=namespace_summary,
                    namespace_event_summary=namespace_event_summary,
                    start_policy_summary=start_policy_summary,
                    control_plane_metrics=metrics,
                    serving_identity=serving_identity,
                )
                bootstrap_nonce = _bootstrap_probe_nonce(payload)
                if bootstrap_nonce is not None:
                    response['bootstrap_probe_nonce'] = bootstrap_nonce
                return response
            if target == 'all':
                return {
                    'project_id': project_id,
                    'ccbd_state': str(getattr(inspection, 'phase', '') or '').strip()
                    or str(getattr(getattr(getattr(inspection, 'lease', None), 'mount_state', None), 'value', '') or 'unmounted'),
                    'agents': [
                        build_agent_payload(
                            project_id=project_id,
                            agent_name=name,
                            registry=registry,
                            inspection=inspection,
                            execution_registry=execution_registry,
                        )
                        for name in registry.list_known_agents()
                    ],
                }
            return build_agent_payload(
                project_id=project_id,
                agent_name=target,
                registry=registry,
                inspection=inspection,
                execution_registry=execution_registry,
            )
        finally:
            if metrics is not None:
                metrics.last_ping_duration_s = max(0.0, monotonic() - started)

    return handle


def _bootstrap_probe_nonce(payload: dict) -> str | None:
    value = str(payload.get('bootstrap_probe_nonce') or '').strip()
    if len(value) == 32 and all(character in '0123456789abcdef' for character in value):
        return value
    return None


__all__ = ['build_ping_handler']
