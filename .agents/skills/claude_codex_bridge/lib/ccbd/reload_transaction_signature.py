from __future__ import annotations

from ccbd.services.lifecycle import current_socket_inode


def update_current_lease_config_signature(
    app,
    config_signature: str,
    *,
    expected_generation: int,
):
    with app.ownership_guard.startup_lock():
        return app.mount_manager.update_config_signature(
            config_signature=config_signature,
            expected_pid=app.pid,
            expected_daemon_instance_id=app.daemon_instance_id,
            expected_generation=expected_generation,
        )


def assert_current_lease_signature_handoff(app, *, expected_generation: int) -> None:
    lease = app.mount_manager.load_state()
    if lease is None:
        raise RuntimeError('ccbd lease does not exist')
    app.mount_manager._assert_expected_holder(
        lease,
        expected_pid=app.pid,
        expected_daemon_instance_id=app.daemon_instance_id,
    )
    if int(lease.generation) != int(expected_generation):
        raise RuntimeError(
            'ccbd lease generation changed: '
            f'expected generation={expected_generation}, found generation={lease.generation}'
        )


def update_mounted_lifecycle_config_signature(
    app,
    config_signature: str,
    *,
    namespace_epoch: int | None,
    expected_generation: int,
):
    with app.ownership_guard.startup_lock():
        assert_mounted_lifecycle_signature_handoff(
            app,
            expected_generation=expected_generation,
        )
        lifecycle = app.lifecycle_store.load()
        assert lifecycle is not None
        signature = str(config_signature or '').strip()
        if not signature:
            raise RuntimeError('config_signature cannot be empty')
        updated = lifecycle.with_updates(
            desired_state='running',
            keeper_pid=getattr(app, 'keeper_pid', None),
            owner_pid=app.pid,
            owner_daemon_instance_id=app.daemon_instance_id,
            config_signature=signature,
            socket_path=str(app.paths.ccbd_socket_path),
            socket_inode=current_socket_inode(app.paths.ccbd_socket_path),
            namespace_epoch=(
                int(namespace_epoch)
                if namespace_epoch is not None
                else lifecycle.namespace_epoch
            ),
            startup_stage='mounted',
            last_progress_at=app.clock(),
            startup_deadline_at=None,
            last_failure_reason=None,
            shutdown_intent=None,
        )
        app.lifecycle_store.save(updated)
        return updated


def assert_mounted_lifecycle_signature_handoff(
    app,
    *,
    expected_generation: int,
) -> None:
    lifecycle = app.lifecycle_store.load()
    if lifecycle is None:
        raise RuntimeError('ccbd lifecycle does not exist')
    _assert_mounted_phase(lifecycle)
    _assert_lifecycle_generation(lifecycle, expected_generation)
    _assert_lifecycle_owner(lifecycle, app.pid)
    _assert_lifecycle_instance(lifecycle, app.daemon_instance_id)


def expected_generation(app) -> int | None:
    lease = getattr(app, 'lease', None)
    if lease is None:
        lease = app.mount_manager.load_state()
    if lease is None:
        return None
    generation = getattr(lease, 'generation', None)
    if generation is None:
        return None
    return int(generation)


def signature_error(
    old_signature: str | None,
    new_signature: str | None,
) -> Exception | None:
    if not old_signature:
        return RuntimeError('current graph config_signature is missing')
    if not new_signature:
        return RuntimeError('target graph config_signature is missing')
    return None


def _assert_mounted_phase(lifecycle) -> None:
    if lifecycle.phase != 'mounted':
        raise RuntimeError(f'ccbd lifecycle is not mounted: {lifecycle.phase}')


def _assert_lifecycle_generation(lifecycle, expected_generation: int) -> None:
    if int(lifecycle.generation) != int(expected_generation):
        raise RuntimeError(
            'ccbd lifecycle generation changed: '
            f'expected generation={expected_generation}, found generation={lifecycle.generation}'
        )


def _assert_lifecycle_owner(lifecycle, app_pid: int) -> None:
    if lifecycle.owner_pid is None:
        return
    if int(lifecycle.owner_pid) != int(app_pid):
        raise RuntimeError(
            f'ccbd lifecycle owner changed: expected pid={app_pid}, '
            f'found pid={lifecycle.owner_pid}'
        )


def _assert_lifecycle_instance(lifecycle, daemon_instance_id: str | None) -> None:
    expected_instance = str(daemon_instance_id or '').strip()
    current_instance = str(lifecycle.owner_daemon_instance_id or '').strip()
    if expected_instance and current_instance != expected_instance:
        raise RuntimeError(
            'ccbd lifecycle owner changed: '
            f'expected daemon_instance_id={expected_instance}, '
            f'found daemon_instance_id={current_instance or "<missing>"}'
        )


__all__ = [
    'assert_current_lease_signature_handoff',
    'assert_mounted_lifecycle_signature_handoff',
    'expected_generation',
    'signature_error',
    'update_current_lease_config_signature',
    'update_mounted_lifecycle_config_signature',
]
