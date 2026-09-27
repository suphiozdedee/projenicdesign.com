from __future__ import annotations

from collections.abc import Mapping

from completion.models import CompletionSourceKind


PROTECTED_PROVIDER_RUNTIME_KEYS = frozenset(
    {
        'backend_family',
        'backend_impl',
        'completion_source',
        'completion_source_kind',
        'managed_home',
        'namespace_ref',
        'namespace_restore_token_present',
        'pane_id',
        'pane_ref',
        'provider_runtime_backend_ref',
        'terminal',
        'tmux_session',
        'tmux_socket_name',
        'tmux_socket_path',
    }
)


def completion_source_for_kind(kind: object) -> str:
    value = _completion_source_kind_value(kind)
    if value == CompletionSourceKind.PROTOCOL_EVENT_STREAM.value:
        return 'provider_event_stream'
    if value == CompletionSourceKind.TERMINAL_TEXT.value:
        return 'terminal_capture'
    return 'provider_native_log'


def redacted_namespace_ref(namespace_ref: Mapping[str, object] | None) -> dict[str, object] | None:
    if not isinstance(namespace_ref, Mapping):
        return None
    return {str(key): value for key, value in namespace_ref.items() if str(key) != 'restore_token'}


def redacted_provider_runtime_backend_ref(
    backend_ref: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if not isinstance(backend_ref, Mapping):
        return None
    payload = {str(key): value for key, value in backend_ref.items() if str(key) != 'restore_token'}
    namespace_ref = payload.get('namespace_ref')
    if isinstance(namespace_ref, Mapping):
        payload['namespace_ref'] = redacted_namespace_ref(namespace_ref)
    return payload


def redacted_restore_tokens(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): redacted_restore_tokens(item)
            for key, item in value.items()
            if str(key) != 'restore_token'
        }
    if isinstance(value, list):
        return [redacted_restore_tokens(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redacted_restore_tokens(item) for item in value)
    return value


def namespace_restore_token_present(namespace_ref: Mapping[str, object] | None) -> bool:
    if not isinstance(namespace_ref, Mapping):
        return False
    token = namespace_ref.get('restore_token')
    return bool(str(token or '').strip())


def build_provider_runtime_backend_ref(
    *,
    provider: str,
    agent_slug: str,
    backend_impl: str,
    namespace_ref: Mapping[str, object] | None,
    pane_ref: Mapping[str, object] | None,
    managed_home: str | None,
    completion_source_kind: object,
) -> dict[str, object]:
    completion_kind = _completion_source_kind_value(completion_source_kind)
    payload: dict[str, object] = {
        'provider': provider,
        'agent_slug': agent_slug,
        'backend_impl': backend_impl,
        'completion_source': completion_source_for_kind(completion_kind),
        'completion_source_kind': completion_kind,
    }
    redacted_namespace = redacted_namespace_ref(namespace_ref)
    if redacted_namespace is not None:
        payload['namespace_ref'] = redacted_namespace
    if isinstance(pane_ref, Mapping):
        payload['pane_ref'] = dict(pane_ref)
    if managed_home:
        payload['managed_home'] = managed_home
    return payload


def merge_provider_payload(
    shared_payload: dict[str, object],
    provider_payload: Mapping[str, object],
) -> list[str]:
    conflicts: list[str] = []
    for key, value in provider_payload.items():
        key_text = str(key)
        if key_text in PROTECTED_PROVIDER_RUNTIME_KEYS and key_text in shared_payload:
            conflicts.append(key_text)
            continue
        shared_payload[key_text] = value
    conflicts.sort()
    if conflicts:
        shared_payload['provider_payload_conflicts'] = conflicts
    return conflicts


def managed_home_from_provider_payload(
    provider: str,
    provider_payload: Mapping[str, object],
) -> str | None:
    provider_key = f'{provider.strip().lower()}_home'
    candidates = (
        provider_key,
        'codex_home',
        'claude_home',
        'gemini_home',
        'opencode_home',
        'managed_home',
        'provider_home',
    )
    for key in candidates:
        value = provider_payload.get(key)
        text = str(value or '').strip()
        if text:
            return text
    return None


def backend_impl_from_session(session_data: Mapping[str, object]) -> str | None:
    backend_ref = session_data.get('provider_runtime_backend_ref')
    if isinstance(backend_ref, Mapping):
        value = str(backend_ref.get('backend_impl') or '').strip()
        if value:
            return value
    value = str(session_data.get('backend_impl') or '').strip()
    if value:
        return value
    terminal = str(session_data.get('terminal') or '').strip()
    if terminal in {'tmux', 'rmux', 'psmux'}:
        return terminal
    return None


def namespace_ref_from_session(session_data: Mapping[str, object]) -> dict[str, object] | None:
    backend_ref = session_data.get('provider_runtime_backend_ref')
    if isinstance(backend_ref, Mapping):
        namespace_ref = backend_ref.get('namespace_ref')
        if isinstance(namespace_ref, Mapping):
            return dict(namespace_ref)
    namespace_ref = session_data.get('namespace_ref')
    if isinstance(namespace_ref, Mapping):
        return dict(namespace_ref)
    return None


def pane_id_from_session(session_data: Mapping[str, object]) -> str | None:
    for container in (
        session_data.get('pane_ref'),
        _provider_runtime_pane_ref(session_data),
    ):
        if isinstance(container, Mapping):
            pane_id = str(container.get('pane_id') or '').strip()
            if pane_id:
                return pane_id
    for key in ('pane_id', 'tmux_session'):
        pane_id = str(session_data.get(key) or '').strip()
        if pane_id:
            return pane_id
    return None


def pane_ref_from_session(session_data: Mapping[str, object]) -> dict[str, object] | None:
    pane_ref = session_data.get('pane_ref')
    if isinstance(pane_ref, Mapping):
        return dict(pane_ref)
    backend_ref = session_data.get('provider_runtime_backend_ref')
    if isinstance(backend_ref, Mapping):
        pane_ref = backend_ref.get('pane_ref')
        if isinstance(pane_ref, Mapping):
            return dict(pane_ref)
    return None


def session_uses_tmux_compatible_pane(session_data: Mapping[str, object]) -> bool:
    backend_impl = backend_impl_from_session(session_data)
    if backend_impl == 'herdr':
        return False
    terminal = str(session_data.get('terminal') or '').strip().lower()
    if terminal == 'mux':
        return backend_impl in {'tmux', 'rmux', 'psmux'}
    if terminal in {'tmux', 'rmux', 'psmux'}:
        return True
    return backend_impl in {'tmux', 'rmux', 'psmux'}


def _provider_runtime_pane_ref(session_data: Mapping[str, object]) -> Mapping[str, object] | None:
    backend_ref = session_data.get('provider_runtime_backend_ref')
    if not isinstance(backend_ref, Mapping):
        return None
    pane_ref = backend_ref.get('pane_ref')
    return pane_ref if isinstance(pane_ref, Mapping) else None


def _completion_source_kind_value(kind: object) -> str:
    if isinstance(kind, CompletionSourceKind):
        return kind.value
    value = str(kind or '').strip()
    try:
        return CompletionSourceKind(value).value
    except ValueError:
        return CompletionSourceKind.TERMINAL_TEXT.value


__all__ = [
    'PROTECTED_PROVIDER_RUNTIME_KEYS',
    'backend_impl_from_session',
    'build_provider_runtime_backend_ref',
    'completion_source_for_kind',
    'managed_home_from_provider_payload',
    'merge_provider_payload',
    'namespace_ref_from_session',
    'namespace_restore_token_present',
    'pane_id_from_session',
    'pane_ref_from_session',
    'redacted_provider_runtime_backend_ref',
    'redacted_namespace_ref',
    'redacted_restore_tokens',
    'session_uses_tmux_compatible_pane',
]
