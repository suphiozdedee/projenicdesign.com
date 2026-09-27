from __future__ import annotations

import os


def _float_env(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or '').strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)


STARTUP_TRANSACTION_TIMEOUT_S = max(0.1, _float_env('CCB_STARTUP_TRANSACTION_TIMEOUT_S', 30.0))
STARTUP_PROGRESS_STALL_TIMEOUT_S = max(0.0, _float_env('CCB_STARTUP_PROGRESS_STALL_TIMEOUT_S', 0.0))
KEEPER_READY_TIMEOUT_S = max(0.1, _float_env('CCB_KEEPER_READY_TIMEOUT_S', 2.0))
CONTROL_PLANE_RPC_TIMEOUT_S = max(0.1, _float_env('CCB_CONTROL_PLANE_RPC_TIMEOUT_S', 0.5))
FOREGROUND_ATTACH_RPC_TIMEOUT_S = max(0.1, _float_env('CCB_FOREGROUND_ATTACH_RPC_TIMEOUT_S', 3.0))
FOREGROUND_ATTACH_TARGET_READY_TIMEOUT_S = min(
    STARTUP_TRANSACTION_TIMEOUT_S,
    max(0.1, _float_env('CCB_FOREGROUND_ATTACH_TARGET_READY_TIMEOUT_S', 10.0)),
)


__all__ = [
    'CONTROL_PLANE_RPC_TIMEOUT_S',
    'FOREGROUND_ATTACH_RPC_TIMEOUT_S',
    'FOREGROUND_ATTACH_TARGET_READY_TIMEOUT_S',
    'KEEPER_READY_TIMEOUT_S',
    'STARTUP_PROGRESS_STALL_TIMEOUT_S',
    'STARTUP_TRANSACTION_TIMEOUT_S',
]
