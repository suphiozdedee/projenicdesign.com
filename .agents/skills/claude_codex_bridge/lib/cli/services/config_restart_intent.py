from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Iterable

from agents.config_identity import project_config_identity_payload
from agents.config_loader import load_project_config, project_config_path
from ccbd.models import MountState
from ccbd.services.mount import MountManager
from ccbd.system import utc_now
from storage.json_store import JsonStore
from storage.paths import PathLayout


SCHEMA_VERSION = 1
RECORD_TYPE = 'ccb_config_restart_intent'


@dataclass(frozen=True)
class ConfigRestartIntent:
    project_id: str
    target_config_digest: str
    source_daemon_instance_id: str | None
    source_generation: int | None
    affected_agents: tuple[str, ...]
    reason: str
    created_at: str

    def to_record(self) -> dict[str, object]:
        return {
            'schema_version': SCHEMA_VERSION,
            'record_type': RECORD_TYPE,
            'project_id': self.project_id,
            'target_config_digest': self.target_config_digest,
            'source_daemon_instance_id': self.source_daemon_instance_id,
            'source_generation': self.source_generation,
            'affected_agents': list(self.affected_agents),
            'reason': self.reason,
            'created_at': self.created_at,
        }

    @classmethod
    def from_record(cls, record: dict[str, object]) -> ConfigRestartIntent:
        if record.get('schema_version') != SCHEMA_VERSION:
            raise ValueError('unsupported config restart intent schema')
        if record.get('record_type') != RECORD_TYPE:
            raise ValueError('invalid config restart intent record type')
        project_id = _required_text(record.get('project_id'), field='project_id')
        target_config_digest = _config_digest(
            record.get('target_config_digest'),
        )
        reason = _required_text(record.get('reason'), field='reason')
        created_at = _required_text(record.get('created_at'), field='created_at')
        source_daemon_instance_id = _optional_text(
            record.get('source_daemon_instance_id')
        )
        source_generation = _optional_positive_int(
            record.get('source_generation'),
            field='source_generation',
        )
        raw_agents = record.get('affected_agents', [])
        if not isinstance(raw_agents, list):
            raise ValueError('affected_agents must be a list')
        affected_agents = _normalized_agents(raw_agents)
        return cls(
            project_id=project_id,
            target_config_digest=target_config_digest,
            source_daemon_instance_id=source_daemon_instance_id,
            source_generation=source_generation,
            affected_agents=affected_agents,
            reason=reason,
            created_at=created_at,
        )


def record_config_restart_intent(
    project_root: Path,
    *,
    target_config_digest: str,
    affected_agents: Iterable[object] = (),
    reason: str,
    layout=None,
    clock=utc_now,
) -> ConfigRestartIntent:
    effective_layout = _layout(project_root, layout=layout)
    lease = _best_effort_mounted_lease(effective_layout)
    source_generation = int(getattr(lease, 'generation', 0) or 0)
    intent = ConfigRestartIntent(
        project_id=effective_layout.project_id,
        target_config_digest=_config_digest(
            target_config_digest,
        ),
        source_daemon_instance_id=_optional_text(
            getattr(lease, 'daemon_instance_id', None)
        ),
        source_generation=source_generation if source_generation > 0 else None,
        affected_agents=_normalized_agents(affected_agents),
        reason=_required_text(reason, field='reason'),
        created_at=_required_text(clock(), field='created_at'),
    )
    JsonStore().save(
        effective_layout.ccbd_config_restart_intent_path,
        intent,
        serializer=lambda value: value.to_record(),
    )
    return intent


def load_config_restart_intent(layout) -> ConfigRestartIntent | None:
    path = layout.ccbd_config_restart_intent_path
    if not path.is_file():
        return None
    try:
        intent = JsonStore().load(path, loader=ConfigRestartIntent.from_record)
    except (OSError, UnicodeError, ValueError):
        return None
    if intent.project_id != layout.project_id:
        return None
    return intent


def config_restart_required_for_inspection(context, inspection) -> bool:
    intent = load_config_restart_intent(context.paths)
    if intent is None or not _intent_targets_active_config(context.paths, intent):
        return False
    lease = getattr(inspection, 'lease', None)
    if lease is None:
        return False
    current_instance = _optional_text(getattr(lease, 'daemon_instance_id', None))
    if intent.source_daemon_instance_id is not None:
        return current_instance == intent.source_daemon_instance_id
    current_generation = int(getattr(lease, 'generation', 0) or 0)
    if intent.source_generation is not None:
        return current_generation == intent.source_generation
    return False


def clear_applied_config_restart_intent(context) -> bool:
    intent = load_config_restart_intent(context.paths)
    if intent is None or not _intent_targets_active_config(context.paths, intent):
        return False
    lease = _best_effort_mounted_lease(context.paths)
    if lease is None:
        return False
    try:
        expected_signature = _active_config_signature(context.paths.project_root)
    except (OSError, UnicodeError, ValueError):
        return False
    current_signature = str(getattr(lease, 'config_signature', '') or '').strip()
    if not current_signature or current_signature != expected_signature:
        return False
    current_instance = _optional_text(getattr(lease, 'daemon_instance_id', None))
    if (
        intent.source_daemon_instance_id is not None
        and current_instance == intent.source_daemon_instance_id
    ):
        return False
    if (
        intent.source_daemon_instance_id is None
        and intent.source_generation is not None
        and int(getattr(lease, 'generation', 0) or 0) == intent.source_generation
    ):
        return False
    try:
        context.paths.ccbd_config_restart_intent_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def discard_config_restart_intent_for_digest(
    project_root: Path,
    target_config_digest: str,
    *,
    layout=None,
) -> bool:
    effective_layout = _layout(project_root, layout=layout)
    intent = load_config_restart_intent(effective_layout)
    if (
        intent is None
        or intent.target_config_digest != str(target_config_digest or '').strip()
    ):
        return False
    try:
        effective_layout.ccbd_config_restart_intent_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _layout(project_root: Path, *, layout=None):
    return layout if layout is not None else PathLayout(project_root)


def _best_effort_mounted_lease(layout):
    try:
        lease = MountManager(layout).load_state()
    except (OSError, UnicodeError, ValueError):
        return None
    if lease is None or lease.mount_state is not MountState.MOUNTED:
        return None
    return lease


def _intent_targets_active_config(layout, intent: ConfigRestartIntent) -> bool:
    try:
        digest = _active_config_digest(layout.project_root)
    except (OSError, UnicodeError, ValueError):
        return False
    return digest == intent.target_config_digest


def _active_config_digest(project_root: Path) -> str:
    return hashlib.sha256(project_config_path(project_root).read_bytes()).hexdigest()


def _active_config_signature(project_root: Path) -> str:
    payload = project_config_identity_payload(load_project_config(project_root).config)
    return str(payload.get('config_signature') or '').strip()


def _required_text(value: object, *, field: str) -> str:
    text = str(value or '').strip()
    if not text:
        raise ValueError(f'{field} cannot be empty')
    return text


def _config_digest(value: object) -> str:
    digest = _required_text(value, field='target_config_digest').lower()
    if len(digest) != 64 or any(
        character not in '0123456789abcdef' for character in digest
    ):
        raise ValueError('target_config_digest must be a SHA-256 hex digest')
    return digest


def _optional_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def _optional_positive_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError(f'{field} must be a positive integer or null')
    return value


def _normalized_agents(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(value or '').strip().lower()
                for value in values
                if str(value or '').strip()
            }
        )
    )


__all__ = [
    'ConfigRestartIntent',
    'clear_applied_config_restart_intent',
    'config_restart_required_for_inspection',
    'discard_config_restart_intent_for_digest',
    'load_config_restart_intent',
    'record_config_restart_intent',
]
