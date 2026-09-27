from __future__ import annotations

import hashlib
import json


def create_node_commit_intent(
    *,
    node_id: str,
    base_commit: str,
    reviewed_tree_digest: str,
    review_input_digest: str,
    reviewer_job_id: str,
    actor: dict[str, str],
    prepared_state_revision: int,
    message_prefix: str,
    prepared_at: str,
) -> dict[str, object]:
    authority = {
        'kind': 'node_commit',
        'node_id': node_id,
        'base_commit': base_commit,
        'reviewed_tree_digest': reviewed_tree_digest,
        'review_input_digest': review_input_digest,
        'reviewer_job_id': reviewer_job_id,
        'actor': actor,
        'prepared_state_revision': prepared_state_revision,
    }
    intent_digest = digest(authority)
    message = f'{message_prefix}\nCCB-Commit-Intent: {intent_digest}'
    return {
        **authority,
        'schema': 'ccb.loop.node_commit_intent.v1',
        'intent_digest': intent_digest,
        'message': message,
        'message_digest': digest(message),
        'status': 'prepared',
        'prepared_at': prepared_at,
    }


def node_commit_intent_matches(
    intent: dict[str, object],
    *,
    node_id: str,
    base_commit: str,
    reviewed_tree_digest: str,
    review_input_digest: str,
    reviewer_job_id: str,
    actor: dict[str, str],
    message_prefix: str,
) -> bool:
    authority = {
        'kind': 'node_commit',
        'node_id': node_id,
        'base_commit': base_commit,
        'reviewed_tree_digest': reviewed_tree_digest,
        'review_input_digest': review_input_digest,
        'reviewer_job_id': reviewer_job_id,
        'actor': actor,
        'prepared_state_revision': intent.get('prepared_state_revision'),
    }
    intent_digest = digest(authority)
    message = f'{message_prefix}\nCCB-Commit-Intent: {intent_digest}'
    return bool(
        intent.get('schema') == 'ccb.loop.node_commit_intent.v1'
        and intent.get('status') in {'prepared', 'completed'}
        and intent.get('intent_digest') == intent_digest
        and intent.get('message') == message
        and intent.get('message_digest') == digest(message)
        and intent.get('actor') == actor
        and isinstance(intent.get('prepared_state_revision'), int)
    )


def create_merge_intent(
    *,
    node_id: str,
    head_before: str,
    reviewed_commit: str,
    reviewed_tree_digest: str,
    actor: dict[str, str],
    prepared_state_revision: int,
    prepared_at: str,
) -> dict[str, object]:
    authority = {
        'kind': 'integration_merge',
        'node_id': node_id,
        'head_before': head_before,
        'reviewed_commit': reviewed_commit,
        'reviewed_tree_digest': reviewed_tree_digest,
        'actor': actor,
        'prepared_state_revision': prepared_state_revision,
    }
    intent_digest = digest(authority)
    message = f'CCB integrate {node_id}\n\nCCB-Merge-Intent: {intent_digest}'
    return {
        **authority,
        'schema': 'ccb.loop.integration_merge_intent.v1',
        'intent_digest': intent_digest,
        'message': message,
        'message_digest': digest(message),
        'status': 'prepared',
        'prepared_at': prepared_at,
    }


def merge_intent_matches(
    intent: dict[str, object],
    *,
    node_id: str,
    head_before: str,
    reviewed_commit: str,
    reviewed_tree_digest: str,
    actor: dict[str, str],
) -> bool:
    authority = {
        'kind': 'integration_merge',
        'node_id': node_id,
        'head_before': head_before,
        'reviewed_commit': reviewed_commit,
        'reviewed_tree_digest': reviewed_tree_digest,
        'actor': actor,
        'prepared_state_revision': intent.get('prepared_state_revision'),
    }
    intent_digest = digest(authority)
    message = f'CCB integrate {node_id}\n\nCCB-Merge-Intent: {intent_digest}'
    return bool(
        intent.get('schema') == 'ccb.loop.integration_merge_intent.v1'
        and intent.get('status') == 'prepared'
        and intent.get('intent_digest') == intent_digest
        and intent.get('message') == message
        and intent.get('message_digest') == digest(message)
        and intent.get('actor') == actor
        and isinstance(intent.get('prepared_state_revision'), int)
    )


def digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return f'sha256:{hashlib.sha256(encoded).hexdigest()}'


__all__ = [
    'create_merge_intent',
    'create_node_commit_intent',
    'digest',
    'merge_intent_matches',
    'node_commit_intent_matches',
]
