from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from storage.atomic import atomic_write_json, ensure_durable_directory
from storage.locks import file_lock


SCHEMA = 'ccb.plan.planner_task_set_import_transaction.v1'
JOURNAL_NAME = 'planner-task-set-import.transaction.json'
LOCK_NAME = 'planner-task-set-import.transaction.lock'
CONFLICTS_NAME = 'planner-task-set-import.transaction.conflicts.json'
TRACE_KEY = 'planner_task_set_import_transaction'
_DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')


class PlannerTaskSetImportConflict(ValueError):
    pass


def canonical_journal_ref(job_id: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', str(job_id or '')):
        raise ValueError('planner task-set import job_id is invalid')
    return f'.ccb/runtime/role-output-imports/{job_id}/{JOURNAL_NAME}'


def transaction_digest(identity: dict[str, object]) -> str:
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def prepare(context, *, identity: dict[str, object]) -> dict[str, object]:
    job_id = str(identity.get('planner_job_id') or '')
    path = _journal_path(context, job_id)
    digest = transaction_digest(identity)
    _assert_safe_transaction_layout(Path(context.project.project_root), path, create=True)
    with file_lock(path.with_name(LOCK_NAME)):
        existing = _read(path)
        if existing is not None:
            _validate(existing, expected_ref=canonical_journal_ref(job_id))
            if existing.get('transaction_digest') != digest or existing.get('identity') != identity:
                conflict = {
                    'reason': 'planner_task_set_import_identity_conflict',
                    'observed_transaction_digest': digest,
                    'observed_identity': identity,
                    'recorded_at': _now(),
                }
                if existing.get('status') == 'committed':
                    _record_committed_conflict(path.with_name(CONFLICTS_NAME), existing, conflict)
                else:
                    existing['status'] = 'failed'
                    existing.setdefault('conflicts', []).append(conflict)
                    existing['updated_at'] = _now()
                    atomic_write_json(path, existing)
                raise PlannerTaskSetImportConflict(conflict['reason'])
            if existing.get('status') == 'failed':
                raise PlannerTaskSetImportConflict('planner task-set import transaction is failed')
            return existing
        now = _now()
        record = {
            'schema': SCHEMA,
            'schema_version': 1,
            'status': 'prepared',
            'journal_ref': canonical_journal_ref(job_id),
            'transaction_digest': digest,
            'identity': identity,
            'created_at': now,
            'updated_at': now,
            'conflicts': [],
        }
        atomic_write_json(path, record)
        return record


def authority_trace(record: dict[str, object], *, source_job: dict[str, object]) -> dict[str, object]:
    return {
        'source': 'loop_runner_role_output_import',
        'source_job': source_job,
        TRACE_KEY: {
            'journal_ref': record['journal_ref'],
            'transaction_digest': record['transaction_digest'],
        },
    }


def commit(context, record: dict[str, object], *, authority: dict[str, object]) -> dict[str, object]:
    job_id = str(record['identity']['planner_job_id'])
    path = _journal_path(context, job_id)
    _assert_safe_transaction_layout(Path(context.project.project_root), path, create=False)
    with file_lock(path.with_name(LOCK_NAME)):
        current = _read(path)
        if current is None:
            raise PlannerTaskSetImportConflict('planner task-set import journal disappeared before commit')
        _validate(current, expected_ref=canonical_journal_ref(job_id))
        if current.get('transaction_digest') != record.get('transaction_digest'):
            raise PlannerTaskSetImportConflict('planner task-set import journal changed before commit')
        if current.get('identity') != record.get('identity'):
            raise PlannerTaskSetImportConflict('planner task-set import identity changed before commit')
        _validate_authority(current['identity'], authority)
        if current.get('status') == 'failed':
            raise PlannerTaskSetImportConflict('planner task-set import transaction is failed')
        if current.get('status') == 'committed':
            if current.get('authority') != authority:
                raise PlannerTaskSetImportConflict('committed planner task-set authority conflicts with replay')
            return current
        current['authority'] = authority
        current['status'] = 'committed'
        current['committed_at'] = _now()
        current['updated_at'] = current['committed_at']
        atomic_write_json(path, current)
        return current


def fail(context, record: dict[str, object], *, reason: str, evidence: object) -> None:
    job_id = str(record['identity']['planner_job_id'])
    path = _journal_path(context, job_id)
    _assert_safe_transaction_layout(Path(context.project.project_root), path, create=False)
    with file_lock(path.with_name(LOCK_NAME)):
        current = _read(path) or record
        if current.get('status') == 'committed':
            return
        current['status'] = 'failed'
        current.setdefault('conflicts', []).append({
            'reason': reason,
            'evidence': evidence,
            'recorded_at': _now(),
        })
        current['updated_at'] = _now()
        atomic_write_json(path, current)


def runner_transaction_committed(project_root: Path, task: dict[str, object]) -> bool:
    trace = task.get('authority_trace') if isinstance(task.get('authority_trace'), dict) else {}
    tx = trace.get(TRACE_KEY) if isinstance(trace.get(TRACE_KEY), dict) else None
    if tx is None:
        return True
    journal_ref = str(tx.get('journal_ref') or '')
    digest = str(tx.get('transaction_digest') or '')
    source_job = trace.get('source_job') if isinstance(trace.get('source_job'), dict) else {}
    job_id = str(source_job.get('job_id') or '')
    try:
        canonical_ref = canonical_journal_ref(job_id)
    except ValueError:
        return False
    if journal_ref != canonical_ref or not _DIGEST_RE.fullmatch(digest):
        return False
    root = Path(project_root).resolve()
    path = root / canonical_ref
    try:
        _assert_safe_transaction_layout(root, path, create=False)
        record = _read(path)
        if record is None:
            return False
        _validate(record, expected_ref=canonical_ref)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if record.get('status') != 'committed' or record.get('transaction_digest') != digest:
        return False
    identity = record.get('identity') if isinstance(record.get('identity'), dict) else {}
    authority = record.get('authority') if isinstance(record.get('authority'), dict) else {}
    try:
        _validate_authority(identity, authority)
    except ValueError:
        return False
    task_id = str(task.get('task_id') or '')
    identity_children = identity.get('ordered_children') if isinstance(identity.get('ordered_children'), list) else []
    authority_children = authority.get('children') if isinstance(authority.get('children'), list) else []
    identity_matches = [child for child in identity_children if isinstance(child, dict) and child.get('task_id') == task_id]
    authority_matches = [child for child in authority_children if isinstance(child, dict) and child.get('task_id') == task_id]
    if len(identity_matches) != 1 or len(authority_matches) != 1:
        return False
    task_set_id = str(identity.get('task_set_id') or '')
    revision = authority.get('task_set_revision')
    binding = task.get('task_set') if isinstance(task.get('task_set'), dict) else {}
    authority_binding = authority_matches[0].get('task_set') if isinstance(authority_matches[0].get('task_set'), dict) else {}
    return (
        binding == authority_binding
        and binding.get('schema') == 'ccb.plan.task_set_binding.v1'
        and binding.get('binding_role') == 'child'
        and binding.get('task_set_id') == task_set_id
        and binding.get('task_set_revision') == revision
        and binding.get('required') is identity_matches[0].get('required', True)
        and binding.get('order') == identity_children.index(identity_matches[0])
        and authority_matches[0].get('task_revision') == binding.get('bound_task_revision')
    )


def _journal_path(context, job_id: str) -> Path:
    return Path(context.project.project_root) / canonical_journal_ref(job_id)


def _read(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        raise ValueError('planner task-set import journal must be an object')
    return payload


def _validate(record: dict[str, object], *, expected_ref: str) -> None:
    if record.get('schema') != SCHEMA or record.get('schema_version') != 1:
        raise ValueError('planner task-set import journal schema mismatch')
    if record.get('journal_ref') != expected_ref:
        raise ValueError('planner task-set import journal ref mismatch')
    identity = record.get('identity')
    digest = str(record.get('transaction_digest') or '')
    if not isinstance(identity, dict) or not _DIGEST_RE.fullmatch(digest) or transaction_digest(identity) != digest:
        raise ValueError('planner task-set import journal digest mismatch')
    if canonical_journal_ref(str(identity.get('planner_job_id') or '')) != expected_ref:
        raise ValueError('planner task-set import journal job identity mismatch')
    if record.get('status') not in {'prepared', 'committed', 'failed'}:
        raise ValueError('planner task-set import journal status invalid')
    if record.get('status') == 'committed':
        authority = record.get('authority')
        if not isinstance(authority, dict):
            raise ValueError('committed planner task-set import authority missing')
        _validate_authority(identity, authority)


def _validate_authority(identity: dict[str, object], authority: dict[str, object]) -> None:
    task_set_id = str(identity.get('task_set_id') or '')
    if authority.get('task_set_id') != task_set_id or authority.get('task_set_revision') != 1:
        raise ValueError('planner task-set committed task-set authority mismatch')
    identity_children = identity.get('ordered_children')
    authority_children = authority.get('children')
    if not isinstance(identity_children, list) or not isinstance(authority_children, list):
        raise ValueError('planner task-set committed children authority missing')
    identity_ids = [child.get('task_id') for child in identity_children if isinstance(child, dict)]
    authority_ids = [child.get('task_id') for child in authority_children if isinstance(child, dict)]
    if len(identity_ids) != len(identity_children) or len(set(identity_ids)) != len(identity_ids):
        raise ValueError('planner task-set identity children are invalid or duplicated')
    if authority_ids != identity_ids or len(set(authority_ids)) != len(authority_ids):
        raise ValueError('planner task-set committed children do not match identity')
    for order, (expected, observed) in enumerate(zip(identity_children, authority_children)):
        binding = observed.get('task_set') if isinstance(observed.get('task_set'), dict) else {}
        if (
            binding.get('schema') != 'ccb.plan.task_set_binding.v1'
            or binding.get('binding_role') != 'child'
            or binding.get('task_set_id') != task_set_id
            or binding.get('task_set_revision') != 1
            or binding.get('required') is not expected.get('required', True)
            or binding.get('order') != order
            or observed.get('task_revision') != binding.get('bound_task_revision')
        ):
            raise ValueError('planner task-set committed child binding mismatch')


def _assert_safe_transaction_layout(root: Path, journal: Path, *, create: bool) -> None:
    root = root.resolve()
    expected = root / canonical_journal_ref(journal.parent.name)
    if journal != expected or root not in journal.parents:
        raise ValueError('planner task-set import journal layout mismatch')
    if create:
        ensure_durable_directory(journal.parent)
    relative_parent = journal.parent.relative_to(root)
    current = root
    for part in relative_parent.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise ValueError('planner task-set import parent layout is unsafe')
        else:
            raise ValueError('planner task-set import parent layout missing')
    for leaf in (journal, journal.with_name(LOCK_NAME), journal.with_name(CONFLICTS_NAME)):
        if leaf.is_symlink():
            raise ValueError('planner task-set import transaction file symlink rejected')


def _record_committed_conflict(path: Path, journal: dict[str, object], conflict: dict[str, object]) -> None:
    existing = _read(path) or {
        'schema': 'ccb.plan.planner_task_set_import_conflicts.v1',
        'transaction_digest': journal['transaction_digest'],
        'conflicts': [],
    }
    if (
        existing.get('schema') != 'ccb.plan.planner_task_set_import_conflicts.v1'
        or existing.get('transaction_digest') != journal.get('transaction_digest')
        or not isinstance(existing.get('conflicts'), list)
    ):
        raise PlannerTaskSetImportConflict('planner task-set committed conflict evidence is invalid')
    existing['conflicts'].append(conflict)
    atomic_write_json(path, existing)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
