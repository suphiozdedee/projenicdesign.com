from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import os
import time
import uuid

from agents.config_identity import project_config_identity_payload
from agents.config_loader import load_project_config
from ccbd.daemon_process import spawn_ccbd_process
from ccbd.keeper_runtime.app_state import KeeperAppState, KeeperAppStateMixin
from ccbd.keeper_runtime import KeeperState, KeeperStateStore, ShutdownIntent, ShutdownIntentStore, keeper_state_is_running
from ccbd.keeper_runtime.failure_policy import exception_summary, keeper_start_failure_suppression_reason
from ccbd.keeper_runtime.loop import cleanup_transient_keeper_files, daemon_matches_project_config, reconcile_once, request_shutdown, run_forever
from ccbd.keeper_runtime.support import reap_child_processes, try_acquire_keeper_lock
from ccbd.services.lifecycle import CcbdLifecycleStore, current_socket_inode, lifecycle_from_inspection
from ccbd.services.mount import MountManager
from ccbd.services.ownership import OwnershipGuard
from ccbd.socket_client import CcbdClient, CcbdClientError
from ccbd.startup_policy import STARTUP_TRANSACTION_TIMEOUT_S
from ccbd.system import parse_utc_timestamp, process_exists, utc_now
from cli.kill_runtime.processes import terminate_pid_tree
from mobile_gateway.project_registry import publish_mobile_gateway_project
from project.identity_store import ensure_project_identity
from storage.paths import PathLayout


class ProjectKeeper(KeeperAppStateMixin):
    def __init__(
        self,
        project_root: str | Path,
        *,
        clock=utc_now,
        pid: int | None = None,
        process_exists_fn=process_exists,
        sleep_fn=time.sleep,
        spawn_ccbd_process_fn=spawn_ccbd_process,
    ) -> None:
        resolved_project_root = Path(project_root).expanduser().resolve()
        ensure_project_identity(resolved_project_root)
        paths = PathLayout(resolved_project_root)
        paths.ensure_runtime_state_root()
        _publish_mobile_gateway_project(paths.project_id, resolved_project_root, paths.ccbd_socket_path, clock=clock)
        mount_manager = MountManager(paths, clock=clock)
        self._runtime_state = KeeperAppState(
            project_root=resolved_project_root,
            paths=paths,
            clock=clock,
            pid=pid or os.getpid(),
            sleep=sleep_fn,
            spawn_ccbd_process=spawn_ccbd_process_fn,
            process_exists=process_exists_fn,
            mount_manager=mount_manager,
            lifecycle_store=CcbdLifecycleStore(paths),
            ownership_guard=OwnershipGuard(paths, mount_manager, clock=clock),
            state_store=KeeperStateStore(paths),
            intent_store=ShutdownIntentStore(paths),
        )

    def run_forever(self, *, poll_interval: float = 0.5, start_timeout_s: float = STARTUP_TRANSACTION_TIMEOUT_S) -> int:
        return run_forever(self, poll_interval=poll_interval, start_timeout_s=start_timeout_s)

    def _reconcile_once(self, *, state: KeeperState, start_timeout_s: float) -> KeeperState:
        return reconcile_once(self, state=state, start_timeout_s=start_timeout_s)

    def _spawn_daemon(self, *, state: KeeperState, start_timeout_s: float) -> KeeperState:
        return _spawn_daemon(self, state=state, start_timeout_s=start_timeout_s)

    def _daemon_matches_project_config(self) -> bool:
        return daemon_matches_project_config(self)

    def _request_shutdown(self) -> None:
        request_shutdown(self)

    def _project_definition_missing(self) -> bool:
        return _project_definition_missing(self)

    def _cleanup_transient_keeper_files(self, *, lock_path: Path) -> None:
        cleanup_transient_keeper_files(self, lock_path=lock_path)

    def _terminate_pid_tree(self, pid: int, *, timeout_s: float) -> bool:
        return terminate_pid_tree(pid, timeout_s=timeout_s, is_pid_alive_fn=self._process_exists)


def _spawn_daemon(app: ProjectKeeper, *, state: KeeperState, start_timeout_s: float) -> KeeperState:
    attempt_at = app.clock()
    starting = None
    startup_accepted_perf_counter_ns = None
    try:
        config = load_project_config(app.project_root).config
        config_signature = str(project_config_identity_payload(config)['config_signature'])
        with app._ownership_guard.startup_lock():
            now = app.clock()
            inspection = app._ownership_guard.inspect()
            lifecycle = app._lifecycle_store.load()
            if lifecycle is None:
                lifecycle = lifecycle_from_inspection(
                    project_id=app.paths.project_id,
                    inspection=inspection,
                    occurred_at=now,
                    keeper_pid=app.pid,
                )
            if lifecycle.desired_state != 'running' or not inspection.takeover_allowed:
                return state
            startup_id = uuid.uuid4().hex
            deadline_at = _timestamp_plus_seconds(now, start_timeout_s)
            starting = lifecycle.with_phase(
                'starting',
                occurred_at=now,
                desired_state='running',
                generation=max(
                    int(lifecycle.generation),
                    int(getattr(getattr(inspection, 'lease', None), 'generation', 0) or 0),
                )
                + 1,
                startup_id=startup_id,
                startup_stage='spawn_requested',
                last_progress_at=now,
                startup_deadline_at=deadline_at,
                keeper_pid=app.pid,
                owner_pid=None,
                owner_daemon_instance_id=None,
                config_signature=config_signature,
                socket_path=str(app.paths.ccbd_socket_path),
                socket_inode=None,
                control_plane_endpoint=None,
                last_failure_reason=None,
                shutdown_intent=None,
            )
            app._lifecycle_store.save(starting)
            startup_accepted_perf_counter_ns = time.perf_counter_ns()
        app._spawn_ccbd_process(
            project_root=app.project_root,
            socket_path=app.paths.ccbd_socket_path,
            ccbd_dir=app.paths.ccbd_dir,
            timeout_s=start_timeout_s,
            keeper_pid=app.pid,
            expected_startup_id=starting.startup_id,
            expected_generation=starting.generation,
            keeper_startup_accepted_perf_counter_ns=startup_accepted_perf_counter_ns,
        )
        _record_daemon_start_success(
            app,
            starting=starting,
            config_signature=config_signature,
        )
        return state.with_success(occurred_at=attempt_at)
    except Exception as exc:
        reason = exception_summary(exc)
        suppression_reason = keeper_start_failure_suppression_reason(state, exc)
        failure_reason = suppression_reason or reason
        desired_state = 'stopped' if suppression_reason is not None else 'running'
        try:
            _record_daemon_start_failure(
                app,
                starting=starting,
                desired_state=desired_state,
                failure_reason=failure_reason,
            )
        except Exception:
            pass
        failed_state = state.with_failure(occurred_at=attempt_at, reason=failure_reason)
        if suppression_reason is not None:
            failed_state = failed_state.with_state('failed', occurred_at=attempt_at)
        return failed_state


def _record_daemon_start_success(
    app: ProjectKeeper,
    *,
    starting,
    config_signature: str,
) -> None:
    with app._ownership_guard.startup_lock():
        current = app._lifecycle_store.load()
        if not _same_startup_transaction(current, starting):
            return
        if current.desired_state != 'running' or current.phase == 'mounted':
            return
        if current.phase != 'starting':
            return
        lease = app._mount_manager.load_state()
        lease_generation = int(getattr(lease, 'generation', 0) or 0)
        if lease_generation not in {0, int(starting.generation)}:
            return
        occurred_at = app.clock()
        app._lifecycle_store.save(
            current.with_phase(
                'mounted',
                occurred_at=occurred_at,
                generation=lease_generation or int(starting.generation),
                owner_pid=int(getattr(lease, 'ccbd_pid', 0) or 0) or None,
                owner_daemon_instance_id=str(getattr(lease, 'daemon_instance_id', '') or '').strip() or None,
                config_signature=str(getattr(lease, 'config_signature', '') or '').strip() or config_signature,
                socket_path=str(getattr(lease, 'socket_path', '') or app.paths.ccbd_socket_path),
                socket_inode=current_socket_inode(getattr(lease, 'socket_path', app.paths.ccbd_socket_path)),
                startup_stage='mounted',
                last_progress_at=occurred_at,
                startup_deadline_at=None,
                last_failure_reason=None,
                shutdown_intent=None,
            )
        )


def _record_daemon_start_failure(
    app: ProjectKeeper,
    *,
    starting,
    desired_state: str,
    failure_reason: str,
) -> None:
    with app._ownership_guard.startup_lock():
        current = app._lifecycle_store.load()
        if current is None:
            return
        if starting is None:
            if current.desired_state != 'running' or current.phase not in {'unmounted', 'failed'}:
                return
        elif not _same_startup_transaction(current, starting):
            return
        elif current.desired_state != 'running' or current.phase != 'starting':
            return
        occurred_at = app.clock()
        app._lifecycle_store.save(
            current.with_phase(
                'failed',
                occurred_at=occurred_at,
                desired_state=desired_state,
                owner_pid=None,
                owner_daemon_instance_id=None,
                socket_inode=None,
                startup_stage='spawn_failed',
                last_progress_at=occurred_at,
                startup_deadline_at=None,
                last_failure_reason=failure_reason,
            )
        )


def _same_startup_transaction(current, expected) -> bool:
    if current is None or expected is None:
        return False
    return (
        current.project_id == expected.project_id
        and int(current.generation) == int(expected.generation)
        and str(current.startup_id or '') == str(expected.startup_id or '')
        and bool(current.startup_id)
    )


def _project_definition_missing(app: ProjectKeeper) -> bool:
    if not app.paths.ccb_dir.exists():
        return True
    return False


def _try_acquire_keeper_lock(path: Path):
    return try_acquire_keeper_lock(path)


def _reap_child_processes(*, waitpid_fn=os.waitpid) -> tuple[int, ...]:
    return reap_child_processes(waitpid_fn=waitpid_fn)


def _publish_mobile_gateway_project(project_id: str, project_root: Path, ccbd_socket_path: Path, *, clock) -> None:
    try:
        publish_mobile_gateway_project(
            project_id=project_id,
            project_root=project_root,
            ccbd_socket_path=ccbd_socket_path,
            display_name=project_root.name,
            updated_at=clock(),
        )
    except Exception:
        pass


def _timestamp_plus_seconds(value: str, seconds: float) -> str:
    return (parse_utc_timestamp(value) + timedelta(seconds=max(0.0, float(seconds)))).isoformat().replace('+00:00', 'Z')


__all__ = [
    'KeeperState',
    'KeeperStateStore',
    'ProjectKeeper',
    'ShutdownIntent',
    'ShutdownIntentStore',
    '_reap_child_processes',
    'keeper_state_is_running',
]
