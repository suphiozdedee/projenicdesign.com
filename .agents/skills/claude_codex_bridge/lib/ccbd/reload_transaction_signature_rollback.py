from __future__ import annotations

from ccbd.reload_transaction_signature import (
    update_current_lease_config_signature,
    update_mounted_lifecycle_config_signature,
)


def rollback_signatures(
    app,
    old_signature: str | None,
    *,
    namespace_epoch: int | None,
    expected_generation: int,
    rollback_lease: bool,
    rollback_lifecycle: bool,
) -> dict[str, object]:
    result = _rollback_state(
        rollback_lease=rollback_lease,
        rollback_lifecycle=rollback_lifecycle,
    )
    if not old_signature:
        return _missing_old_signature_result(result)
    if rollback_lifecycle:
        _rollback_lifecycle_signature(
            app,
            old_signature,
            namespace_epoch=namespace_epoch,
            expected_generation=expected_generation,
            result=result,
        )
    if rollback_lease:
        _rollback_lease_signature(
            app,
            old_signature,
            expected_generation=expected_generation,
            result=result,
        )
    return result


def _rollback_state(
    *,
    rollback_lease: bool,
    rollback_lifecycle: bool,
) -> dict[str, object]:
    return {
        'attempted': bool(rollback_lease or rollback_lifecycle),
        'complete': True,
        'lease': None,
        'lifecycle': None,
        'errors': [],
    }


def _missing_old_signature_result(result: dict[str, object]) -> dict[str, object]:
    result['complete'] = not result['attempted']
    if result['attempted']:
        result['errors'] = ['old_config_signature_missing']
    return result


def _rollback_lease_signature(
    app,
    old_signature: str,
    *,
    expected_generation: int,
    result: dict[str, object],
) -> None:
    try:
        result['lease'] = update_current_lease_config_signature(
            app,
            old_signature,
            expected_generation=expected_generation,
        )
    except Exception as exc:
        _record_rollback_error(result, f'lease rollback failed: {exc}')


def _rollback_lifecycle_signature(
    app,
    old_signature: str,
    *,
    namespace_epoch: int | None,
    expected_generation: int,
    result: dict[str, object],
) -> None:
    try:
        result['lifecycle'] = update_mounted_lifecycle_config_signature(
            app,
            old_signature,
            namespace_epoch=namespace_epoch,
            expected_generation=expected_generation,
        )
    except Exception as exc:
        _record_rollback_error(result, f'lifecycle rollback failed: {exc}')


def _record_rollback_error(result: dict[str, object], message: str) -> None:
    result['complete'] = False
    errors = list(result.get('errors') or [])
    errors.append(message)
    result['errors'] = errors


__all__ = ['rollback_signatures']
