from __future__ import annotations

import random
from typing import Any, Callable


NETWORK_ERRORS = {
    "httpConnectionFailed",
    "responseStreamConnectionFailed",
    "responseStreamDisconnected",
    "responseTooManyFailedAttempts",
}


def codex_error_class(error: dict[str, Any] | None) -> str:
    if not isinstance(error, dict):
        return "unknown"
    info = error.get("codexErrorInfo")
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and len(info) == 1:
        key = next(iter(info))
        if isinstance(key, str):
            return key
    return "unknown"


def full_jitter_delay(
    attempt: int,
    *,
    base_seconds: float = 1.0,
    cap_seconds: float = 60.0,
    random_value: Callable[[], float] = random.random,
) -> float:
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    if base_seconds <= 0 or cap_seconds <= 0:
        raise ValueError("backoff durations must be positive")
    ceiling = min(cap_seconds, base_seconds * (2**attempt))
    return max(0.0, min(1.0, random_value())) * ceiling
