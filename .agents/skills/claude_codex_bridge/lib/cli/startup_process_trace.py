"""Benchmark-only process bootstrap timing handoff.

The source benchmark starts in ``ccb_test`` and then ``exec``-replaces that
wrapper with ``ccb.py``.  Absolute monotonic timestamps are accepted only from
that source-test envelope, consumed before the regular CLI import fan-out, and
kept in process memory.  Only durations and the random trace id may reach CLI
output.
"""

from __future__ import annotations

import os


PROCESS_BOOTSTRAP_TIMING_KEYS = (
    "popen_begin_to_ccb_test_entry",
    "ccb_test_entry_to_pre_exec",
    "ccb_test_pre_exec_to_ccb_py_entry",
    "ccb_py_entry_to_main",
    "ccb_py_main_to_cli_start",
)

_RAW_TRACE_ENV_KEYS = (
    "CCB_STARTUP_TIMING_TRACE",
    "CCB_STARTUP_TRACE_ID",
    "CCB_STARTUP_TRACE_SPAWN_NS",
    "CCB_STARTUP_TRACE_WRAPPER_ENTRY_NS",
    "CCB_STARTUP_TRACE_WRAPPER_PRE_EXEC_NS",
)
_trace_state: tuple[str, tuple[int, ...]] | None = None


def capture_source_wrapper_trace(ccb_py_entry_ns: int) -> None:
    """Consume a valid source-wrapper trace envelope as early as possible."""

    global _trace_state
    raw = {key: os.environ.get(key) for key in _RAW_TRACE_ENV_KEYS}
    for key in _RAW_TRACE_ENV_KEYS:
        os.environ.pop(key, None)
    _trace_state = None
    if raw["CCB_STARTUP_TIMING_TRACE"] != "1":
        return
    if os.environ.get("CCB_TEST_ENTRYPOINT") != "1":
        return
    trace_id = str(raw["CCB_STARTUP_TRACE_ID"] or "")
    if not _valid_trace_id(trace_id):
        return
    try:
        points = (
            int(str(raw["CCB_STARTUP_TRACE_SPAWN_NS"] or "")),
            int(str(raw["CCB_STARTUP_TRACE_WRAPPER_ENTRY_NS"] or "")),
            int(str(raw["CCB_STARTUP_TRACE_WRAPPER_PRE_EXEC_NS"] or "")),
            int(ccb_py_entry_ns),
        )
    except (TypeError, ValueError):
        return
    if not _strictly_valid_points(points):
        return
    _trace_state = (trace_id, points)


def mark_ccb_main(ccb_py_main_ns: int) -> None:
    """Append the post-import ``ccb.py`` main checkpoint when trace is valid."""

    global _trace_state
    if _trace_state is None:
        return
    trace_id, points = _trace_state
    candidate = (*points, int(ccb_py_main_ns))
    if not _strictly_valid_points(candidate):
        _trace_state = None
        return
    _trace_state = (trace_id, candidate)


def consume_process_bootstrap_trace(
    cli_started_ns: int,
) -> tuple[str | None, dict[str, float] | None, int | None]:
    """Return non-overlapping durations and erase the in-process raw state."""

    global _trace_state
    state = _trace_state
    _trace_state = None
    if state is None:
        return None, None, None
    trace_id, points = state
    candidate = (*points, int(cli_started_ns))
    if len(candidate) != len(PROCESS_BOOTSTRAP_TIMING_KEYS) + 1:
        return None, None, None
    if not _strictly_valid_points(candidate):
        return None, None, None
    timings = {
        label: (right - left) / 1_000_000.0
        for label, left, right in zip(
            PROCESS_BOOTSTRAP_TIMING_KEYS,
            candidate,
            candidate[1:],
        )
    }
    return trace_id, timings, points[3]


def _strictly_valid_points(points: tuple[int, ...]) -> bool:
    return bool(points) and all(value > 0 for value in points) and all(
        left <= right for left, right in zip(points, points[1:])
    )


def _valid_trace_id(value: str) -> bool:
    suffix = value.removeprefix("trace_")
    return len(value) == 38 and len(suffix) == 32 and all(
        character in "0123456789abcdef" for character in suffix
    )


__all__ = [
    "PROCESS_BOOTSTRAP_TIMING_KEYS",
    "capture_source_wrapper_trace",
    "consume_process_bootstrap_trace",
    "mark_ccb_main",
]
