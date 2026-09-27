from __future__ import annotations

from ccbd.services.runtime_recovery_policy import should_attempt_background_recovery


def provider_supports_resume(dispatcher, agent_name: str) -> bool:
    try:
        spec = dispatcher._registry.spec_for(agent_name)
        manifest = dispatcher._provider_catalog.get(spec.provider)
    except Exception:
        return False
    return bool(manifest.supports_resume)


def can_attempt_runtime_recovery(dispatcher, runtime) -> bool:
    if dispatcher._execution_service is None or dispatcher._runtime_service is None:
        return False
    if not should_attempt_background_recovery(runtime):
        return False
    return provider_supports_resume(dispatcher, runtime.agent_name)


__all__ = ["can_attempt_runtime_recovery", "provider_supports_resume"]
