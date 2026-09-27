from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping, Sequence


READINESS_SCHEMA_VERSION = 1
READINESS_POINT_NAMES = (
    'T0_cli_entry',
    'T1_lifecycle_intent',
    'T2_control_plane_ready',
    'T3_namespace_attachable',
    'T4_requested_agents_ready',
    'T5_foreground_attached',
    'T6_fully_warm',
)
_TRACE_ID_PATTERN = re.compile(r'^trace_[0-9a-f]{32}$')
_MAX_ENVELOPE_AGE_NS = 60 * 60 * 1_000_000_000
_TERMINAL_POINT_STATUSES = frozenset(
    {
        'reached',
        'observed_upper_bound',
        'not_required_already_mounted',
        'not_applicable_no_attach',
        'not_applicable_no_namespace',
        'not_reached_at_rpc_return',
        'failed_before_ready',
    }
)


class StartupReadinessRecorder:
    """In-memory, diagnostics-only readiness milestones for one startup RPC."""

    __slots__ = (
        '_attach_mode',
        '_desired_agents',
        '_effective_requested_agents',
        '_expected_daemon_generation',
        '_keeper_startup_id',
        '_origin_ns',
        '_points',
        '_rpc_accepted_ms',
        '_trace_id',
    )

    def __init__(
        self,
        *,
        trace_id: str,
        origin_ns: int,
        attach_mode: str,
        expected_daemon_generation: int | None,
        keeper_startup_id: str | None,
        points: Mapping[str, Mapping[str, object]],
    ) -> None:
        self._trace_id = trace_id
        self._origin_ns = origin_ns
        self._attach_mode = attach_mode
        self._expected_daemon_generation = expected_daemon_generation
        self._keeper_startup_id = keeper_startup_id
        self._points = {key: dict(value) for key, value in points.items()}
        self._rpc_accepted_ms: float | None = None
        self._effective_requested_agents: tuple[str, ...] = ()
        self._desired_agents: tuple[str, ...] = ()

    @classmethod
    def from_rpc_payload(
        cls,
        payload: object,
        *,
        now_ns: int | None = None,
        trusted_keeper_checkpoint=None,
    ) -> StartupReadinessRecorder | None:
        try:
            if not isinstance(payload, Mapping) or payload.get('schema_version') != 1:
                return None
            trace_id = str(payload.get('trace_id') or '').strip()
            if not _TRACE_ID_PATTERN.fullmatch(trace_id):
                return None
            origin_ns = _positive_int(payload.get('origin_monotonic_ns'))
            observed_now_ns = int(time.perf_counter_ns() if now_ns is None else now_ns)
            if (
                origin_ns is None
                or observed_now_ns < origin_ns
                or observed_now_ns - origin_ns > _MAX_ENVELOPE_AGE_NS
            ):
                return None
            attach_mode = str(payload.get('attach_mode') or '').strip()
            if attach_mode not in {'no_attach', 'interactive'}:
                return None
            expected_generation = _positive_json_int(
                payload.get('expected_daemon_generation')
            )
            if expected_generation is None:
                return None
            keeper_startup_id = _optional_text(payload.get('keeper_startup_id'))
            t1 = _clean_input_point(
                payload.get('T1_lifecycle_intent'),
                allowed_statuses={
                    'observed_upper_bound',
                    'not_required_already_mounted',
                },
            )
            t2 = _clean_input_point(
                payload.get('T2_control_plane_ready'),
                allowed_statuses={'reached'},
            )
            if t1 is None or t2 is None or t2.get('elapsed_ms') is None:
                return None
            max_elapsed_ms = (observed_now_ns - origin_ns) / 1_000_000.0
            if float(t2['elapsed_ms']) > max_elapsed_ms + 0.01:
                return None
            if (
                t1.get('elapsed_ms') is not None
                and float(t1['elapsed_ms']) > float(t2['elapsed_ms']) + 0.01
            ):
                return None
            exact_t1 = _exact_keeper_t1_point(
                trusted_keeper_checkpoint,
                origin_ns=origin_ns,
                observed_now_ns=observed_now_ns,
                expected_generation=expected_generation,
                keeper_startup_id=keeper_startup_id,
                input_t1=t1,
                t2_elapsed_ms=float(t2['elapsed_ms']),
            )
            if exact_t1 is not None:
                t1 = exact_t1
            points: dict[str, Mapping[str, object]] = {
                'T0_cli_entry': _point_record(
                    status='reached',
                    elapsed_ms=0.0,
                    source='ccb_py_process_entry',
                    agents=(),
                ),
                'T1_lifecycle_intent': t1,
                'T2_control_plane_ready': t2,
            }
            if attach_mode == 'no_attach':
                points['T5_foreground_attached'] = _point_record(
                    status='not_applicable_no_attach',
                    elapsed_ms=None,
                    source='ccb_no_attach',
                    agents=(),
                )
            return cls(
                trace_id=trace_id,
                origin_ns=origin_ns,
                attach_mode=attach_mode,
                expected_daemon_generation=expected_generation,
                keeper_startup_id=keeper_startup_id,
                points=points,
            )
        except Exception:
            return None

    def mark(
        self,
        point_name: str,
        *,
        status: str = 'reached',
        source: str,
        agents: Sequence[str] = (),
        now_ns: int | None = None,
    ) -> None:
        try:
            name = str(point_name or '').strip()
            clean_status = str(status or '').strip()
            if name == 'rpc_accepted':
                if self._rpc_accepted_ms is not None:
                    return
                observed_ns = int(time.perf_counter_ns() if now_ns is None else now_ns)
                if observed_ns >= self._origin_ns:
                    self._rpc_accepted_ms = (observed_ns - self._origin_ns) / 1_000_000.0
                return
            if name not in READINESS_POINT_NAMES or clean_status not in _TERMINAL_POINT_STATUSES:
                return
            if name in self._points:
                return
            observed_ns = int(time.perf_counter_ns() if now_ns is None else now_ns)
            if observed_ns < self._origin_ns:
                return
            elapsed_ms = (
                (observed_ns - self._origin_ns) / 1_000_000.0
                if clean_status in {'reached', 'observed_upper_bound', 'failed_before_ready'}
                else None
            )
            self._points[name] = _point_record(
                status=clean_status,
                elapsed_ms=elapsed_ms,
                source=_optional_text(source) or 'daemon_recorder',
                agents=_clean_agents(agents),
            )
        except Exception:
            return

    def set_agent_scopes(
        self,
        effective_requested_agents: Sequence[str],
        desired_agents: Sequence[str],
    ) -> None:
        try:
            self._effective_requested_agents = _clean_agents(effective_requested_agents)
            self._desired_agents = _clean_agents(desired_agents)
        except Exception:
            return

    def to_record(
        self,
        *,
        startup_run_id: str | None,
        daemon_generation: int | None,
    ) -> dict[str, object]:
        try:
            actual_generation = _positive_json_int(daemon_generation)
            generation_matches = actual_generation == self._expected_daemon_generation
            points = {
                name: dict(
                    self._points.get(name)
                    or _point_record(
                        status='not_observed',
                        elapsed_ms=None,
                        source='daemon_recorder',
                        agents=(),
                    )
                )
                for name in READINESS_POINT_NAMES
            }
            timeline_complete = _timeline_is_complete(
                points=points,
                attach_mode=self._attach_mode,
                generation_matches=generation_matches,
                rpc_accepted_ms=self._rpc_accepted_ms,
                effective_requested_agents=self._effective_requested_agents,
                desired_agents=self._desired_agents,
            )
            return {
                'schema_version': READINESS_SCHEMA_VERSION,
                'trace_id': self._trace_id,
                'clock': 'host_perf_counter_ns',
                'origin': 'ccb_py_entry',
                'attach_mode': self._attach_mode,
                'startup_run_id': _optional_text(startup_run_id),
                'keeper_startup_id': self._keeper_startup_id,
                'daemon_generation': actual_generation,
                'expected_daemon_generation': self._expected_daemon_generation,
                'generation_correlation': 'matched' if generation_matches else 'mismatch',
                'rpc_accepted_ms': self._rpc_accepted_ms,
                'effective_requested_agents': list(self._effective_requested_agents),
                'desired_agents': list(self._desired_agents),
                'points': points,
                'timeline_complete': timeline_complete,
            }
        except Exception:
            return {}


def _clean_input_point(
    value: object,
    *,
    allowed_statuses: set[str],
) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    status = str(value.get('status') or '').strip()
    if status not in allowed_statuses:
        return None
    elapsed_ms = _optional_duration(value.get('elapsed_ms'))
    if status in {'reached', 'observed_upper_bound'} and elapsed_ms is None:
        return None
    if status == 'not_required_already_mounted' and value.get('elapsed_ms') is not None:
        return None
    return _point_record(
        status=status,
        elapsed_ms=elapsed_ms,
        source=_optional_text(value.get('source')) or 'cli_start',
        agents=(),
    )


def _point_record(
    *,
    status: str,
    elapsed_ms: float | None,
    source: str,
    agents: Sequence[str],
) -> dict[str, object]:
    return {
        'status': status,
        'elapsed_ms': elapsed_ms,
        'source': source,
        'agents': list(agents),
    }


def _optional_duration(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 and str(parsed) == str(value).strip() else None


def _positive_json_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _optional_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text[:256] if text else None


def _clean_agents(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        return ()
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _timeline_is_complete(
    *,
    points: Mapping[str, Mapping[str, object]],
    attach_mode: str,
    generation_matches: bool,
    rpc_accepted_ms: float | None,
    effective_requested_agents: Sequence[str],
    desired_agents: Sequence[str],
) -> bool:
    if not generation_matches or not _is_duration(rpc_accepted_ms):
        return False
    expected_statuses = {
        'T0_cli_entry': {'reached'},
        'T1_lifecycle_intent': {
            'reached',
            'observed_upper_bound',
            'not_required_already_mounted',
        },
        'T2_control_plane_ready': {'reached'},
        'T3_namespace_attachable': {'reached'},
        'T4_requested_agents_ready': {'reached'},
        'T5_foreground_attached': (
            {'not_applicable_no_attach'} if attach_mode == 'no_attach' else {'reached'}
        ),
        'T6_fully_warm': {'reached'},
    }
    if any(
        name not in points or points[name].get('status') not in statuses
        for name, statuses in expected_statuses.items()
    ):
        return False
    elapsed: dict[str, float | None] = {}
    for name in READINESS_POINT_NAMES:
        point = points[name]
        status = str(point.get('status') or '')
        raw_elapsed = point.get('elapsed_ms')
        if status in {'reached', 'observed_upper_bound', 'failed_before_ready'}:
            if not _is_duration(raw_elapsed):
                return False
            elapsed[name] = float(raw_elapsed)
        elif raw_elapsed is not None:
            return False
        else:
            elapsed[name] = None
    if elapsed['T0_cli_entry'] != 0.0:
        return False
    if any(points[name].get('agents') for name in (
        'T0_cli_entry',
        'T1_lifecycle_intent',
        'T2_control_plane_ready',
        'T3_namespace_attachable',
        'T5_foreground_attached',
    )):
        return False
    if set(points['T4_requested_agents_ready'].get('agents') or ()) != set(
        effective_requested_agents
    ):
        return False
    if set(points['T6_fully_warm'].get('agents') or ()) != set(desired_agents):
        return False
    ordered = (
        float(elapsed['T2_control_plane_ready']),
        float(rpc_accepted_ms),
        float(elapsed['T3_namespace_attachable']),
        float(elapsed['T4_requested_agents_ready']),
        float(elapsed['T6_fully_warm']),
    )
    if any(left > right for left, right in zip(ordered, ordered[1:])):
        return False
    t1_elapsed = elapsed['T1_lifecycle_intent']
    if t1_elapsed is not None and t1_elapsed > ordered[0]:
        return False
    if (
        points['T1_lifecycle_intent'].get('status') == 'observed_upper_bound'
        and t1_elapsed != ordered[0]
    ):
        return False
    return True


def _is_duration(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _exact_keeper_t1_point(
    checkpoint,
    *,
    origin_ns: int,
    observed_now_ns: int,
    expected_generation: int,
    keeper_startup_id: str | None,
    input_t1: Mapping[str, object],
    t2_elapsed_ms: float,
) -> dict[str, object] | None:
    if checkpoint is None or input_t1.get('status') != 'observed_upper_bound':
        return None
    if not keeper_startup_id:
        return None
    try:
        checkpoint_startup_id = str(getattr(checkpoint, 'startup_id'))
        checkpoint_generation = int(getattr(checkpoint, 'generation'))
        accepted_ns = int(getattr(checkpoint, 'accepted_perf_counter_ns'))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if (
        checkpoint_startup_id != keeper_startup_id
        or checkpoint_generation != expected_generation
        or accepted_ns <= 0
        or accepted_ns < origin_ns
        or accepted_ns > observed_now_ns
    ):
        return None
    elapsed_ms = (accepted_ns - origin_ns) / 1_000_000.0
    if elapsed_ms > t2_elapsed_ms + 0.01:
        return None
    return _point_record(
        status='reached',
        elapsed_ms=elapsed_ms,
        source='keeper_lifecycle_starting_committed',
        agents=(),
    )


__all__ = [
    'READINESS_POINT_NAMES',
    'READINESS_SCHEMA_VERSION',
    'StartupReadinessRecorder',
]
