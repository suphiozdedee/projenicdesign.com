from __future__ import annotations

from collections.abc import Mapping

from agents.models import RuntimeBindingSource
from agents.runtime_binding import merge_runtime_binding, runtime_binding_from_runtime
from provider_runtime.session_payload import (
    namespace_restore_token_present,
    redacted_namespace_ref,
    redacted_provider_runtime_backend_ref,
)

from ..runtime_attach import (
    binding_source_for_attach,
    health_for_attach,
    normalized_text,
    pane_id_from_runtime_ref,
    resolve_session_fields,
    state_for_attach,
    terminal_backend_from_runtime_ref,
)
from .attach_models import AttachRuntimeValues


def resolve_attach_runtime_values(
    *,
    existing,
    spec,
    workspace_path: str,
    backend_type: str,
    pid: int | None,
    runtime_ref: str | None,
    session_ref: str | None,
    health: str | None,
    provider: str | None,
    runtime_root: str | None,
    runtime_pid: int | None,
    terminal_backend: str | None,
    provider_runtime_backend_ref: Mapping[str, object] | None,
    namespace_ref: Mapping[str, object] | None,
    pane_ref: Mapping[str, object] | None,
    namespace_restore_token_present: bool | None,
    herdr_auto_restore_mode: str | None,
    herdr_agent_state_ref: str | None,
    pane_id: str | None,
    active_pane_id: str | None,
    pane_title_marker: str | None,
    pane_state: str | None,
    tmux_socket_name: str | None,
    tmux_socket_path: str | None,
    tmux_window_name: str | None,
    tmux_window_id: str | None,
    session_file: str | None,
    session_id: str | None,
    slot_key: str | None,
    window_id: str | None,
    workspace_epoch: int | None,
    lifecycle_state: str | None,
    daemon_generation: int | None,
    managed_by: str | None,
    binding_source: str | RuntimeBindingSource | None,
) -> AttachRuntimeValues:
    merged_binding = merge_runtime_binding(
        runtime_binding_from_runtime(existing),
        runtime_ref=runtime_ref,
        session_ref=session_ref,
        workspace_path=workspace_path,
    )
    session_file_value, session_id_value, session_ref_value = resolve_session_fields(
        existing,
        session_ref=merged_binding.session_ref,
        session_file=session_file,
        session_id=session_id,
        session_ref_explicit=session_ref is not None,
        session_file_explicit=session_file is not None,
        session_id_explicit=session_id is not None,
    )
    runtime_ref_value = merged_binding.runtime_ref
    binding_source_value = binding_source_for_attach(existing, explicit=binding_source)
    next_health = health_for_attach(
        existing,
        explicit=health,
        binding_source=binding_source_value,
    )
    next_state = state_for_attach(existing.state if existing is not None else None, next_health)
    pane_id_value = preferred_pane_id(existing, pane_id=pane_id, runtime_ref_value=runtime_ref_value)
    runtime_root_value = preferred_text(existing, 'runtime_root', runtime_root)
    runtime_pid_value = next_runtime_pid(existing, runtime_pid=runtime_pid, pid=pid)
    terminal_backend_value = preferred_terminal_backend(existing, terminal_backend=terminal_backend, runtime_ref_value=runtime_ref_value)
    raw_provider_runtime_backend_ref = preferred_mapping(
        existing,
        'provider_runtime_backend_ref',
        provider_runtime_backend_ref,
    )
    raw_namespace_ref = preferred_mapping(existing, 'namespace_ref', namespace_ref)
    provider_runtime_backend_ref_value = redacted_provider_runtime_backend_ref(
        raw_provider_runtime_backend_ref
    )
    namespace_ref_value = redacted_namespace_ref(raw_namespace_ref)
    pane_ref_value = preferred_mapping(existing, 'pane_ref', pane_ref)
    backend_namespace_ref = (
        raw_provider_runtime_backend_ref.get('namespace_ref')
        if isinstance(raw_provider_runtime_backend_ref, Mapping)
        else None
    )
    namespace_restore_token_present_value = preferred_bool(
        existing,
        'namespace_restore_token_present',
        namespace_restore_token_present,
    ) or namespace_restore_token_present_fn(raw_namespace_ref) or namespace_restore_token_present_fn(
        backend_namespace_ref if isinstance(backend_namespace_ref, Mapping) else None
    )
    herdr_auto_restore_mode_value = preferred_text(
        existing,
        'herdr_auto_restore_mode',
        herdr_auto_restore_mode,
    )
    herdr_agent_state_ref_value = preferred_text(
        existing,
        'herdr_agent_state_ref',
        herdr_agent_state_ref,
    )
    active_pane_id_value = preferred_active_pane_id(existing, active_pane_id=active_pane_id, pane_id_value=pane_id_value)
    tmux_socket_name_value = preferred_text(existing, 'tmux_socket_name', tmux_socket_name)
    tmux_socket_path_value = preferred_text(existing, 'tmux_socket_path', tmux_socket_path)
    tmux_window_name_value = preferred_text(existing, 'tmux_window_name', tmux_window_name)
    tmux_window_id_value = preferred_text(existing, 'tmux_window_id', tmux_window_id)
    daemon_generation_value = next_daemon_generation(existing, daemon_generation=daemon_generation)
    authority_epoch_changed = runtime_authority_changed(
        existing,
        runtime_ref=runtime_ref_value,
        session_ref=session_ref_value,
        runtime_root=runtime_root_value,
        runtime_pid=runtime_pid_value,
        pane_id=pane_id_value,
        active_pane_id=active_pane_id_value,
        tmux_socket_name=tmux_socket_name_value,
        tmux_socket_path=tmux_socket_path_value,
        tmux_window_name=tmux_window_name_value,
        tmux_window_id=tmux_window_id_value,
        daemon_generation=daemon_generation_value,
    )
    binding_generation, runtime_generation = next_authority_epoch_generations(
        existing,
        authority_epoch_changed=authority_epoch_changed,
    )
    return AttachRuntimeValues(
        backend_type=backend_type,
        runtime_ref=runtime_ref_value,
        session_ref=session_ref_value,
        workspace_path=merged_binding.workspace_path,
        state=next_state,
        health=next_health,
        provider=next_provider(existing, spec, provider),
        runtime_root=runtime_root_value,
        runtime_pid=runtime_pid_value,
        terminal_backend=terminal_backend_value,
        provider_runtime_backend_ref=provider_runtime_backend_ref_value,
        namespace_ref=namespace_ref_value,
        pane_ref=pane_ref_value,
        namespace_restore_token_present=namespace_restore_token_present_value,
        herdr_auto_restore_mode=herdr_auto_restore_mode_value,
        herdr_agent_state_ref=herdr_agent_state_ref_value,
        pane_id=pane_id_value,
        active_pane_id=active_pane_id_value,
        pane_title_marker=preferred_text(existing, 'pane_title_marker', pane_title_marker),
        pane_state=preferred_text(existing, 'pane_state', pane_state),
        tmux_socket_name=tmux_socket_name_value,
        tmux_socket_path=tmux_socket_path_value,
        tmux_window_name=tmux_window_name_value,
        tmux_window_id=tmux_window_id_value,
        session_file=session_file_value,
        session_id=session_id_value,
        slot_key=preferred_slot_key(existing, spec_name=spec.name, slot_key=slot_key),
        window_id=preferred_text(existing, 'window_id', window_id),
        workspace_epoch=preferred_workspace_epoch(existing, workspace_epoch=workspace_epoch),
        lifecycle_state=next_lifecycle_state(existing, lifecycle_state=lifecycle_state, next_state=next_state),
        binding_generation=binding_generation,
        runtime_generation=runtime_generation,
        daemon_generation=daemon_generation_value,
        authority_epoch_changed=authority_epoch_changed,
        managed_by=preferred_text(existing, 'managed_by', managed_by, default='ccbd') or 'ccbd',
        binding_source=binding_source_value,
    )


def next_provider(existing, spec, provider: str | None) -> str:
    current = existing.provider if existing is not None else spec.provider
    return str(provider or current or spec.provider).strip() or spec.provider


def preferred_text(existing, field_name: str, explicit_value: str | None, *, default: str | None = None) -> str | None:
    normalized = normalized_text(explicit_value)
    if normalized is not None:
        return normalized
    if existing is not None:
        return getattr(existing, field_name)
    return default


def preferred_terminal_backend(existing, *, terminal_backend: str | None, runtime_ref_value: str | None) -> str | None:
    return (
        normalized_text(terminal_backend)
        or terminal_backend_from_runtime_ref(runtime_ref_value)
        or (existing.terminal_backend if existing is not None else None)
    )


def preferred_mapping(
    existing,
    field_name: str,
    explicit_value: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if isinstance(explicit_value, Mapping):
        return dict(explicit_value)
    current = getattr(existing, field_name, None) if existing is not None else None
    return dict(current) if isinstance(current, Mapping) else None


def preferred_bool(existing, field_name: str, explicit_value: bool | None) -> bool:
    if explicit_value is not None:
        return bool(explicit_value)
    return bool(getattr(existing, field_name, False)) if existing is not None else False


def namespace_restore_token_present_fn(namespace_ref: Mapping[str, object] | None) -> bool:
    return namespace_restore_token_present(namespace_ref)


def preferred_pane_id(existing, *, pane_id: str | None, runtime_ref_value: str | None) -> str | None:
    return (
        normalized_text(pane_id)
        or pane_id_from_runtime_ref(runtime_ref_value)
        or (existing.pane_id if existing is not None else None)
    )


def preferred_active_pane_id(existing, *, active_pane_id: str | None, pane_id_value: str | None) -> str | None:
    return (
        normalized_text(active_pane_id)
        or pane_id_value
        or (existing.active_pane_id if existing is not None else None)
    )


def preferred_slot_key(existing, *, spec_name: str, slot_key: str | None) -> str:
    return normalized_text(slot_key) or (existing.slot_key if existing is not None else None) or spec_name


def preferred_workspace_epoch(existing, *, workspace_epoch: int | None) -> int | None:
    if workspace_epoch is not None:
        return int(workspace_epoch)
    if existing is not None:
        return existing.workspace_epoch
    return None


def next_runtime_pid(existing, *, runtime_pid: int | None, pid: int | None) -> int | None:
    if runtime_pid is not None:
        return runtime_pid
    if pid is not None:
        return pid
    return existing.runtime_pid if existing is not None else None


def next_lifecycle_state(existing, *, lifecycle_state: str | None, next_state) -> str | None:
    return (
        normalized_text(lifecycle_state)
        or (existing.lifecycle_state if existing is not None else None)
        or next_state.value
    )


def next_authority_epoch_generations(
    existing,
    *,
    authority_epoch_changed: bool,
) -> tuple[int, int]:
    if existing is None:
        return 1, 1
    current_binding = positive_generation(getattr(existing, 'binding_generation', None))
    current_runtime = positive_generation(getattr(existing, 'runtime_generation', None))
    current_epoch = max(current_binding, current_runtime)
    if authority_epoch_changed:
        next_epoch = current_epoch + 1 if current_epoch > 0 else 1
        return next_epoch, next_epoch
    binding_generation = current_binding if current_binding > 0 else 1
    runtime_generation = current_runtime if current_runtime > 0 else binding_generation
    return binding_generation, runtime_generation


def positive_generation(value: object) -> int:
    try:
        generation = int(value or 0)
    except Exception:
        return 0
    return generation if generation > 0 else 0


def next_daemon_generation(existing, *, daemon_generation: int | None) -> int | None:
    if daemon_generation is not None:
        return int(daemon_generation)
    if existing is None:
        return None
    current = getattr(existing, 'daemon_generation', None)
    return int(current) if current is not None else None


def runtime_authority_changed(
    existing,
    *,
    runtime_ref: str | None,
    session_ref: str | None,
    runtime_root: str | None,
    runtime_pid: int | None,
    pane_id: str | None,
    active_pane_id: str | None,
    tmux_socket_name: str | None,
    tmux_socket_path: str | None,
    tmux_window_name: str | None,
    tmux_window_id: str | None,
    daemon_generation: int | None,
) -> bool:
    if existing is None:
        return True
    identity_changed = any(
        (
            runtime_ref != getattr(existing, 'runtime_ref', None),
            session_ref != getattr(existing, 'session_ref', None),
            runtime_root != getattr(existing, 'runtime_root', None),
            runtime_pid != getattr(existing, 'runtime_pid', None),
            pane_id != getattr(existing, 'pane_id', None),
            active_pane_id != getattr(existing, 'active_pane_id', None),
            tmux_socket_name != getattr(existing, 'tmux_socket_name', None),
            tmux_socket_path != getattr(existing, 'tmux_socket_path', None),
            tmux_window_name != getattr(existing, 'tmux_window_name', None),
            tmux_window_id != getattr(existing, 'tmux_window_id', None),
        )
    )
    current_daemon_generation = getattr(existing, 'daemon_generation', None)
    daemon_generation_changed = daemon_generation != (
        int(current_daemon_generation) if current_daemon_generation is not None else None
    )
    return identity_changed or daemon_generation_changed


__all__ = ['resolve_attach_runtime_values']
