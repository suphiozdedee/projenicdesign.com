from __future__ import annotations

import re
import uuid


_STARTUP_RUN_ID_PATTERN = re.compile(r'start_[0-9a-f]{32}')


def new_startup_run_id() -> str:
    """Return a correlation identity for one start command transaction."""

    return f'start_{uuid.uuid4().hex}'


def resolve_startup_run_id(value: object, *, generate_if_missing: bool = True) -> str | None:
    """Validate a supplied identity, optionally creating one for legacy clients."""

    normalized = str(value or '').strip()
    if not normalized:
        return new_startup_run_id() if generate_if_missing else None
    if _STARTUP_RUN_ID_PATTERN.fullmatch(normalized) is None:
        raise ValueError('startup_run_id must match start_<32 lowercase hex characters>')
    return normalized


__all__ = ['new_startup_run_id', 'resolve_startup_run_id']
