from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .api import (
        LayoutResult,
        TerminalBackend,
        TmuxBackend,
        create_auto_layout,
        detect_terminal,
        get_backend,
        get_backend_for_namespace_teardown,
        get_backend_for_session,
        get_pane_id_from_session,
        get_shell_type,
        is_windows,
        is_wsl,
    )

__all__ = [
    "LayoutResult",
    "TerminalBackend",
    "TmuxBackend",
    "create_auto_layout",
    "detect_terminal",
    "get_backend",
    "get_backend_for_namespace_teardown",
    "get_backend_for_session",
    "get_pane_id_from_session",
    "get_shell_type",
    "is_windows",
    "is_wsl",
]

_API_EXPORTS = frozenset(__all__)


def __getattr__(name: str) -> Any:
    if name not in _API_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".api", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _API_EXPORTS)
