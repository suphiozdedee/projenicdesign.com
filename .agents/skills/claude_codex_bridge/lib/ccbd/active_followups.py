from __future__ import annotations

from dataclasses import dataclass, field

from storage.jsonl_store import JsonlStore
from storage.paths import PathLayout


SCHEMA_VERSION = 1
ACTIVE_FOLLOWUP_STATUSES = frozenset({'accepted', 'injected', 'rejected', 'too_late', 'terminal'})


@dataclass(frozen=True)
class ActiveFollowupRecord:
    followup_id: str
    job_id: str
    message: str
    agent_name: str
    provider: str
    sequence: int
    status: str
    reason: str
    mechanism: str
    expected_provider_turn_ref: str | None
    provider_turn_ref: str | None
    created_at: str
    updated_at: str
    diagnostics: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.followup_id or '').strip():
            raise ValueError('followup_id cannot be empty')
        if not str(self.job_id or '').strip():
            raise ValueError('job_id cannot be empty')
        if self.status not in ACTIVE_FOLLOWUP_STATUSES:
            raise ValueError(f'unsupported active follow-up status: {self.status}')
        if int(self.sequence) <= 0:
            raise ValueError('active follow-up sequence must be positive')
        object.__setattr__(self, 'diagnostics', dict(self.diagnostics or {}))

    def to_record(self) -> dict[str, object]:
        return {
            'schema_version': SCHEMA_VERSION,
            'record_type': 'active_job_followup',
            'followup_id': self.followup_id,
            'job_id': self.job_id,
            'message': self.message,
            'agent_name': self.agent_name,
            'provider': self.provider,
            'sequence': self.sequence,
            'status': self.status,
            'reason': self.reason,
            'mechanism': self.mechanism,
            'expected_provider_turn_ref': self.expected_provider_turn_ref,
            'provider_turn_ref': self.provider_turn_ref,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'diagnostics': dict(self.diagnostics),
        }

    def public_record(self) -> dict[str, object]:
        record = self.to_record()
        record.pop('message', None)
        return record

    @classmethod
    def from_record(cls, record: dict[str, object]) -> 'ActiveFollowupRecord':
        if record.get('schema_version') != SCHEMA_VERSION:
            raise ValueError(f'active follow-up schema_version must be {SCHEMA_VERSION}')
        if record.get('record_type') != 'active_job_followup':
            raise ValueError('record_type must be active_job_followup')
        return cls(
            followup_id=str(record.get('followup_id') or ''),
            job_id=str(record.get('job_id') or ''),
            message=str(record.get('message') or ''),
            agent_name=str(record.get('agent_name') or ''),
            provider=str(record.get('provider') or ''),
            sequence=int(record.get('sequence') or 0),
            status=str(record.get('status') or ''),
            reason=str(record.get('reason') or ''),
            mechanism=str(record.get('mechanism') or ''),
            expected_provider_turn_ref=(
                str(record.get('expected_provider_turn_ref'))
                if record.get('expected_provider_turn_ref') is not None
                else None
            ),
            provider_turn_ref=(
                str(record.get('provider_turn_ref')) if record.get('provider_turn_ref') is not None else None
            ),
            created_at=str(record.get('created_at') or ''),
            updated_at=str(record.get('updated_at') or ''),
            diagnostics=dict(record.get('diagnostics') or {}),
        )


class ActiveFollowupStore:
    def __init__(self, layout: PathLayout, store: JsonlStore | None = None) -> None:
        self._layout = layout
        self._store = store or JsonlStore()

    def append(self, record: ActiveFollowupRecord) -> None:
        self._store.append(
            self._layout.ccbd_active_followups_path,
            record,
            serializer=lambda value: value.to_record(),
        )

    def list_all(self) -> list[ActiveFollowupRecord]:
        return self._store.read_all(
            self._layout.ccbd_active_followups_path,
            loader=ActiveFollowupRecord.from_record,
        )

    def get_latest(self, followup_id: str) -> ActiveFollowupRecord | None:
        return self._store.find_last(
            self._layout.ccbd_active_followups_path,
            predicate=lambda payload: str(payload.get('followup_id') or '') == str(followup_id),
            loader=ActiveFollowupRecord.from_record,
        )

    def latest_for_job(self, job_id: str) -> tuple[ActiveFollowupRecord, ...]:
        latest: dict[str, ActiveFollowupRecord] = {}
        for record in self.list_all():
            if record.job_id == job_id:
                latest[record.followup_id] = record
        return tuple(sorted(latest.values(), key=lambda item: (item.sequence, item.created_at, item.followup_id)))

    def accepted(self) -> tuple[ActiveFollowupRecord, ...]:
        first_index: dict[str, int] = {}
        latest: dict[str, ActiveFollowupRecord] = {}
        for index, record in enumerate(self.list_all()):
            first_index.setdefault(record.followup_id, index)
            latest[record.followup_id] = record
        return tuple(
            record
            for record in sorted(latest.values(), key=lambda item: first_index[item.followup_id])
            if record.status == 'accepted'
        )

    def next_sequence(self, job_id: str) -> int:
        records = self.latest_for_job(job_id)
        return max((record.sequence for record in records), default=0) + 1


__all__ = [
    'ACTIVE_FOLLOWUP_STATUSES',
    'ActiveFollowupRecord',
    'ActiveFollowupStore',
    'SCHEMA_VERSION',
]
