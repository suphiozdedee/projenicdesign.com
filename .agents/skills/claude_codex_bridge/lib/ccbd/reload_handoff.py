from __future__ import annotations

from dataclasses import dataclass

from ccbd.models import SCHEMA_VERSION
from ccbd.system import parse_utc_timestamp
from storage.json_store import JsonStore
from storage.paths import PathLayout

RELOAD_HANDOFF_TTL_S = 60.0
_RECORD_TYPE = 'ccbd_reload_handoff'


@dataclass(frozen=True)
class ReloadHandoff:
    project_id: str
    started_at: str
    old_config_signature: str
    target_config_signature: str
    daemon_pid: int
    daemon_instance_id: str
    generation: int
    status: str = 'applying'

    def __post_init__(self) -> None:
        if not _clean_text(self.project_id):
            raise ValueError('project_id cannot be empty')
        if not _clean_text(self.started_at):
            raise ValueError('started_at cannot be empty')
        if not _clean_text(self.old_config_signature):
            raise ValueError('old_config_signature cannot be empty')
        if not _clean_text(self.target_config_signature):
            raise ValueError('target_config_signature cannot be empty')
        if int(self.daemon_pid) <= 0:
            raise ValueError('daemon_pid must be positive')
        if not _clean_text(self.daemon_instance_id):
            raise ValueError('daemon_instance_id cannot be empty')
        if int(self.generation) <= 0:
            raise ValueError('generation must be positive')
        if self.status != 'applying':
            raise ValueError("status must be 'applying'")

    def to_record(self) -> dict[str, object]:
        return {
            'schema_version': SCHEMA_VERSION,
            'record_type': _RECORD_TYPE,
            'project_id': self.project_id,
            'started_at': self.started_at,
            'old_config_signature': self.old_config_signature,
            'target_config_signature': self.target_config_signature,
            'daemon_pid': int(self.daemon_pid),
            'daemon_instance_id': self.daemon_instance_id,
            'generation': int(self.generation),
            'status': self.status,
            'ttl_s': RELOAD_HANDOFF_TTL_S,
        }

    @classmethod
    def from_record(cls, record: dict[str, object]) -> ReloadHandoff:
        if record.get('schema_version') != SCHEMA_VERSION:
            raise ValueError(f'schema_version must be {SCHEMA_VERSION}')
        if record.get('record_type') != _RECORD_TYPE:
            raise ValueError(f"record_type must be '{_RECORD_TYPE}'")
        return cls(
            project_id=str(record.get('project_id') or ''),
            started_at=str(record.get('started_at') or ''),
            old_config_signature=str(record.get('old_config_signature') or ''),
            target_config_signature=str(record.get('target_config_signature') or ''),
            daemon_pid=int(record.get('daemon_pid') or 0),
            daemon_instance_id=str(record.get('daemon_instance_id') or ''),
            generation=int(record.get('generation') or 0),
            status=str(record.get('status') or ''),
        )


@dataclass(frozen=True)
class ReloadHandoffStore:
    layout: PathLayout
    store: JsonStore | None = None

    def __post_init__(self) -> None:
        if self.store is None:
            object.__setattr__(self, 'store', JsonStore())

    def load(self) -> ReloadHandoff | None:
        path = self.layout.ccbd_reload_handoff_path
        if not path.exists():
            return None
        return self.store.load(path, loader=ReloadHandoff.from_record)

    def save(self, handoff: ReloadHandoff) -> None:
        self.store.save(
            self.layout.ccbd_reload_handoff_path,
            handoff,
            serializer=lambda value: value.to_record(),
        )

    def clear(self) -> None:
        try:
            self.layout.ccbd_reload_handoff_path.unlink()
        except FileNotFoundError:
            return


def begin_reload_handoff(app, *, target_config_identity: dict[str, object]) -> ReloadHandoff | None:
    current = app.current_service_graph()
    old_signature = _clean_text(current.config_identity.get('config_signature'))
    target_signature = _clean_text(target_config_identity.get('config_signature'))
    if not old_signature or not target_signature or old_signature == target_signature:
        return None
    lease = getattr(app, 'lease', None)
    if lease is None:
        lease = app.mount_manager.load_state()
    daemon_pid = int(getattr(lease, 'ccbd_pid', 0) or 0)
    daemon_instance_id = str(getattr(lease, 'daemon_instance_id', '') or '').strip()
    generation = int(getattr(lease, 'generation', 0) or 0)
    if daemon_pid <= 0 or not daemon_instance_id or generation <= 0:
        return None
    handoff = ReloadHandoff(
        project_id=str(getattr(app, 'project_id', '') or ''),
        started_at=app.clock(),
        old_config_signature=old_signature,
        target_config_signature=target_signature,
        daemon_pid=daemon_pid,
        daemon_instance_id=daemon_instance_id,
        generation=generation,
    )
    ReloadHandoffStore(app.paths).save(handoff)
    return handoff


def clear_reload_handoff(app) -> None:
    ReloadHandoffStore(app.paths).clear()


def reload_handoff_allows_signature_mismatch(
    app,
    *,
    expected_config_signature: str,
    actual_config_signature: str,
    now: str | None = None,
) -> bool:
    expected = _clean_text(expected_config_signature)
    actual = _clean_text(actual_config_signature)
    if not expected or not actual:
        return False
    try:
        handoff = ReloadHandoffStore(app.paths).load()
    except Exception:
        return False
    if handoff is None:
        return False
    if not _handoff_age_valid(handoff, now=now or app.clock()):
        return False
    if handoff.project_id != _expected_project_id(app):
        return False
    if handoff.old_config_signature != actual:
        return False
    if handoff.target_config_signature != expected:
        return False
    return _matches_current_holder(app, handoff)


def _handoff_age_valid(handoff: ReloadHandoff, *, now: str) -> bool:
    try:
        age_s = (parse_utc_timestamp(now) - parse_utc_timestamp(handoff.started_at)).total_seconds()
    except Exception:
        return False
    return 0.0 <= age_s <= RELOAD_HANDOFF_TTL_S


def _matches_current_holder(app, handoff: ReloadHandoff) -> bool:
    try:
        inspection = app._ownership_guard.inspect()
    except Exception:
        return False
    lease = getattr(inspection, 'lease', None)
    if lease is None:
        return False
    if int(getattr(lease, 'ccbd_pid', 0) or 0) != int(handoff.daemon_pid):
        return False
    if str(getattr(lease, 'daemon_instance_id', '') or '').strip() != handoff.daemon_instance_id:
        return False
    if int(getattr(lease, 'generation', 0) or 0) != int(handoff.generation):
        return False
    return bool(getattr(inspection, 'pid_alive', False) and getattr(inspection, 'socket_connectable', False))


def _expected_project_id(app) -> str:
    return str(getattr(app, 'project_id', '') or getattr(app.paths, 'project_id', '') or '').strip()


def _clean_text(value: object) -> str:
    return str(value or '').strip()


__all__ = [
    'RELOAD_HANDOFF_TTL_S',
    'ReloadHandoff',
    'ReloadHandoffStore',
    'begin_reload_handoff',
    'clear_reload_handoff',
    'reload_handoff_allows_signature_mismatch',
]
