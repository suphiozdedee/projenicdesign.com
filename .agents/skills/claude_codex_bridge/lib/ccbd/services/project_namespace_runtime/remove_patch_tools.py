from __future__ import annotations

from .backend import kill_window, session_window_target


def remove_tool_windows(
    backend,
    *,
    old_topology,
    new_topology,
    current,
    result,
    timeout_s: float | None,
) -> None:
    old_windows = _window_map(old_topology)
    new_windows = _window_map(new_topology)
    for window_name, old_window in old_windows.items():
        if window_name in new_windows:
            continue
        if str(getattr(old_window, 'kind', '') or '') != 'tool':
            continue
        _kill_tool_window_if_present(
            backend,
            target=session_window_target(current.tmux_session_name, window_name),
            timeout_s=timeout_s,
        )
        _append_unique(result.removed_windows, window_name)


def _window_map(topology) -> dict[str, object]:
    return {
        str(window.name): window
        for window in tuple(getattr(topology, 'windows', ()) or ())
    }


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _kill_tool_window_if_present(backend, *, target: str, timeout_s: float | None) -> None:
    try:
        kill_window(backend, target=target, timeout_s=timeout_s)
    except Exception as exc:
        if _window_missing_error(exc):
            return
        raise


def _window_missing_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "can't find window" in text
        or 'window not found' in text
        or 'no such window' in text
    )


__all__ = ['remove_tool_windows']
