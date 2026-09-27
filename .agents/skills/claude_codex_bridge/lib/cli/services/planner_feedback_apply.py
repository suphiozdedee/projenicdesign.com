from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from contextlib import contextmanager

from storage.atomic import atomic_write_json, atomic_write_text
from storage.locks import file_lock

from .planner_feedback import (
    PlannerBackfillProposal,
    parse_planner_feedback_reply,
    planner_feedback_digest,
)


TRANSACTION_SCHEMA = 'ccb.plan.planner_backfill_transaction.v2'
BACKFILL_SCHEMA = 'ccb.plan.planner_backfill.v2'
_DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
_SEMANTICS = ('brief', 'roadmap', 'todo')
_TRANSACTION_FIELDS = {
    'schema', 'schema_version', 'task_set_id', 'task_set_revision',
    'closure_intent_id', 'planner_feedback_digest', 'preimage_plan_revision',
    'target_plan_revision', 'targets', 'transaction_digest',
}
_TARGET_FIELDS = {
    'path', 'preimage_digest', 'preimage_exists', 'preimage_text',
    'target_digest', 'target_text',
}
_BACKFILL_FIELDS = {
    'schema', 'schema_version', 'authority', 'proposal', 'transaction_digest',
    'target_plan_revision', 'transaction_path', 'backfill_digest',
}


def current_plan_revision(context, plan_slug: str) -> str:
    return plan_revision_authority(context, plan_slug)['digest']


def plan_revision_authority(context, plan_slug: str) -> dict[str, object]:
    targets = select_plan_projection_targets(context, plan_projection_root(context, plan_slug))
    files = []
    for path in targets:
        if path.is_file():
            files.append({
                'path': str(path.relative_to(context.project.project_root)),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    payload = {'schema': 'ccb.plan.revision.v1', 'files': files}
    payload['digest'] = _digest(payload)
    return payload


def apply_planner_feedback(
    context,
    proposal: PlannerBackfillProposal,
    authority: dict[str, object],
) -> dict[str, object]:
    """Apply an authenticated, revision-fenced Planner projection transaction."""
    normalized = _authority(proposal, authority)
    plan_root = plan_projection_root(context, str(normalized['plan_slug']))
    task_set_root = plan_root / 'task-sets' / str(normalized['task_set_id'])
    task_set = _read_json(task_set_root / 'task-set.json')
    closure = _read_json(task_set_root / 'closure.json')
    _validate_source_authority(task_set, closure, proposal, normalized)
    revision = int(normalized['task_set_revision'])
    tx_path = task_set_root / f'planner-backfill-r{revision}.transaction.json'
    backfill_path = task_set_root / f'planner-backfill-r{revision}.json'
    lock_path = task_set_root / f'planner-backfill-r{revision}.lock'

    # Every Planner projection for a plan takes this lock before its identity lock.
    # This makes the task-set and Detailer paths share one revision fence.
    with plan_projection_lock(context, str(normalized['plan_slug'])):
        with file_lock(lock_path):
            persisted_tx = _read_json(tx_path) if tx_path.is_file() else None
            expected_tx = _derive_transaction(
                context,
                plan_root,
                proposal,
                normalized,
                persisted=persisted_tx,
            )
            if persisted_tx is None:
                if current_plan_revision(context, str(normalized['plan_slug'])) != normalized['expected_plan_revision']:
                    raise ValueError('planner backfill plan revision conflict')
                atomic_write_json(tx_path, expected_tx)
            elif persisted_tx != expected_tx:
                raise ValueError('planner backfill transaction authority conflict')

            if backfill_path.is_file():
                existing = _read_json(backfill_path)
                expected = _backfill_record(proposal, normalized, expected_tx, tx_path, context)
                if existing != expected:
                    raise ValueError('planner backfill conflicts with persisted authority')
                validate_plan_projection_targets(context, expected_tx)
                if current_plan_revision(context, str(normalized['plan_slug'])) != expected_tx['target_plan_revision']:
                    raise ValueError('planner backfill replay target revision conflict')
                return _result(backfill_path, tx_path, existing, idempotent=True)

            apply_plan_projection_targets(context, expected_tx, write=atomic_write_text)
            validate_plan_projection_targets(context, expected_tx)
            observed_target = current_plan_revision(context, str(normalized['plan_slug']))
            if observed_target != expected_tx['target_plan_revision']:
                raise ValueError('planner backfill target revision conflict')
            record = _backfill_record(proposal, normalized, expected_tx, tx_path, context)
            atomic_write_json(backfill_path, record)
            return _result(backfill_path, tx_path, record, idempotent=False)


def validate_planner_backfill_record(
    context,
    path: Path,
    *,
    task_set: dict[str, object],
    closure: dict[str, object],
) -> dict[str, object]:
    record = _read_json(path)
    if set(record) != _BACKFILL_FIELDS or record.get('schema') != BACKFILL_SCHEMA or record.get('schema_version') != 2:
        raise ValueError('planner backfill record schema or fields invalid')
    digest = record.get('backfill_digest')
    if digest != _semantic_digest(record, omit='backfill_digest'):
        raise ValueError('planner backfill record digest invalid')
    authority = record.get('authority')
    proposal_record = record.get('proposal')
    if not isinstance(authority, dict) or not isinstance(proposal_record, dict):
        raise ValueError('planner backfill record authority invalid')
    proposal_reply = '**planner-backfill.json**\n```json\n' + json.dumps(proposal_record) + '\n```\n'
    proposal = parse_planner_feedback_reply(proposal_reply)
    normalized = _authority(proposal, authority)
    expected_authority = {
        'task_set_id': task_set['task_set_id'],
        'task_set_revision': task_set['task_set_revision'],
        'closure_digest': closure['closure_digest'],
        'ordered_terminal_evidence_digest': closure['ordered_terminal_evidence_digest'],
        'expected_plan_revision': (task_set.get('plan_revision') or {}).get('digest'),
        'plan_slug': task_set['plan_slug'],
    }
    if any(authority.get(key) != value for key, value in expected_authority.items()):
        raise ValueError('planner backfill record task-set authority mismatch')
    if proposal.frontdesk_notification_required not in {True, False}:
        raise ValueError('planner backfill notification authority invalid')
    tx_path = _canonical_relative_path(
        context,
        record.get('transaction_path'),
        expected=_transaction_path(context, task_set),
        label='planner backfill transaction',
    )
    tx = _read_json(tx_path)
    _validate_transaction_shape(context, tx, task_set=task_set)
    expected_tx = _derive_transaction(
        context,
        plan_projection_root(context, str(task_set['plan_slug'])),
        proposal,
        normalized,
        persisted=tx,
    )
    if tx != expected_tx:
        raise ValueError('planner backfill transaction is not derivable from proposal authority')
    if record.get('transaction_digest') != tx['transaction_digest'] or record.get('target_plan_revision') != tx['target_plan_revision']:
        raise ValueError('planner backfill transaction binding mismatch')
    validate_plan_projection_targets(context, tx)
    if current_plan_revision(context, str(task_set['plan_slug'])) != tx['target_plan_revision']:
        raise ValueError('planner backfill projected plan revision conflict')
    return record


def _derive_transaction(context, plan_root, proposal, authority, *, persisted):
    sections = plan_projection_sections(proposal)
    expected_paths = select_plan_projection_targets(context, plan_root)
    if persisted is not None:
        _validate_transaction_shape(
            context,
            persisted,
            task_set={'task_set_id': authority['task_set_id'],
                      'task_set_revision': authority['task_set_revision'],
                      'plan_slug': authority['plan_slug']},
        )
    targets = []
    projected = {}
    preimages = {}
    preimage_paths = set()
    for semantic, path in zip(_SEMANTICS, expected_paths):
        current = path.read_text(encoding='utf-8') if path.is_file() else ''
        _validate_managed_markers(
            context,
            current,
            path=path,
            identity=str(authority['task_set_id']),
            revision=int(authority['task_set_revision']),
            semantic=semantic,
            persisted=persisted,
        )
        persisted_target = _persisted_target(persisted, str(path.relative_to(context.project.project_root)))
        if persisted_target is None:
            before = current
            preimage_exists = path.is_file()
            _reject_marker_collision(before, str(authority['task_set_id']), int(authority['task_set_revision']), semantic)
        else:
            current_digest = plan_projection_text_digest(current)
            if current_digest not in {persisted_target['target_digest'], persisted_target['preimage_digest']}:
                raise ValueError(f'planner backfill file revision conflict: {persisted_target["path"]}')
            before = str(persisted_target['preimage_text'])
            preimage_exists = persisted_target['preimage_exists']
            _reject_marker_collision(before, str(authority['task_set_id']), int(authority['task_set_revision']), semantic)
        after = _append_owned_block(
            before,
            str(authority['task_set_id']),
            int(authority['task_set_revision']),
            semantic,
            sections[semantic],
        )
        relative = str(path.relative_to(context.project.project_root))
        target = canonical_plan_projection_target(
            context, path, before=before, preimage_exists=preimage_exists,
            target_text=after,
        )
        targets.append(target)
        preimages[relative] = before
        if preimage_exists:
            preimage_paths.add(relative)
        projected[relative] = after
    preimage_revision = plan_projection_revision_from_texts(
        context, plan_root, preimages, include_paths=preimage_paths
    )
    if preimage_revision != authority['expected_plan_revision']:
        raise ValueError('planner backfill transaction preimage revision conflict')
    tx = {
        'schema': TRANSACTION_SCHEMA,
        'schema_version': 2,
        'task_set_id': authority['task_set_id'],
        'task_set_revision': authority['task_set_revision'],
        'closure_intent_id': authority['closure_intent_id'],
        'planner_feedback_digest': authority['planner_feedback_digest'],
        'preimage_plan_revision': preimage_revision,
        'target_plan_revision': plan_projection_revision_from_texts(context, plan_root, projected),
        'targets': targets,
    }
    tx['transaction_digest'] = _semantic_digest(tx)
    return tx


def _validate_transaction_shape(context, tx, *, task_set) -> None:
    if set(tx) != _TRANSACTION_FIELDS or tx.get('schema') != TRANSACTION_SCHEMA or tx.get('schema_version') != 2:
        raise ValueError('planner backfill transaction schema or fields invalid')
    if tx.get('task_set_id') != task_set['task_set_id'] or tx.get('task_set_revision') != task_set['task_set_revision']:
        raise ValueError('planner backfill transaction identity mismatch')
    if tx.get('transaction_digest') != _semantic_digest(tx, omit='transaction_digest'):
        raise ValueError('planner backfill transaction digest invalid')
    targets = tx.get('targets')
    if not isinstance(targets, list) or len(targets) != 3:
        raise ValueError('planner backfill transaction targets invalid')
    plan_root = plan_projection_root(context, str(task_set['plan_slug']))
    expected_paths = [str(path.relative_to(context.project.project_root)) for path in select_plan_projection_targets(context, plan_root)]
    observed_paths = []
    for target in targets:
        if not isinstance(target, dict) or set(target) != _TARGET_FIELDS:
            raise ValueError('planner backfill transaction target fields invalid')
        path = str(target.get('path') or '')
        _canonical_relative_path(context, path, expected=None, label='planner backfill target')
        observed_paths.append(path)
        if not isinstance(target.get('preimage_exists'), bool):
            raise ValueError('planner backfill transaction target existence invalid')
        if target.get('preimage_digest') != plan_projection_text_digest(str(target.get('preimage_text') or '')) or target.get('target_digest') != plan_projection_text_digest(str(target.get('target_text') or '')):
            raise ValueError('planner backfill transaction target digest invalid')
    if observed_paths != expected_paths or len(set(observed_paths)) != 3:
        raise ValueError('planner backfill transaction target path authority invalid')
    if (
        not _DIGEST_RE.fullmatch(str(tx.get('preimage_plan_revision') or ''))
        or not _DIGEST_RE.fullmatch(str(tx.get('target_plan_revision') or ''))
    ):
        raise ValueError('planner backfill transaction plan revision invalid')


@contextmanager
def plan_projection_lock(context, plan_slug: str):
    """Serialize all Planner projections for one plan, independent of identity."""
    with file_lock(plan_projection_root(context, plan_slug) / '.planner-projection.lock'):
        yield


def canonical_plan_projection_target(context, path: Path, *, before: str, preimage_exists: bool, target_text: str) -> dict[str, object]:
    return {
        'path': str(path.relative_to(context.project.project_root)),
        'preimage_digest': plan_projection_text_digest(before),
        'preimage_exists': preimage_exists,
        'preimage_text': before,
        'target_digest': plan_projection_text_digest(target_text),
        'target_text': target_text,
    }


def apply_plan_projection_targets(context, transaction, *, write) -> None:
    """Preflight the complete target set, then atomically apply its pending writes.

    A persisted transaction permits either its preimage or exact target digest,
    which is the only recoverable state after an interrupted write sequence.
    """
    root = Path(context.project.project_root)
    plan_root = (root / str(transaction['targets'][0]['path'])).parent
    selected = select_plan_projection_targets(context, plan_root)
    expected = [str(path.relative_to(root)) for path in selected]
    observed = [str(item['path']) for item in transaction['targets']]
    if observed != expected:
        raise ValueError('planner backfill target selection changed before write')
    pending = []
    for item, path in zip(transaction['targets'], selected):
        current = path.read_text(encoding='utf-8') if path.is_file() else ''
        digest = plan_projection_text_digest(current)
        if digest == item['target_digest']:
            continue
        if digest != item['preimage_digest'] or path.is_file() is not item['preimage_exists']:
            raise ValueError(f'planner backfill file revision conflict: {item["path"]}')
        pending.append((path, item['target_text']))
    for path, target_text in pending:
        write(path, target_text)


def validate_plan_projection_targets(context, transaction) -> None:
    root = Path(context.project.project_root)
    plan_root = (root / str(transaction['targets'][0]['path'])).parent
    selected = select_plan_projection_targets(context, plan_root)
    if [str(path.relative_to(root)) for path in selected] != [item['path'] for item in transaction['targets']]:
        raise ValueError('planner backfill projected target selection drift')
    for item, path in zip(transaction['targets'], selected):
        if not path.is_file() or plan_projection_text_digest(path.read_text(encoding='utf-8')) != item['target_digest']:
            raise ValueError(f'planner backfill projected target drift: {item["path"]}')


def plan_projection_sections(proposal: PlannerBackfillProposal) -> dict[str, str]:
    refs = [*proposal.decision_refs, *proposal.open_question_refs, *proposal.evidence_refs]
    brief = '\n'.join([
        f'### Planner closure: {proposal.task_or_task_set_id}',
        proposal.brief_summary,
        f'- Result: `{proposal.result}`',
        f'- Next milestone: `{proposal.next_milestone["ref"]}`',
        *[f'- Reference: `{value}`' for value in refs],
    ])

    def transitions(title: str, values) -> str:
        lines = [title]
        for item in values:
            lines.extend((f'### {item["id"]}', f'- Status: `{item["status"]}`', str(item['summary'])))
            lines.extend(f'- Evidence: `{ref}`' for ref in item['evidence_refs'])
        return '\n'.join(lines)

    return {
        'brief': brief,
        'roadmap': transitions('## Planner closure transitions', proposal.roadmap_transitions),
        'todo': transitions('## Planner closure TODO transitions', proposal.todo_transitions),
    }


def _marker(identity: str, revision: int, semantic: str) -> tuple[str, str]:
    stem = f'ccb-planner-backfill:{identity}:r{revision}:{semantic}'
    return f'<!-- {stem}:start -->', f'<!-- {stem}:end -->'


def _reject_marker_collision(text: str, identity: str, revision: int, semantic: str) -> None:
    start, end = _marker(identity, revision, semantic)
    if start in text or end in text:
        raise ValueError('planner backfill marker collision')


def _append_owned_block(text: str, identity: str, revision: int, semantic: str, body: str) -> str:
    _reject_marker_collision(text, identity, revision, semantic)
    start, end = _marker(identity, revision, semantic)
    block = f'{start}\n{body.rstrip()}\n{end}'
    prefix = text.rstrip()
    return (prefix + '\n\n' if prefix else '') + block + '\n'


def _validate_managed_markers(
    context,
    text: str,
    *,
    path: Path,
    identity: str,
    revision: int,
    semantic: str,
    persisted,
) -> None:
    marker_pattern = re.compile(
        r'<!-- ccb-planner-backfill:([A-Za-z0-9][A-Za-z0-9_-]{0,79}):'
        r'r([1-9][0-9]*):(brief|roadmap|todo):(start|end) -->'
    )
    managed_comments = re.findall(r'<!--\s*ccb-planner-backfill:.*?-->', text, flags=re.DOTALL)
    matches = list(marker_pattern.finditer(text))
    if len(managed_comments) != len(matches):
        raise ValueError('planner backfill malformed managed marker')
    stack = None
    for match in matches:
        marker_identity, marker_revision_text, marker_semantic, boundary = match.groups()
        marker_revision = int(marker_revision_text)
        if marker_semantic != semantic or (
            marker_identity == identity and marker_revision > revision
        ):
            raise ValueError('planner backfill foreign or future managed marker')
        if boundary == 'start':
            if stack is not None:
                raise ValueError('planner backfill nested managed marker')
            stack = (match, marker_revision)
            continue
        if stack is None or stack[1] != marker_revision:
            raise ValueError('planner backfill unmatched managed marker')
        start_match = stack[0]
        block = text[start_match.start():match.end()]
        _authenticate_marker_block(
            context,
            block,
            path=path,
            identity=marker_identity,
            marker_revision=marker_revision,
            current_revision=revision,
            current_identity=identity,
            semantic=semantic,
            persisted=persisted,
        )
        stack = None
    if stack is not None:
        raise ValueError('planner backfill unmatched managed marker')


def _authenticate_marker_block(
    context,
    block,
    *,
    path,
    identity,
    marker_revision,
    current_revision,
    current_identity,
    semantic,
    persisted,
) -> None:
    relative = str(path.relative_to(context.project.project_root))
    if identity == current_identity and marker_revision == current_revision:
        transaction = persisted
        if transaction is None:
            raise ValueError('planner backfill current marker lacks transaction authority')
    else:
        task_set_root = path.parent / 'task-sets' / identity
        tx_path = task_set_root / f'planner-backfill-r{marker_revision}.transaction.json'
        backfill_path = task_set_root / f'planner-backfill-r{marker_revision}.json'
        transaction = _read_json(tx_path)
        backfill = _read_json(backfill_path)
        _validate_transaction_shape(
            context,
            transaction,
            task_set={
                'task_set_id': identity,
                'task_set_revision': marker_revision,
                'plan_slug': path.parent.name,
            },
        )
        backfill_authority = backfill.get('authority') if isinstance(backfill.get('authority'), dict) else {}
        if (
            set(backfill) != _BACKFILL_FIELDS
            or backfill.get('schema') != BACKFILL_SCHEMA
            or backfill.get('schema_version') != 2
            or backfill.get('backfill_digest') != _semantic_digest(backfill, omit='backfill_digest')
            or backfill.get('transaction_digest') != transaction.get('transaction_digest')
            or backfill_authority.get('task_set_id') != identity
            or backfill_authority.get('task_set_revision') != marker_revision
        ):
            raise ValueError('planner backfill prior marker authority invalid')
    matches = [target for target in transaction.get('targets', ()) if target.get('path') == relative]
    if len(matches) != 1:
        raise ValueError('planner backfill marker target authority invalid')
    start, end = _marker(identity, marker_revision, semantic)
    expected_matches = re.findall(re.escape(start) + r'.*?' + re.escape(end), matches[0]['target_text'], re.DOTALL)
    if expected_matches != [block]:
        raise ValueError('planner backfill marker content authority invalid')


def _authority(proposal, value) -> dict[str, object]:
    required = {
        'task_set_id', 'task_set_revision', 'closure_intent_id', 'closure_digest',
        'ordered_terminal_evidence_digest', 'expected_plan_revision', 'planner_job_id',
        'planner_source_job_id', 'planner_effective_job_id', 'planner_retry_lineage',
        'planner_feedback_digest', 'plan_slug',
    }
    legacy_required = required - {
        'planner_source_job_id', 'planner_effective_job_id', 'planner_retry_lineage',
    }
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(required), frozenset(legacy_required),
    }:
        raise ValueError('planner backfill authority fields invalid')
    result = dict(value)
    if set(value) == legacy_required:
        result['planner_source_job_id'] = result['planner_job_id']
        result['planner_effective_job_id'] = result['planner_job_id']
        result['planner_retry_lineage'] = []
    for field in ('closure_digest', 'ordered_terminal_evidence_digest', 'expected_plan_revision', 'planner_feedback_digest'):
        if not _DIGEST_RE.fullmatch(str(result[field])):
            raise ValueError(f'planner backfill {field} invalid')
    if result['planner_feedback_digest'] != planner_feedback_digest(proposal):
        raise ValueError('planner backfill proposal digest mismatch')
    if (
        result['planner_job_id'] != result['planner_effective_job_id']
        or not str(result['planner_source_job_id'] or '')
        or not str(result['planner_effective_job_id'] or '')
        or not isinstance(result['planner_retry_lineage'], list)
    ):
        raise ValueError('planner backfill Planner retry authority invalid')
    _validate_retry_lineage_authority(result)
    return result


def _validate_retry_lineage_authority(authority: dict[str, object]) -> None:
    source = str(authority['planner_source_job_id'])
    current = source
    message_id = None
    seen = {source}
    edge_fields = {
        'message_id', 'source_attempt_id', 'successor_attempt_id',
        'retry_source_job_id', 'retry_successor_job_id', 'retry_index',
    }
    for expected_index, edge in enumerate(authority['planner_retry_lineage'], start=1):
        if not isinstance(edge, dict) or set(edge) != edge_fields:
            raise ValueError('planner backfill retry lineage edge invalid')
        successor = str(edge['retry_successor_job_id'])
        if (
            edge['retry_source_job_id'] != current
            or successor in seen
            or edge['retry_index'] != expected_index
            or not str(edge['source_attempt_id'] or '')
            or not str(edge['successor_attempt_id'] or '')
        ):
            raise ValueError('planner backfill retry lineage chain invalid')
        if message_id is None:
            message_id = str(edge['message_id'] or '')
        if not message_id or edge['message_id'] != message_id:
            raise ValueError('planner backfill retry lineage message invalid')
        current = successor
        seen.add(successor)
    if current != authority['planner_effective_job_id']:
        raise ValueError('planner backfill retry lineage effective job invalid')


def _validate_source_authority(task_set, closure, proposal, authority) -> None:
    expected = {
        'task_set_id': authority['task_set_id'],
        'task_set_revision': authority['task_set_revision'],
        'ordered_terminal_evidence_digest': authority['ordered_terminal_evidence_digest'],
        'closure_digest': authority['closure_digest'],
        'aggregate_result': proposal.aggregate_result,
    }
    if {key: closure.get(key) for key in expected} != expected:
        raise ValueError('planner backfill task-set closure authority mismatch')
    if task_set.get('task_set_id') != authority['task_set_id'] or task_set.get('task_set_revision') != authority['task_set_revision']:
        raise ValueError('planner backfill task-set identity mismatch')
    if task_set.get('state') != 'closure_pending' or task_set.get('plan_slug') != authority['plan_slug']:
        raise ValueError('planner backfill task set authority mismatch')
    if (task_set.get('plan_revision') or {}).get('digest') != authority['expected_plan_revision']:
        raise ValueError('planner backfill expected plan revision mismatch')
    proposal_expected = {
        'task_or_task_set_id': authority['task_set_id'],
        'task_or_task_set_revision': authority['task_set_revision'],
        'closure_evidence_digest': authority['ordered_terminal_evidence_digest'],
        'expected_plan_revision': authority['expected_plan_revision'],
        'aggregate_result': closure['aggregate_result'],
    }
    if any(getattr(proposal, key) != expected_value for key, expected_value in proposal_expected.items()):
        raise ValueError('planner backfill proposal authority mismatch')
    closure_ref = f'docs/plantree/plans/{authority["plan_slug"]}/task-sets/{authority["task_set_id"]}/closure.json'
    if closure_ref not in proposal.evidence_refs:
        raise ValueError('planner backfill required closure evidence ref missing')


def _backfill_record(proposal, authority, transaction, tx_path, context) -> dict[str, object]:
    record = {
        'schema': BACKFILL_SCHEMA,
        'schema_version': 2,
        'authority': json.loads(json.dumps(authority, sort_keys=True)),
        'proposal': proposal.to_record(),
        'transaction_digest': transaction['transaction_digest'],
        'target_plan_revision': transaction['target_plan_revision'],
        'transaction_path': str(tx_path.relative_to(context.project.project_root)),
    }
    record = json.loads(json.dumps(record, sort_keys=True))
    record['backfill_digest'] = _semantic_digest(record)
    return record


def select_plan_projection_targets(context, plan_root) -> list[Path]:
    root = Path(context.project.project_root)
    _reject_symlink_components(root, plan_root)
    candidates = ('README.md', 'brief.md', 'roadmap.md', 'Roadmap.md', 'TODO.md', 'todo.md')
    for name in candidates:
        _reject_symlink_components(root, plan_root / name)
    roadmap = _select_variant(plan_root, 'roadmap.md', 'Roadmap.md', default='roadmap.md')
    todo = _select_variant(plan_root, 'TODO.md', 'todo.md', default='TODO.md')
    return [plan_root / 'brief.md', roadmap, todo]


def _select_variant(plan_root: Path, first: str, second: str, *, default: str) -> Path:
    try:
        child_names = {child.name for child in plan_root.iterdir()}
    except FileNotFoundError:
        child_names = set()
    existing = [plan_root / name for name in (first, second) if name in child_names]
    if len(existing) > 1:
        raise ValueError(f'planner backfill ambiguous semantic files: {first}, {second}')
    return existing[0] if existing else plan_root / default


def _reject_symlink_components(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError('planner backfill lexical path escapes project root') from exc
    current = root
    if current.is_symlink():
        raise ValueError('planner backfill project root symlink is forbidden')
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f'planner backfill symlink path is forbidden: {current}')


def _canonical_relative_path(context, value, *, expected, label) -> Path:
    raw = str(value or '')
    relative = Path(raw)
    if relative.is_absolute() or not raw or raw != relative.as_posix():
        raise ValueError(f'{label} path invalid')
    root = Path(context.project.project_root).resolve()
    resolved = (root / relative).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f'{label} path escapes project root')
    if expected is not None:
        expected_resolved = expected.resolve()
        expected_relative = expected_resolved.relative_to(root).as_posix()
        if resolved != expected_resolved or raw != expected_relative:
            raise ValueError(f'{label} path authority mismatch')
    return resolved


def _transaction_path(context, task_set) -> Path:
    return (
        Path(context.project.project_root)
        / 'docs/plantree/plans'
        / str(task_set['plan_slug'])
        / 'task-sets'
        / str(task_set['task_set_id'])
        / f'planner-backfill-r{task_set["task_set_revision"]}.transaction.json'
    )


def _persisted_target(transaction, path):
    if transaction is None:
        return None
    matches = [item for item in transaction.get('targets', ()) if item.get('path') == path]
    if len(matches) != 1:
        raise ValueError('planner backfill persisted target authority invalid')
    return matches[0]


def plan_projection_revision_from_texts(context, plan_root, projected, *, include_paths=None) -> str:
    files = []
    for path in select_plan_projection_targets(context, plan_root):
        relative = str(path.relative_to(context.project.project_root))
        projected_included = relative in projected and (
            include_paths is None or relative in include_paths
        )
        if projected_included or (relative not in projected and path.is_file()):
            text = projected[relative] if relative in projected else path.read_text(encoding='utf-8')
            files.append({'path': relative, 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()})
    return _digest({'schema': 'ccb.plan.revision.v1', 'files': files})


def _result(backfill_path, tx_path, record, *, idempotent):
    return {
        'status': 'imported',
        'idempotent': idempotent,
        'planner_backfill_path': str(backfill_path),
        'transaction_path': str(tx_path),
        'target_plan_revision': record['target_plan_revision'],
        'backfill_digest': record['backfill_digest'],
    }


def plan_projection_root(context, slug) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', str(slug or '')):
        raise ValueError('planner backfill plan_slug invalid')
    path = Path(context.project.project_root) / 'docs/plantree/plans' / str(slug)
    if not path.is_dir():
        raise ValueError('planner backfill plan root missing')
    return path


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError(f'planner backfill authority unreadable: {path}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'planner backfill authority is not object: {path}')
    return value


def plan_projection_text_digest(text):
    return 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest()


def _semantic_digest(value, *, omit=None):
    payload = {key: item for key, item in value.items() if key != omit}
    return _digest(payload)


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return 'sha256:' + hashlib.sha256(encoded).hexdigest()


__all__ = [
    'BACKFILL_SCHEMA',
    'TRANSACTION_SCHEMA',
    'apply_planner_feedback',
    'apply_plan_projection_targets',
    'canonical_plan_projection_target',
    'current_plan_revision',
    'plan_revision_authority',
    'plan_projection_lock',
    'plan_projection_revision_from_texts',
    'plan_projection_root',
    'plan_projection_sections',
    'plan_projection_text_digest',
    'select_plan_projection_targets',
    'validate_plan_projection_targets',
    'validate_planner_backfill_record',
]
