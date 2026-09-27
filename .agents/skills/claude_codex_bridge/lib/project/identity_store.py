from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import socket
from typing import Callable

from process_liveness import process_exists as _platform_process_exists
from storage.atomic import atomic_write_json
from storage.locks import file_lock

from .ids import compute_legacy_project_id, project_slug_from_name


PROJECT_IDENTITY_FILENAME = 'project.identity.json'
PROJECT_IDENTITY_LOCK_FILENAME = 'project.identity.lock'
PROJECT_IDENTITY_RECORD_TYPE = 'ccb_project_identity'
PROJECT_IDENTITY_SCHEMA_VERSION = 1

_PROJECT_ID_PATTERN = re.compile(r'^[0-9a-f]{64}$')
_PROJECT_SLUG_PATTERN = re.compile(r'^[a-z0-9._-]+$')
_EVIDENCE_PATH_FIELDS = (
    'project_root',
    'anchor_path',
    'socket_path',
    'tmux_socket_path',
    'workspace_path',
    'runtime_root',
    'session_file',
)


class ProjectIdentityConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectIdentity:
    project_id: str
    project_slug: str
    created_at: str
    bound_root: str
    binding_epoch: int
    identity_origin: str
    last_bound_at: str
    legacy_project_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _PROJECT_ID_PATTERN.fullmatch(str(self.project_id or '')) is None:
            raise ValueError('project_id must be 64 lowercase hexadecimal characters')
        if _PROJECT_SLUG_PATTERN.fullmatch(str(self.project_slug or '')) is None:
            raise ValueError('project_slug contains unsupported characters')
        if not str(self.created_at or '').strip():
            raise ValueError('created_at cannot be empty')
        root = Path(str(self.bound_root or '')).expanduser()
        if not root.is_absolute():
            raise ValueError('bound_root must be absolute')
        if int(self.binding_epoch) <= 0:
            raise ValueError('binding_epoch must be positive')
        if not str(self.identity_origin or '').strip():
            raise ValueError('identity_origin cannot be empty')
        if not str(self.last_bound_at or '').strip():
            raise ValueError('last_bound_at cannot be empty')
        for legacy_id in self.legacy_project_ids:
            if _PROJECT_ID_PATTERN.fullmatch(str(legacy_id or '')) is None:
                raise ValueError('legacy_project_ids must contain 64-character lowercase hexadecimal IDs')

    def to_record(self) -> dict[str, object]:
        return {
            'schema_version': PROJECT_IDENTITY_SCHEMA_VERSION,
            'record_type': PROJECT_IDENTITY_RECORD_TYPE,
            'project_id': self.project_id,
            'project_slug': self.project_slug,
            'created_at': self.created_at,
            'bound_root': self.bound_root,
            'binding_epoch': self.binding_epoch,
            'identity_origin': self.identity_origin,
            'last_bound_at': self.last_bound_at,
            'legacy_project_ids': list(self.legacy_project_ids),
        }

    @classmethod
    def from_record(cls, payload: dict[str, object]) -> ProjectIdentity:
        if payload.get('schema_version') != PROJECT_IDENTITY_SCHEMA_VERSION:
            raise ValueError(
                f'schema_version must be {PROJECT_IDENTITY_SCHEMA_VERSION}'
            )
        if payload.get('record_type') != PROJECT_IDENTITY_RECORD_TYPE:
            raise ValueError(
                f"record_type must be '{PROJECT_IDENTITY_RECORD_TYPE}'"
            )
        legacy_ids = payload.get('legacy_project_ids') or []
        if not isinstance(legacy_ids, list):
            raise ValueError('legacy_project_ids must be a list')
        return cls(
            project_id=str(payload['project_id']),
            project_slug=str(payload['project_slug']),
            created_at=str(payload['created_at']),
            bound_root=str(payload['bound_root']),
            binding_epoch=int(payload['binding_epoch']),
            identity_origin=str(payload['identity_origin']),
            last_bound_at=str(payload['last_bound_at']),
            legacy_project_ids=tuple(str(value) for value in legacy_ids),
        )


@dataclass(frozen=True)
class _LegacyEvidence:
    project_ids: tuple[str, ...]
    project_roots: tuple[Path, ...]
    active_runtime: bool


def project_identity_path(project_root: Path) -> Path:
    return _resolved_path(project_root) / '.ccb' / PROJECT_IDENTITY_FILENAME


def load_project_identity(project_root: Path) -> ProjectIdentity | None:
    path = project_identity_path(project_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise ValueError(f'cannot read project identity {path}: {exc}') from exc
    if not isinstance(payload, dict):
        raise ValueError(f'{path}: expected JSON object')
    try:
        return ProjectIdentity.from_record(payload)
    except Exception as exc:
        raise ValueError(f'invalid project identity {path}: {exc}') from exc


def ensure_project_identity(
    project_root: Path,
    *,
    clock: Callable[[], str] | None = None,
    id_factory: Callable[[], str] | None = None,
    process_exists_fn: Callable[[int | None], bool] | None = None,
    socket_connectable_fn: Callable[[str | Path], bool] | None = None,
) -> ProjectIdentity:
    root = _resolved_path(project_root)
    ccb_dir = root / '.ccb'
    if not ccb_dir.is_dir():
        raise FileNotFoundError(f'project anchor does not exist: {ccb_dir}')
    now_fn = clock or _utc_now
    identity_path = ccb_dir / PROJECT_IDENTITY_FILENAME
    lock_path = ccb_dir / PROJECT_IDENTITY_LOCK_FILENAME
    with file_lock(lock_path):
        existing = load_project_identity(root)
        if existing is not None:
            evidence = _legacy_evidence(
                ccb_dir,
                process_exists_fn=process_exists_fn or _process_exists,
                socket_connectable_fn=socket_connectable_fn or _socket_connectable,
            )
            rebound = _rebind_existing_identity(
                existing,
                root=root,
                occurred_at=now_fn(),
                active_runtime=evidence.active_runtime,
            )
            if rebound != existing:
                atomic_write_json(identity_path, rebound.to_record())
            return rebound
        created = _build_initial_identity(
            root,
            occurred_at=now_fn(),
            id_factory=id_factory or _new_project_id,
            process_exists_fn=process_exists_fn or _process_exists,
            socket_connectable_fn=socket_connectable_fn or _socket_connectable,
        )
        atomic_write_json(identity_path, created.to_record())
        return created


def _rebind_existing_identity(
    identity: ProjectIdentity,
    *,
    root: Path,
    occurred_at: str,
    active_runtime: bool,
) -> ProjectIdentity:
    recorded_root = _resolved_path(Path(identity.bound_root))
    if recorded_root == root or _same_existing_path(recorded_root, root):
        if identity.bound_root == str(root):
            return identity
        return replace(identity, bound_root=str(root), last_bound_at=occurred_at)
    if recorded_root.exists():
        raise ProjectIdentityConflictError(
            'project identity is already bound to another existing root: '
            f'project_id={identity.project_id} bound_root={recorded_root} '
            f'current_root={root}; copied projects require an explicit fork'
        )
    if active_runtime:
        raise ProjectIdentityConflictError(
            'cannot relocate project identity while recorded runtime authority '
            f'is still active: project_id={identity.project_id} '
            f'bound_root={recorded_root} current_root={root}'
        )
    return replace(
        identity,
        bound_root=str(root),
        binding_epoch=identity.binding_epoch + 1,
        last_bound_at=occurred_at,
    )


def _build_initial_identity(
    root: Path,
    *,
    occurred_at: str,
    id_factory: Callable[[], str],
    process_exists_fn: Callable[[int | None], bool],
    socket_connectable_fn: Callable[[str | Path], bool],
) -> ProjectIdentity:
    legacy_current_id = compute_legacy_project_id(root)
    evidence = _legacy_evidence(
        root / '.ccb',
        process_exists_fn=process_exists_fn,
        socket_connectable_fn=socket_connectable_fn,
    )
    project_id = ''
    slug_root = root
    origin = 'new-random'
    if len(evidence.project_ids) > 1:
        joined = ', '.join(evidence.project_ids)
        raise ProjectIdentityConflictError(
            'cannot migrate project identity because legacy runtime records '
            f'disagree: project_ids={joined}; repair or reset the conflicting '
            'runtime residue before retrying'
        )
    if len(evidence.project_ids) == 1:
        recorded_id = evidence.project_ids[0]
        if recorded_id == legacy_current_id:
            project_id = recorded_id
            origin = 'legacy-current-root'
        elif len(evidence.project_roots) == 1 and not evidence.project_roots[0].exists():
            if evidence.active_runtime:
                raise ProjectIdentityConflictError(
                    'cannot adopt moved legacy project identity while recorded '
                    f'runtime authority is still active: project_id={recorded_id} '
                    f'previous_root={evidence.project_roots[0]} current_root={root}'
                )
            project_id = recorded_id
            slug_root = evidence.project_roots[0]
            origin = 'legacy-relocated-runtime'
        else:
            recorded_roots = ', '.join(str(path) for path in evidence.project_roots) or 'unknown'
            raise ProjectIdentityConflictError(
                'cannot safely bind legacy project identity to the current root: '
                f'project_id={recorded_id} recorded_roots={recorded_roots} '
                f'current_root={root}; repair the stale binding or use an '
                'explicit project-fork workflow'
            )
    elif _legacy_anchor_has_content(root / '.ccb'):
        project_id = legacy_current_id
        origin = 'legacy-current-root'
    if not project_id:
        project_id = str(id_factory())
    if _PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise ValueError('id_factory must return 64 lowercase hexadecimal characters')
    legacy_ids = tuple(
        value for value in evidence.project_ids if value != project_id
    )
    return ProjectIdentity(
        project_id=project_id,
        project_slug=project_slug_from_name(slug_root.name, project_id),
        created_at=occurred_at,
        bound_root=str(root),
        binding_epoch=1,
        identity_origin=origin,
        last_bound_at=occurred_at,
        legacy_project_ids=legacy_ids,
    )


def _legacy_evidence(
    ccb_dir: Path,
    *,
    process_exists_fn: Callable[[int | None], bool],
    socket_connectable_fn: Callable[[str | Path], bool],
) -> _LegacyEvidence:
    records = _legacy_runtime_records(ccb_dir)
    project_ids: set[str] = set()
    roots: set[Path] = set()
    active_runtime = False
    for record in records:
        project_id = str(record.get('project_id') or '').strip()
        if _PROJECT_ID_PATTERN.fullmatch(project_id):
            project_ids.add(project_id)
        for field in _EVIDENCE_PATH_FIELDS:
            root = _project_root_from_path_value(record.get(field))
            if root is not None:
                roots.add(root)
        if str(record.get('record_type') or '') == 'ccbd_lease':
            pid = _positive_int(record.get('ccbd_pid'))
            socket_path = str(record.get('socket_path') or '').strip()
            if process_exists_fn(pid) or (
                socket_path and socket_connectable_fn(socket_path)
            ):
                active_runtime = True
        elif str(record.get('record_type') or '') == 'ccbd_keeper':
            if (
                str(record.get('state') or '') == 'running'
                and process_exists_fn(_positive_int(record.get('keeper_pid')))
            ):
                active_runtime = True
        elif str(record.get('record_type') or '') == 'ccbd_lifecycle':
            if process_exists_fn(_positive_int(record.get('owner_pid'))):
                active_runtime = True
            if (
                str(record.get('desired_state') or '') == 'running'
                and process_exists_fn(_positive_int(record.get('keeper_pid')))
            ):
                active_runtime = True
    return _LegacyEvidence(
        project_ids=tuple(sorted(project_ids)),
        project_roots=tuple(sorted(roots, key=str)),
        active_runtime=active_runtime,
    )


def _legacy_runtime_records(ccb_dir: Path) -> tuple[dict[str, object], ...]:
    paths = [
        ccb_dir / 'ccbd' / 'lifecycle.json',
        ccb_dir / 'ccbd' / 'lease.json',
        ccb_dir / 'ccbd' / 'keeper.json',
        ccb_dir / 'ccbd' / 'state.json',
        ccb_dir / 'runtime-root-ref.json',
    ]
    agents_dir = ccb_dir / 'agents'
    if agents_dir.is_dir():
        paths.extend(sorted(agents_dir.glob('*/runtime.json')))
    records: list[dict[str, object]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return tuple(records)


def _legacy_anchor_has_content(ccb_dir: Path) -> bool:
    ignored = {
        PROJECT_IDENTITY_FILENAME,
        PROJECT_IDENTITY_LOCK_FILENAME,
    }
    try:
        return any(child.name not in ignored for child in ccb_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False


def _project_root_from_path_value(raw: object) -> Path | None:
    text = str(raw or '').strip()
    if not text:
        return None
    normalized = text.replace('\\', '/').rstrip('/')
    marker = '/.ccb'
    index = normalized.rfind(marker)
    if index <= 0:
        return None
    return Path(normalized[:index]).expanduser()


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _new_project_id() -> str:
    return secrets.token_hex(32)


def _resolved_path(path: Path) -> Path:
    candidate = Path(path).expanduser()
    try:
        return candidate.resolve(strict=False)
    except Exception:
        return candidate.absolute()


def _same_existing_path(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except OSError:
        return False


def _process_exists(pid: int | None) -> bool:
    return _platform_process_exists(pid)


def _is_windows() -> bool:
    return os.name == 'nt'


def _socket_connectable(path: str | Path, *, timeout_s: float = 0.1) -> bool:
    # Legacy runtime evidence uses AF_UNIX sockets; Windows control-plane endpoints
    # are represented separately and should not be probed through this path.
    if _is_windows():
        return False
    target = Path(path)
    if not target.exists() or not hasattr(socket, 'AF_UNIX'):
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_s)
    try:
        client.connect(str(target))
        return True
    except OSError:
        return False
    finally:
        client.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


__all__ = [
    'PROJECT_IDENTITY_FILENAME',
    'PROJECT_IDENTITY_LOCK_FILENAME',
    'ProjectIdentity',
    'ProjectIdentityConflictError',
    'ensure_project_identity',
    'load_project_identity',
    'project_identity_path',
]
