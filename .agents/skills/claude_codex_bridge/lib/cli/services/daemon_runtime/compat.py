from __future__ import annotations

import time

from agents.config_identity import project_config_identity_payload
from agents.config_loader import load_project_config
from ccbd.socket_client import CcbdClient, CcbdClientError
from cli.services.config_restart_intent import (
    config_restart_required_for_inspection,
)
from runtime_env.source_identity import (
    current_source_runtime_identity,
    source_runtime_identity_matches,
)

from .models import CcbdServiceError, DaemonHandle


def daemon_matches_project_config(context, client) -> bool:
    expected = project_config_identity_payload(
        load_project_config(context.project.project_root).config
    )
    payload = client.ping('ccbd')
    if not _payload_source_runtime_matches_current(payload):
        return False
    actual_signature = str(payload.get('config_signature') or '').strip()
    if actual_signature:
        if actual_signature == expected['config_signature']:
            return True
        # Config drift is a reload-pending state for the mounted project
        # daemon. Explicit `ccb reload` applies the new disk config without
        # forcing a daemon restart or interrupting existing agents.
        return True
    known_agents = payload.get('known_agents')
    if not isinstance(known_agents, list):
        return False
    actual_agents = tuple(
        str(item).strip().lower() for item in known_agents if str(item).strip()
    )
    return actual_agents == tuple(expected['known_agents'])


def inspection_matches_project_config(context, inspection) -> bool:
    if current_source_runtime_identity() is not None:
        return False
    try:
        expected = project_config_identity_payload(
            load_project_config(context.project.project_root).config
        )
    except Exception:
        return False
    actual_signature = _inspection_config_signature(inspection)
    return bool(actual_signature) and actual_signature == expected['config_signature']


def connect_compatible_daemon(
    context,
    inspection,
    *,
    restart_on_mismatch: bool,
    honor_config_restart_intent: bool = False,
    socket_path=None,
    probe_client_factory=CcbdClient,
    runtime_client_factory=None,
    daemon_matches_project_config_fn=daemon_matches_project_config,
    shutdown_incompatible_daemon_fn=None,
) -> DaemonHandle | None:
    if not inspection.socket_connectable:
        return None
    effective_socket_path = socket_path or context.paths.ccbd_socket_path
    runtime_client_factory = runtime_client_factory or probe_client_factory
    if (
        honor_config_restart_intent
        and restart_on_mismatch
        and config_restart_required_for_inspection(context, inspection)
    ):
        if shutdown_incompatible_daemon_fn is None:
            raise ValueError(
                'shutdown_incompatible_daemon_fn is required for a pending config restart'
            )
        shutdown_incompatible_daemon_fn(
            context,
            runtime_client_factory(effective_socket_path),
        )
        return None
    if inspection_matches_project_config(context, inspection):
        return DaemonHandle(
            client=runtime_client_factory(effective_socket_path),
            inspection=inspection,
            started=False,
        )
    probe_client = probe_client_factory(effective_socket_path)
    try:
        matches_config = daemon_matches_project_config_fn(context, probe_client)
    except CcbdClientError:
        if current_source_runtime_identity() is not None:
            if not restart_on_mismatch:
                return None
            if shutdown_incompatible_daemon_fn is None:
                raise ValueError(
                    'shutdown_incompatible_daemon_fn is required when restart_on_mismatch'
                )
            shutdown_incompatible_daemon_fn(
                context,
                runtime_client_factory(effective_socket_path),
            )
            return None
        # A transient ping failure is not evidence of config drift.
        return DaemonHandle(
            client=runtime_client_factory(effective_socket_path),
            inspection=inspection,
            started=False,
        )
    if matches_config:
        return DaemonHandle(
            client=runtime_client_factory(effective_socket_path),
            inspection=inspection,
            started=False,
        )
    if not restart_on_mismatch:
        return None
    if shutdown_incompatible_daemon_fn is None:
        raise ValueError('shutdown_incompatible_daemon_fn is required when restart_on_mismatch')
    shutdown_incompatible_daemon_fn(
        context,
        runtime_client_factory(effective_socket_path),
    )
    return None


def _inspection_config_signature(inspection) -> str:
    lifecycle_signature = str(
        getattr(getattr(inspection, 'lifecycle', None), 'config_signature', '') or ''
    ).strip()
    if lifecycle_signature:
        return lifecycle_signature
    return str(getattr(getattr(inspection, 'lease', None), 'config_signature', '') or '').strip()


def _payload_source_runtime_matches_current(payload: dict[str, object]) -> bool:
    expected = current_source_runtime_identity()
    if expected is None:
        return True
    return source_runtime_identity_matches(expected, payload.get('source_runtime_identity'))


def shutdown_incompatible_daemon(
    context,
    client,
    *,
    inspect_daemon_fn,
    incompatible_daemon_error: str,
    shutdown_timeout_s: float,
    unavailable_health_states,
) -> None:
    try:
        client.stop_all(force=False)
    except CcbdClientError:
        pass
    deadline = time.time() + shutdown_timeout_s
    while time.time() < deadline:
        _, _, inspection = inspect_daemon_fn(context)
        if (
            not inspection.socket_connectable
            or inspection.health in unavailable_health_states
        ):
            return
        time.sleep(0.05)
    raise CcbdServiceError(
        f'{incompatible_daemon_error}; old ccbd did not shut down in time'
    )


__all__ = [
    'connect_compatible_daemon',
    'daemon_matches_project_config',
    'inspection_matches_project_config',
    'shutdown_incompatible_daemon',
]
