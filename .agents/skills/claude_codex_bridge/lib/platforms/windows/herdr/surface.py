from __future__ import annotations

from collections.abc import Mapping

from platforms.windows.herdr.ccbd_surface_projection import build_herdr_surface_projection
from provider_runtime.session_payload import redacted_restore_tokens


def herdr_surface_projection_from_remote(remote: Mapping[str, object] | None) -> dict[str, object] | None:
    if not isinstance(remote, Mapping):
        return None
    projection = _mapping(remote.get('herdr_surface_projection'))
    if projection.get('backend_impl') != 'herdr':
        return None
    return redacted_restore_tokens(dict(projection))


def herdr_surface_projection_from_namespace_state(namespace_state: object | None) -> dict[str, object] | None:
    if namespace_state is None:
        return None
    if str(getattr(namespace_state, 'backend_impl', '') or '').strip() != 'herdr':
        return None
    namespace_ref = getattr(namespace_state, 'namespace_ref', None)
    if not callable(namespace_ref):
        return None
    projection = build_herdr_surface_projection(
        {
            'backend_impl': 'herdr',
            'namespace_ref': namespace_ref(),
        }
    )
    return dict(projection) if projection is not None else None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


__all__ = [
    'herdr_surface_projection_from_namespace_state',
    'herdr_surface_projection_from_remote',
]
