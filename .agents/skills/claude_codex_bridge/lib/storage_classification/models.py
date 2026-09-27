from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class StorageClass(str, Enum):
    SECRET = 'secret'
    SESSION = 'session'
    AUTHORITY = 'authority'
    STARTUP_AUTHORITY_BUNDLE = 'startup_authority_bundle'
    RUNTIME_EPHEMERAL = 'runtime_ephemeral'
    WORKSPACE = 'workspace'
    USER_CONTENT = 'user_content'
    PROJECTED_CONFIG = 'projected_config'
    REBUILDABLE_CACHE = 'rebuildable_cache'
    RESIDUE = 'residue'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class StorageEntry:
    path: Path
    relative_path: str
    storage_class: StorageClass
    size_bytes: int
    provider: str | None = None
    agent: str | None = None
    active: bool | None = None
    is_active_version: bool | None = None
    reachable_from_current_symlink: bool | None = None
    reclaimable: bool | None = None
    reason: str | None = None
    root_kind: str = 'project'

    def to_record(self) -> dict[str, object]:
        return {
            'path': str(self.path),
            'relative_path': self.relative_path.replace('\\', '/'),
            'storage_class': self.storage_class.value,
            'size_bytes': self.size_bytes,
            'provider': self.provider,
            'agent': self.agent,
            'active': self.active,
            'is_active_version': self.is_active_version,
            'reachable_from_current_symlink': self.reachable_from_current_symlink,
            'reclaimable': self.reclaimable,
            'reason': self.reason,
            'root_kind': self.root_kind,
        }


__all__ = ['StorageClass', 'StorageEntry']
