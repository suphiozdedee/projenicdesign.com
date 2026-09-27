from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import threading

from storage.atomic import atomic_write_json

SCHEMA_VERSION = 1
NOTIFICATION_KIND_TASK_COMPLETED = 'task_completed'
INVALIDATION_KIND_PROJECT_SUMMARY = 'project_summary_changed'
INVALIDATION_KIND_AGENT_ACTIVITY = 'agent_activity_changed'
INVALIDATION_KIND_CONVERSATION = 'conversation_changed'
INVALIDATION_KIND_RESYNC = 'resync_required'

_STATE_RECORD_TYPE = 'ccb_mobile_notification_state'
_BUSY_STATES = frozenset({'active'})
_COMPLETED_STATES = frozenset({'idle', 'failed'})
_DEFAULT_RECENT_LIMIT = 100
_DEFAULT_COMPLETION_LIMIT = 1024


@dataclass(frozen=True)
class MobileNotificationSnapshot:
    project_id: str
    project_short_name: str
    namespace_epoch: int | None
    agent: str
    activity_state: str
    observed_at: str


@dataclass(frozen=True)
class MobileInvalidationSnapshot:
    """Redacted change detector. It never contains conversation content."""

    project_id: str
    project_short_name: str
    namespace_epoch: int | None
    agent: str
    activity_state: str
    conversation_fingerprint: str
    observed_at: str


@dataclass(frozen=True)
class MobileNotificationEvent:
    id: str
    kind: str
    project_id: str
    project_short_name: str
    agent: str
    completed_at: str
    dedupe_key: str
    namespace_epoch: int | None = None
    scope: str | None = None
    activity_state: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            'id': self.id,
            'kind': self.kind,
            'project_id': self.project_id,
            'project_short_name': self.project_short_name,
            'agent': self.agent,
            'completed_at': self.completed_at,
            'dedupe_key': self.dedupe_key,
        }
        if self.namespace_epoch is not None:
            payload['namespace_epoch'] = self.namespace_epoch
        if self.scope:
            payload['scope'] = self.scope
        if self.activity_state:
            payload['activity_state'] = self.activity_state
        return payload


class MobileNotificationStore:
    """Bounded event store with durable completion priority.

    Completion and invalidation records use separately bounded persistence,
    but share one durable monotonic sequence and one cursor surface. This
    keeps completion retention independent from compact latest-state
    invalidations while allowing a client to resume one logical journal. A
    trimmed/missing cursor yields an explicit resync event rather than
    pretending the client is current.
    """

    def __init__(
        self,
        mobile_dir: Path,
        *,
        recent_limit: int = _DEFAULT_RECENT_LIMIT,
        completion_limit: int = _DEFAULT_COMPLETION_LIMIT,
    ) -> None:
        self._mobile_dir = Path(mobile_dir)
        self._recent_limit = max(1, int(recent_limit))
        self._completion_limit = max(self._recent_limit, int(completion_limit))
        self._lock = threading.RLock()

    @property
    def events_path(self) -> Path:
        """Compatibility location for pre-8.1.1 mixed journals (read-only)."""
        return self._mobile_dir / 'notifications.jsonl'

    @property
    def completion_events_path(self) -> Path:
        return self._mobile_dir / 'completion-notifications.jsonl'

    @property
    def invalidation_events_path(self) -> Path:
        return self._mobile_dir / 'invalidation-notifications.jsonl'

    @property
    def state_path(self) -> Path:
        return self._mobile_dir / 'notification-state.json'

    def sync_snapshots(self, snapshots: list[MobileNotificationSnapshot]) -> list[MobileNotificationEvent]:
        with self._lock:
            state = self._load_state()
            agent_states = _state_agents(state)
            completions = self._completion_events()
            state['next_event_sequence'] = max(
                _int(state.get('next_event_sequence'), 1),
                _next_sequence_after(completions + self._invalidation_events()),
            )
            existing = {event.dedupe_key for event in completions}
            emitted: list[MobileNotificationEvent] = []
            for raw_snapshot in snapshots:
                snapshot = replace(
                    raw_snapshot,
                    activity_state=_canonical_activity_state(
                        raw_snapshot.activity_state
                    ),
                )
                key = _snapshot_key(snapshot)
                prior = agent_states.get(key)
                completion_sequence = _int(_map(prior).get('completion_sequence'), 0)
                if _is_task_completion_transition(prior, snapshot):
                    completion_sequence += 1
                    dedupe_key = _dedupe_key(snapshot, completion_sequence=completion_sequence)
                    if dedupe_key not in existing:
                        event = self._next_completion_event(state, snapshot, dedupe_key)
                        completions.append(event)
                        existing.add(dedupe_key)
                        emitted.append(event)
                agent_states[key] = _snapshot_state(snapshot, completion_sequence=completion_sequence)
            state['agents'] = agent_states
            self._write_completion_events(completions)
            self._write_state(state)
            return emitted

    def sync_invalidations(self, snapshots: list[MobileInvalidationSnapshot]) -> list[MobileNotificationEvent]:
        with self._lock:
            state = self._load_state()
            agents = _state_invalidation_agents(state)
            records = {
                _invalidation_record_key(event): event
                for event in self._invalidation_events()
                if _invalidation_record_key(event)
            }
            state['next_event_sequence'] = max(
                _int(state.get('next_event_sequence'), 1),
                _next_sequence_after(self._completion_events() + list(records.values())),
            )
            emitted: list[MobileNotificationEvent] = []
            summary_changed: dict[str, MobileInvalidationSnapshot] = {}
            for raw_snapshot in snapshots:
                snapshot = replace(
                    raw_snapshot,
                    activity_state=_canonical_activity_state(
                        raw_snapshot.activity_state
                    ),
                )
                key = _snapshot_key_for_invalidation(snapshot)
                prior = agents.get(key)
                if prior:
                    activity_changed = (
                        snapshot.activity_state not in {'', 'unknown'}
                        and str(prior.get('activity_state') or '') != snapshot.activity_state
                        or prior.get('namespace_epoch') != snapshot.namespace_epoch
                    )
                    conversation_changed = (
                        str(prior.get('conversation_fingerprint') or '')
                        != snapshot.conversation_fingerprint
                    )
                    if activity_changed:
                        event = self._next_invalidation_event(
                            state, snapshot, INVALIDATION_KIND_AGENT_ACTIVITY, 'agent'
                        )
                        records[_invalidation_record_key(event)] = event
                        emitted.append(event)
                        summary_changed[snapshot.project_id] = snapshot
                    if conversation_changed:
                        event = self._next_invalidation_event(
                            state, snapshot, INVALIDATION_KIND_CONVERSATION, 'conversation'
                        )
                        records[_invalidation_record_key(event)] = event
                        emitted.append(event)
                next_state = _invalidation_snapshot_state(snapshot)
                if prior and snapshot.activity_state in {'', 'unknown'}:
                    next_state['activity_state'] = str(
                        prior.get('activity_state') or 'unknown'
                    )
                agents[key] = next_state
            for snapshot in summary_changed.values():
                event = self._next_invalidation_event(
                    state, snapshot, INVALIDATION_KIND_PROJECT_SUMMARY, 'project', agent=''
                )
                records[_invalidation_record_key(event)] = event
                emitted.append(event)
            state['invalidations'] = agents
            self._write_invalidation_events(list(records.values()))
            self._write_state(state)
            return emitted

    def events_since(self, last_event_id: str | None = None) -> list[MobileNotificationEvent]:
        with self._lock:
            events = sorted(
                self._completion_events() + self._invalidation_events(), key=_event_sequence
            )
            cursor = str(last_event_id or '').strip()
            if not cursor:
                # A new subscription gets only bounded invalidation state and
                # no historical completion popups; REST remains authoritative.
                return events[-self._recent_limit :]
            sequence = _event_sequence_from_id(cursor)
            known = any(event.id == cursor for event in events)
            if known or cursor.startswith('mnotif_resync_'):
                return [event for event in events if _event_sequence(event) > sequence]
            # A cursor can be absent because the compact invalidation key was
            # overwritten, a legacy journal was retired, or completion history
            # was bounded. Make recovery explicit and advance the cursor so the
            # client does not receive a resync loop.
            newest = max(
                (_event_sequence(event) for event in events),
                default=max(_int(self._load_state().get('next_event_sequence'), 1) - 1, sequence),
            )
            return [
                *[event for event in events if _event_sequence(event) > sequence],
                MobileNotificationEvent(
                    id=f'mnotif_resync_{newest:012d}',
                    kind=INVALIDATION_KIND_RESYNC,
                    project_id='',
                    project_short_name='',
                    agent='',
                    completed_at='1970-01-01T00:00:00Z',
                    dedupe_key=f'resync:{newest}',
                    scope='resync',
                ),
            ]

    def _next_completion_event(
        self, state: dict[str, object], snapshot: MobileNotificationSnapshot, dedupe_key: str
    ) -> MobileNotificationEvent:
        return MobileNotificationEvent(
            id=_next_event_id(state), kind=NOTIFICATION_KIND_TASK_COMPLETED,
            project_id=snapshot.project_id, project_short_name=snapshot.project_short_name,
            agent=snapshot.agent, completed_at=snapshot.observed_at, dedupe_key=dedupe_key,
        )

    def _next_invalidation_event(
        self,
        state: dict[str, object],
        snapshot: MobileInvalidationSnapshot,
        kind: str,
        scope: str,
        *,
        agent: str | None = None,
    ) -> MobileNotificationEvent:
        selected_agent = snapshot.agent if agent is None else agent
        fingerprint = snapshot.activity_state if kind == INVALIDATION_KIND_AGENT_ACTIVITY else snapshot.conversation_fingerprint
        digest = hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:16]
        return MobileNotificationEvent(
            id=_next_event_id(state), kind=kind, project_id=snapshot.project_id,
            project_short_name=snapshot.project_short_name, agent=selected_agent,
            completed_at=snapshot.observed_at,
            dedupe_key=':'.join(('invalidation', kind, snapshot.project_id, str(snapshot.namespace_epoch), selected_agent, digest)),
            namespace_epoch=snapshot.namespace_epoch, scope=scope,
            activity_state=(
                snapshot.activity_state
                if kind == INVALIDATION_KIND_AGENT_ACTIVITY
                else None
            ),
        )

    def _completion_events(self) -> list[MobileNotificationEvent]:
        current = _events_from_records(_read_jsonl(self.completion_events_path))
        if current:
            return current
        # Upgrade in place without throwing away notifications written by the
        # previous mixed-journal implementation.
        return [
            event for event in _events_from_records(_read_jsonl(self.events_path))
            if event.kind == NOTIFICATION_KIND_TASK_COMPLETED
        ][-self._completion_limit :]

    def _invalidation_events(self) -> list[MobileNotificationEvent]:
        current = _events_from_records(_read_jsonl(self.invalidation_events_path))
        if current:
            return current
        return [
            event for event in _events_from_records(_read_jsonl(self.events_path))
            if event.kind != NOTIFICATION_KIND_TASK_COMPLETED
        ][-self._recent_limit :]

    def _write_completion_events(self, events: list[MobileNotificationEvent]) -> None:
        _write_jsonl(self.completion_events_path, events[-self._completion_limit :])

    def _write_invalidation_events(self, events: list[MobileNotificationEvent]) -> None:
        ordered = sorted(events, key=_event_sequence)[-self._recent_limit :]
        _write_jsonl(self.invalidation_events_path, ordered)

    def _load_state(self) -> dict[str, object]:
        try:
            payload = json.loads(self.state_path.read_text(encoding='utf-8'))
        except Exception:
            payload = {}
        if not isinstance(payload, dict) or str(payload.get('record_type') or '') != _STATE_RECORD_TYPE:
            payload = {}
        payload.setdefault('schema_version', SCHEMA_VERSION)
        payload.setdefault('record_type', _STATE_RECORD_TYPE)
        payload.setdefault('next_event_sequence', 1)
        payload.setdefault('agents', {})
        payload.setdefault('invalidations', {})
        return payload

    def _write_state(self, state: dict[str, object]) -> None:
        self._mobile_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.state_path, state)


def encode_sse_event(event: MobileNotificationEvent | dict[str, object]) -> bytes:
    payload = event.to_payload() if isinstance(event, MobileNotificationEvent) else dict(event)
    return (
        f"id: {str(payload.get('id') or '').strip()}\n"
        f"event: {str(payload.get('kind') or 'message').strip() or 'message'}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n\n"
    ).encode('utf-8')


def _is_task_completion_transition(prior: object, snapshot: MobileNotificationSnapshot) -> bool:
    prior_record = _map(prior)
    return (
        bool(prior_record)
        and str(prior_record.get('activity_state') or '').strip().lower() in _BUSY_STATES
        and snapshot.activity_state in _COMPLETED_STATES
        and prior_record.get('namespace_epoch') is not None
        and prior_record.get('namespace_epoch') == snapshot.namespace_epoch
    )


def _canonical_activity_state(value: object) -> str:
    state = str(value or '').strip().lower()
    if state in {'active', 'pending'}:
        return 'active'
    if state in {'idle', 'free'}:
        return 'idle'
    if state in {'exception', 'failed'}:
        return 'failed'
    return 'unknown'


def _snapshot_key(snapshot: MobileNotificationSnapshot) -> str:
    return '\0'.join((snapshot.project_id, snapshot.agent))


def _snapshot_key_for_invalidation(snapshot: MobileInvalidationSnapshot) -> str:
    return '\0'.join((snapshot.project_id, snapshot.agent))


def _snapshot_state(snapshot: MobileNotificationSnapshot, *, completion_sequence: int) -> dict[str, object]:
    return {'project_id': snapshot.project_id, 'namespace_epoch': snapshot.namespace_epoch, 'agent': snapshot.agent, 'activity_state': snapshot.activity_state, 'observed_at': snapshot.observed_at, 'completion_sequence': int(completion_sequence)}


def _dedupe_key(snapshot: MobileNotificationSnapshot, *, completion_sequence: int) -> str:
    return ':'.join((snapshot.project_id, str(snapshot.namespace_epoch), snapshot.agent, str(int(completion_sequence))))


def _state_agents(state: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(k): dict(v) for k, v in _map(state.get('agents')).items() if isinstance(v, dict)}


def _state_invalidation_agents(state: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(k): dict(v) for k, v in _map(state.get('invalidations')).items() if isinstance(v, dict)}


def _invalidation_snapshot_state(snapshot: MobileInvalidationSnapshot) -> dict[str, object]:
    return {'project_id': snapshot.project_id, 'namespace_epoch': snapshot.namespace_epoch, 'agent': snapshot.agent, 'activity_state': snapshot.activity_state, 'conversation_fingerprint': snapshot.conversation_fingerprint, 'observed_at': snapshot.observed_at}


def _invalidation_record_key(event: MobileNotificationEvent) -> str:
    if event.kind not in {INVALIDATION_KIND_PROJECT_SUMMARY, INVALIDATION_KIND_AGENT_ACTIVITY, INVALIDATION_KIND_CONVERSATION}:
        return ''
    return '\0'.join((event.project_id, event.agent, event.kind, str(event.namespace_epoch)))


def _event_from_record(record: dict[str, object]) -> MobileNotificationEvent | None:
    required = {'id', 'kind', 'project_id', 'project_short_name', 'agent', 'completed_at', 'dedupe_key'}
    if not required.issubset(record):
        return None
    return MobileNotificationEvent(
        id=str(record.get('id') or ''), kind=str(record.get('kind') or ''),
        project_id=str(record.get('project_id') or ''), project_short_name=str(record.get('project_short_name') or ''),
        agent=str(record.get('agent') or ''), completed_at=str(record.get('completed_at') or ''),
        dedupe_key=str(record.get('dedupe_key') or ''), namespace_epoch=_optional_int(record.get('namespace_epoch')),
        scope=_optional_text(record.get('scope')),
        activity_state=_optional_text(record.get('activity_state')),
    )


def _events_from_records(records: list[dict[str, object]]) -> list[MobileNotificationEvent]:
    return [event for record in records if (event := _event_from_record(record)) is not None]


def _next_event_id(state: dict[str, object]) -> str:
    sequence = max(1, _int(state.get('next_event_sequence'), 1))
    state['next_event_sequence'] = sequence + 1
    return f'mnotif_{sequence:012d}'


def _event_sequence(event: MobileNotificationEvent) -> int:
    return _event_sequence_from_id(event.id)


def _event_sequence_from_id(value: str) -> int:
    text = str(value or '')
    for prefix in ('mnotif_resync_', 'mnotif_'):
        if text.startswith(prefix):
            try:
                return int(text[len(prefix):])
            except ValueError:
                return -1
    return -1


def _next_sequence_after(events: list[MobileNotificationEvent]) -> int:
    return max([0, *(_event_sequence(event) for event in events)]) + 1


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            records.append(dict(payload))
    return records


def _write_jsonl(path: Path, events: list[MobileNotificationEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ''.join(json.dumps(event.to_payload(), ensure_ascii=False, sort_keys=True) + '\n' for event in events)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(text, encoding='utf-8')
    temp.replace(path)


def _map(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _optional_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


__all__ = [
    'MobileNotificationEvent', 'MobileInvalidationSnapshot', 'MobileNotificationSnapshot',
    'MobileNotificationStore', 'NOTIFICATION_KIND_TASK_COMPLETED',
    'INVALIDATION_KIND_AGENT_ACTIVITY', 'INVALIDATION_KIND_CONVERSATION',
    'INVALIDATION_KIND_PROJECT_SUMMARY', 'INVALIDATION_KIND_RESYNC', 'SCHEMA_VERSION',
    'encode_sse_event',
]
