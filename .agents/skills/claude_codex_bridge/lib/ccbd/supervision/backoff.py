from __future__ import annotations

from datetime import timedelta
from pathlib import Path

RECOVERY_BACKOFF_DELAYS_S = (30, 60, 120, 300, 600, 1800)


def is_in_backoff_window(runtime, *, now: str, parse_utc_timestamp_fn, backoff_delay_seconds_fn) -> bool:
    if not str(runtime.last_failure_reason or '').strip():
        return False
    if not str(runtime.last_reconcile_at or '').strip():
        return False
    try:
        checked_at = parse_utc_timestamp_fn(now)
        prior_attempt_at = parse_utc_timestamp_fn(runtime.last_reconcile_at)
    except Exception:
        return False
    recovery_failures = int(getattr(runtime, 'recovery_failure_count', 0) or 0)
    delay_s = (
        recovery_backoff_delay_seconds(recovery_failures)
        if recovery_failures > 0
        else backoff_delay_seconds_fn(runtime.restart_count)
    )
    return checked_at < (prior_attempt_at + timedelta(seconds=delay_s))


def backoff_delay_seconds(restart_count: int) -> int:
    failures = max(1, int(restart_count or 0))
    return min(2 ** (failures - 1), 30)


def recovery_backoff_delay_seconds(recovery_failure_count: int) -> int:
    failures = max(1, int(recovery_failure_count or 0))
    index = min(failures, len(RECOVERY_BACKOFF_DELAYS_S)) - 1
    return RECOVERY_BACKOFF_DELAYS_S[index]


def same_socket_path(left: str, right: str) -> bool:
    left_text = str(left or '').strip()
    right_text = str(right or '').strip()
    if not left_text or not right_text:
        return False
    try:
        return Path(left_text).expanduser().resolve() == Path(right_text).expanduser().resolve()
    except Exception:
        return left_text == right_text
