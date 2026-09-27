#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


REPORT_SCHEMA = 'ccb.g5.source_fake_runtime_report.v1'
CAMPAIGN_SCHEMA = 'ccb.g5.source_fake_runtime_campaign.v1'
ROW_SCHEMA = 'ccb.g5.source_fake_runtime_evidence_row.v1'
REQUIRED_SCENARIOS = (
    'pass',
    'reviewer_rework_pass',
    'reviewer_rework_exhausted_blocked',
    'worker_failure_partial',
    'all_workers_failed_blocked',
    'reviewer_provider_failure',
    'round_reviewer_blocked',
    'integration_verification_failure',
    'root_verification_failure',
    'restart_replay_pass',
)
SCENARIO_OUTCOMES = {
    'pass': ('pass', 'done', 'pass', 'round_reviewer_reply'),
    'reviewer_rework_pass': ('pass', 'done', 'pass', 'round_reviewer_reply'),
    'reviewer_rework_exhausted_blocked': (
        'valid_non_success', 'blocked', 'blocked', 'required_node_failure'
    ),
    'worker_failure_partial': ('valid_non_success', 'partial', 'partial', 'required_node_failure'),
    'all_workers_failed_blocked': ('valid_non_success', 'blocked', 'blocked', 'required_node_failure'),
    'reviewer_provider_failure': ('valid_non_success', 'partial', 'partial', 'required_node_failure'),
    'round_reviewer_blocked': ('valid_non_success', 'blocked', 'blocked', 'round_reviewer_reply'),
    'integration_verification_failure': (
        'valid_non_success', 'replan_required', 'replan_required', 'integration_verification_failed'
    ),
    'root_verification_failure': (
        'valid_non_success', 'replan_required', 'replan_required', 'root_verification_failed'
    ),
    'restart_replay_pass': ('pass', 'done', 'pass', 'round_reviewer_reply'),
}
SCENARIO_MATRIX = {
    'pass': (4, 'mixed_dag'),
    'reviewer_rework_pass': (1, 'parallel'),
    'reviewer_rework_exhausted_blocked': (1, 'parallel'),
    'worker_failure_partial': (2, 'parallel'),
    'all_workers_failed_blocked': (4, 'parallel'),
    'reviewer_provider_failure': (2, 'parallel'),
    'round_reviewer_blocked': (4, 'mixed_dag'),
    'integration_verification_failure': (4, 'mixed_dag'),
    'root_verification_failure': (4, 'mixed_dag'),
    'restart_replay_pass': (2, 'parallel'),
}
REPORT_KEYS = {
    'schema', 'status', 'execution_mode', 'coverage', 'project_root', 'project_id',
    'role_store', 'provider', 'scenario', 'expected', 'observed', 'config_version',
    'matrix', 'task_id', 'loop_id', 'bundle', 'jobs', 'integration', 'task', 'round',
    'release', 'root_changes', 'runner_results', 'execution', 'checks', 'paths',
    'raw_evidence_sha256', 'command_log', 'external_cleanup', 'post_cleanup',
}
OUTCOME_KEYS = {'classification', 'task_status', 'round_result', 'round_source'}
PATH_KEYS = {'report', 'bundle', 'scheduler_state', 'round', 'integration_state', 'raw_observed'}
RAW_DIGEST_KEYS = {'bundle', 'scheduler_state', 'round', 'integration_state', 'raw_observed'}
POST_CLEANUP_KEYS = {'owned_processes', 'socket_entries', 'connectable_sockets', 'child_worktrees'}


class CampaignFailure(RuntimeError):
    pass


def aggregate_reports(
    *,
    report_paths: list[Path],
    output_dir: Path,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve(strict=False)
    if '.ccb' in output_dir.parts:
        raise CampaignFailure('campaign output must be outside project .ccb roots')
    if not report_paths:
        raise CampaignFailure('at least one explicit --report is required')
    rows = []
    seen: set[str] = set()
    for raw_path in report_paths:
        path = raw_path.expanduser().resolve(strict=True)
        before = _sha256_file(path)
        payload = _read_json(path)
        row = _validate_report(path, payload, report_sha256=before)
        scenario = str(row['scenario'])
        if scenario in seen:
            raise CampaignFailure(f'duplicate scenario report: {scenario}')
        seen.add(scenario)
        if _sha256_file(path) != before:
            raise CampaignFailure(f'raw report mutated during aggregation: {path}')
        rows.append(row)
    missing = sorted(set(REQUIRED_SCENARIOS) - seen)
    extra = sorted(seen - set(REQUIRED_SCENARIOS))
    if missing or extra:
        raise CampaignFailure(f'scenario set mismatch: missing={missing}, extra={extra}')
    rows.sort(key=lambda item: str(item['scenario']))
    campaign = {
        'schema': CAMPAIGN_SCHEMA,
        'status': 'pass',
        'execution_mode': 'source_fake_runtime',
        'provider': 'fake',
        'coverage': {
            'real_provider': False,
            'live_provider': False,
            'disclaimer': 'Source/fake runtime campaign only; no live or real provider coverage.',
        },
        'required_scenarios': sorted(REQUIRED_SCENARIOS),
        'row_count': len(rows),
        'rows': rows,
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(output_dir / 'campaign.json', campaign)
    (output_dir / 'evidence_rows.jsonl').write_text(
        ''.join(json.dumps(row, sort_keys=True, ensure_ascii=False) + '\n' for row in rows),
        encoding='utf-8',
    )
    (output_dir / 'B7.md').write_text(_render_b7(campaign), encoding='utf-8')
    return campaign


def _validate_report(
    path: Path,
    payload: dict[str, Any],
    *,
    report_sha256: str,
) -> dict[str, Any]:
    _require_exact_keys(payload, REPORT_KEYS, subject='report')
    if payload.get('schema') != REPORT_SCHEMA:
        raise CampaignFailure(f'unsupported report schema: {path}')
    if payload.get('status') != 'pass':
        raise CampaignFailure(f'report evidence checks did not pass: {path}')
    if payload.get('execution_mode') != 'source_fake_runtime' or payload.get('provider') != 'fake':
        raise CampaignFailure(f'report mode/provider mismatch: {path}')
    coverage = _mapping(payload.get('coverage'))
    _require_exact_keys(
        coverage,
        {'provider', 'real_provider', 'live_provider', 'disclaimer'},
        subject='report.coverage',
    )
    if coverage.get('real_provider') is not False or coverage.get('live_provider') is not False:
        raise CampaignFailure(f'report coverage disclaimer missing: {path}')
    scenario = str(payload.get('scenario') or '')
    if scenario not in REQUIRED_SCENARIOS:
        raise CampaignFailure(f'unknown scenario: {scenario!r}')
    expected = _mapping(payload.get('expected'))
    observed = _mapping(payload.get('observed'))
    matrix = _mapping(payload.get('matrix'))
    _require_exact_keys(expected, OUTCOME_KEYS, subject='report.expected')
    _require_exact_keys(observed, OUTCOME_KEYS, subject='report.observed')
    expected_contract = dict(
        zip(
            ('classification', 'task_status', 'round_result', 'round_source'),
            SCENARIO_OUTCOMES[scenario],
        )
    )
    if expected != expected_contract:
        raise CampaignFailure(f'{scenario} expected outcome contract mismatch')
    expected_count, expected_shape = SCENARIO_MATRIX[scenario]
    if matrix != {'requested_count': expected_count, 'requested_shape': expected_shape}:
        raise CampaignFailure(f'{scenario} campaign matrix contract mismatch')
    node_count = _mapping(payload.get('bundle')).get('node_count')
    if node_count != expected_count:
        raise CampaignFailure(f'{scenario} bundle node count does not match campaign matrix')
    for expected_key, observed_key in (
        ('classification', 'classification'),
        ('task_status', 'task_status'),
        ('round_result', 'round_result'),
        ('round_source', 'round_source'),
    ):
        if expected.get(expected_key) != observed.get(observed_key):
            raise CampaignFailure(
                f'{scenario} expected/observed mismatch for {expected_key}: '
                f'{expected.get(expected_key)!r} != {observed.get(observed_key)!r}'
            )
    checks = _mapping(payload.get('checks'))
    failed = sorted(key for key, value in checks.items() if value is not True)
    if failed:
        raise CampaignFailure(f'{scenario} report checks failed: {failed}')
    raw_digests = _mapping(payload.get('raw_evidence_sha256'))
    evidence_paths = _mapping(payload.get('paths'))
    _require_exact_keys(raw_digests, RAW_DIGEST_KEYS, subject='report.raw_evidence_sha256')
    _require_exact_keys(evidence_paths, PATH_KEYS, subject='report.paths')
    path_keys = {
        'bundle': 'bundle',
        'scheduler_state': 'scheduler_state',
        'round': 'round',
        'integration_state': 'integration_state',
        'raw_observed': 'raw_observed',
    }
    for digest_key, path_key in path_keys.items():
        evidence_path = Path(str(evidence_paths.get(path_key) or ''))
        digest = str(raw_digests.get(digest_key) or '')
        if not evidence_path.is_file() or not digest or _sha256_file(evidence_path) != digest:
            raise CampaignFailure(f'{scenario} raw evidence digest mismatch: {digest_key}')
    project_root = Path(str(payload.get('project_root') or '')).resolve(strict=True)
    post_cleanup = _mapping(payload.get('post_cleanup'))
    _require_exact_keys(post_cleanup, POST_CLEANUP_KEYS, subject='report.post_cleanup')
    if any(
        post_cleanup.get(key)
        for key in ('owned_processes', 'socket_entries', 'connectable_sockets', 'child_worktrees')
    ):
        raise CampaignFailure(f'{scenario} cleanup residue is non-empty')
    return {
        'schema': ROW_SCHEMA,
        'scenario': scenario,
        'classification': observed.get('classification'),
        'project_root': str(project_root),
        'task_id': payload.get('task_id'),
        'loop_id': payload.get('loop_id'),
        'task_status': observed.get('task_status'),
        'round_result': observed.get('round_result'),
        'round_source': observed.get('round_source'),
        'node_count': node_count,
        'requested_shape': matrix.get('requested_shape'),
        'report_path': str(path),
        'report_sha256': report_sha256,
        'raw_evidence_sha256': raw_digests,
        'release': payload.get('release'),
        'post_cleanup': post_cleanup,
    }


def _render_b7(campaign: dict[str, Any]) -> str:
    lines = [
        '# G5 Source/Fake Runtime Campaign B7',
        '',
        'Status: pass',
        '',
        'Coverage: source/fake runtime only; no live or real provider coverage.',
        '',
        '| Scenario | Classification | Task | Round | Source | Report SHA256 |',
        '|---|---|---|---|---|---|',
    ]
    for row in campaign['rows']:
        lines.append(
            f"| {row['scenario']} | {row['classification']} | {row['task_status']} | "
            f"{row['round_result']} | {row['round_source']} | {row['report_sha256']} |"
        )
    return '\n'.join(lines) + '\n'


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _require_exact_keys(value: dict[str, Any], expected: set[str], *, subject: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise CampaignFailure(f'{subject} key mismatch: missing={missing}, extra={extra}')


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignFailure(f'invalid JSON report {path}: {exc}') from exc
    if not isinstance(payload, dict):
        raise CampaignFailure(f'report must be a JSON object: {path}')
    return payload


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Aggregate explicit G5 source/fake runtime reports.')
    parser.add_argument('--report', action='append', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--json', action='store_true')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    try:
        campaign = aggregate_reports(
            report_paths=[Path(value) for value in args.report],
            output_dir=Path(args.output_dir),
        )
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, sort_keys=True))
        return 1
    if args.json:
        print(json.dumps(campaign, sort_keys=True, indent=2))
    else:
        print(f"campaign_status: {campaign['status']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
