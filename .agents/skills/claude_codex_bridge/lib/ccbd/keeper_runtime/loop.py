from __future__ import annotations

from pathlib import Path

from agents.config_identity import project_config_identity_payload
from agents.config_loader import load_project_config
from ccbd.models import LeaseHealth, MountState
from ccbd.reload_handoff import reload_handoff_allows_signature_mismatch
from ccbd.services.project_namespace_state import ProjectNamespaceStateStore
from ccbd.services.lifecycle import current_socket_inode, lifecycle_from_inspection
from ccbd.services.runtime_identity import reconcile_runtime_project_identity
from ccbd.socket_client import CcbdClient, CcbdClientError
from ccbd.startup_policy import CONTROL_PLANE_RPC_TIMEOUT_S, STARTUP_TRANSACTION_TIMEOUT_S

from .records import KeeperState
from .state import restart_backoff_active
from .support import reap_child_processes, try_acquire_keeper_lock


def run_forever(app, *, poll_interval: float = 0.5, start_timeout_s: float = STARTUP_TRANSACTION_TIMEOUT_S) -> int:
    lock_path = app.paths.ccbd_dir / 'keeper.lock'
    lock_handle = try_acquire_keeper_lock(lock_path)
    if lock_handle is None:
        return 0
    cleanup_transient = False
    state = initial_keeper_state(app)
    app._state_store.save(state)
    try:
        while True:
            reap_child_processes()
            state, should_stop, cleanup_transient = run_iteration(
                app,
                state=state,
                start_timeout_s=start_timeout_s,
                cleanup_transient=cleanup_transient,
            )
            app._state_store.save(state)
            if should_stop:
                return 0
            app._sleep(max(0.05, float(poll_interval)))
    finally:
        try:
            lock_handle.close()
        except Exception:
            pass
        if cleanup_transient:
            cleanup_transient_keeper_files(app, lock_path=lock_path)


def reconcile_once(app, *, state: KeeperState, start_timeout_s: float) -> KeeperState:
    now = app.clock()
    if restart_backoff_active(state=state, now=now):
        return state
    inspection = app._ownership_guard.inspect()
    lifecycle = ensure_project_lifecycle(app, inspection=inspection, now=now)
    if lifecycle.desired_state != 'running':
        return state
    connectable_state = reconcile_connectable_daemon(
        app,
        state=state,
        inspection=inspection,
        lifecycle=lifecycle,
        now=now,
    )
    if connectable_state is not None:
        return connectable_state
    restart_state = restart_state_from_inspection(app, state=state, inspection=inspection, occurred_at=now)
    if restart_state is not None:
        return app._spawn_daemon(state=restart_state, start_timeout_s=start_timeout_s)
    return state


def initial_keeper_state(app) -> KeeperState:
    now = app.clock()
    return KeeperState(
        project_id=app.paths.project_id,
        keeper_pid=app.pid,
        started_at=now,
        last_check_at=now,
        state='running',
    )


def run_iteration(
    app,
    *,
    state: KeeperState,
    start_timeout_s: float,
    cleanup_transient: bool,
) -> tuple[KeeperState, bool, bool]:
    now = app.clock()
    if app._project_definition_missing():
        return state, True, True
    if shutdown_requested(app, project_id=state.project_id):
        return state.with_state('stopped', occurred_at=now), True, cleanup_transient
    checked = state.with_check(now)
    next_state = app._reconcile_once(state=checked, start_timeout_s=start_timeout_s)
    if next_state.state != 'running':
        return next_state, True, cleanup_transient
    return next_state, False, cleanup_transient


def shutdown_requested(app, *, project_id: str) -> bool:
    current_intent = app._intent_store.load()
    return current_intent is not None and current_intent.project_id == project_id


def ensure_project_lifecycle(app, *, inspection, now: str):
    lifecycle = app._lifecycle_store.load()
    lease = getattr(inspection, 'lease', None)
    if (
        lifecycle is not None
        and lifecycle.keeper_pid == app.pid
        and lifecycle.project_id == app.paths.project_id
        and (
            lease is None
            or str(getattr(lease, 'project_id', '') or '') == app.paths.project_id
        )
    ):
        return lifecycle
    with app._ownership_guard.startup_lock():
        lifecycle = app._lifecycle_store.load()
        inspection = app._ownership_guard.inspect()
        identity_result = reconcile_runtime_project_identity(
            project_id=app.paths.project_id,
            lifecycle=lifecycle,
            inspection=inspection,
            mount_manager=app._mount_manager,
            occurred_at=app.clock(),
            socket_path=str(app.paths.ccbd_socket_path),
            keeper_pid=app.pid,
            config_signature=current_config_signature(app),
        )
        lifecycle = identity_result.lifecycle
        if lifecycle is None:
            lifecycle = lifecycle_from_inspection(
                project_id=app.paths.project_id,
                inspection=inspection,
                occurred_at=app.clock(),
                config_signature=current_config_signature(app),
                keeper_pid=app.pid,
            )
            app._lifecycle_store.save(lifecycle)
            return lifecycle
        if identity_result.reconciled:
            app._lifecycle_store.save(lifecycle)
            return lifecycle
        if lifecycle.keeper_pid == app.pid:
            return lifecycle
        lifecycle = lifecycle.with_updates(keeper_pid=app.pid)
        app._lifecycle_store.save(lifecycle)
        return lifecycle


def reconcile_connectable_daemon(app, *, state: KeeperState, inspection, lifecycle, now: str) -> KeeperState | None:
    if not inspection.socket_connectable:
        return None
    # The spawned child exclusively owns promotion of its in-flight startup
    # transaction.  A mounted lease plus a connectable ping during
    # `starting/runtime_bootstrap` is progress evidence, not permission for the
    # keeper's steady-state reconciliation path to synthesize mounted early.
    if _startup_promotion_owned_by_child(lifecycle, inspection.lease):
        return state
    try:
        if daemon_matches_project_config(app):
            mounted_kwargs = _mounted_lifecycle_kwargs(app, lifecycle=lifecycle, inspection=inspection)
            if _mounted_lifecycle_is_current(lifecycle, mounted_kwargs):
                return state.with_success(occurred_at=now)
            return _record_connectable_mounted(
                app,
                state=state,
                observed_inspection=inspection,
            )
        request_shutdown(app)
        _record_connectable_restart(
            app,
            observed_inspection=inspection,
        )
        return state.with_restart_attempt(occurred_at=now)
    except Exception as exc:
        failure_reason = f'config_check_failed:{exc}'
        _record_connectable_observation_failure(
            app,
            lifecycle=lifecycle,
            inspection=inspection,
            now=now,
            failure_reason=failure_reason,
        )
        return state.with_failure(occurred_at=now, reason=failure_reason)


def restart_state_from_inspection(app, *, state: KeeperState, inspection, occurred_at: str) -> KeeperState | None:
    stale = stale_restart_state(app, state=state, inspection=inspection, occurred_at=occurred_at)
    if stale is not None:
        return stale
    if inspection.health in {LeaseHealth.MISSING, LeaseHealth.UNMOUNTED, LeaseHealth.STALE}:
        return state.with_restart_attempt(occurred_at=occurred_at)
    return None


def stale_restart_state(app, *, state: KeeperState, inspection, occurred_at: str) -> KeeperState | None:
    if inspection.health is not LeaseHealth.STALE or not inspection.pid_alive or inspection.lease is None:
        return None
    if inspection.heartbeat_fresh:
        return None
    pid = int(inspection.lease.ccbd_pid or 0)
    if pid > 0:
        app._terminate_pid_tree(pid, timeout_s=1.0)
    return state.with_restart_attempt(occurred_at=occurred_at)


def daemon_matches_project_config(app) -> bool:
    expected = project_config_identity_payload(load_project_config(app.project_root).config)
    payload = CcbdClient(app.paths.ccbd_socket_path, timeout_s=CONTROL_PLANE_RPC_TIMEOUT_S).ping('ccbd')
    actual_signature = str(payload.get('config_signature') or '').strip()
    if actual_signature:
        expected_signature = str(expected['config_signature'])
        if actual_signature == expected_signature:
            return True
        if reload_handoff_allows_signature_mismatch(
            app,
            expected_config_signature=expected_signature,
            actual_config_signature=actual_signature,
        ):
            return True
        # A mounted daemon with an older service graph is still the active
        # project daemon. Explicit `ccb reload` owns applying disk config drift.
        return True
    known_agents = payload.get('known_agents')
    if not isinstance(known_agents, list):
        return False
    actual_agents = tuple(str(item).strip().lower() for item in known_agents if str(item).strip())
    return actual_agents == tuple(expected['known_agents'])


def request_shutdown(app) -> None:
    client = CcbdClient(app.paths.ccbd_socket_path, timeout_s=CONTROL_PLANE_RPC_TIMEOUT_S)
    try:
        client.stop_all(force=False)
    except CcbdClientError:
        inspection = app._ownership_guard.inspect()
        if inspection.lease is not None and inspection.pid_alive:
            app._terminate_pid_tree(int(inspection.lease.ccbd_pid or 0), timeout_s=1.0)


def current_config_signature(app) -> str | None:
    try:
        config = load_project_config(app.project_root).config
    except Exception:
        return None
    return str(project_config_identity_payload(config)['config_signature'])


def _current_namespace_epoch(app, *, fallback: int | None) -> int | None:
    try:
        state = ProjectNamespaceStateStore(app.paths).load()
    except Exception:
        state = None
    if state is not None:
        return int(state.namespace_epoch)
    return fallback


def _mounted_lifecycle_kwargs(app, *, lifecycle, inspection) -> dict[str, object]:
    lease = inspection.lease
    return {
        'desired_state': 'running',
        'generation': int(getattr(lease, 'generation', 0) or lifecycle.generation),
        'keeper_pid': app.pid,
        'owner_pid': int(getattr(lease, 'ccbd_pid', 0) or 0) or None,
        'owner_daemon_instance_id': str(getattr(lease, 'daemon_instance_id', '') or '').strip() or None,
        'config_signature': str(getattr(lease, 'config_signature', '') or '').strip() or lifecycle.config_signature,
        'socket_path': str(getattr(lease, 'socket_path', '') or app.paths.ccbd_socket_path),
        'socket_inode': current_socket_inode(getattr(lease, 'socket_path', app.paths.ccbd_socket_path)),
        'namespace_epoch': _current_namespace_epoch(app, fallback=lifecycle.namespace_epoch),
    }


def _mounted_lifecycle_is_current(lifecycle, mounted_kwargs: dict[str, object]) -> bool:
    if lifecycle.phase != 'mounted':
        return False
    if lifecycle.last_failure_reason is not None or lifecycle.shutdown_intent is not None:
        return False
    for key, value in mounted_kwargs.items():
        if getattr(lifecycle, key) != value:
            return False
    return True


def _record_connectable_observation_failure(
    app,
    *,
    lifecycle,
    inspection,
    now: str,
    failure_reason: str,
) -> None:
    del lifecycle, now
    with app._ownership_guard.startup_lock():
        current = app._lifecycle_store.load()
        if current is None or current.desired_state != 'running':
            return
        current_inspection = app._ownership_guard.inspect()
        current_lease = current_inspection.lease
        if not _same_observed_lease(current_lease, inspection.lease):
            return
        occurred_at = app.clock()
        if current_lease is None or not current_inspection.socket_connectable:
            if current.phase == 'starting':
                return
            app._lifecycle_store.save(
                current.with_phase(
                    'failed',
                    occurred_at=occurred_at,
                    desired_state='running',
                    last_failure_reason=failure_reason,
                )
            )
            return
        if int(current.generation) != int(current_lease.generation):
            return
        mounted_kwargs = _mounted_lifecycle_kwargs(app, lifecycle=current, inspection=current_inspection)
        if current.phase == 'mounted':
            app._lifecycle_store.save(
                current.with_updates(
                    desired_state='running',
                    keeper_pid=mounted_kwargs['keeper_pid'],
                    owner_pid=mounted_kwargs['owner_pid'],
                    owner_daemon_instance_id=mounted_kwargs['owner_daemon_instance_id'],
                    config_signature=mounted_kwargs['config_signature'],
                    socket_path=mounted_kwargs['socket_path'],
                    socket_inode=mounted_kwargs['socket_inode'],
                    namespace_epoch=mounted_kwargs['namespace_epoch'],
                    last_failure_reason=failure_reason,
                    shutdown_intent=None,
                )
            )
            return
        app._lifecycle_store.save(
            current.with_phase(
                'mounted',
                occurred_at=occurred_at,
                **mounted_kwargs,
                last_failure_reason=failure_reason,
                shutdown_intent=None,
            )
        )


def _record_connectable_mounted(app, *, state: KeeperState, observed_inspection) -> KeeperState:
    with app._ownership_guard.startup_lock():
        lifecycle = app._lifecycle_store.load()
        current_inspection = app._ownership_guard.inspect()
        current_lease = current_inspection.lease
        if (
            lifecycle is None
            or lifecycle.desired_state != 'running'
            or _startup_promotion_owned_by_child(lifecycle, current_lease)
            or not _same_observed_lease(current_lease, observed_inspection.lease)
            or current_lease is None
            or current_lease.mount_state is not MountState.MOUNTED
        ):
            return state
        occurred_at = app.clock()
        mounted_kwargs = _mounted_lifecycle_kwargs(
            app,
            lifecycle=lifecycle,
            inspection=current_inspection,
        )
        if not _mounted_lifecycle_is_current(lifecycle, mounted_kwargs):
            generation_changed = int(lifecycle.generation) != int(current_lease.generation)
            app._lifecycle_store.save(
                lifecycle.with_phase(
                    'mounted',
                    occurred_at=occurred_at,
                    **mounted_kwargs,
                    startup_id=None if generation_changed else lifecycle.startup_id,
                    startup_stage='mounted',
                    last_progress_at=occurred_at,
                    startup_deadline_at=None,
                    last_failure_reason=None,
                    shutdown_intent=None,
                )
            )
        return state.with_success(occurred_at=occurred_at)


def _startup_promotion_owned_by_child(lifecycle, lease) -> bool:
    if lifecycle is None or lifecycle.phase != 'starting':
        return False
    stage = str(getattr(lifecycle, 'startup_stage', '') or '')
    if stage not in {
        'spawn_requested',
        'socket_listening',
        'publishing_mounted',
        'runtime_bootstrap',
    }:
        return False
    if lease is None or lease.mount_state is not MountState.MOUNTED:
        return False
    try:
        return (
            str(lifecycle.project_id) == str(lease.project_id)
            and int(lifecycle.generation) == int(lease.generation)
            and int(lifecycle.owner_pid or 0) == int(lease.ccbd_pid)
            and str(lifecycle.owner_daemon_instance_id or '')
            == str(lease.daemon_instance_id or '')
            and str(lifecycle.socket_path or '') == str(lease.socket_path or '')
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _record_connectable_restart(app, *, observed_inspection) -> None:
    with app._ownership_guard.startup_lock():
        lifecycle = app._lifecycle_store.load()
        current_lease = app._ownership_guard.inspect().lease
        if (
            lifecycle is None
            or lifecycle.desired_state != 'running'
            or not _same_observed_lease(current_lease, observed_inspection.lease)
        ):
            return
        app._lifecycle_store.save(
            lifecycle.with_phase(
                'stopping',
                occurred_at=app.clock(),
                desired_state='running',
                last_failure_reason=None,
            )
        )


def _same_observed_lease(current, observed) -> bool:
    if current is None or observed is None:
        return current is None and observed is None
    try:
        return (
            str(current.project_id) == str(observed.project_id)
            and int(current.ccbd_pid) == int(observed.ccbd_pid)
            and int(current.generation) == int(observed.generation)
            and str(current.daemon_instance_id or '') == str(observed.daemon_instance_id or '')
            and str(current.socket_path) == str(observed.socket_path)
            and current.mount_state is observed.mount_state
        )
    except (AttributeError, TypeError, ValueError):
        return False


def cleanup_transient_keeper_files(app, *, lock_path: Path) -> None:
    for path in (
        app.paths.ccbd_keeper_path,
        app.paths.ccbd_reload_handoff_path,
        app.paths.ccbd_dir / 'keeper.stdout.log',
        app.paths.ccbd_dir / 'keeper.stderr.log',
        Path(lock_path),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except Exception:
            continue
    for path in (app.paths.ccbd_dir,):
        try:
            path.rmdir()
        except OSError:
            continue


__all__ = ['cleanup_transient_keeper_files', 'daemon_matches_project_config', 'reconcile_once', 'request_shutdown', 'run_forever']
