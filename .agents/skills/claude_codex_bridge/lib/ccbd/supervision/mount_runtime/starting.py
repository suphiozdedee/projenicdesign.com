from __future__ import annotations

from dataclasses import replace

from agents.models import AgentState, RuntimeBindingSource, normalize_runtime_binding_source


def build_starting_runtime(
    agent_name: str,
    *,
    runtime,
    attempted_at: str,
    layout,
    registry,
    runtime_service,
    generation_getter,
):
    spec = registry.spec_for(agent_name)
    workspace_path = str(layout.workspace_path(agent_name, workspace_root=spec.workspace_root))
    generation = generation_getter()
    if runtime is None:
        attached = runtime_service.attach(
            agent_name=agent_name,
            workspace_path=workspace_path,
            backend_type=spec.runtime_mode.value,
            health='starting',
            provider=spec.provider,
            lifecycle_state='starting',
            managed_by='ccbd',
            binding_source='provider-session',
        )
        current = attached
    else:
        current = runtime
        if authority_adopt_required(runtime, generation=generation):
            current = runtime_service.adopt_runtime_authority(
                runtime,
                daemon_generation=generation,
            )

    candidate = replace(
        current,
        state=AgentState.STARTING,
        health='starting',
        workspace_path=current.workspace_path or workspace_path,
        backend_type=current.backend_type or spec.runtime_mode.value,
        provider=current.provider or spec.provider,
        lifecycle_state='starting',
        daemon_generation=current.daemon_generation if runtime is not None else generation,
        desired_state='mounted',
        reconcile_state='starting',
        last_reconcile_at=attempted_at,
        last_failure_reason=None,
    )
    if candidate != current:
        current = registry.upsert_authority(candidate)
    started, _ = runtime_service.begin_mount_attempt(
        current,
        attempted_at=attempted_at,
    )
    return started


def authority_adopt_required(runtime, *, generation: int | None) -> bool:
    if generation is None:
        return False
    if normalize_runtime_binding_source(
        getattr(runtime, 'binding_source', RuntimeBindingSource.PROVIDER_SESSION)
    ) is RuntimeBindingSource.EXTERNAL_ATTACH:
        return False
    if runtime.state not in {AgentState.IDLE, AgentState.BUSY, AgentState.DEGRADED}:
        return False
    current_generation = getattr(runtime, 'daemon_generation', None)
    try:
        current_generation = int(current_generation) if current_generation is not None else None
    except Exception:
        current_generation = None
    return current_generation != int(generation)


__all__ = ["build_starting_runtime"]
