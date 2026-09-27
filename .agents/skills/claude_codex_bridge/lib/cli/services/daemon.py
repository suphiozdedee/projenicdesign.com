from __future__ import annotations

import os
from pathlib import Path

from ccbd.keeper import KeeperStateStore
from ccbd.models import LeaseHealth, MountState
from ccbd.services.mount import MountManager
from ccbd.services.ownership import OwnershipGuard
from ccbd.services.project_inspection import load_project_daemon_inspection
from ccbd.socket_client import CcbdClient, CcbdClientError
from cli.kill_runtime.processes import is_pid_alive, kill_pid, terminate_pid_tree
from cli.context import CliContext
from storage.path_helpers import socket_placement_payload
from .daemon_runtime import (
    CcbdServiceError,
    DaemonHandle,
    KillSummary,
    LocalPingSummary,
)
from .daemon_runtime.compat import (
    connect_compatible_daemon as _connect_compatible_daemon_runtime_impl,
)
from .daemon_runtime.compat import (
    daemon_matches_project_config as _daemon_matches_project_config,
)
from .daemon_runtime.compat import (
    shutdown_incompatible_daemon as _shutdown_incompatible_daemon_runtime_impl,
)
from .daemon_runtime.keeper import clear_shutdown_intent
from .daemon_runtime.keeper import ensure_keeper_started as _ensure_keeper_started_runtime_impl
from .daemon_runtime.keeper import finalize_shutdown_lifecycle as _finalize_shutdown_lifecycle_runtime_impl
from .daemon_runtime.keeper import keeper_pid as _keeper_pid_runtime_impl
from .daemon_runtime.keeper import record_running_intent as _record_running_intent_runtime_impl
from .daemon_runtime.keeper import record_shutdown_intent
from .daemon_runtime.keeper import wait_for_keeper_exit as _wait_for_keeper_exit_runtime_impl
from .daemon_runtime.facade import incompatible_daemon_error as _incompatible_daemon_error_impl
from .daemon_runtime.facade import should_restart_unreachable_daemon as _should_restart_unreachable_daemon
from .daemon_runtime.policy import CONTROL_PLANE_RPC_TIMEOUT_S
from .daemon_runtime.processes import lease_pid as _lease_pid
from .daemon_runtime.processes import restart_unreachable_daemon as _restart_unreachable_daemon_runtime_impl
from .daemon_runtime.processes import wait_for_pid_exit as _wait_for_pid_exit
from .daemon_runtime import connect_mounted_daemon as _connect_mounted_daemon_runtime
from .daemon_runtime import ensure_daemon_started as _ensure_daemon_started_runtime
from .daemon_runtime import shutdown_daemon as _shutdown_daemon_runtime

from .daemon_runtime.facade import SHUTDOWN_TIMEOUT_S as _DEF_SHUTDOWN_TIMEOUT_S
from .daemon_runtime.facade import STARTUP_PROGRESS_STALL_TIMEOUT_S as _DEF_STARTUP_PROGRESS_STALL_TIMEOUT_S
from .daemon_runtime.facade import START_TIMEOUT_S as _DEF_START_TIMEOUT_S


def inspect_daemon(
    context: CliContext,
    *,
    assume_mounted_socket_connectable: bool = False,
):
    manager = MountManager(context.paths)
    guard = OwnershipGuard(context.paths, manager)
    lease_inspection = guard.inspect(
        assume_mounted_socket_connectable=assume_mounted_socket_connectable,
    )
    return manager, guard, load_project_daemon_inspection(
        context.project.project_id,
        lease_inspection=lease_inspection,
        lifecycle_store=contextual_lifecycle_store(context),
        occurred_at=contextual_now(),
    )


def ensure_daemon_started(context: CliContext) -> DaemonHandle:
    try:
        return _ensure_daemon_started_runtime(
            context,
            clear_shutdown_intent_fn=clear_shutdown_intent,
            record_running_intent_fn=_record_running_intent,
            ensure_keeper_started_fn=_ensure_keeper_started,
            inspect_daemon_fn=inspect_daemon,
            connect_compatible_daemon_fn=_connect_start_compatible_daemon,
            should_restart_unreachable_daemon_fn=_should_restart_unreachable_daemon,
            restart_unreachable_daemon_fn=_restart_unreachable_daemon,
            incompatible_daemon_error_fn=_incompatible_daemon_error,
            start_timeout_s=_DEF_START_TIMEOUT_S,
            progress_stall_timeout_s=_DEF_STARTUP_PROGRESS_STALL_TIMEOUT_S,
        )
    except CcbdServiceError as exc:
        raise _augment_start_failure(context, exc) from exc


def connect_mounted_daemon(context: CliContext, *, allow_restart_stale: bool) -> DaemonHandle:
    return _connect_mounted_daemon_runtime(
        context,
        allow_restart_stale=allow_restart_stale,
        inspect_daemon_fn=inspect_daemon,
        connect_compatible_daemon_fn=_connect_compatible_daemon,
        ensure_daemon_started_fn=ensure_daemon_started,
        should_restart_unreachable_daemon_fn=_should_restart_unreachable_daemon,
        incompatible_daemon_error_fn=_incompatible_daemon_error,
    )


def connect_current_mounted_daemon(context: CliContext) -> DaemonHandle:
    _manager, _guard, inspection = inspect_daemon(context)
    phase = _current_daemon_phase(inspection)
    if phase != 'mounted':
        if phase == 'unmounted':
            raise CcbdServiceError('project ccbd is unmounted; run `ccb` first')
        if phase == 'starting':
            raise CcbdServiceError('project ccbd is starting; wait for keeper to finish startup')
        if phase == 'stopping':
            raise CcbdServiceError('project ccbd is stopping; wait for shutdown to finish')
        raise CcbdServiceError(f'ccbd is unavailable: {getattr(inspection, "reason", "unknown")}')
    if not getattr(inspection, 'socket_connectable', False):
        raise CcbdServiceError(f'ccbd is unavailable: {getattr(inspection, "reason", "socket_unreachable")}')
    return DaemonHandle(
        client=_build_control_plane_client(context.paths.ccbd_socket_path),
        inspection=inspection,
        started=False,
    )


def connect_managed_caller_daemon(context: CliContext) -> DaemonHandle:
    if not _is_managed_caller_context(context):
        raise CcbdServiceError('managed caller daemon connection requires an active agent runtime')
    _manager, _guard, inspection = inspect_daemon(
        context,
        assume_mounted_socket_connectable=True,
    )
    phase = _current_daemon_phase(inspection)
    if phase != 'mounted':
        raise _current_daemon_unavailable(inspection, phase=phase)
    if str(getattr(inspection, 'desired_state', '') or '').strip() != 'running':
        raise CcbdServiceError('project ccbd is stopping; managed agent commands cannot restart it')
    lease_socket = _validate_managed_caller_lease(context, inspection)
    handle = _connect_compatible_daemon(
        context,
        inspection,
        restart_on_mismatch=False,
        socket_path=lease_socket,
    )
    if handle is None:
        raise CcbdServiceError(_incompatible_daemon_error())
    return handle


def _current_daemon_phase(inspection) -> str:
    phase = str(getattr(inspection, 'phase', '') or '').strip()
    if phase:
        return phase
    health = getattr(inspection, 'health', None)
    if health is LeaseHealth.HEALTHY:
        return 'mounted'
    if health in {LeaseHealth.MISSING, LeaseHealth.UNMOUNTED}:
        return 'unmounted'
    return 'failed'


def invoke_mounted_daemon(
    context: CliContext,
    *,
    allow_restart_stale: bool,
    request_fn,
):
    managed_caller = _is_managed_caller_context(context)
    connect = connect_managed_caller_daemon if managed_caller else (
        lambda current: connect_mounted_daemon(
            current,
            allow_restart_stale=allow_restart_stale,
        )
    )
    handle = connect(context)
    assert handle.client is not None
    try:
        return request_fn(handle.client)
    except CcbdClientError as exc:
        normalized = _normalize_request_failure(context)
        if normalized is not None:
            raise normalized from exc
        handle = connect(context)
        assert handle.client is not None
        return request_fn(handle.client)


def _is_managed_caller_context(context: CliContext) -> bool:
    if str(getattr(context.project, 'source', '') or '') != 'caller-runtime':
        return False
    project_root = _resolved_path(context.project.project_root)
    caller_root = _resolved_env_path('CCB_CALLER_PROJECT_ROOT')
    if caller_root is None or caller_root != project_root:
        return False
    caller_project_id = str(os.environ.get('CCB_CALLER_PROJECT_ID') or '').strip()
    if not caller_project_id or caller_project_id != str(context.project.project_id):
        return False
    runtime_dir = _resolved_env_path('CCB_CALLER_RUNTIME_DIR')
    if runtime_dir is None:
        return False
    agents_dir = _resolved_path(context.paths.agents_dir)
    try:
        relative = runtime_dir.relative_to(agents_dir)
    except ValueError:
        return False
    if len(relative.parts) < 2:
        return False
    actor = str(os.environ.get('CCB_CALLER_ACTOR') or '').strip().lower()
    return bool(actor) and relative.parts[0].lower() == actor


def _resolved_env_path(name: str):
    raw = str(os.environ.get(name) or '').strip()
    return _resolved_path(raw) if raw else None


def _resolved_path(value):
    current = Path(value).expanduser()
    try:
        return current.resolve()
    except Exception:
        return current.absolute()


def _current_daemon_unavailable(inspection, *, phase: str) -> CcbdServiceError:
    if phase == 'unmounted':
        return CcbdServiceError('project ccbd is unmounted; managed agent commands cannot start it')
    if phase == 'starting':
        return CcbdServiceError('project ccbd is starting; wait for keeper to finish startup')
    if phase == 'stopping':
        return CcbdServiceError('project ccbd is stopping; managed agent commands cannot restart it')
    return CcbdServiceError(f'ccbd is unavailable: {getattr(inspection, "reason", "unknown")}')


def _validate_managed_caller_lease(context: CliContext, inspection) -> Path:
    lease = getattr(inspection, 'lease', None)
    if lease is None:
        raise CcbdServiceError('ccbd is unavailable: managed caller lease is missing')
    if str(getattr(lease, 'project_id', '') or '') != str(context.project.project_id):
        raise CcbdServiceError('ccbd is unavailable: managed caller lease project mismatch')
    if getattr(lease, 'mount_state', None) is not MountState.MOUNTED:
        raise CcbdServiceError('ccbd is unavailable: managed caller lease is not mounted')
    raw_socket = str(getattr(lease, 'socket_path', '') or '').strip()
    if not raw_socket:
        raise CcbdServiceError('ccbd is unavailable: managed caller lease socket is missing')
    return _resolved_path(raw_socket)


def ping_local_state(context: CliContext) -> LocalPingSummary:
    _, _, inspection = inspect_daemon(context)
    socket_placement = context.paths.ccbd_socket_placement
    tmux_socket_placement = context.paths.ccbd_tmux_socket_placement
    socket_payload = socket_placement_payload(socket_placement)
    tmux_socket_payload = socket_placement_payload(tmux_socket_placement, prefix='tmux')
    return LocalPingSummary(
        project_id=context.project.project_id,
        mount_state=inspection.phase,
        desired_state=inspection.desired_state,
        health=inspection.health.value,
        generation=inspection.generation,
        project_anchor_path=str(context.paths.ccb_dir),
        runtime_state_root=str(context.paths.runtime_state_root),
        runtime_root_kind=context.paths.runtime_state_placement.root_kind,
        runtime_relocation_reason=context.paths.runtime_state_placement.relocation_reason,
        runtime_filesystem_hint=context.paths.runtime_state_placement.filesystem_hint,
        runtime_marker_status=context.paths.runtime_marker_status,
        socket_path=inspection.socket_path,
        preferred_socket_path=socket_payload['preferred_socket_path'],
        effective_socket_path=socket_payload['effective_socket_path'],
        socket_root_kind=socket_payload['socket_root_kind'],
        socket_fallback_reason=socket_payload['socket_fallback_reason'],
        socket_filesystem_hint=socket_payload['socket_filesystem_hint'],
        tmux_socket_path=tmux_socket_payload['tmux_effective_socket_path'],
        tmux_preferred_socket_path=tmux_socket_payload['tmux_preferred_socket_path'],
        tmux_effective_socket_path=tmux_socket_payload['tmux_effective_socket_path'],
        tmux_socket_root_kind=tmux_socket_payload['tmux_socket_root_kind'],
        tmux_socket_fallback_reason=tmux_socket_payload['tmux_socket_fallback_reason'],
        tmux_socket_filesystem_hint=tmux_socket_payload['tmux_socket_filesystem_hint'],
        last_heartbeat_at=inspection.lease.last_heartbeat_at if inspection.lease else None,
        pid_alive=inspection.pid_alive,
        socket_connectable=inspection.socket_connectable,
        heartbeat_fresh=inspection.heartbeat_fresh,
        takeover_allowed=inspection.takeover_allowed,
        reason=inspection.reason,
        ccbd_pid=inspection.lease.ccbd_pid if inspection.lease else None,
        keeper_pid=inspection.lease.keeper_pid if inspection.lease else None,
        startup_id=getattr(inspection, 'startup_id', None),
        startup_stage=getattr(inspection, 'startup_stage', None),
        last_progress_at=getattr(inspection, 'last_progress_at', None),
        startup_deadline_at=getattr(inspection, 'startup_deadline_at', None),
        last_failure_reason=inspection.last_failure_reason,
        shutdown_intent=inspection.shutdown_intent,
    )


def refresh_agent_health(context: CliContext) -> None:
    try:
        handle = connect_mounted_daemon(context, allow_restart_stale=False)
    except CcbdServiceError:
        return
    client = handle.client
    if client is None:
        return
    try:
        client.ping('all')
    except CcbdClientError:
        return


def shutdown_daemon(context: CliContext, *, force: bool) -> KillSummary:
    return _shutdown_daemon_runtime(
        context,
        force=force,
        record_shutdown_intent_fn=record_shutdown_intent,
        finalize_shutdown_lifecycle_fn=_finalize_shutdown_lifecycle,
        inspect_daemon_fn=inspect_daemon,
        client_factory=lambda current: _build_control_plane_client(current.paths.ccbd_socket_path),
        lease_pid_fn=_lease_pid,
        keeper_pid_fn=_keeper_pid,
        wait_for_pid_exit_fn=_wait_for_pid_exit,
        wait_for_keeper_exit_fn=_wait_for_keeper_exit,
        is_pid_alive_fn=is_pid_alive,
        terminate_pid_tree_fn=terminate_pid_tree,
        shutdown_timeout_s=_DEF_SHUTDOWN_TIMEOUT_S,
    )


def _connect_compatible_daemon(
    context: CliContext,
    inspection,
    *,
    restart_on_mismatch: bool,
    honor_config_restart_intent: bool = False,
    socket_path=None,
) -> DaemonHandle | None:
    return _connect_compatible_daemon_runtime_impl(
        context,
        inspection,
        restart_on_mismatch=restart_on_mismatch,
        honor_config_restart_intent=honor_config_restart_intent,
        socket_path=socket_path,
        probe_client_factory=_build_probe_control_plane_client,
        runtime_client_factory=_build_control_plane_client,
        daemon_matches_project_config_fn=_daemon_matches_project_config,
        shutdown_incompatible_daemon_fn=_shutdown_incompatible_daemon,
    )


def _connect_start_compatible_daemon(
    context: CliContext,
    inspection,
    *,
    restart_on_mismatch: bool,
) -> DaemonHandle | None:
    return _connect_compatible_daemon(
        context,
        inspection,
        restart_on_mismatch=restart_on_mismatch,
        honor_config_restart_intent=True,
    )


def _build_control_plane_client(socket_path):
    return CcbdClient(socket_path)


def _build_probe_control_plane_client(socket_path):
    try:
        return CcbdClient(socket_path, timeout_s=CONTROL_PLANE_RPC_TIMEOUT_S)
    except TypeError:
        # Some tests still patch legacy single-argument constructors.
        return CcbdClient(socket_path)


def _shutdown_incompatible_daemon(context: CliContext, client: CcbdClient) -> None:
    _shutdown_incompatible_daemon_runtime_impl(
        context,
        client,
        inspect_daemon_fn=inspect_daemon,
        incompatible_daemon_error=_incompatible_daemon_error(),
        shutdown_timeout_s=_DEF_SHUTDOWN_TIMEOUT_S,
        unavailable_health_states={
            LeaseHealth.MISSING,
            LeaseHealth.UNMOUNTED,
            LeaseHealth.STALE,
        },
    )


def _incompatible_daemon_error() -> str:
    return _incompatible_daemon_error_impl()


def _ensure_keeper_started(context: CliContext) -> bool:
    return _ensure_keeper_started_runtime_impl(
        context,
        mount_manager_factory=MountManager,
        ownership_guard_factory=OwnershipGuard,
        process_exists_fn=is_pid_alive,
        ready_timeout_s=2.0,
    )


def _record_running_intent(context: CliContext) -> None:
    return _record_running_intent_runtime_impl(context)


def _wait_for_keeper_exit(context: CliContext, *, timeout_s: float) -> bool:
    return _wait_for_keeper_exit_runtime_impl(
        context,
        timeout_s=timeout_s,
        process_exists_fn=is_pid_alive,
    )


def _keeper_pid(context: CliContext, lease) -> int:
    return _keeper_pid_runtime_impl(
        context,
        lease,
        process_exists_fn=is_pid_alive,
    )


def _finalize_shutdown_lifecycle(context: CliContext) -> None:
    _finalize_shutdown_lifecycle_runtime_impl(context)


def _restart_unreachable_daemon(context: CliContext, inspection) -> None:
    _restart_unreachable_daemon_runtime_impl(
        context,
        inspection,
        shutdown_timeout_s=_DEF_SHUTDOWN_TIMEOUT_S,
        inspect_daemon_fn=inspect_daemon,
        manager_factory=MountManager,
        kill_pid_fn=kill_pid,
    )


def _augment_start_failure(context: CliContext, exc: CcbdServiceError) -> CcbdServiceError:
    message = str(exc or '').strip()
    if not message.startswith('ccbd is unavailable:'):
        return exc
    try:
        _, _, inspection = inspect_daemon(context)
    except Exception:
        inspection = None
    if inspection is not None and inspection.health not in {
        LeaseHealth.MISSING,
        LeaseHealth.UNMOUNTED,
        LeaseHealth.STALE,
    }:
        return exc
    try:
        keeper_state = KeeperStateStore(context.paths).load()
    except Exception:
        keeper_state = None
    failure_reason = str(getattr(keeper_state, 'last_failure_reason', '') or '').strip()
    if not failure_reason or failure_reason in message:
        return exc
    return CcbdServiceError(f'{message}; keeper_last_failure: {failure_reason}')


def _normalize_request_failure(context: CliContext) -> CcbdServiceError | None:
    try:
        _, _, inspection = inspect_daemon(context)
    except Exception:
        return None
    phase = str(getattr(inspection, 'phase', '') or '').strip()
    if phase == 'stopping':
        return CcbdServiceError('project ccbd is stopping; wait for shutdown to finish')
    if phase == 'unmounted' and str(getattr(inspection, 'desired_state', '') or '').strip() == 'stopped':
        return CcbdServiceError('project ccbd is unmounted; run `ccb` first')
    return None


def contextual_lifecycle_store(context: CliContext):
    from ccbd.services.lifecycle import CcbdLifecycleStore

    return CcbdLifecycleStore(context.paths)


def contextual_now() -> str:
    from ccbd.system import utc_now

    return utc_now()
