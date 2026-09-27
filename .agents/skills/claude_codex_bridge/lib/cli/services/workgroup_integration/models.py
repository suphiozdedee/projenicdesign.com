from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from cli.services.loop_execution_scope import normalize_scope_paths


WORKGROUP_GIT_TRANSACTION_SCHEMA = 'ccb.loop.workgroup_git_transaction.v1'
WORKGROUP_GIT_TRANSACTION_VERSION = 1
MAX_WORKGROUP_NODES = 4
_SEGMENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')


class GitIntegrationError(RuntimeError):
    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        self.code = str(code)
        self.stage = str(stage)
        self.message = str(message)
        self.details = dict(details or {})
        super().__init__(f'{self.code} at {self.stage}: {self.message}')

    def to_record(self) -> dict[str, object]:
        return {
            'code': self.code,
            'stage': self.stage,
            'message': self.message,
            'details': self.details,
        }


@dataclass(frozen=True)
class WorkgroupNodeSpec:
    node_id: str
    workgroup_id: str
    allowed_paths: tuple[str, ...]
    integration_order: int
    depends_on: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        node_id = _segment(self.node_id, field_name='node_id')
        workgroup_id = _segment(self.workgroup_id, field_name='workgroup_id')
        if isinstance(self.integration_order, bool) or not isinstance(self.integration_order, int):
            raise ValueError('integration_order must be a positive integer')
        if self.integration_order <= 0:
            raise ValueError('integration_order must be a positive integer')
        dependencies = tuple(
            _segment(value, field_name=f'{node_id}.depends_on')
            for value in self.depends_on
        )
        if len(set(dependencies)) != len(dependencies):
            raise ValueError(f'{node_id}.depends_on contains duplicate node ids')
        if node_id in dependencies:
            raise ValueError(f'{node_id}.depends_on cannot contain itself')
        allowed_paths = tuple(
            normalize_scope_paths(
                list(self.allowed_paths),
                field_name=f'{node_id}.allowed_paths',
            )
        )
        if not allowed_paths:
            raise ValueError(f'{node_id}.allowed_paths must not be empty')
        object.__setattr__(self, 'node_id', node_id)
        object.__setattr__(self, 'workgroup_id', workgroup_id)
        object.__setattr__(self, 'depends_on', dependencies)
        object.__setattr__(self, 'allowed_paths', allowed_paths)

    @classmethod
    def from_bundle_node(cls, value: object) -> WorkgroupNodeSpec:
        if not isinstance(value, dict):
            raise ValueError('bundle node must be an object')
        depends_on = value.get('depends_on')
        allowed_paths = value.get('allowed_paths')
        if not isinstance(depends_on, list):
            raise ValueError('bundle node depends_on must be a list')
        if not isinstance(allowed_paths, list):
            raise ValueError('bundle node allowed_paths must be a list')
        return cls(
            node_id=str(value.get('node_id') or ''),
            workgroup_id=str(value.get('workgroup_id') or ''),
            depends_on=tuple(str(item) for item in depends_on),
            allowed_paths=tuple(str(item) for item in allowed_paths),
            integration_order=value.get('integration_order'),
        )

    def to_record(self) -> dict[str, object]:
        return {
            'node_id': self.node_id,
            'workgroup_id': self.workgroup_id,
            'depends_on': list(self.depends_on),
            'allowed_paths': list(self.allowed_paths),
            'integration_order': self.integration_order,
        }


@dataclass(frozen=True)
class VerificationCommand:
    label: str
    argv: tuple[str, ...]
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        label = str(self.label or '').strip()
        if not label or '\n' in label or '\r' in label:
            raise ValueError('verification label must be a non-empty single line')
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError('verification argv must be a non-empty tuple')
        argv = tuple(str(item) for item in self.argv)
        if any(not item for item in argv):
            raise ValueError('verification argv entries must not be empty')
        if len(argv) > 128 or any(len(item) > 4096 for item in argv):
            raise ValueError('verification argv exceeds the bounded command shape')
        timeout = float(self.timeout_seconds)
        if not 0 < timeout <= 3600:
            raise ValueError('verification timeout_seconds must be between 0 and 3600')
        object.__setattr__(self, 'label', label)
        object.__setattr__(self, 'argv', argv)
        object.__setattr__(self, 'timeout_seconds', timeout)

    def to_record(self) -> dict[str, object]:
        return {
            'label': self.label,
            'argv': list(self.argv),
            'timeout_seconds': self.timeout_seconds,
        }


def _segment(value: object, *, field_name: str) -> str:
    text = str(value or '').strip()
    if not _SEGMENT_RE.fullmatch(text):
        raise ValueError(f'{field_name} must match {_SEGMENT_RE.pattern}')
    return text


def normalized_path(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


__all__ = [
    'GitIntegrationError',
    'MAX_WORKGROUP_NODES',
    'VerificationCommand',
    'WORKGROUP_GIT_TRANSACTION_SCHEMA',
    'WORKGROUP_GIT_TRANSACTION_VERSION',
    'WorkgroupNodeSpec',
]
