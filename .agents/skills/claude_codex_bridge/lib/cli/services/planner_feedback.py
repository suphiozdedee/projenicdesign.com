from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any


_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
_DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
_BACKFILL_SCHEMA = 'ccb.planner.backfill_proposal.v1'
_FRONTDESK_SCHEMA = 'ccb.planner.frontdesk_status.v1'
_MODES = frozenset({'detailer_replan', 'task_set_closure'})
_AGGREGATE_RESULTS = frozenset({'pass', 'partial', 'replan_required', 'blocked'})
_SEMANTIC_RESULTS = frozenset(
    {'closure_complete', 'closure_partial', 'task_set_replanned', 'closure_blocked'}
)
_SEMANTIC_BY_AGGREGATE = {
    'pass': 'closure_complete',
    'partial': 'closure_partial',
    'replan_required': 'task_set_replanned',
    'blocked': 'closure_blocked',
}
_MILESTONE_KINDS = frozenset({'selected', 'workflow_terminal', 'blocked_none'})
_TRANSITION_KEYS = frozenset({'id', 'status', 'summary', 'evidence_refs'})
_MILESTONE_KEYS = frozenset({'kind', 'ref', 'rationale'})
_FRONTDESK_KEYS = frozenset(
    {
        'schema',
        'notification_identity',
        'aggregate_result',
        'accepted_scope',
        'unresolved_scope',
        'blockers',
        'next_milestone',
        'evidence_refs',
        'user_report_body',
    }
)


class PlannerFeedbackError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlannerBackfillProposal:
    schema: str
    mode: str
    expected_plan_revision: str
    task_or_task_set_id: str
    task_or_task_set_revision: int
    closure_evidence_digest: str
    aggregate_result: str
    result: str
    brief_summary: str
    roadmap_transitions: tuple[dict[str, object], ...]
    todo_transitions: tuple[dict[str, object], ...]
    decision_refs: tuple[str, ...]
    open_question_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    accepted_scope: tuple[str, ...]
    unresolved_scope: tuple[str, ...]
    blockers: tuple[str, ...]
    replan_inputs: tuple[str, ...]
    next_milestone: dict[str, str]
    frontdesk_notification_required: bool
    frontdesk_status: dict[str, object]

    def to_record(self) -> dict[str, object]:
        record = asdict(self)
        record['roadmap_transitions'] = [dict(item) for item in self.roadmap_transitions]
        record['todo_transitions'] = [dict(item) for item in self.todo_transitions]
        record['next_milestone'] = dict(self.next_milestone)
        record['frontdesk_status'] = dict(self.frontdesk_status)
        return record


def parse_planner_feedback_reply(reply: str) -> PlannerBackfillProposal:
    payload = _json_section(reply, 'planner-backfill.json')
    required = {
        'schema',
        'mode',
        'expected_plan_revision',
        'task_or_task_set_id',
        'task_or_task_set_revision',
        'closure_evidence_digest',
        'aggregate_result',
        'result',
        'brief_summary',
        'roadmap_transitions',
        'todo_transitions',
        'decision_refs',
        'open_question_refs',
        'evidence_refs',
        'accepted_scope',
        'unresolved_scope',
        'blockers',
        'replan_inputs',
        'next_milestone',
        'frontdesk_notification_required',
        'frontdesk_status',
    }
    _exact_fields(payload, required, label='planner-backfill.json')
    schema = _text(payload['schema'], label='schema')
    if schema != _BACKFILL_SCHEMA:
        raise PlannerFeedbackError('planner_backfill_schema_invalid', f'expected schema {_BACKFILL_SCHEMA}')
    mode = _enum(payload['mode'], _MODES, label='mode')
    expected_plan_revision = _digest(payload['expected_plan_revision'], label='expected_plan_revision')
    identity = _identifier(payload['task_or_task_set_id'], label='task_or_task_set_id')
    identity_revision = _positive_int(payload['task_or_task_set_revision'], label='task_or_task_set_revision')
    closure_digest = _digest(payload['closure_evidence_digest'], label='closure_evidence_digest')
    aggregate_result = _enum(payload['aggregate_result'], _AGGREGATE_RESULTS, label='aggregate_result')
    result = _enum(payload['result'], _SEMANTIC_RESULTS, label='result')
    expected_result = _SEMANTIC_BY_AGGREGATE[aggregate_result]
    if result != expected_result:
        raise PlannerFeedbackError(
            'planner_backfill_result_laundering',
            f'Planner result {result!r} cannot represent aggregate result {aggregate_result!r}',
        )
    brief_summary = _text(payload['brief_summary'], label='brief_summary')
    roadmap = _transitions(payload['roadmap_transitions'], label='roadmap_transitions')
    todos = _transitions(payload['todo_transitions'], label='todo_transitions')
    decision_refs = _text_list(payload['decision_refs'], label='decision_refs')
    open_question_refs = _text_list(payload['open_question_refs'], label='open_question_refs')
    evidence_refs = _text_list(payload['evidence_refs'], label='evidence_refs')
    accepted_scope = _text_list(payload['accepted_scope'], label='accepted_scope')
    unresolved_scope = _text_list(payload['unresolved_scope'], label='unresolved_scope')
    blockers = _text_list(payload['blockers'], label='blockers')
    replan_inputs = _text_list(payload['replan_inputs'], label='replan_inputs')
    next_milestone = _next_milestone(payload['next_milestone'])
    notification_required = payload['frontdesk_notification_required']
    if not isinstance(notification_required, bool):
        raise PlannerFeedbackError(
            'planner_backfill_notification_flag_invalid',
            'frontdesk_notification_required must be boolean',
        )
    frontdesk_status = _frontdesk_status(payload['frontdesk_status'])
    _validate_semantic_scope(
        aggregate_result=aggregate_result,
        unresolved_scope=unresolved_scope,
        blockers=blockers,
        replan_inputs=replan_inputs,
    )
    _validate_frontdesk_alignment(
        frontdesk_status,
        aggregate_result=aggregate_result,
        accepted_scope=accepted_scope,
        unresolved_scope=unresolved_scope,
        blockers=blockers,
        next_milestone=next_milestone,
        evidence_refs=evidence_refs,
    )
    return PlannerBackfillProposal(
        schema=schema,
        mode=mode,
        expected_plan_revision=expected_plan_revision,
        task_or_task_set_id=identity,
        task_or_task_set_revision=identity_revision,
        closure_evidence_digest=closure_digest,
        aggregate_result=aggregate_result,
        result=result,
        brief_summary=brief_summary,
        roadmap_transitions=roadmap,
        todo_transitions=todos,
        decision_refs=decision_refs,
        open_question_refs=open_question_refs,
        evidence_refs=evidence_refs,
        accepted_scope=accepted_scope,
        unresolved_scope=unresolved_scope,
        blockers=blockers,
        replan_inputs=replan_inputs,
        next_milestone=next_milestone,
        frontdesk_notification_required=notification_required,
        frontdesk_status=frontdesk_status,
    )


def validate_planner_feedback_authority(
    proposal: PlannerBackfillProposal,
    *,
    mode: str,
    expected_plan_revision: str,
    task_or_task_set_id: str,
    task_or_task_set_revision: int,
    closure_evidence_digest: str,
    aggregate_result: str,
    evidence_refs: tuple[str, ...] | list[str],
) -> None:
    expected = {
        'mode': mode,
        'expected_plan_revision': expected_plan_revision,
        'task_or_task_set_id': task_or_task_set_id,
        'task_or_task_set_revision': task_or_task_set_revision,
        'closure_evidence_digest': closure_evidence_digest,
        'aggregate_result': aggregate_result,
    }
    actual = {
        'mode': proposal.mode,
        'expected_plan_revision': proposal.expected_plan_revision,
        'task_or_task_set_id': proposal.task_or_task_set_id,
        'task_or_task_set_revision': proposal.task_or_task_set_revision,
        'closure_evidence_digest': proposal.closure_evidence_digest,
        'aggregate_result': proposal.aggregate_result,
    }
    if actual != expected:
        raise PlannerFeedbackError(
            'planner_backfill_authority_mismatch',
            f'planner backfill authority differs; expected={expected}, actual={actual}',
        )
    required_refs = tuple(dict.fromkeys(_text(value, label='evidence_ref') for value in evidence_refs))
    missing = [value for value in required_refs if value not in proposal.evidence_refs]
    if missing:
        raise PlannerFeedbackError(
            'planner_backfill_evidence_refs_missing',
            f'planner backfill omits required evidence refs: {missing}',
        )


def planner_feedback_digest(proposal: PlannerBackfillProposal) -> str:
    payload = json.dumps(
        proposal.to_record(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return f'sha256:{hashlib.sha256(payload).hexdigest()}'


def frontdesk_status_envelope(proposal: PlannerBackfillProposal) -> dict[str, object]:
    envelope = dict(proposal.frontdesk_status)
    envelope['planner_feedback_digest'] = planner_feedback_digest(proposal)
    return envelope


def _json_section(reply: str, label: str) -> dict[str, object]:
    pattern = re.compile(
        rf'(?s)\A\s*\*\*{re.escape(label)}\*\*\s*\n```json\s*\n'
        rf'((?:(?!\n```).)*)\n```\s*\Z'
    )
    matches = pattern.findall(str(reply or ''))
    if len(matches) != 1:
        raise PlannerFeedbackError(
            'planner_backfill_section_invalid',
            f'expected exactly one fenced {label} section; found {len(matches)}',
        )
    try:
        payload = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise PlannerFeedbackError('planner_backfill_invalid_json', str(exc)) from exc
    if not isinstance(payload, dict):
        raise PlannerFeedbackError('planner_backfill_not_object', f'{label} must be an object')
    return payload


def _frontdesk_status(value: Any) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PlannerFeedbackError('frontdesk_status_invalid', 'frontdesk_status must be an object')
    _exact_fields(value, _FRONTDESK_KEYS, label='frontdesk_status')
    schema = _text(value['schema'], label='frontdesk_status.schema')
    if schema != _FRONTDESK_SCHEMA:
        raise PlannerFeedbackError('frontdesk_status_schema_invalid', f'expected schema {_FRONTDESK_SCHEMA}')
    return {
        'schema': schema,
        'notification_identity': _identifier(
            value['notification_identity'], label='frontdesk_status.notification_identity'
        ),
        'aggregate_result': _enum(
            value['aggregate_result'], _AGGREGATE_RESULTS, label='frontdesk_status.aggregate_result'
        ),
        'accepted_scope': list(_text_list(value['accepted_scope'], label='frontdesk_status.accepted_scope')),
        'unresolved_scope': list(
            _text_list(value['unresolved_scope'], label='frontdesk_status.unresolved_scope')
        ),
        'blockers': list(_text_list(value['blockers'], label='frontdesk_status.blockers')),
        'next_milestone': _next_milestone(value['next_milestone']),
        'evidence_refs': list(_text_list(value['evidence_refs'], label='frontdesk_status.evidence_refs')),
        'user_report_body': _text(value['user_report_body'], label='frontdesk_status.user_report_body'),
    }


def _validate_semantic_scope(
    *,
    aggregate_result: str,
    unresolved_scope: tuple[str, ...],
    blockers: tuple[str, ...],
    replan_inputs: tuple[str, ...],
) -> None:
    if aggregate_result == 'pass' and (unresolved_scope or blockers or replan_inputs):
        raise PlannerFeedbackError(
            'planner_backfill_pass_has_unresolved_scope',
            'pass cannot contain unresolved scope, blockers, or replan inputs',
        )
    if aggregate_result != 'pass' and not unresolved_scope:
        raise PlannerFeedbackError(
            'planner_backfill_unresolved_scope_missing',
            f'{aggregate_result} requires non-empty unresolved_scope',
        )
    if aggregate_result == 'blocked' and not blockers:
        raise PlannerFeedbackError('planner_backfill_blockers_missing', 'blocked requires blockers')
    if aggregate_result == 'replan_required' and not replan_inputs:
        raise PlannerFeedbackError('planner_backfill_replan_inputs_missing', 'replan_required requires replan_inputs')


def _validate_frontdesk_alignment(
    status: dict[str, object],
    *,
    aggregate_result: str,
    accepted_scope: tuple[str, ...],
    unresolved_scope: tuple[str, ...],
    blockers: tuple[str, ...],
    next_milestone: dict[str, str],
    evidence_refs: tuple[str, ...],
) -> None:
    expected = {
        'aggregate_result': aggregate_result,
        'accepted_scope': list(accepted_scope),
        'unresolved_scope': list(unresolved_scope),
        'blockers': list(blockers),
        'next_milestone': next_milestone,
        'evidence_refs': list(evidence_refs),
    }
    actual = {key: status[key] for key in expected}
    if actual != expected:
        raise PlannerFeedbackError(
            'frontdesk_status_authority_mismatch',
            f'Frontdesk status differs from Planner proposal; expected={expected}, actual={actual}',
        )


def _next_milestone(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise PlannerFeedbackError('planner_backfill_next_milestone_invalid', 'next_milestone must be an object')
    _exact_fields(value, _MILESTONE_KEYS, label='next_milestone')
    return {
        'kind': _enum(value['kind'], _MILESTONE_KINDS, label='next_milestone.kind'),
        'ref': _text(value['ref'], label='next_milestone.ref'),
        'rationale': _text(value['rationale'], label='next_milestone.rationale'),
    }


def _transitions(value: Any, *, label: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise PlannerFeedbackError('planner_backfill_transitions_invalid', f'{label} must be a list')
    result: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise PlannerFeedbackError('planner_backfill_transitions_invalid', f'{label}[{index}] must be an object')
        _exact_fields(item, _TRANSITION_KEYS, label=f'{label}[{index}]')
        result.append(
            {
                'id': _identifier(item['id'], label=f'{label}[{index}].id'),
                'status': _text(item['status'], label=f'{label}[{index}].status'),
                'summary': _text(item['summary'], label=f'{label}[{index}].summary'),
                'evidence_refs': list(
                    _text_list(item['evidence_refs'], label=f'{label}[{index}].evidence_refs')
                ),
            }
        )
    return tuple(result)


def _exact_fields(value: dict[str, object], expected: frozenset[str] | set[str], *, label: str) -> None:
    if set(value) != set(expected):
        missing = sorted(set(expected) - set(value))
        unknown = sorted(set(value) - set(expected))
        raise PlannerFeedbackError(
            'planner_backfill_fields_invalid',
            f'{label} fields differ; missing={missing}, unknown={unknown}',
        )


def _text_list(value: Any, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PlannerFeedbackError('planner_backfill_list_invalid', f'{label} must be a list')
    result = tuple(_text(item, label=f'{label} item') for item in value)
    if len(set(result)) != len(result):
        raise PlannerFeedbackError('planner_backfill_list_invalid', f'{label} contains duplicates')
    return result


def _positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PlannerFeedbackError('planner_backfill_revision_invalid', f'{label} must be a positive integer')
    return value


def _identifier(value: Any, *, label: str) -> str:
    text = _text(value, label=label)
    if not _ID_RE.fullmatch(text):
        raise PlannerFeedbackError('planner_backfill_id_invalid', f'{label} is invalid')
    return text


def _digest(value: Any, *, label: str) -> str:
    text = _text(value, label=label).lower()
    if not _DIGEST_RE.fullmatch(text):
        raise PlannerFeedbackError('planner_backfill_digest_invalid', f'{label} must be a sha256 digest')
    return text


def _enum(value: Any, allowed: frozenset[str], *, label: str) -> str:
    text = _text(value, label=label).lower()
    if text not in allowed:
        raise PlannerFeedbackError('planner_backfill_enum_invalid', f'{label} must be one of {sorted(allowed)}')
    return text


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlannerFeedbackError('planner_backfill_text_invalid', f'{label} must be non-empty text')
    return value.strip()


__all__ = [
    'PlannerBackfillProposal',
    'PlannerFeedbackError',
    'frontdesk_status_envelope',
    'parse_planner_feedback_reply',
    'planner_feedback_digest',
    'validate_planner_feedback_authority',
]
