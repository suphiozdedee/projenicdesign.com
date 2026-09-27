from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from provider_core.instance_resolution import named_agent_instance
from provider_core.session_binding_evidence import (
    session_ccb_session_id,
    session_file,
    session_id,
    session_pane_title_marker,
    session_ref,
    session_runtime_pid,
    session_runtime_ref,
    session_runtime_root,
    session_terminal,
    session_tmux_socket_name,
    session_tmux_socket_path,
)
from provider_runtime.session_payload import (
    namespace_ref_from_session,
    namespace_restore_token_present,
    pane_ref_from_session,
    redacted_namespace_ref,
    redacted_provider_runtime_backend_ref,
)


@dataclass(frozen=True)
class ProviderRuntimeFacts:
    runtime_ref: str | None
    session_ref: str | None
    runtime_root: str | None
    runtime_pid: int | None
    terminal_backend: str | None
    pane_id: str | None
    pane_title_marker: str | None
    pane_state: str | None
    tmux_socket_name: str | None
    tmux_socket_path: str | None
    session_file: str | None
    session_id: str | None
    ccb_session_id: str | None
    provider_runtime_backend_ref: dict[str, object] | None = None
    namespace_ref: dict[str, object] | None = None
    pane_ref: dict[str, object] | None = None
    namespace_restore_token_present: bool = False
    herdr_auto_restore_mode: str | None = None
    herdr_agent_state_ref: str | None = None


def load_provider_session(binding, workspace_path: Path, agent_name: str):
    instance = named_agent_instance(agent_name, primary_agent=str(getattr(binding, "provider", "") or ""))
    try:
        return binding.load_session(workspace_path, instance)
    except Exception:
        return None


def ensure_provider_pane(session) -> tuple[bool, str]:
    ensure = getattr(session, 'ensure_pane', None)
    if not callable(ensure):
        return False, 'ensure_pane not supported'
    try:
        return ensure()
    except Exception as exc:
        return False, str(exc)


def build_provider_runtime_facts(
    session,
    *,
    binding,
    provider: str,
    pane_id_override: str | None = None,
) -> ProviderRuntimeFacts:
    pane_id = str(pane_id_override or getattr(session, 'pane_id', '') or '').strip() or None
    session_data = getattr(session, 'data', None)
    session_data = session_data if isinstance(session_data, Mapping) else {}
    raw_backend_ref = session_data.get('provider_runtime_backend_ref')
    raw_backend_ref = raw_backend_ref if isinstance(raw_backend_ref, Mapping) else None
    backend_ref = redacted_provider_runtime_backend_ref(raw_backend_ref)
    raw_namespace_ref = namespace_ref_from_session(session_data)
    namespace_ref = redacted_namespace_ref(raw_namespace_ref)
    pane_ref = pane_ref_from_session(session_data)
    backend_namespace_ref = raw_backend_ref.get('namespace_ref') if raw_backend_ref is not None else None
    session_namespace_ref = session_data.get('namespace_ref')
    restore_token_present = (
        bool(session_data.get('namespace_restore_token_present', False))
        or namespace_restore_token_present(raw_namespace_ref)
        or namespace_restore_token_present(
            backend_namespace_ref if isinstance(backend_namespace_ref, Mapping) else None
        )
        or namespace_restore_token_present(
            session_namespace_ref if isinstance(session_namespace_ref, Mapping) else None
        )
    )
    auto_restore_mode = _optional_text(
        session_data.get('herdr_auto_restore_mode')
        or (backend_ref or {}).get('herdr_auto_restore_mode')
    )
    agent_state_ref = _optional_text(
        session_data.get('herdr_agent_state_ref')
        or (backend_ref or {}).get('herdr_agent_state_ref')
    )
    return ProviderRuntimeFacts(
        runtime_ref=session_runtime_ref(session, pane_id_override=pane_id),
        session_ref=session_ref(
            session,
            session_id_attr=binding.session_id_attr,
            session_path_attr=binding.session_path_attr,
        ),
        runtime_root=session_runtime_root(session),
        runtime_pid=session_runtime_pid(session, provider=provider),
        terminal_backend=session_terminal(session),
        provider_runtime_backend_ref=backend_ref,
        namespace_ref=namespace_ref,
        pane_ref=pane_ref,
        namespace_restore_token_present=restore_token_present,
        herdr_auto_restore_mode=auto_restore_mode,
        herdr_agent_state_ref=agent_state_ref,
        pane_id=pane_id,
        pane_title_marker=session_pane_title_marker(session),
        pane_state='alive' if pane_id else None,
        tmux_socket_name=session_tmux_socket_name(session),
        tmux_socket_path=session_tmux_socket_path(session),
        session_file=session_file(session),
        session_id=session_id(session, session_id_attr=binding.session_id_attr),
        ccb_session_id=session_ccb_session_id(session),
    )


def _optional_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


__all__ = [
    'ProviderRuntimeFacts',
    'build_provider_runtime_facts',
    'ensure_provider_pane',
    'load_provider_session',
]
