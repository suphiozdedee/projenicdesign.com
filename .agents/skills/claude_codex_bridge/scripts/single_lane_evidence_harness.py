#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable


INPUT_SCHEMA = "ccb.single_lane.evidence_input.v1"
ROW_SCHEMA = "ccb.single_lane.evidence_row.v1"
REPORT_SCHEMA = "ccb.single_lane.evidence_report.v1"
EXECUTION_MODE = "deterministic_fixture"

CASE_REQUIREMENTS: tuple[tuple[str, str, int, str, str], ...] = (
    ("one-group-pass", "success", 1, "single_unit", "pass"),
    ("two-group-parallel-pass", "success", 2, "parallel", "pass"),
    ("three-group-serial-pass", "success", 3, "serial", "pass"),
    ("four-group-mixed-dag-pass", "success", 4, "mixed_dag", "pass"),
    ("reviewer-rework-pass", "reviewer_rework", 2, "parallel", "pass"),
    ("worker-failure-partial", "worker_failure", 2, "parallel", "valid_non_success"),
    ("reviewer-failure-replan", "reviewer_failure", 2, "serial", "valid_non_success"),
    ("round-failure-blocked", "round_failure", 1, "single_unit", "valid_non_success"),
    ("unknown-submission-restart-replay", "restart_replay", 2, "parallel", "pass"),
    ("merge-conflict-replan", "merge_conflict", 2, "parallel", "valid_non_success"),
    ("root-test-failure-rollback", "root_test_failure", 2, "serial", "valid_non_success"),
    ("release-busy-bounded", "release_busy", 1, "single_unit", "valid_non_success"),
    ("release-persistent-failure", "release_failure", 1, "single_unit", "system_failure"),
    ("missing-node-evidence", "missing_evidence", 2, "parallel", "test_design_failure"),
    ("provider-fake-success", "fake_success", 1, "single_unit", "system_failure"),
    ("duplicate-ask", "duplicate_ask", 1, "single_unit", "system_failure"),
    ("duplicate-integration", "duplicate_integration", 2, "serial", "system_failure"),
    ("process-runtime-leak", "runtime_leak", 1, "single_unit", "system_failure"),
    ("artifact-digest-mismatch", "digest_mismatch", 1, "single_unit", "system_failure"),
)
REQUIRED_CASE_IDS = tuple(item[0] for item in CASE_REQUIREMENTS)
CASE_BY_ID = {item[0]: item for item in CASE_REQUIREMENTS}

INPUT_KEYS = {"schema", "campaign_id", "execution_mode", "required_case_ids", "cases"}
CASE_KEYS = {
    "case_id",
    "scenario",
    "task",
    "bundle",
    "nodes",
    "dependency_readiness",
    "integration",
    "root",
    "round",
    "topology_release",
    "runtime_residue",
    "ui_placement",
    "immaculate_freshness",
    "authority_log",
    "artifacts",
}
NESTED_KEYS = {
    "task": {"task_id", "revision", "semantic", "digest"},
    "bundle": {"revision", "semantic", "digest", "selection"},
    "selection": {"workgroup_count", "complexity", "cutability", "execution_shape", "rationale"},
    "node": {
        "node_id",
        "dependencies",
        "intent_records",
        "jobs",
        "workspace",
        "branch",
        "base_commit",
        "head_commit",
        "tree_manifest",
        "tree_digest",
        "review_result",
        "reviewed_tree_digest",
        "reviewed_commit",
        "integrated",
    },
    "dependency_readiness": {"ready_node_ids", "blocked_node_ids", "observations"},
    "integration": {
        "worktree",
        "base_commit",
        "order",
        "head_commit",
        "tree_manifest",
        "tree_digest",
        "checks",
        "conflict",
        "status",
    },
    "root": {
        "pre_manifest",
        "pre_digest",
        "post_manifest",
        "post_digest",
        "promotion",
        "rollback",
        "rollback_manifest",
        "rollback_digest",
        "checks",
    },
    "round": {
        "result",
        "task_status",
        "round_reviewer_job",
        "round_review_result",
        "script_owned_import",
        "provider_prose",
    },
    "topology_release": {
        "desired",
        "desired_digest",
        "observed",
        "observed_digest",
        "released_count",
        "retained_count",
        "drained_agents",
        "release_incomplete",
        "release_status",
    },
    "runtime_residue": {"captured_before_cleanup", "processes", "runtime_files", "bounded_retained_busy"},
    "bounded_retained_busy": {"agent_ids", "reason", "bounded", "retry_attempt", "max_retries"},
    "ui_placement": {"socket", "windows", "placements", "sidebar_agents"},
    "immaculate_freshness": {"activations", "provider_sessions"},
    "authority_log": {"events", "asks", "integrations", "legacy_files", "reported_classification"},
    "artifact": {"name", "path", "content", "sha256"},
}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PASS_JOB_STATUSES = {"completed", "passed"}
NON_SUCCESS_RESULTS = {"partial", "blocked", "replan_required"}
SYSTEM_FAILURE_PRECEDENCE = {
    "malformed_evidence",
    "authority_drift",
    "unexplained_runtime_residue",
    "cleanup_or_process_leak",
    "digest_mismatch",
    "duplicate_ask",
    "duplicate_integration",
    "fake_success",
    "release_failure",
    "inconsistent_success",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_manifest(raw_manifest: dict[str, Any], *, source_commit: str) -> dict[str, Any]:
    source_commit = _require_commit(source_commit)
    manifest = deepcopy(raw_manifest)
    manifest_issues = _manifest_issues(manifest)
    raw_cases = manifest.get("cases") if isinstance(manifest.get("cases"), list) else []
    cases_by_id: dict[str, list[Any]] = {}
    for raw_case in raw_cases:
        case_id = raw_case.get("case_id") if isinstance(raw_case, dict) else None
        if isinstance(case_id, str):
            cases_by_id.setdefault(case_id, []).append(raw_case)

    rows: list[dict[str, Any]] = []
    for case_id in REQUIRED_CASE_IDS:
        matches = cases_by_id.get(case_id, [])
        if not matches:
            rows.append(_missing_case_row(case_id, source_commit, "required case row is missing"))
            continue
        if len(matches) > 1:
            rows.append(_missing_case_row(case_id, source_commit, "duplicate case rows are malformed", system=True))
            continue
        rows.append(normalize_case(matches[0], source_commit=source_commit))

    unknown_case_ids = sorted(set(cases_by_id) - set(REQUIRED_CASE_IDS))
    if unknown_case_ids:
        manifest_issues.append(f"unknown case ids: {', '.join(unknown_case_ids)}")

    if manifest_issues:
        rows = [_with_manifest_failure(row, manifest_issues) for row in rows]

    rows_jsonl = _rows_jsonl(rows)
    counts = Counter(row["classification"] for row in rows)
    missing_case_ids = [row["case_id"] for row in rows if row["classification"] == "test_design_failure"]
    system_failure_case_ids = [row["case_id"] for row in rows if row["classification"] == "system_failure"]
    non_success_case_ids = [row["case_id"] for row in rows if row["classification"] == "valid_non_success"]
    mismatch_case_ids = [row["case_id"] for row in rows if not row["classification_matches_expected"]]
    overall = _overall_classification(
        rows,
        manifest_valid=not manifest_issues,
        mismatch_case_ids=mismatch_case_ids,
    )
    return {
        "schema": REPORT_SCHEMA,
        "record_type": "ccb_single_lane_evidence_report",
        "source_commit": source_commit,
        "campaign_id": _text_or_unknown(manifest.get("campaign_id")),
        "execution_mode": EXECUTION_MODE,
        "live_execution_claimed": False,
        "classification": overall,
        "pass": overall == "pass",
        "complete": all(row["observed"] for row in rows) and not manifest_issues,
        "summary": {
            "required_case_count": len(REQUIRED_CASE_IDS),
            "observed_case_count": sum(row["observed"] for row in rows),
            "classification_counts": {
                name: counts.get(name, 0)
                for name in ("pass", "valid_non_success", "test_design_failure", "system_failure")
            },
            "missing_or_incomplete_case_ids": missing_case_ids,
            "system_failure_case_ids": system_failure_case_ids,
            "valid_non_success_case_ids": non_success_case_ids,
            "classification_mismatch_case_ids": mismatch_case_ids,
        },
        "artifact_digests": {
            "input_sha256": sha256_value(manifest),
            "rows_jsonl_sha256": sha256_bytes(rows_jsonl.encode("utf-8")),
        },
        "rows": rows,
    }


def normalize_case(raw_case: dict[str, Any], *, source_commit: str) -> dict[str, Any]:
    case_id = raw_case.get("case_id") if isinstance(raw_case, dict) else "unknown"
    requirement = CASE_BY_ID.get(str(case_id))
    diagnostics: list[dict[str, str]] = []
    missing, malformed = _validate_case_shape(raw_case)
    if missing or malformed:
        return _invalid_case_row(
            raw_case,
            source_commit=source_commit,
            missing=missing,
            malformed=malformed,
        )
    diagnostics.extend(_diagnostics("missing_evidence", missing))
    diagnostics.extend(_diagnostics("malformed_evidence", malformed))

    task = _dict(raw_case.get("task"))
    bundle = _dict(raw_case.get("bundle"))
    nodes = raw_case.get("nodes") if isinstance(raw_case.get("nodes"), list) else []
    integration = _dict(raw_case.get("integration"))
    root = _dict(raw_case.get("root"))
    round_evidence = _dict(raw_case.get("round"))
    topology = _dict(raw_case.get("topology_release"))
    residue = _dict(raw_case.get("runtime_residue"))
    ui = _dict(raw_case.get("ui_placement"))
    freshness = _dict(raw_case.get("immaculate_freshness"))
    authority = _dict(raw_case.get("authority_log"))
    artifacts = raw_case.get("artifacts") if isinstance(raw_case.get("artifacts"), list) else []

    computed = {
        "task_digest_matches": _digest_matches(task.get("semantic"), task.get("digest")),
        "bundle_digest_matches": _digest_matches(bundle.get("semantic"), bundle.get("digest")),
        "node_tree_digests_match": all(
            _digest_matches(node.get("tree_manifest"), node.get("tree_digest"))
            for node in nodes
            if isinstance(node, dict)
        ),
        "integration_tree_digest_matches": _digest_matches(
            integration.get("tree_manifest"), integration.get("tree_digest")
        ),
        "root_digests_match": _root_digests_match(root),
        "topology_digests_match": _digest_matches(topology.get("desired"), topology.get("desired_digest"))
        and _digest_matches(topology.get("observed"), topology.get("observed_digest")),
        "artifact_digests_match": all(
            _digest_matches(artifact.get("content"), artifact.get("sha256"))
            for artifact in artifacts
            if isinstance(artifact, dict)
        ),
    }
    if not missing and any(value is False for value in computed.values()):
        diagnostics.append(_diagnostic("digest_mismatch", "one or more claimed digests do not match canonical evidence"))

    authority_checks = _authority_checks(authority, round_evidence)
    if not missing:
        diagnostics.extend(_authority_diagnostics(authority_checks))
    residue_checks = _runtime_residue_checks(residue, topology)
    if not missing:
        diagnostics.extend(_residue_diagnostics(residue_checks))
    release_checks = _release_checks(topology, nodes, residue_checks)
    if not missing:
        diagnostics.extend(_release_diagnostics(release_checks))
    semantic_checks = {"failures": []} if missing else _semantic_checks(
        requirement=requirement,
        scenario=raw_case.get("scenario"),
        bundle=bundle,
        nodes=nodes,
        dependency_readiness=_dict(raw_case.get("dependency_readiness")),
        integration=integration,
        root=root,
        round_evidence=round_evidence,
        topology=topology,
        ui=ui,
        freshness=freshness,
        authority=authority,
    )
    diagnostics.extend(_diagnostics("inconsistent_success", semantic_checks["failures"]))

    classification = _classify(
        diagnostics=diagnostics,
        round_result=round_evidence.get("result"),
        bounded_busy=residue_checks["bounded_busy_valid"],
        complete=not missing,
    )
    return {
        "schema": ROW_SCHEMA,
        "record_type": "ccb_single_lane_evidence_row",
        "source_commit": source_commit,
        "case_id": str(case_id),
        "scenario": _text_or_unknown(raw_case.get("scenario")),
        "execution_mode": EXECUTION_MODE,
        "observed": True,
        "classification": classification,
        "expected_classification": requirement[4] if requirement else "system_failure",
        "classification_matches_expected": bool(requirement and classification == requirement[4]),
        "complete": not missing and not malformed,
        "diagnostics": diagnostics,
        "task": {
            "task_id": task.get("task_id"),
            "revision": task.get("revision"),
            "digest": task.get("digest"),
            "digest_matches": computed["task_digest_matches"],
        },
        "bundle": {
            "revision": bundle.get("revision"),
            "digest": bundle.get("digest"),
            "digest_matches": computed["bundle_digest_matches"],
            "adaptive_selection": deepcopy(bundle.get("selection")),
        },
        "nodes": [_normalize_node(node) for node in nodes if isinstance(node, dict)],
        "dependency_readiness": deepcopy(raw_case.get("dependency_readiness")),
        "integration": _normalize_integration(integration, computed["integration_tree_digest_matches"]),
        "root": _normalize_root(root, computed["root_digests_match"]),
        "round": deepcopy(round_evidence),
        "topology_release": _normalize_topology(topology, computed["topology_digests_match"], release_checks),
        "runtime_residue": _normalize_residue(residue, residue_checks),
        "ui_placement": deepcopy(ui),
        "immaculate_freshness": deepcopy(freshness),
        "authority_checks": authority_checks,
        "artifact_digests": {
            "all_match": computed["artifact_digests_match"],
            "artifacts": [
                {"name": item.get("name"), "path": item.get("path"), "sha256": item.get("sha256")}
                for item in artifacts
                if isinstance(item, dict)
            ],
        },
    }


def write_outputs(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "single_lane_evidence_report.v1.json"
    rows_path = output_dir / "single_lane_evidence_rows.v1.jsonl"
    markdown_path = output_dir / "single_lane_evidence_b7.v1.md"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows_path.write_text(_rows_jsonl(report["rows"]), encoding="utf-8")
    markdown_path.write_text(markdown_b7(report), encoding="utf-8")
    return {"report_json": str(report_path), "rows_jsonl": str(rows_path), "markdown_b7": str(markdown_path)}


def markdown_b7(report: dict[str, Any]) -> str:
    lines = [
        "# Single-Lane Multi-Workgroup B7 Evidence",
        "",
        f"- Schema: `{report['schema']}`",
        f"- Source commit: `{report['source_commit']}`",
        f"- Execution mode: `{report['execution_mode']}` (no live runtime claim)",
        f"- Classification: `{report['classification']}`",
        f"- Complete: `{str(report['complete']).lower()}`",
        f"- Input SHA-256: `{report['artifact_digests']['input_sha256']}`",
        f"- Rows JSONL SHA-256: `{report['artifact_digests']['rows_jsonl_sha256']}`",
        "",
        "| Case | Groups | Shape | Result | Release | Classification | Expected | Match |",
        "| :--- | ---: | :--- | :--- | :--- | :--- | :--- | :---: |",
    ]
    for row in report["rows"]:
        selection = _dict(_dict(row.get("bundle")).get("adaptive_selection"))
        topology = _dict(row.get("topology_release"))
        lines.append(
            "| {case} | {groups} | {shape} | {result} | {release} | {classification} | {expected} | {match} |".format(
                case=_md(row["case_id"]),
                groups=selection.get("workgroup_count", "?"),
                shape=_md(selection.get("execution_shape", "unknown")),
                result=_md(_dict(row.get("round")).get("result", "unknown")),
                release=_md(topology.get("release_status", "unknown")),
                classification=row["classification"],
                expected=row["expected_classification"],
                match="yes" if row["classification_matches_expected"] else "no",
            )
        )
    failures = [row for row in report["rows"] if row["classification"] in {"system_failure", "test_design_failure"}]
    lines.extend(["", "## Failure Evidence", ""])
    if not failures:
        lines.append("None.")
    else:
        for row in failures:
            reasons = "; ".join(item["message"] for item in row["diagnostics"]) or "unspecified"
            lines.append(f"- `{row['case_id']}`: {row['classification']} - {reasons}")
    lines.extend(["", "Provider prose is retained as evidence only; every classification above is script-owned.", ""])
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize deterministic single-lane workflow evidence.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-commit", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if ".ccb" in args.output_dir.resolve().parts:
        raise SystemExit("output-dir must not be inside .ccb authority/runtime state")
    raw_manifest = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(raw_manifest, dict):
        raise SystemExit("evidence input must be a JSON object")
    source_commit = args.source_commit or _git_source_commit()
    report = normalize_manifest(raw_manifest, source_commit=source_commit)
    paths = write_outputs(report, args.output_dir)
    print(json.dumps({"classification": report["classification"], "pass": report["pass"], "paths": paths}, sort_keys=True))
    return {"pass": 0, "valid_non_success": 2, "system_failure": 3, "test_design_failure": 4}[report["classification"]]


def _manifest_issues(manifest: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if set(manifest) != INPUT_KEYS:
        issues.append(f"manifest keys must be exactly {sorted(INPUT_KEYS)}")
    if manifest.get("schema") != INPUT_SCHEMA:
        issues.append(f"manifest schema must be {INPUT_SCHEMA}")
    if manifest.get("execution_mode") != EXECUTION_MODE:
        issues.append("fixtures must declare deterministic_fixture and cannot claim live execution")
    if manifest.get("required_case_ids") != list(REQUIRED_CASE_IDS):
        issues.append("required_case_ids does not match the canonical E1 matrix")
    if not isinstance(manifest.get("campaign_id"), str) or not manifest.get("campaign_id"):
        issues.append("campaign_id must be a non-empty string")
    if not isinstance(manifest.get("cases"), list):
        issues.append("cases must be a list")
    else:
        for index, case in enumerate(manifest["cases"]):
            if not isinstance(case, dict):
                issues.append(f"cases[{index}] must be an object")
            elif not isinstance(case.get("case_id"), str) or not case.get("case_id"):
                issues.append(f"cases[{index}].case_id must be a non-empty string")
    return issues


def _validate_case_shape(raw_case: Any) -> tuple[list[str], list[str]]:
    if not isinstance(raw_case, dict):
        return [], ["case row must be an object"]
    missing = [f"case.{key}" for key in sorted(CASE_KEYS - set(raw_case))]
    malformed = [f"case has unknown field {key}" for key in sorted(set(raw_case) - CASE_KEYS)]
    for name in ("task", "bundle", "dependency_readiness", "integration", "root", "round", "topology_release", "runtime_residue", "ui_placement", "immaculate_freshness", "authority_log"):
        value = raw_case.get(name)
        if value is None:
            continue
        if not isinstance(value, dict):
            malformed.append(f"{name} must be an object")
            continue
        expected = NESTED_KEYS[name]
        missing.extend(f"{name}.{key}" for key in sorted(expected - set(value)))
        malformed.extend(f"{name} has unknown field {key}" for key in sorted(set(value) - expected))
    bundle = raw_case.get("bundle")
    if isinstance(bundle, dict) and "selection" in bundle:
        selection = bundle["selection"]
        if not isinstance(selection, dict):
            malformed.append("bundle.selection must be an object")
        else:
            missing.extend(f"bundle.selection.{key}" for key in sorted(NESTED_KEYS["selection"] - set(selection)))
            malformed.extend(
                f"bundle.selection has unknown field {key}" for key in sorted(set(selection) - NESTED_KEYS["selection"])
            )
    for index, node in enumerate(raw_case.get("nodes", []) if isinstance(raw_case.get("nodes"), list) else []):
        if not isinstance(node, dict):
            malformed.append(f"nodes[{index}] must be an object")
            continue
        missing.extend(f"nodes[{index}].{key}" for key in sorted(NESTED_KEYS["node"] - set(node)))
        malformed.extend(f"nodes[{index}] has unknown field {key}" for key in sorted(set(node) - NESTED_KEYS["node"]))
        if "dependencies" in node:
            _validate_string_list(node.get("dependencies"), f"nodes[{index}].dependencies", malformed)
        if "intent_records" in node:
            _validate_object_list(node.get("intent_records"), f"nodes[{index}].intent_records", malformed)
        if "jobs" in node:
            _validate_object_list(node.get("jobs"), f"nodes[{index}].jobs", malformed)
        for intent_index, intent in enumerate(node.get("intent_records", []) if isinstance(node.get("intent_records"), list) else []):
            if isinstance(intent, dict):
                if set(intent) != {"purpose", "attempt", "state"}:
                    malformed.append(f"nodes[{index}].intent_records[{intent_index}] fields are invalid")
                if not isinstance(intent.get("purpose"), str) or not isinstance(intent.get("state"), str):
                    malformed.append(f"nodes[{index}].intent_records[{intent_index}] purpose and state must be strings")
                if not isinstance(intent.get("attempt"), int) or isinstance(intent.get("attempt"), bool):
                    malformed.append(f"nodes[{index}].intent_records[{intent_index}].attempt must be an integer")
        for job_index, job in enumerate(node.get("jobs", []) if isinstance(node.get("jobs"), list) else []):
            if isinstance(job, dict):
                if not {"purpose", "attempt", "job_id", "status"} <= set(job) <= {
                    "purpose",
                    "attempt",
                    "job_id",
                    "status",
                    "submission_history",
                }:
                    malformed.append(f"nodes[{index}].jobs[{job_index}] fields are invalid")
                for field in ("purpose", "job_id", "status"):
                    if not isinstance(job.get(field), str):
                        malformed.append(f"nodes[{index}].jobs[{job_index}].{field} must be a string")
                if not isinstance(job.get("attempt"), int) or isinstance(job.get("attempt"), bool):
                    malformed.append(f"nodes[{index}].jobs[{job_index}].attempt must be an integer")
                if "submission_history" in job:
                    _validate_string_list(
                        job.get("submission_history"),
                        f"nodes[{index}].jobs[{job_index}].submission_history",
                        malformed,
                    )
        if not isinstance(node.get("node_id"), str) or not re.fullmatch(r"node-[0-9]{3}", node.get("node_id", "")):
            malformed.append(f"nodes[{index}].node_id must use node-NNN format")
        for field in ("workspace", "branch"):
            _validate_text(node.get(field), f"nodes[{index}].{field}", malformed)
        for field in ("base_commit", "head_commit"):
            _validate_commit(node.get(field), f"nodes[{index}].{field}", malformed)
        _validate_digest(node.get("tree_digest"), f"nodes[{index}].tree_digest", malformed)
        if node.get("review_result") not in ("pass", "failed", "not_run"):
            malformed.append(f"nodes[{index}].review_result is invalid")
        if node.get("reviewed_tree_digest") is not None:
            _validate_digest(node.get("reviewed_tree_digest"), f"nodes[{index}].reviewed_tree_digest", malformed)
        if node.get("reviewed_commit") is not None:
            _validate_commit(node.get("reviewed_commit"), f"nodes[{index}].reviewed_commit", malformed)
        if not isinstance(node.get("integrated"), bool):
            malformed.append(f"nodes[{index}].integrated must be boolean")
    for index, artifact in enumerate(raw_case.get("artifacts", []) if isinstance(raw_case.get("artifacts"), list) else []):
        if not isinstance(artifact, dict):
            malformed.append(f"artifacts[{index}] must be an object")
            continue
        missing.extend(f"artifacts[{index}].{key}" for key in sorted(NESTED_KEYS["artifact"] - set(artifact)))
        malformed.extend(
            f"artifacts[{index}] has unknown field {key}" for key in sorted(set(artifact) - NESTED_KEYS["artifact"])
        )
        _validate_text(artifact.get("name"), f"artifacts[{index}].name", malformed)
        _validate_text(artifact.get("path"), f"artifacts[{index}].path", malformed)
        _validate_digest(artifact.get("sha256"), f"artifacts[{index}].sha256", malformed)
    if not isinstance(raw_case.get("nodes"), list):
        malformed.append("nodes must be a list")
    if not isinstance(raw_case.get("artifacts"), list):
        malformed.append("artifacts must be a list")
    if not isinstance(raw_case.get("case_id"), str) or not raw_case.get("case_id"):
        malformed.append("case_id must be a non-empty string")
    if not isinstance(raw_case.get("scenario"), str) or not raw_case.get("scenario"):
        malformed.append("scenario must be a non-empty string")
    task = raw_case.get("task")
    if isinstance(task, dict):
        if not isinstance(task.get("revision"), int) or isinstance(task.get("revision"), bool) or task.get("revision", 0) < 1:
            malformed.append("task.revision must be a positive integer")
        if not isinstance(task.get("task_id"), str) or not task.get("task_id"):
            malformed.append("task.task_id must be a non-empty string")
        _validate_digest(task.get("digest"), "task.digest", malformed)
    bundle = raw_case.get("bundle")
    if isinstance(bundle, dict):
        if not isinstance(bundle.get("revision"), int) or isinstance(bundle.get("revision"), bool) or bundle.get("revision", 0) < 1:
            malformed.append("bundle.revision must be a positive integer")
        _validate_digest(bundle.get("digest"), "bundle.digest", malformed)
        selection = bundle.get("selection")
        if isinstance(selection, dict):
            count = selection.get("workgroup_count")
            if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 4:
                malformed.append("bundle.selection.workgroup_count must be an integer from one to four")
            if selection.get("execution_shape") not in ("single_unit", "parallel", "serial", "mixed_dag"):
                malformed.append("bundle.selection.execution_shape is invalid")
            if selection.get("complexity") not in ("atomic", "bounded", "complex", "very_complex"):
                malformed.append("bundle.selection.complexity is invalid")
            if selection.get("cutability") not in ("none", "limited", "high"):
                malformed.append("bundle.selection.cutability is invalid")
            _validate_text(selection.get("rationale"), "bundle.selection.rationale", malformed)
    dependency_readiness = raw_case.get("dependency_readiness")
    if isinstance(dependency_readiness, dict):
        _validate_string_list(
            dependency_readiness.get("ready_node_ids"),
            "dependency_readiness.ready_node_ids",
            malformed,
        )
        _validate_string_list(
            dependency_readiness.get("blocked_node_ids"),
            "dependency_readiness.blocked_node_ids",
            malformed,
        )
        _validate_object_list(
            dependency_readiness.get("observations"),
            "dependency_readiness.observations",
            malformed,
        )
        for index, observation in enumerate(
            dependency_readiness.get("observations", [])
            if isinstance(dependency_readiness.get("observations"), list)
            else []
        ):
            if isinstance(observation, dict):
                if set(observation) != {"node_id", "dependencies", "ready"}:
                    malformed.append(f"dependency_readiness.observations[{index}] fields are invalid")
                _validate_text(
                    observation.get("node_id"),
                    f"dependency_readiness.observations[{index}].node_id",
                    malformed,
                )
                _validate_string_list(
                    observation.get("dependencies"),
                    f"dependency_readiness.observations[{index}].dependencies",
                    malformed,
                )
                if not isinstance(observation.get("ready"), bool):
                    malformed.append(f"dependency_readiness.observations[{index}].ready must be boolean")
    integration = raw_case.get("integration")
    if isinstance(integration, dict):
        _validate_string_list(integration.get("order"), "integration.order", malformed)
        _validate_object_list(integration.get("checks"), "integration.checks", malformed)
        _validate_text(integration.get("worktree"), "integration.worktree", malformed)
        _validate_commit(integration.get("base_commit"), "integration.base_commit", malformed)
        _validate_commit(integration.get("head_commit"), "integration.head_commit", malformed)
        _validate_digest(integration.get("tree_digest"), "integration.tree_digest", malformed)
        _validate_checks(integration.get("checks"), "integration.checks", malformed)
        if not isinstance(integration.get("conflict"), bool):
            malformed.append("integration.conflict must be boolean")
        if integration.get("status") not in ("passed", "partial", "conflict", "failed"):
            malformed.append("integration.status is invalid")
    root = raw_case.get("root")
    if isinstance(root, dict):
        _validate_object_list(root.get("checks"), "root.checks", malformed)
        for field in ("pre_digest", "post_digest"):
            _validate_digest(root.get(field), f"root.{field}", malformed)
        if root.get("rollback_digest") is not None:
            _validate_digest(root.get("rollback_digest"), "root.rollback_digest", malformed)
        if root.get("promotion") not in ("promoted", "not_promoted"):
            malformed.append("root.promotion is invalid")
        if root.get("rollback") not in ("not_required", "restored"):
            malformed.append("root.rollback is invalid")
        _validate_checks(root.get("checks"), "root.checks", malformed)
    topology = raw_case.get("topology_release")
    if isinstance(topology, dict):
        for name in ("released_count", "retained_count"):
            if not isinstance(topology.get(name), int) or isinstance(topology.get(name), bool) or topology.get(name, -1) < 0:
                malformed.append(f"topology_release.{name} must be a non-negative integer")
        if not isinstance(topology.get("release_incomplete"), bool):
            malformed.append("topology_release.release_incomplete must be boolean")
        for name in ("desired", "observed"):
            topology_value = topology.get(name)
            if not isinstance(topology_value, dict):
                malformed.append(f"topology_release.{name} must be an object")
            else:
                _validate_string_list(
                    topology_value.get("agents"),
                    f"topology_release.{name}.agents",
                    malformed,
                )
        _validate_string_list(topology.get("drained_agents"), "topology_release.drained_agents", malformed)
        _validate_digest(topology.get("desired_digest"), "topology_release.desired_digest", malformed)
        _validate_digest(topology.get("observed_digest"), "topology_release.observed_digest", malformed)
        if topology.get("release_status") not in ("released", "retained_busy", "failed"):
            malformed.append("topology_release.release_status is invalid")
    round_evidence = raw_case.get("round")
    if isinstance(round_evidence, dict):
        if not isinstance(round_evidence.get("script_owned_import"), bool):
            malformed.append("round.script_owned_import must be boolean")
        if round_evidence.get("result") not in ("pass", "partial", "blocked", "replan_required"):
            malformed.append("round.result is invalid")
        if round_evidence.get("round_review_result") not in ("pass", "failed", "not_run"):
            malformed.append("round.round_review_result is invalid")
        for field in ("task_status", "round_reviewer_job", "provider_prose"):
            _validate_text(
                round_evidence.get(field),
                f"round.{field}",
                malformed,
                allow_empty=field == "provider_prose",
            )
    residue = raw_case.get("runtime_residue")
    if isinstance(residue, dict):
        _validate_object_list(residue.get("processes"), "runtime_residue.processes", malformed)
        _validate_object_list(residue.get("runtime_files"), "runtime_residue.runtime_files", malformed)
        busy = residue.get("bounded_retained_busy")
        if busy is not None:
            if not isinstance(busy, dict):
                malformed.append("runtime_residue.bounded_retained_busy must be an object or null")
            else:
                _validate_string_list(
                    busy.get("agent_ids"),
                    "runtime_residue.bounded_retained_busy.agent_ids",
                    malformed,
                )
                if busy.get("reason") != "retained_busy" or busy.get("bounded") is not True:
                    malformed.append("runtime_residue.bounded_retained_busy contract is invalid")
                for field in ("retry_attempt", "max_retries"):
                    if not isinstance(busy.get(field), int) or isinstance(busy.get(field), bool):
                        malformed.append(f"runtime_residue.bounded_retained_busy.{field} must be an integer")
        for name in ("processes", "runtime_files"):
            for index, item in enumerate(residue.get(name, []) if isinstance(residue.get(name), list) else []):
                if isinstance(item, dict):
                    if set(item) != {"kind", "agent_id", "state"}:
                        malformed.append(f"runtime_residue.{name}[{index}] fields are invalid")
                    for field in ("kind", "agent_id", "state"):
                        _validate_text(item.get(field), f"runtime_residue.{name}[{index}].{field}", malformed)
        if not isinstance(residue.get("captured_before_cleanup"), bool):
            malformed.append("runtime_residue.captured_before_cleanup must be boolean")
    node_count = len(raw_case.get("nodes", [])) if isinstance(raw_case.get("nodes"), list) else 0
    artifacts = raw_case.get("artifacts") if isinstance(raw_case.get("artifacts"), list) else []
    artifact_names = {item.get("name") for item in artifacts if isinstance(item, dict)}
    if not {"task", "bundle", "round"} <= artifact_names:
        missing.append("artifacts must include task, bundle, and round evidence")
    if isinstance(integration, dict) and not integration.get("checks"):
        missing.append("integration.checks must include command evidence")
    if isinstance(root, dict) and not root.get("checks"):
        missing.append("root.checks must include command evidence")
    ui = raw_case.get("ui_placement")
    if isinstance(ui, dict):
        if not ui.get("socket"):
            missing.append("ui_placement.socket is missing")
        elif not isinstance(ui.get("socket"), str):
            malformed.append("ui_placement.socket must be a string")
        _validate_string_list(ui.get("windows"), "ui_placement.windows", malformed)
        _validate_object_list(ui.get("placements"), "ui_placement.placements", malformed)
        _validate_string_list(ui.get("sidebar_agents"), "ui_placement.sidebar_agents", malformed)
        for index, placement in enumerate(ui.get("placements", []) if isinstance(ui.get("placements"), list) else []):
            if isinstance(placement, dict):
                if set(placement) != {"agent_id", "window", "pane"}:
                    malformed.append(f"ui_placement.placements[{index}] fields are invalid")
                if any(not isinstance(placement.get(field), str) for field in ("agent_id", "window", "pane")):
                    malformed.append(f"ui_placement.placements[{index}] fields must be strings")
        if not isinstance(ui.get("placements"), list) or len(ui.get("placements", [])) != 2 * node_count + 2:
            missing.append("ui_placement.placements must cover orchestrator, workgroups, and round reviewer")
    freshness = raw_case.get("immaculate_freshness")
    if isinstance(freshness, dict):
        expected = 2 * node_count + 2
        _validate_object_list(freshness.get("activations"), "immaculate_freshness.activations", malformed)
        _validate_object_list(
            freshness.get("provider_sessions"),
            "immaculate_freshness.provider_sessions",
            malformed,
        )
        for index, activation in enumerate(
            freshness.get("activations", []) if isinstance(freshness.get("activations"), list) else []
        ):
            if isinstance(activation, dict):
                if set(activation) != {"activation_id", "immaculate"}:
                    malformed.append(f"immaculate_freshness.activations[{index}] fields are invalid")
                if not isinstance(activation.get("activation_id"), str):
                    malformed.append(f"immaculate_freshness.activations[{index}].activation_id must be a string")
                if not isinstance(activation.get("immaculate"), bool):
                    malformed.append(f"immaculate_freshness.activations[{index}].immaculate must be boolean")
        for index, session in enumerate(
            freshness.get("provider_sessions", [])
            if isinstance(freshness.get("provider_sessions"), list)
            else []
        ):
            if isinstance(session, dict):
                if set(session) != {"session_id", "reused"}:
                    malformed.append(f"immaculate_freshness.provider_sessions[{index}] fields are invalid")
                if not isinstance(session.get("session_id"), str):
                    malformed.append(f"immaculate_freshness.provider_sessions[{index}].session_id must be a string")
                if not isinstance(session.get("reused"), bool):
                    malformed.append(f"immaculate_freshness.provider_sessions[{index}].reused must be boolean")
        if not isinstance(freshness.get("activations"), list) or len(freshness.get("activations", [])) != expected:
            missing.append("immaculate_freshness.activations must cover every dynamic role")
        if not isinstance(freshness.get("provider_sessions"), list) or len(freshness.get("provider_sessions", [])) != expected:
            missing.append("immaculate_freshness.provider_sessions must cover every dynamic role")
    authority = raw_case.get("authority_log")
    if isinstance(authority, dict):
        for name in ("events", "asks", "integrations"):
            _validate_object_list(authority.get(name), f"authority_log.{name}", malformed)
        _validate_string_list(authority.get("legacy_files"), "authority_log.legacy_files", malformed)
        for index, ask in enumerate(authority.get("asks", []) if isinstance(authority.get("asks"), list) else []):
            if isinstance(ask, dict) and not isinstance(ask.get("intent_key"), str):
                malformed.append(f"authority_log.asks[{index}].intent_key must be a string")
        for index, integration_event in enumerate(
            authority.get("integrations", []) if isinstance(authority.get("integrations"), list) else []
        ):
            if isinstance(integration_event, dict) and not isinstance(integration_event.get("node_id"), str):
                malformed.append(f"authority_log.integrations[{index}].node_id must be a string")
    missing_paths = {path.removeprefix("case.") for path in missing}
    malformed = [
        message
        for message in malformed
        if not any(message.startswith(f"{path} ") or message.startswith(f"{path}[") for path in missing_paths)
    ]
    return missing, malformed


def _validate_string_list(value: Any, path: str, malformed: list[str]) -> None:
    if not isinstance(value, list):
        malformed.append(f"{path} must be a list")
    elif any(not isinstance(item, str) for item in value):
        malformed.append(f"{path} must contain only strings")
    elif len(value) != len(set(value)):
        malformed.append(f"{path} must contain unique strings")


def _validate_object_list(value: Any, path: str, malformed: list[str]) -> None:
    if not isinstance(value, list):
        malformed.append(f"{path} must be a list")
    elif any(not isinstance(item, dict) for item in value):
        malformed.append(f"{path} must contain only objects")


def _validate_text(value: Any, path: str, malformed: list[str], *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value):
        malformed.append(f"{path} must be a {'string' if allow_empty else 'non-empty string'}")


def _validate_digest(value: Any, path: str, malformed: list[str]) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        malformed.append(f"{path} must be a lowercase SHA-256 digest")


def _validate_commit(value: Any, path: str, malformed: list[str]) -> None:
    if not isinstance(value, str) or not COMMIT_RE.fullmatch(value):
        malformed.append(f"{path} must be a full lowercase Git commit")


def _validate_checks(value: Any, path: str, malformed: list[str]) -> None:
    for index, check in enumerate(value if isinstance(value, list) else []):
        if not isinstance(check, dict):
            continue
        if set(check) != {"command", "status"}:
            malformed.append(f"{path}[{index}] fields are invalid")
        _validate_text(check.get("command"), f"{path}[{index}].command", malformed)
        if check.get("status") not in ("passed", "failed", "not_run"):
            malformed.append(f"{path}[{index}].status is invalid")


def _semantic_checks(
    *,
    requirement: tuple[str, str, int, str, str] | None,
    scenario: Any,
    bundle: dict[str, Any],
    nodes: list[Any],
    dependency_readiness: dict[str, Any],
    integration: dict[str, Any],
    root: dict[str, Any],
    round_evidence: dict[str, Any],
    topology: dict[str, Any],
    ui: dict[str, Any],
    freshness: dict[str, Any],
    authority: dict[str, Any],
) -> dict[str, list[str]]:
    failures: list[str] = []
    selection = _dict(bundle.get("selection"))
    node_ids = [node.get("node_id") for node in nodes if isinstance(node, dict)]
    if requirement:
        _, expected_scenario, expected_count, expected_shape, _expected_classification = requirement
        if scenario != expected_scenario:
            failures.append(f"fixture requires scenario {expected_scenario}")
        if selection.get("workgroup_count") != expected_count or len(nodes) != expected_count:
            failures.append(f"fixture requires {expected_count} workgroups")
        if selection.get("execution_shape") != expected_shape:
            failures.append(f"fixture requires execution shape {expected_shape}")
    if not isinstance(bundle.get("revision"), int) or bundle.get("revision", 0) < 1:
        failures.append("bundle revision must be positive")
    if len(set(node_ids)) != len(node_ids) or any(not isinstance(node_id, str) for node_id in node_ids):
        failures.append("node ids must be unique strings")
    if set(dependency_readiness.get("ready_node_ids", [])) | set(dependency_readiness.get("blocked_node_ids", [])) != set(node_ids):
        failures.append("dependency readiness must account for every node")
    if not _dependency_graph_valid(nodes):
        failures.append("node dependencies must form an acyclic graph over known nodes")
    integrated_nodes = [node.get("node_id") for node in nodes if isinstance(node, dict) and node.get("integrated")]
    if integration.get("order") != integrated_nodes:
        failures.append("integration order must match script-owned integrated nodes")
    if len(integration.get("order", [])) != len(set(integration.get("order", []))):
        failures.append("integration order contains duplicate nodes")
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("review_result") == "pass":
            if node.get("reviewed_tree_digest") != node.get("tree_digest") or node.get("reviewed_commit") != node.get("head_commit"):
                failures.append(f"reviewed identity drift for {node.get('node_id')}")
        if node.get("integrated") and node.get("review_result") != "pass":
            failures.append(f"unreviewed node integrated: {node.get('node_id')}")
    if round_evidence.get("result") == "pass":
        failures.extend(
            _pass_failures(
                nodes=nodes,
                integration=integration,
                root=root,
                round_evidence=round_evidence,
                topology=topology,
                ui=ui,
                freshness=freshness,
            )
        )
    elif round_evidence.get("result") not in NON_SUCCESS_RESULTS:
        failures.append("round result must be pass, partial, blocked, or replan_required")
    if round_evidence.get("result") != "pass" and root.get("promotion") == "promoted" and root.get("rollback") != "restored":
        failures.append("non-pass promotion must be rolled back")
    if authority.get("reported_classification") == "pass" and round_evidence.get("result") != "pass":
        failures.append("fake success contradicts script-owned round result")
    return {"failures": failures}


def _pass_failures(
    *,
    nodes: list[Any],
    integration: dict[str, Any],
    root: dict[str, Any],
    round_evidence: dict[str, Any],
    topology: dict[str, Any],
    ui: dict[str, Any],
    freshness: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            failures.append("pass requires object node evidence")
            continue
        if node.get("review_result") != "pass" or not node.get("integrated"):
            failures.append(f"pass requires reviewed and integrated node {node.get('node_id')}")
        purposes = [job.get("purpose") for job in node.get("jobs", []) if isinstance(job, dict)]
        terminal_jobs = [job for job in node.get("jobs", []) if isinstance(job, dict) and job.get("status") in PASS_JOB_STATUSES]
        if not {"worker", "reviewer"} <= set(purposes) or len(terminal_jobs) < 2:
            failures.append(f"pass requires terminal worker and reviewer jobs for {node.get('node_id')}")
    if integration.get("status") != "passed" or integration.get("conflict"):
        failures.append("pass requires conflict-free successful integration")
    if not integration.get("checks") or any(check.get("status") != "passed" for check in integration.get("checks", [])):
        failures.append("pass requires successful integration checks")
    if root.get("promotion") != "promoted" or root.get("post_digest") != integration.get("tree_digest"):
        failures.append("pass requires exact integrated root promotion")
    if not root.get("checks") or any(check.get("status") != "passed" for check in root.get("checks", [])):
        failures.append("pass requires successful project-root checks")
    if round_evidence.get("round_review_result") != "pass" or not round_evidence.get("script_owned_import"):
        failures.append("pass requires round review and script-owned result import")
    expected_agents = 2 * len(nodes) + 2
    if len(ui.get("placements", [])) != expected_agents:
        failures.append("pass requires UI placement for orchestrator, workgroups, and round reviewer")
    else:
        placement_agents = [item.get("agent_id") for item in ui.get("placements", []) if isinstance(item, dict)]
        placement_panes = [item.get("pane") for item in ui.get("placements", []) if isinstance(item, dict)]
        desired_agents = _dict(topology.get("desired")).get("agents", [])
        if set(placement_agents) != set(desired_agents) or len(set(placement_panes)) != expected_agents:
            failures.append("pass requires unique UI placement for the exact desired dynamic agents")
        if set(ui.get("sidebar_agents", [])) != set(desired_agents):
            failures.append("pass requires every desired dynamic agent in sidebar evidence")
    if not _freshness_valid(freshness, expected_agents):
        failures.append("pass requires unique immaculate activations and provider sessions")
    return failures


def _authority_checks(authority: dict[str, Any], round_evidence: dict[str, Any]) -> dict[str, bool]:
    events = authority.get("events", []) if isinstance(authority.get("events"), list) else []
    asks = authority.get("asks", []) if isinstance(authority.get("asks"), list) else []
    integrations = authority.get("integrations", []) if isinstance(authority.get("integrations"), list) else []
    accepted_provider_mutation = any(
        event.get("actor") == "provider" and event.get("accepted") and event.get("mutation") != "evidence_only"
        for event in events
        if isinstance(event, dict)
    )
    intent_keys = [item.get("intent_key") for item in asks if isinstance(item, dict)]
    integration_keys = [item.get("node_id") for item in integrations if isinstance(item, dict)]
    legacy_files = authority.get("legacy_files", []) if isinstance(authority.get("legacy_files"), list) else []
    fake_success = authority.get("reported_classification") == "pass" and round_evidence.get("result") != "pass"
    return {
        "provider_prose_is_evidence_only": not accepted_provider_mutation,
        "script_owned_result_import": round_evidence.get("script_owned_import") is True,
        "topology_dispatch_absent": not any(Path(str(path)).name == "topology_dispatch.json" for path in legacy_files),
        "duplicate_asks_absent": len(intent_keys) == len(set(intent_keys)),
        "duplicate_integrations_absent": len(integration_keys) == len(set(integration_keys)),
        "fake_success_absent": not fake_success,
    }


def _runtime_residue_checks(residue: dict[str, Any], topology: dict[str, Any]) -> dict[str, Any]:
    processes = residue.get("processes", []) if isinstance(residue.get("processes"), list) else []
    files = residue.get("runtime_files", []) if isinstance(residue.get("runtime_files"), list) else []
    active_processes = [item for item in processes if isinstance(item, dict) and item.get("state") in {"running", "busy"}]
    active_files = [item for item in files if isinstance(item, dict)]
    busy = _dict(residue.get("bounded_retained_busy"))
    busy_ids = set(busy.get("agent_ids", [])) if isinstance(busy.get("agent_ids"), list) else set()
    observed_agents = set(_dict(topology.get("observed")).get("agents", []))
    active_ids = {item.get("agent_id") for item in active_processes + active_files}
    bounded_busy_valid = bool(
        busy_ids
        and busy.get("bounded") is True
        and busy.get("reason") == "retained_busy"
        and isinstance(busy.get("retry_attempt"), int)
        and isinstance(busy.get("max_retries"), int)
        and 0 <= busy["retry_attempt"] <= busy["max_retries"]
        and active_ids == busy_ids
        and observed_agents == busy_ids
        and topology.get("retained_count") == len(busy_ids)
    )
    return {
        "captured_before_cleanup": residue.get("captured_before_cleanup") is True,
        "active_process_ids": sorted(item for item in active_ids if isinstance(item, str)),
        "active_runtime_file_count": len(active_files),
        "bounded_busy_valid": bounded_busy_valid,
        "unexplained_residue_absent": not active_ids or bounded_busy_valid,
    }


def _release_checks(topology: dict[str, Any], nodes: list[Any], residue_checks: dict[str, Any]) -> dict[str, bool]:
    desired_agents = _dict(topology.get("desired")).get("agents", [])
    observed_agents = _dict(topology.get("observed")).get("agents", [])
    expected_count = 2 * len(nodes) + 2
    bounded_busy = residue_checks["bounded_busy_valid"]
    return {
        "desired_count_matches": isinstance(desired_agents, list) and len(desired_agents) == expected_count,
        "released_count_matches": topology.get("released_count") == expected_count - topology.get("retained_count", -1),
        "drained_agents_match_released": (
            isinstance(topology.get("drained_agents"), list)
            and set(topology.get("drained_agents", [])) == set(desired_agents) - set(observed_agents)
        ),
        "observed_empty_or_bounded_busy": observed_agents == [] or bounded_busy,
        "release_complete_or_bounded_busy": (
            topology.get("release_status") == "released" and topology.get("retained_count") == 0
        )
        or (
            topology.get("release_status") == "retained_busy"
            and topology.get("release_incomplete") is True
            and bounded_busy
        ),
    }


def _classify(*, diagnostics: list[dict[str, str]], round_result: Any, bounded_busy: bool, complete: bool) -> str:
    codes = {item["code"] for item in diagnostics}
    if codes & SYSTEM_FAILURE_PRECEDENCE:
        return "system_failure"
    if "missing_evidence" in codes or not complete:
        return "test_design_failure"
    if bounded_busy or round_result in NON_SUCCESS_RESULTS:
        return "valid_non_success"
    return "pass"


def _authority_diagnostics(checks: dict[str, bool]) -> list[dict[str, str]]:
    mapping = {
        "provider_prose_is_evidence_only": ("authority_drift", "provider mutation crossed the script authority boundary"),
        "script_owned_result_import": ("authority_drift", "script-owned round result import is absent"),
        "topology_dispatch_absent": ("authority_drift", "legacy topology_dispatch.json authority is present"),
        "duplicate_asks_absent": ("duplicate_ask", "duplicate exact-once ask intent was observed"),
        "duplicate_integrations_absent": ("duplicate_integration", "a node was integrated more than once"),
        "fake_success_absent": ("fake_success", "reported pass contradicts script-owned task/round authority"),
    }
    return [_diagnostic(*mapping[name]) for name, value in checks.items() if not value]


def _residue_diagnostics(checks: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if not checks["captured_before_cleanup"]:
        result.append(_diagnostic("cleanup_or_process_leak", "runtime residue was not captured before cleanup"))
    if not checks["unexplained_residue_absent"]:
        result.append(_diagnostic("unexplained_runtime_residue", "active ccbd/tmux/provider process or runtime file remains"))
    return result


def _release_diagnostics(checks: dict[str, bool]) -> list[dict[str, str]]:
    if all(checks.values()):
        return []
    return [_diagnostic("release_failure", "release counts, observed topology, or bounded-busy evidence are inconsistent")]


def _normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": node.get("node_id"),
        "dependencies": deepcopy(node.get("dependencies")),
        "intent_records": deepcopy(node.get("intent_records")),
        "jobs": deepcopy(node.get("jobs")),
        "workspace": node.get("workspace"),
        "branch": node.get("branch"),
        "base_commit": node.get("base_commit"),
        "head_commit": node.get("head_commit"),
        "tree_digest": node.get("tree_digest"),
        "tree_digest_matches": _digest_matches(node.get("tree_manifest"), node.get("tree_digest")),
        "review_result": node.get("review_result"),
        "reviewed_tree_digest": node.get("reviewed_tree_digest"),
        "reviewed_commit": node.get("reviewed_commit"),
        "integrated": node.get("integrated"),
    }


def _normalize_integration(integration: dict[str, Any], digest_matches: bool) -> dict[str, Any]:
    return {
        "worktree": integration.get("worktree"),
        "base_commit": integration.get("base_commit"),
        "order": deepcopy(integration.get("order")),
        "head_commit": integration.get("head_commit"),
        "tree_digest": integration.get("tree_digest"),
        "tree_digest_matches": digest_matches,
        "checks": deepcopy(integration.get("checks")),
        "conflict": integration.get("conflict"),
        "status": integration.get("status"),
    }


def _normalize_root(root: dict[str, Any], digest_matches: bool) -> dict[str, Any]:
    return {
        "pre_digest": root.get("pre_digest"),
        "post_digest": root.get("post_digest"),
        "promotion": root.get("promotion"),
        "rollback": root.get("rollback"),
        "rollback_digest": root.get("rollback_digest"),
        "digests_match": digest_matches,
        "checks": deepcopy(root.get("checks")),
    }


def _normalize_topology(topology: dict[str, Any], digest_matches: bool, release_checks: dict[str, bool]) -> dict[str, Any]:
    return {
        "desired_digest": topology.get("desired_digest"),
        "observed_digest": topology.get("observed_digest"),
        "digests_match": digest_matches,
        "released_count": topology.get("released_count"),
        "retained_count": topology.get("retained_count"),
        "drained_agents": deepcopy(topology.get("drained_agents")),
        "release_incomplete": topology.get("release_incomplete"),
        "release_status": topology.get("release_status"),
        "observed_agents": deepcopy(_dict(topology.get("observed")).get("agents")),
        "checks": release_checks,
    }


def _normalize_residue(residue: dict[str, Any], checks: dict[str, Any]) -> dict[str, Any]:
    return {
        "captured_before_cleanup": residue.get("captured_before_cleanup"),
        "processes": deepcopy(residue.get("processes")),
        "runtime_files": deepcopy(residue.get("runtime_files")),
        "bounded_retained_busy": deepcopy(residue.get("bounded_retained_busy")),
        "checks": checks,
    }


def _missing_case_row(case_id: str, source_commit: str, message: str, *, system: bool = False) -> dict[str, Any]:
    requirement = CASE_BY_ID[case_id]
    return {
        "schema": ROW_SCHEMA,
        "record_type": "ccb_single_lane_evidence_row",
        "source_commit": source_commit,
        "case_id": case_id,
        "scenario": requirement[1],
        "execution_mode": EXECUTION_MODE,
        "observed": False,
        "classification": "system_failure" if system else "test_design_failure",
        "expected_classification": requirement[4],
        "classification_matches_expected": False,
        "complete": False,
        "diagnostics": [_diagnostic("malformed_evidence" if system else "missing_evidence", message)],
        "task": None,
        "bundle": None,
        "nodes": [],
        "dependency_readiness": None,
        "integration": None,
        "root": None,
        "round": None,
        "topology_release": None,
        "runtime_residue": None,
        "ui_placement": None,
        "immaculate_freshness": None,
        "authority_checks": None,
        "artifact_digests": None,
    }


def _invalid_case_row(
    raw_case: dict[str, Any],
    *,
    source_commit: str,
    missing: list[str],
    malformed: list[str],
) -> dict[str, Any]:
    case_id = str(raw_case.get("case_id") or "unknown")
    requirement = CASE_BY_ID.get(case_id)
    if requirement is None:
        return _missing_case_row(REQUIRED_CASE_IDS[0], source_commit, f"unknown case id: {case_id}", system=True)
    row = _missing_case_row(case_id, source_commit, "invalid evidence row", system=bool(malformed))
    row["observed"] = True
    row["scenario"] = _text_or_unknown(raw_case.get("scenario"))
    row["diagnostics"] = [
        *_diagnostics("missing_evidence", missing),
        *_diagnostics("malformed_evidence", malformed),
    ]
    row["classification"] = "system_failure" if malformed else "test_design_failure"
    row["classification_matches_expected"] = row["classification"] == row["expected_classification"]
    return row


def _with_manifest_failure(row: dict[str, Any], issues: list[str]) -> dict[str, Any]:
    updated = deepcopy(row)
    if updated["classification"] != "system_failure":
        updated["classification"] = "test_design_failure"
    updated["complete"] = False
    updated["diagnostics"].extend(_diagnostics("missing_evidence", issues))
    return updated


def _dependency_graph_valid(nodes: list[Any]) -> bool:
    graph = {
        node.get("node_id"): node.get("dependencies", [])
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get("node_id"), str) and isinstance(node.get("dependencies"), list)
    }
    if any(dependency not in graph or dependency == node_id for node_id, dependencies in graph.items() for dependency in dependencies):
        return False
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return False
        if node_id in visited:
            return True
        visiting.add(node_id)
        if not all(visit(dependency) for dependency in graph[node_id]):
            return False
        visiting.remove(node_id)
        visited.add(node_id)
        return True

    return len(graph) == len(nodes) and all(visit(node_id) for node_id in graph)


def _freshness_valid(freshness: dict[str, Any], expected_count: int) -> bool:
    activations = freshness.get("activations", []) if isinstance(freshness.get("activations"), list) else []
    sessions = freshness.get("provider_sessions", []) if isinstance(freshness.get("provider_sessions"), list) else []
    activation_ids = [item.get("activation_id") for item in activations if isinstance(item, dict)]
    session_ids = [item.get("session_id") for item in sessions if isinstance(item, dict)]
    return bool(
        len(activations) == expected_count
        and len(sessions) == expected_count
        and len(set(activation_ids)) == expected_count
        and len(set(session_ids)) == expected_count
        and all(item.get("immaculate") is True for item in activations if isinstance(item, dict))
        and all(item.get("reused") is False for item in sessions if isinstance(item, dict))
    )


def _root_digests_match(root: dict[str, Any]) -> bool:
    checks = [
        _digest_matches(root.get("pre_manifest"), root.get("pre_digest")),
        _digest_matches(root.get("post_manifest"), root.get("post_digest")),
    ]
    if root.get("rollback") == "restored":
        checks.append(_digest_matches(root.get("rollback_manifest"), root.get("rollback_digest")))
        checks.append(root.get("rollback_digest") == root.get("pre_digest"))
    return all(checks)


def _digest_matches(value: Any, claimed: Any) -> bool:
    return isinstance(claimed, str) and bool(SHA256_RE.fullmatch(claimed)) and sha256_value(value) == claimed


def _diagnostic(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _diagnostics(code: str, messages: Iterable[str]) -> list[dict[str, str]]:
    return [_diagnostic(code, message) for message in messages]


def _overall_classification(
    rows: list[dict[str, Any]],
    *,
    manifest_valid: bool,
    mismatch_case_ids: list[str],
) -> str:
    if not manifest_valid:
        return "test_design_failure"
    if any(not row["observed"] and row["classification"] == "test_design_failure" for row in rows):
        return "test_design_failure"
    if mismatch_case_ids:
        return "system_failure"
    if any(not row["observed"] for row in rows):
        return "system_failure"
    return "pass"


def _rows_jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(canonical_json(row) + "\n" for row in rows)


def _require_commit(value: str) -> str:
    if not COMMIT_RE.fullmatch(value):
        raise ValueError("source_commit must be a full lowercase 40-character Git SHA")
    return value


def _git_source_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    return _require_commit(completed.stdout.strip())


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text_or_unknown(value: Any) -> str:
    return value if isinstance(value, str) and value else "unknown"


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
