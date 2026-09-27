from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import MutableMapping


EXPECTED_STARTUP_ID_ENV = 'CCB_CCBD_EXPECTED_STARTUP_ID'
EXPECTED_GENERATION_ENV = 'CCB_CCBD_EXPECTED_GENERATION'
KEEPER_STARTUP_ACCEPTED_PERF_COUNTER_NS_ENV = (
    'CCB_CCBD_KEEPER_STARTUP_ACCEPTED_PERF_COUNTER_NS'
)
_STARTUP_ID_PATTERN = re.compile(r'[0-9a-f]{32}')


class StartupFenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExpectedStartupFence:
    startup_id: str
    generation: int

    def __post_init__(self) -> None:
        startup_id = str(self.startup_id or '').strip()
        if _STARTUP_ID_PATTERN.fullmatch(startup_id) is None:
            raise ValueError('expected startup_id must be 32 lowercase hex characters')
        if int(self.generation) <= 0:
            raise ValueError('expected startup generation must be positive')


@dataclass(frozen=True)
class KeeperStartupCheckpoint:
    startup_id: str
    generation: int
    accepted_perf_counter_ns: int


def consume_expected_startup_fence(
    environ: MutableMapping[str, str] | None = None,
) -> ExpectedStartupFence | None:
    target = os.environ if environ is None else environ
    startup_id = str(target.pop(EXPECTED_STARTUP_ID_ENV, '') or '').strip()
    generation_text = str(target.pop(EXPECTED_GENERATION_ENV, '') or '').strip()
    if not startup_id and not generation_text:
        return None
    if not startup_id or not generation_text:
        raise StartupFenceError('expected startup fence requires both startup_id and generation')
    try:
        generation = int(generation_text)
    except ValueError as exc:
        raise StartupFenceError('expected startup generation must be a positive integer') from exc
    try:
        return ExpectedStartupFence(startup_id=startup_id, generation=generation)
    except ValueError as exc:
        raise StartupFenceError(str(exc)) from exc


def consume_keeper_startup_checkpoint(
    fence: ExpectedStartupFence | None,
    environ: MutableMapping[str, str] | None = None,
) -> KeeperStartupCheckpoint | None:
    target = os.environ if environ is None else environ
    raw_value = str(
        target.pop(KEEPER_STARTUP_ACCEPTED_PERF_COUNTER_NS_ENV, '') or ''
    ).strip()
    if fence is None or not raw_value:
        return None
    try:
        accepted_ns = int(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None
    if accepted_ns <= 0 or str(accepted_ns) != raw_value:
        return None
    return KeeperStartupCheckpoint(
        startup_id=fence.startup_id,
        generation=fence.generation,
        accepted_perf_counter_ns=accepted_ns,
    )


def validate_expected_startup_lifecycle(
    fence: ExpectedStartupFence,
    lifecycle,
    *,
    project_id: str,
    config_signature: str,
    socket_path: str | Path,
) -> None:
    if lifecycle is None:
        raise StartupFenceError('expected startup lifecycle is missing')
    checks = (
        (str(lifecycle.project_id) == str(project_id), 'project_id mismatch'),
        (str(lifecycle.desired_state) == 'running', 'desired_state is not running'),
        (str(lifecycle.phase) == 'starting', 'phase is not starting'),
        (str(lifecycle.startup_id or '') == fence.startup_id, 'startup_id mismatch'),
        (int(lifecycle.generation) == fence.generation, 'generation mismatch'),
        (
            str(lifecycle.config_signature or '') == str(config_signature or ''),
            'config_signature mismatch',
        ),
        (
            _normalized_path(lifecycle.socket_path) == _normalized_path(socket_path),
            'socket_path mismatch',
        ),
    )
    for valid, reason in checks:
        if not valid:
            raise StartupFenceError(f'expected startup lifecycle rejected: {reason}')


def _normalized_path(value: str | Path | None) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    return os.path.abspath(os.path.expanduser(text))


__all__ = [
    'EXPECTED_GENERATION_ENV',
    'EXPECTED_STARTUP_ID_ENV',
    'ExpectedStartupFence',
    'KEEPER_STARTUP_ACCEPTED_PERF_COUNTER_NS_ENV',
    'KeeperStartupCheckpoint',
    'StartupFenceError',
    'consume_expected_startup_fence',
    'consume_keeper_startup_checkpoint',
    'validate_expected_startup_lifecycle',
]
