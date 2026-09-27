from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1
SCHEDULE_RECORD_TYPE = 'maintenance_heartbeat_schedule'
STATUS_RECORD_TYPE = 'maintenance_heartbeat_status'
ACTIVATION_RECORD_TYPE = 'maintenance_heartbeat_activation'
RUNNER_RECORD_TYPE = 'maintenance_heartbeat_runner'


@dataclass(frozen=True)
class MaintenanceHeartbeatSchedule:
    project_id: str
    next_run_at: str | None = None
    reason: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None

    def __post_init__(self) -> None:
        if not str(self.project_id or '').strip():
            raise ValueError('project_id cannot be empty')

    def to_record(self) -> dict[str, Any]:
        return {
            'schema_version': SCHEMA_VERSION,
            'record_type': SCHEDULE_RECORD_TYPE,
            'project_id': self.project_id,
            'next_run_at': self.next_run_at,
            'reason': self.reason,
            'updated_at': self.updated_at,
            'updated_by': self.updated_by,
        }

    @classmethod
    def from_record(cls, payload: dict[str, Any]) -> 'MaintenanceHeartbeatSchedule':
        _validate_header(payload, record_type=SCHEDULE_RECORD_TYPE)
        return cls(
            project_id=str(payload.get('project_id') or ''),
            next_run_at=_optional_text(payload.get('next_run_at')),
            reason=_optional_text(payload.get('reason')),
            updated_at=_optional_text(payload.get('updated_at')),
            updated_by=_optional_text(payload.get('updated_by')),
        )


@dataclass(frozen=True)
class MaintenanceHeartbeatStatus:
    project_id: str
    last_tick_status: str | None = None
    last_tick_at: str | None = None
    last_ok_at: str | None = None
    last_error: str | None = None
    unknown_streak: int = 0
    updated_at: str | None = None
    source_kind: str | None = None
    recommended_action: str | None = None
    next_heartbeat_after_s: int | None = None
    needs_user: bool = False
    summary: dict[str, Any] | None = None
    evidence: tuple[dict[str, Any], ...] = ()
    last_activation_status: str | None = None
    last_activation_id: str | None = None
    last_activation_job_id: str | None = None
    last_activation_target: str | None = None
    last_activation_dedup_key: str | None = None

    def __post_init__(self) -> None:
        if not str(self.project_id or '').strip():
            raise ValueError('project_id cannot be empty')
        unknown_streak = int(self.unknown_streak)
        if unknown_streak < 0:
            raise ValueError('unknown_streak cannot be negative')
        object.__setattr__(self, 'unknown_streak', unknown_streak)
        next_after = self.next_heartbeat_after_s
        if next_after is not None and int(next_after) <= 0:
            raise ValueError('next_heartbeat_after_s must be positive')
        object.__setattr__(self, 'next_heartbeat_after_s', int(next_after) if next_after is not None else None)
        object.__setattr__(self, 'needs_user', bool(self.needs_user))
        object.__setattr__(self, 'summary', _optional_mapping(self.summary))
        object.__setattr__(self, 'evidence', _tuple_of_mappings(self.evidence))

    def to_record(self) -> dict[str, Any]:
        return {
            'schema_version': SCHEMA_VERSION,
            'record_type': STATUS_RECORD_TYPE,
            'project_id': self.project_id,
            'last_tick_status': self.last_tick_status,
            'last_tick_at': self.last_tick_at,
            'last_ok_at': self.last_ok_at,
            'last_error': self.last_error,
            'unknown_streak': int(self.unknown_streak),
            'updated_at': self.updated_at,
            'source_kind': self.source_kind,
            'recommended_action': self.recommended_action,
            'next_heartbeat_after_s': self.next_heartbeat_after_s,
            'needs_user': bool(self.needs_user),
            'summary': self.summary or {},
            'evidence': list(self.evidence),
            'last_activation_status': self.last_activation_status,
            'last_activation_id': self.last_activation_id,
            'last_activation_job_id': self.last_activation_job_id,
            'last_activation_target': self.last_activation_target,
            'last_activation_dedup_key': self.last_activation_dedup_key,
        }

    @classmethod
    def from_record(cls, payload: dict[str, Any]) -> 'MaintenanceHeartbeatStatus':
        _validate_header(payload, record_type=STATUS_RECORD_TYPE)
        return cls(
            project_id=str(payload.get('project_id') or ''),
            last_tick_status=_optional_text(payload.get('last_tick_status')),
            last_tick_at=_optional_text(payload.get('last_tick_at')),
            last_ok_at=_optional_text(payload.get('last_ok_at')),
            last_error=_optional_text(payload.get('last_error')),
            unknown_streak=int(payload.get('unknown_streak') or 0),
            updated_at=_optional_text(payload.get('updated_at')),
            source_kind=_optional_text(payload.get('source_kind')),
            recommended_action=_optional_text(payload.get('recommended_action')),
            next_heartbeat_after_s=_optional_int(payload.get('next_heartbeat_after_s')),
            needs_user=bool(payload.get('needs_user', False)),
            summary=_optional_mapping(payload.get('summary')),
            evidence=_tuple_of_mappings(payload.get('evidence')),
            last_activation_status=_optional_text(payload.get('last_activation_status')),
            last_activation_id=_optional_text(payload.get('last_activation_id')),
            last_activation_job_id=_optional_text(payload.get('last_activation_job_id')),
            last_activation_target=_optional_text(payload.get('last_activation_target')),
            last_activation_dedup_key=_optional_text(payload.get('last_activation_dedup_key')),
        )


@dataclass(frozen=True)
class MaintenanceHeartbeatRunner:
    project_id: str
    runner_id: str
    pid: int | None = None
    state: str = 'unknown'
    source: str | None = None
    started_at: str | None = None
    last_seen_at: str | None = None
    last_wake_at: str | None = None
    last_tick_at: str | None = None
    last_tick_status: str | None = None
    observed_next_run_at: str | None = None
    sleep_until: str | None = None
    exit_reason: str | None = None

    def __post_init__(self) -> None:
        if not str(self.project_id or '').strip():
            raise ValueError('project_id cannot be empty')
        if not str(self.runner_id or '').strip():
            raise ValueError('runner_id cannot be empty')
        if not str(self.state or '').strip():
            raise ValueError('state cannot be empty')
        if self.pid is not None and int(self.pid) <= 0:
            raise ValueError('pid must be positive')
        object.__setattr__(self, 'pid', int(self.pid) if self.pid is not None else None)

    def to_record(self) -> dict[str, Any]:
        return {
            'schema_version': SCHEMA_VERSION,
            'record_type': RUNNER_RECORD_TYPE,
            'project_id': self.project_id,
            'runner_id': self.runner_id,
            'pid': self.pid,
            'state': self.state,
            'source': self.source,
            'started_at': self.started_at,
            'last_seen_at': self.last_seen_at,
            'last_wake_at': self.last_wake_at,
            'last_tick_at': self.last_tick_at,
            'last_tick_status': self.last_tick_status,
            'observed_next_run_at': self.observed_next_run_at,
            'sleep_until': self.sleep_until,
            'exit_reason': self.exit_reason,
        }

    @classmethod
    def from_record(cls, payload: dict[str, Any]) -> 'MaintenanceHeartbeatRunner':
        _validate_header(payload, record_type=RUNNER_RECORD_TYPE)
        return cls(
            project_id=str(payload.get('project_id') or ''),
            runner_id=str(payload.get('runner_id') or ''),
            pid=_optional_int(payload.get('pid')),
            state=str(payload.get('state') or 'unknown'),
            source=_optional_text(payload.get('source')),
            started_at=_optional_text(payload.get('started_at')),
            last_seen_at=_optional_text(payload.get('last_seen_at')),
            last_wake_at=_optional_text(payload.get('last_wake_at')),
            last_tick_at=_optional_text(payload.get('last_tick_at')),
            last_tick_status=_optional_text(payload.get('last_tick_status')),
            observed_next_run_at=_optional_text(payload.get('observed_next_run_at')),
            sleep_until=_optional_text(payload.get('sleep_until')),
            exit_reason=_optional_text(payload.get('exit_reason')),
        )


@dataclass(frozen=True)
class MaintenanceHeartbeatActivation:
    project_id: str
    activation_id: str
    status: str
    condition_kind: str
    trigger_kind: str
    source: str
    observed_at: str
    target_agent: str
    delivery_mode: str
    payload_kind: str
    dedup_key: str
    reason: str
    created_by: str = 'maintenance-heartbeat'
    not_before: str | None = None
    expires_at: str | None = None
    job_id: str | None = None
    submitted_at: str | None = None
    suppressed_reason: str | None = None
    error: str | None = None
    repeat_count: int = 0
    payload_summary: dict[str, Any] | None = None
    evidence: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            'project_id',
            'activation_id',
            'status',
            'condition_kind',
            'trigger_kind',
            'source',
            'observed_at',
            'target_agent',
            'delivery_mode',
            'payload_kind',
            'dedup_key',
            'reason',
            'created_by',
        ):
            if not str(getattr(self, field_name) or '').strip():
                raise ValueError(f'{field_name} cannot be empty')
        repeat_count = int(self.repeat_count)
        if repeat_count < 0:
            raise ValueError('repeat_count cannot be negative')
        object.__setattr__(self, 'repeat_count', repeat_count)
        object.__setattr__(self, 'payload_summary', _optional_mapping(self.payload_summary))
        object.__setattr__(self, 'evidence', _tuple_of_mappings(self.evidence))

    def to_record(self) -> dict[str, Any]:
        return {
            'schema_version': SCHEMA_VERSION,
            'record_type': ACTIVATION_RECORD_TYPE,
            'project_id': self.project_id,
            'activation_id': self.activation_id,
            'status': self.status,
            'condition_kind': self.condition_kind,
            'trigger_kind': self.trigger_kind,
            'source': self.source,
            'observed_at': self.observed_at,
            'target_agent': self.target_agent,
            'delivery_mode': self.delivery_mode,
            'payload_kind': self.payload_kind,
            'dedup_key': self.dedup_key,
            'reason': self.reason,
            'created_by': self.created_by,
            'not_before': self.not_before,
            'expires_at': self.expires_at,
            'job_id': self.job_id,
            'submitted_at': self.submitted_at,
            'suppressed_reason': self.suppressed_reason,
            'error': self.error,
            'repeat_count': int(self.repeat_count),
            'payload_summary': self.payload_summary or {},
            'evidence': list(self.evidence),
        }

    @classmethod
    def from_record(cls, payload: dict[str, Any]) -> 'MaintenanceHeartbeatActivation':
        _validate_header(payload, record_type=ACTIVATION_RECORD_TYPE)
        return cls(
            project_id=str(payload.get('project_id') or ''),
            activation_id=str(payload.get('activation_id') or ''),
            status=str(payload.get('status') or ''),
            condition_kind=str(payload.get('condition_kind') or ''),
            trigger_kind=str(payload.get('trigger_kind') or ''),
            source=str(payload.get('source') or ''),
            observed_at=str(payload.get('observed_at') or ''),
            target_agent=str(payload.get('target_agent') or ''),
            delivery_mode=str(payload.get('delivery_mode') or ''),
            payload_kind=str(payload.get('payload_kind') or ''),
            dedup_key=str(payload.get('dedup_key') or ''),
            reason=str(payload.get('reason') or ''),
            created_by=str(payload.get('created_by') or 'maintenance-heartbeat'),
            not_before=_optional_text(payload.get('not_before')),
            expires_at=_optional_text(payload.get('expires_at')),
            job_id=_optional_text(payload.get('job_id')),
            submitted_at=_optional_text(payload.get('submitted_at')),
            suppressed_reason=_optional_text(payload.get('suppressed_reason')),
            error=_optional_text(payload.get('error')),
            repeat_count=int(payload.get('repeat_count') or 0),
            payload_summary=_optional_mapping(payload.get('payload_summary')),
            evidence=_tuple_of_mappings(payload.get('evidence')),
        )


def _validate_header(payload: dict[str, Any], *, record_type: str) -> None:
    if payload.get('schema_version') != SCHEMA_VERSION:
        raise ValueError(f'schema_version must be {SCHEMA_VERSION}')
    if payload.get('record_type') != record_type:
        raise ValueError(f"record_type must be '{record_type}'")


def _optional_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('expected integer')
    return int(value)


def _optional_mapping(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('expected object')
    return dict(value)


def _tuple_of_mappings(value: object) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError('expected array')
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('expected object array')
        result.append(dict(item))
    return tuple(result)


__all__ = [
    'ACTIVATION_RECORD_TYPE',
    'MaintenanceHeartbeatActivation',
    'MaintenanceHeartbeatRunner',
    'MaintenanceHeartbeatSchedule',
    'MaintenanceHeartbeatStatus',
    'RUNNER_RECORD_TYPE',
    'SCHEMA_VERSION',
    'SCHEDULE_RECORD_TYPE',
    'STATUS_RECORD_TYPE',
]
