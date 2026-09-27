from __future__ import annotations

from typing import Mapping

NAMESPACE_BACKEND_FAMILY = 'tmux-family'
HERDR_BACKEND_FAMILY = 'herdr-native'
HERDR_IPC_KIND = 'herdr_socket'


def resolved_namespace_backend_family(backend_impl: object, raw_family: object = None) -> str:
    family = str(raw_family or '').strip()
    if family:
        return family
    impl = str(backend_impl or '').strip()
    return HERDR_BACKEND_FAMILY if impl == 'herdr' else NAMESPACE_BACKEND_FAMILY


def redacted_namespace_projection(fields: Mapping[str, object]) -> dict[str, object]:
    restore_token = fields.get('namespace_restore_token')
    return {
        'namespace_backend_family': resolved_namespace_backend_family(
            fields.get('backend_impl'),
            fields.get('namespace_backend_family'),
        ),
        'namespace_backend_impl': str(fields.get('backend_impl') or 'tmux'),
        'namespace_id': _clean(fields.get('namespace_id')),
        'namespace_session_name': _clean(fields.get('namespace_session_name')),
        'namespace_ipc_kind': _clean(fields.get('namespace_ipc_kind')),
        'namespace_ipc_ref': _clean(fields.get('namespace_ipc_ref')),
        'namespace_restore_token_present': bool(_clean(restore_token)),
    }


def _clean(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


__all__ = [
    'HERDR_BACKEND_FAMILY',
    'HERDR_IPC_KIND',
    'NAMESPACE_BACKEND_FAMILY',
    'redacted_namespace_projection',
    'resolved_namespace_backend_family',
]
