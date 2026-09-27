from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Callable

from ccbd.models import SCHEMA_VERSION
from storage.json_store import JsonStore
from storage.paths import PathLayout


_QUEUE_RECORD_TYPE = 'ccbd_reload_drain_queue'
_INTENT_KINDS = {'unload', 'replace'}
_PHASES = {'pending_unload', 'pending_replace', 'draining', 'retiring', 'retired', 'rejected'}
_STATUSES = {'pending', 'waiting', 'idle_ready', 'timed_out', 'rejected_queue_full', 'retired'}
_TERMINAL_STATUSES = {'timed_out', 'rejected_queue_full', 'retired'}


@dataclass(frozen=True)
class DrainBounds:
    max_pending: int = 16
    timeout_s: float = 300.0
    max_age_s: float = 900.0

    def __post_init__(self) -> None:
        if int(self.max_pending) <= 0:
            raise ValueError('max_pending must be positive')
        if float(self.timeout_s) <= 0:
            raise ValueError('timeout_s must be positive')
        if float(self.max_age_s) <= 0:
            raise ValueError('max_age_s must be positive')

    def to_record(self) -> dict[str, object]:
        return {
            'max_pending': int(self.max_pending),
            'timeout_s': float(self.timeout_s),
            'max_age_s': float(self.max_age_s),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object] | None) -> DrainBounds:
        payload = dict(record or {})
        return cls(
            max_pending=int(payload.get('max_pending', 16)),
            timeout_s=float(payload.get('timeout_s', 300.0)),
            max_age_s=float(payload.get('max_age_s', 900.0)),
        )


@dataclass(frozen=True)
class DrainIntent:
    intent_id: str
    intent_kind: str
    agent_name: str
    created_at_s: float
    reason: str | None = None
    old_config_signature: str | None = None
    new_config_signature: str | None = None

    def __post_init__(self) -> None:
        if not str(self.intent_id or '').strip():
            raise ValueError('intent_id cannot be empty')
        if self.intent_kind not in _INTENT_KINDS:
            raise ValueError(f'invalid drain intent kind: {self.intent_kind!r}')
        if not str(self.agent_name or '').strip():
            raise ValueError('agent_name cannot be empty')

    @property
    def initial_phase(self) -> str:
        return f'pending_{self.intent_kind}'

    def to_record(self) -> dict[str, object]:
        return {
            'intent_id': self.intent_id,
            'intent_kind': self.intent_kind,
            'agent_name': self.agent_name,
            'created_at_s': float(self.created_at_s),
            'reason': self.reason,
            'old_config_signature': self.old_config_signature,
            'new_config_signature': self.new_config_signature,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> DrainIntent:
        return cls(
            intent_id=str(record['intent_id']),
            intent_kind=str(record['intent_kind']),
            agent_name=str(record['agent_name']),
            created_at_s=float(record['created_at_s']),
            reason=_clean_text(record.get('reason')),
            old_config_signature=_clean_text(record.get('old_config_signature')),
            new_config_signature=_clean_text(record.get('new_config_signature')),
        )


@dataclass(frozen=True)
class DrainRecord:
    intent: DrainIntent
    phase: str
    status: str
    created_at_s: float
    updated_at_s: float
    deadline_at_s: float
    max_age_deadline_at_s: float
    reason: str | None = None
    busy: bool | None = None
    transition_count: int = 0

    def __post_init__(self) -> None:
        if self.phase not in _PHASES:
            raise ValueError(f'invalid drain phase: {self.phase!r}')
        if self.status not in _STATUSES:
            raise ValueError(f'invalid drain status: {self.status!r}')

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def with_transition(
        self,
        *,
        phase: str,
        status: str,
        now_s: float,
        reason: str,
        busy: bool | None,
    ) -> DrainRecord:
        return replace(
            self,
            phase=phase,
            status=status,
            updated_at_s=float(now_s),
            reason=reason,
            busy=busy,
            transition_count=self.transition_count + 1,
        )

    def to_record(self) -> dict[str, object]:
        return {
            'intent': self.intent.to_record(),
            'phase': self.phase,
            'status': self.status,
            'created_at_s': float(self.created_at_s),
            'updated_at_s': float(self.updated_at_s),
            'deadline_at_s': float(self.deadline_at_s),
            'max_age_deadline_at_s': float(self.max_age_deadline_at_s),
            'reason': self.reason,
            'busy': self.busy,
            'transition_count': int(self.transition_count),
        }

    @classmethod
    def pending(cls, intent: DrainIntent, *, bounds: DrainBounds, now_s: float) -> DrainRecord:
        now = float(now_s)
        return cls(
            intent=intent,
            phase=intent.initial_phase,
            status='pending',
            created_at_s=now,
            updated_at_s=now,
            deadline_at_s=now + float(bounds.timeout_s),
            max_age_deadline_at_s=float(intent.created_at_s) + float(bounds.max_age_s),
            reason=intent.reason,
            busy=None,
            transition_count=0,
        )

    @classmethod
    def rejected_queue_full(cls, intent: DrainIntent, *, bounds: DrainBounds, now_s: float) -> DrainRecord:
        now = float(now_s)
        return cls(
            intent=intent,
            phase='rejected',
            status='rejected_queue_full',
            created_at_s=now,
            updated_at_s=now,
            deadline_at_s=now + float(bounds.timeout_s),
            max_age_deadline_at_s=float(intent.created_at_s) + float(bounds.max_age_s),
            reason='pending drain queue is full',
            busy=None,
            transition_count=0,
        )

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> DrainRecord:
        return cls(
            intent=DrainIntent.from_record(dict(record['intent'])),
            phase=str(record['phase']),
            status=str(record['status']),
            created_at_s=float(record['created_at_s']),
            updated_at_s=float(record['updated_at_s']),
            deadline_at_s=float(record['deadline_at_s']),
            max_age_deadline_at_s=float(record['max_age_deadline_at_s']),
            reason=_clean_text(record.get('reason')),
            busy=_clean_bool_or_none(record.get('busy')),
            transition_count=int(record.get('transition_count', 0)),
        )


@dataclass(frozen=True)
class DrainQueue:
    bounds: DrainBounds
    records: tuple[DrainRecord, ...] = ()

    @classmethod
    def empty(cls, bounds: DrainBounds | None = None) -> DrainQueue:
        return cls(bounds=bounds or DrainBounds(), records=())

    @property
    def pending_count(self) -> int:
        return len([record for record in self.records if not record.terminal])

    def enqueue(self, intent: DrainIntent, *, now_s: float) -> DrainQueueResult:
        if float(now_s) > float(intent.created_at_s) + float(self.bounds.max_age_s):
            expired = DrainRecord.pending(intent, bounds=self.bounds, now_s=now_s).with_transition(
                phase='draining',
                status='timed_out',
                now_s=now_s,
                reason='intent age exceeds max_age_s',
                busy=None,
            )
            return DrainQueueResult(queue=self, record=expired, accepted=False)
        if self.pending_count >= int(self.bounds.max_pending):
            rejected = DrainRecord.rejected_queue_full(intent, bounds=self.bounds, now_s=now_s)
            return DrainQueueResult(queue=self, record=rejected, accepted=False)
        record = DrainRecord.pending(intent, bounds=self.bounds, now_s=now_s)
        return DrainQueueResult(queue=replace(self, records=(*self.records, record)), record=record, accepted=True)

    def replace_record(self, updated: DrainRecord) -> DrainQueue:
        records = []
        replaced = False
        for record in self.records:
            if record.intent.intent_id == updated.intent.intent_id:
                records.append(updated)
                replaced = True
            else:
                records.append(record)
        if not replaced:
            return self
        return replace(self, records=tuple(records))

    def active_records_for(self, agent_name: str) -> tuple[DrainRecord, ...]:
        return tuple(
            record
            for record in self.records
            if record.intent.agent_name == agent_name and not record.terminal
        )

    def blocks_new_work_for(self, agent_name: str) -> bool:
        return bool(self.active_records_for(agent_name))

    def to_record(self) -> dict[str, object]:
        return {
            'schema_version': SCHEMA_VERSION,
            'record_type': _QUEUE_RECORD_TYPE,
            'bounds': self.bounds.to_record(),
            'records': [record.to_record() for record in self.records],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> DrainQueue:
        if record.get('schema_version') != SCHEMA_VERSION:
            raise ValueError(f'schema_version must be {SCHEMA_VERSION}')
        if record.get('record_type') != _QUEUE_RECORD_TYPE:
            raise ValueError(f"record_type must be '{_QUEUE_RECORD_TYPE}'")
        return cls(
            bounds=DrainBounds.from_record(record.get('bounds') if isinstance(record.get('bounds'), Mapping) else None),
            records=tuple(DrainRecord.from_record(dict(item)) for item in tuple(record.get('records') or ())),
        )


@dataclass(frozen=True)
class DrainQueueResult:
    queue: DrainQueue
    record: DrainRecord
    accepted: bool


BusyPredicate = Callable[[DrainRecord], bool]


def plan_drain_transition(record: DrainRecord, *, now_s: float, is_busy: BusyPredicate) -> DrainRecord:
    now = float(now_s)
    if record.terminal:
        return record
    if record.status == 'idle_ready':
        return record
    if now >= float(record.max_age_deadline_at_s):
        return record.with_transition(
            phase='draining',
            status='timed_out',
            now_s=now,
            reason='drain max_age_s exceeded',
            busy=None,
        )
    if now >= float(record.deadline_at_s):
        return record.with_transition(
            phase='draining',
            status='timed_out',
            now_s=now,
            reason='drain timeout_s exceeded',
            busy=None,
        )
    busy = bool(is_busy(record))
    if busy:
        return record.with_transition(
            phase='draining',
            status='waiting',
            now_s=now,
            reason='agent is busy; drain remains bounded and pending',
            busy=True,
        )
    return record.with_transition(
        phase='retiring',
        status='idle_ready',
        now_s=now,
        reason='agent is idle and ready for retire step',
        busy=False,
    )


def retire_record(record: DrainRecord, *, now_s: float) -> DrainRecord:
    if record.status == 'retired':
        return record
    if record.status != 'idle_ready':
        return record
    return record.with_transition(
        phase='retired',
        status='retired',
        now_s=now_s,
        reason='record retired; Phase 4 performs no runtime or tmux mutation',
        busy=False,
    )


class DrainQueueStore:
    def __init__(self, layout: PathLayout, *, bounds: DrainBounds | None = None, store: JsonStore | None = None) -> None:
        self._layout = layout
        self._bounds = bounds or DrainBounds()
        self._store = store or JsonStore()

    def load(self) -> DrainQueue:
        path = self._layout.ccbd_reload_drain_path
        if not path.exists():
            return DrainQueue.empty(self._bounds)
        return self._store.load(path, loader=DrainQueue.from_record)

    def save(self, queue: DrainQueue) -> None:
        self._store.save(self._layout.ccbd_reload_drain_path, queue, serializer=lambda value: value.to_record())


def drain_intent_suggestions_for_reload_operations(
    operations: tuple[Mapping[str, object], ...] | list[Mapping[str, object]],
    *,
    old_config_signature: object,
    new_config_signature: object,
) -> list[dict[str, object]]:
    suggestions: list[dict[str, object]] = []
    old_signature = _clean_text(old_config_signature)
    new_signature = _clean_text(new_config_signature)
    for operation in operations:
        op = str(operation.get('op') or '').strip()
        if op == 'remove_agent':
            intent_kind = 'unload'
            initial_phase = 'pending_unload'
        elif op == 'replace_agent':
            intent_kind = 'replace'
            initial_phase = 'pending_replace'
        else:
            continue
        agent_name = _clean_text(operation.get('agent'))
        if not agent_name:
            continue
        reason = _clean_text(operation.get('reason'))
        intent_id = _stable_intent_id(
            intent_kind=intent_kind,
            agent_name=agent_name,
            old_config_signature=old_signature,
            new_config_signature=new_signature,
            reason=reason,
        )
        suggestions.append(
            {
                'intent_id': intent_id,
                'intent_kind': intent_kind,
                'agent': agent_name,
                'initial_phase': initial_phase,
                'dry_run_only': True,
                'reason': reason,
            }
        )
    return suggestions


def _stable_intent_id(
    *,
    intent_kind: str,
    agent_name: str,
    old_config_signature: str | None,
    new_config_signature: str | None,
    reason: str | None,
) -> str:
    material = '\0'.join(
        (
            intent_kind,
            agent_name,
            old_config_signature or '',
            new_config_signature or '',
            reason or '',
        )
    )
    return f'drain_{sha256(material.encode("utf-8")).hexdigest()[:16]}'


def _clean_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def _clean_bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


__all__ = [
    'BusyPredicate',
    'DrainBounds',
    'DrainIntent',
    'DrainQueue',
    'DrainQueueResult',
    'DrainQueueStore',
    'DrainRecord',
    'drain_intent_suggestions_for_reload_operations',
    'plan_drain_transition',
    'retire_record',
]
