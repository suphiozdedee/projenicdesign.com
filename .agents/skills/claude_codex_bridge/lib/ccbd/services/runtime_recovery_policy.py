from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypedDict, cast

from agents.models import AgentState, RuntimeBindingSource, normalize_runtime_binding_source

PROVIDER_AUTH_REVOKED_RUNTIME_HEALTH = 'provider-auth-revoked'
PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH = 'provider-recovery-blocked'
RUNTIME_RECOVERY_CIRCUIT_OPEN_HEALTH = 'recovery-circuit-open'
RUNTIME_RECOVERY_PROBING_HEALTH = 'recovering'
HERDR_RECOVERY_OWNER = 'ccb'
HERDR_RECOVERY_PROBATION_SECONDS = 90
HERDR_RECOVERY_CIRCUIT_THRESHOLD = 3
HERDR_AUTO_RESTORE_DISABLED = 'disabled'
HERDR_AUTO_RESTORE_MODES = frozenset(
    {'disabled', 'observe-only', 'unsupported', 'unknown'}
)
PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTHS = frozenset(
    {
        PROVIDER_AUTH_REVOKED_RUNTIME_HEALTH,
        PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH,
    }
)
RECOVERY_BLOCKED_RUNTIME_HEALTHS = (
    PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTHS
    | frozenset({RUNTIME_RECOVERY_CIRCUIT_OPEN_HEALTH})
)
HARD_BLOCKED_RUNTIME_HEALTHS = (
    frozenset({'session-missing', RUNTIME_RECOVERY_PROBING_HEALTH})
    | RECOVERY_BLOCKED_RUNTIME_HEALTHS
)
RECOVERABLE_RUNTIME_HEALTHS = frozenset({'pane-dead', 'pane-missing'})
HERDR_RECOVERABLE_RUNTIME_HEALTHS = RECOVERABLE_RUNTIME_HEALTHS | frozenset(
    {'process-dead', 'namespace-crashed', 'daemon-unavailable'}
)


class HerdrRecoveryPolicy(TypedDict):
    owner: Literal['ccb']
    herdr_auto_restore_mode: Literal['disabled', 'observe-only', 'unsupported', 'unknown']
    probation_seconds: int
    circuit_threshold: int
    restore_token_required: bool


def normalized_runtime_health(runtime) -> str:
    return str(getattr(runtime, 'health', '') or '').strip().lower()


def herdr_recovery_policy(runtime) -> HerdrRecoveryPolicy | None:
    if not _runtime_uses_herdr(runtime):
        return None
    return {
        'owner': HERDR_RECOVERY_OWNER,
        'herdr_auto_restore_mode': herdr_auto_restore_mode(runtime),
        'probation_seconds': HERDR_RECOVERY_PROBATION_SECONDS,
        'circuit_threshold': HERDR_RECOVERY_CIRCUIT_THRESHOLD,
        'restore_token_required': True,
    }


def herdr_auto_restore_mode(runtime) -> Literal['disabled', 'observe-only', 'unsupported', 'unknown']:
    value = str(getattr(runtime, 'herdr_auto_restore_mode', '') or '').strip().lower()
    if value in HERDR_AUTO_RESTORE_MODES:
        return cast(Literal['disabled', 'observe-only', 'unsupported', 'unknown'], value)
    return 'unknown'


def herdr_recovery_capable(runtime) -> bool:
    policy = herdr_recovery_policy(runtime)
    if policy is None:
        return True
    return policy['owner'] == HERDR_RECOVERY_OWNER and (
        policy['herdr_auto_restore_mode'] == HERDR_AUTO_RESTORE_DISABLED
    )


def should_record_recovery_capability_block(runtime) -> bool:
    if runtime is None or getattr(runtime, 'state', None) is not AgentState.DEGRADED:
        return False
    binding_source = normalize_runtime_binding_source(
        getattr(runtime, 'binding_source', RuntimeBindingSource.PROVIDER_SESSION)
    )
    if binding_source is RuntimeBindingSource.EXTERNAL_ATTACH:
        return False
    if herdr_recovery_policy(runtime) is None:
        return False
    if herdr_recovery_capable(runtime):
        return False
    return runtime_health_recoverable(runtime)


def recovery_circuit_threshold(runtime, *, default: int) -> int:
    policy = herdr_recovery_policy(runtime)
    if policy is None:
        return default
    return policy['circuit_threshold']


def runtime_health_recoverable(runtime) -> bool:
    health = normalized_runtime_health(runtime)
    if herdr_recovery_policy(runtime) is not None:
        return health in HERDR_RECOVERABLE_RUNTIME_HEALTHS
    return health in RECOVERABLE_RUNTIME_HEALTHS


def should_attempt_background_recovery(runtime) -> bool:
    if runtime is None or getattr(runtime, 'state', None) is not AgentState.DEGRADED:
        return False
    binding_source = normalize_runtime_binding_source(
        getattr(runtime, 'binding_source', RuntimeBindingSource.PROVIDER_SESSION)
    )
    if binding_source is RuntimeBindingSource.EXTERNAL_ATTACH:
        return False
    if not herdr_recovery_capable(runtime):
        return False
    return runtime_health_recoverable(runtime)


def _runtime_uses_herdr(runtime) -> bool:
    candidates = (
        getattr(runtime, 'terminal_backend', None),
        getattr(runtime, 'backend_impl', None),
        _runtime_backend_ref_value(runtime, 'backend_impl'),
    )
    return any(str(value or '').strip().lower() == 'herdr' for value in candidates)


def _runtime_backend_ref_value(runtime, key: str) -> object | None:
    backend_ref = getattr(runtime, 'provider_runtime_backend_ref', None)
    if isinstance(backend_ref, Mapping):
        return backend_ref.get(key)
    return None


__all__ = [
    'HARD_BLOCKED_RUNTIME_HEALTHS',
    'HERDR_AUTO_RESTORE_DISABLED',
    'HERDR_AUTO_RESTORE_MODES',
    'HERDR_RECOVERY_CIRCUIT_THRESHOLD',
    'HERDR_RECOVERY_OWNER',
    'HERDR_RECOVERY_PROBATION_SECONDS',
    'HERDR_RECOVERABLE_RUNTIME_HEALTHS',
    'HerdrRecoveryPolicy',
    'PROVIDER_AUTH_REVOKED_RUNTIME_HEALTH',
    'PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTH',
    'PROVIDER_RECOVERY_BLOCKED_RUNTIME_HEALTHS',
    'RECOVERY_BLOCKED_RUNTIME_HEALTHS',
    'RECOVERABLE_RUNTIME_HEALTHS',
    'RUNTIME_RECOVERY_CIRCUIT_OPEN_HEALTH',
    'RUNTIME_RECOVERY_PROBING_HEALTH',
    'herdr_auto_restore_mode',
    'herdr_recovery_capable',
    'herdr_recovery_policy',
    'normalized_runtime_health',
    'recovery_circuit_threshold',
    'runtime_health_recoverable',
    'should_record_recovery_capability_block',
    'should_attempt_background_recovery',
]
