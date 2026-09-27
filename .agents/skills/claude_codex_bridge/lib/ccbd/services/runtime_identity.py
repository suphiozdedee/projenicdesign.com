from __future__ import annotations

from dataclasses import dataclass

from ccbd.models import MountState

from .lifecycle import build_lifecycle, lifecycle_from_inspection
from .ownership import OwnershipConflictError


@dataclass(frozen=True)
class RuntimeIdentityReconcileResult:
    lifecycle: object | None
    reconciled: bool
    previous_project_ids: tuple[str, ...] = ()


def reconcile_runtime_project_identity(
    *,
    project_id: str,
    lifecycle,
    inspection,
    mount_manager,
    occurred_at: str,
    socket_path: str,
    keeper_pid: int | None = None,
    config_signature: str | None = None,
) -> RuntimeIdentityReconcileResult:
    current_id = str(project_id or '').strip()
    if not current_id:
        raise ValueError('project_id cannot be empty')
    lease = getattr(inspection, 'lease', None)
    lifecycle_id = str(getattr(lifecycle, 'project_id', '') or '').strip()
    lease_id = str(getattr(lease, 'project_id', '') or '').strip()
    foreign_ids = tuple(
        sorted(
            {
                value
                for value in (lifecycle_id, lease_id)
                if value and value != current_id
            }
        )
    )
    if not foreign_ids:
        return RuntimeIdentityReconcileResult(
            lifecycle=lifecycle,
            reconciled=False,
        )

    if lease is not None and lease_id != current_id:
        if (
            getattr(lease, 'mount_state', None) is MountState.MOUNTED
            and not bool(getattr(inspection, 'takeover_allowed', False))
        ):
            raise OwnershipConflictError(
                'cannot relocate project runtime while foreign authority is active: '
                f'expected project_id={current_id}, found project_id={lease_id}, '
                f'pid={getattr(lease, "ccbd_pid", None)}, '
                f'reason={getattr(inspection, "reason", "unknown")}'
            )
        if getattr(lease, 'mount_state', None) is MountState.MOUNTED:
            mount_manager.mark_unmounted(
                expected_pid=_positive_int(getattr(lease, 'ccbd_pid', None)),
                expected_daemon_instance_id=(
                    str(getattr(lease, 'daemon_instance_id', '') or '').strip()
                    or None
                ),
            )

    generation = max(
        int(getattr(lifecycle, 'generation', 0) or 0),
        int(getattr(lease, 'generation', 0) or 0),
    )
    if lease is not None and lease_id == current_id:
        rebuilt = lifecycle_from_inspection(
            project_id=current_id,
            inspection=inspection,
            occurred_at=occurred_at,
            config_signature=config_signature,
            keeper_pid=keeper_pid,
        ).with_updates(
            generation=generation,
            socket_path=str(socket_path),
        )
    else:
        desired_state = str(
            getattr(lifecycle, 'desired_state', '') or 'stopped'
        )
        if desired_state not in {'running', 'stopped'}:
            desired_state = 'stopped'
        rebuilt = build_lifecycle(
            project_id=current_id,
            occurred_at=occurred_at,
            desired_state=desired_state,
            phase='unmounted',
            generation=generation,
            keeper_pid=keeper_pid,
            config_signature=config_signature,
            socket_path=str(socket_path),
        )
    return RuntimeIdentityReconcileResult(
        lifecycle=rebuilt,
        reconciled=True,
        previous_project_ids=foreign_ids,
    )


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


__all__ = [
    'RuntimeIdentityReconcileResult',
    'reconcile_runtime_project_identity',
]
