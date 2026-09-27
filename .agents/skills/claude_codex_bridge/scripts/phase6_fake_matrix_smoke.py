#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_CLOSURE_SMOKE = REPO_ROOT / "scripts" / "workflow_closure_smoke.py"
HISTORY_DIR = REPO_ROOT / "docs" / "plantree" / "plans" / "agentic-loop-workflow" / "history"
DEFAULT_TEST_ROOT = Path(os.environ.get("CCB_PHASE6_MATRIX_TEST_ROOT", "/home/bfly/yunwei/test_ccb2"))
SOURCE_WRAPPER_ROOT = Path("/home/bfly/yunwei/test_ccb2")
DEFAULT_CCB_TEST = REPO_ROOT / "ccb_test"
DIRECT_CASE_ID = "smoke-direct-execution-pass"
ROUTE_SMOKE_CASE_IDS = (
    "smoke-needs-detail-pass",
    "smoke-macro-adjustment",
    "smoke-blocked",
)
ROUTE_SMOKE_RUNNER = "workflow_closure_smoke.run_phase6_route_smoke"
EXECUTION_SMOKE_CASE_IDS = (
    "smoke-partial-completion",
    "smoke-reviewer-reject-rework",
    "smoke-reviewer-cannot-accept",
)
EXECUTION_SMOKE_RUNNER = "workflow_closure_smoke.run_phase6_execution_case_smoke"
BUSY_RELEASE_CASE_ID = "smoke-busy-release"
BUSY_RELEASE_RUNNER = "phase6_fake_matrix_smoke.run_busy_release_smoke"
PASS_CLEANUP_STATUSES = {"released", "ok"}
VALID_NON_SUCCESS_CLEANUP_STATUSES = {"released", "retained_busy", "ok"}
BOUNDED_RELEASE_INCOMPLETE_REASON_MARKERS = (
    "active",
    "ask",
    "busy",
    "drain",
    "hidden",
    "inherited",
    "park",
    "provider",
    "release_policy",
    "resident",
    "retained",
    "runtime_state=busy",
)
CLASSIFICATIONS = {
    "pass",
    "valid_non_success",
    "system_failure",
    "role_failure",
    "provider_failure",
    "test_design_failure",
}

REQUIRED_CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": DIRECT_CASE_ID,
        "task_id": DIRECT_CASE_ID,
        "expected_route": "direct_execution",
        "expected_classification": "pass",
        "expected_round_results": ("pass",),
        "expected_final_statuses": ("done",),
        "expected_cleanup_statuses": ("released",),
        "runner": "workflow_closure_smoke.run_workflow_smoke",
    },
    {
        "case_id": "smoke-needs-detail-pass",
        "task_id": "smoke-needs-detail-pass",
        "expected_route": "needs_detail",
        "expected_classification": "pass",
        "expected_round_results": ("pass",),
        "expected_final_statuses": ("done",),
        "expected_cleanup_statuses": ("released",),
        "runner": ROUTE_SMOKE_RUNNER,
    },
    {
        "case_id": "smoke-macro-adjustment",
        "task_id": "smoke-macro-adjustment",
        "expected_route": "macro_adjustment_request",
        "expected_classification": "valid_non_success",
        "expected_round_results": ("replan_required",),
        "expected_final_statuses": ("replan_required",),
        "expected_cleanup_statuses": ("released",),
        "runner": ROUTE_SMOKE_RUNNER,
    },
    {
        "case_id": "smoke-blocked",
        "task_id": "smoke-blocked",
        "expected_route": "blocked",
        "expected_classification": "valid_non_success",
        "expected_round_results": ("blocked",),
        "expected_final_statuses": ("blocked",),
        "expected_cleanup_statuses": ("released",),
        "runner": ROUTE_SMOKE_RUNNER,
    },
    {
        "case_id": "smoke-partial-completion",
        "task_id": "smoke-partial-completion",
        "expected_route": "partial_completion",
        "expected_classification": "valid_non_success",
        "expected_round_results": ("partial",),
        "expected_final_statuses": ("partial",),
        "expected_cleanup_statuses": ("released",),
        "runner": EXECUTION_SMOKE_RUNNER,
    },
    {
        "case_id": "smoke-reviewer-reject-rework",
        "task_id": "smoke-reviewer-reject-rework",
        "expected_route": "direct_execution",
        "expected_classification": "pass",
        "expected_round_results": ("pass",),
        "expected_final_statuses": ("done",),
        "expected_cleanup_statuses": ("released",),
        "runner": EXECUTION_SMOKE_RUNNER,
    },
    {
        "case_id": "smoke-reviewer-cannot-accept",
        "task_id": "smoke-reviewer-cannot-accept",
        "expected_route": "direct_execution",
        "expected_classification": "valid_non_success",
        "expected_round_results": ("replan_required",),
        "expected_final_statuses": ("replan_required",),
        "expected_cleanup_statuses": ("released",),
        "runner": EXECUTION_SMOKE_RUNNER,
    },
    {
        "case_id": BUSY_RELEASE_CASE_ID,
        "task_id": BUSY_RELEASE_CASE_ID,
        "expected_route": "direct_execution",
        "expected_classification": "valid_non_success",
        "expected_round_results": ("busy",),
        "expected_final_statuses": ("running",),
        "expected_cleanup_statuses": ("retained_busy",),
        "runner": BUSY_RELEASE_RUNNER,
    },
)


def case_manifest() -> list[dict[str, Any]]:
    return [
        {
            "case_id": case["case_id"],
            "task_id": case["task_id"],
            "expected_route": case["expected_route"],
            "expected_classification": case["expected_classification"],
            "expected_round_results": list(case["expected_round_results"]),
            "expected_final_statuses": list(case["expected_final_statuses"]),
            "expected_cleanup_statuses": list(case["expected_cleanup_statuses"]),
            "runner": case["runner"],
            "implemented": bool(case["runner"]),
        }
        for case in REQUIRED_CASES
    ]


def build_matrix_report(
    *,
    direct_evidence: dict[str, Any] | None = None,
    direct_smoke_payload: dict[str, Any] | None = None,
    route_smoke_payloads: dict[str, dict[str, Any]] | None = None,
    case_evidence: dict[str, dict[str, Any]] | None = None,
    selected_case_ids: list[str] | None = None,
) -> dict[str, Any]:
    if direct_evidence is not None and direct_smoke_payload is not None:
        raise ValueError("provide either direct_evidence or direct_smoke_payload, not both")
    if (direct_evidence is not None or direct_smoke_payload is not None) and case_evidence and DIRECT_CASE_ID in case_evidence:
        raise ValueError("provide direct_execution evidence through one input path only")
    overlap = set(route_smoke_payloads or ()) & set(case_evidence or ())
    if overlap:
        raise ValueError(f"provide route evidence through one input path only: {', '.join(sorted(overlap))}")

    observed_rows: dict[str, dict[str, Any]] = {}
    for case_id, evidence in (case_evidence or {}).items():
        observed_rows[case_id] = _row_from_evidence(case_id, evidence)
    for case_id, payload in (route_smoke_payloads or {}).items():
        observed_rows[case_id] = _route_row_from_workflow_smoke(case_id, payload)
    if direct_smoke_payload is not None:
        observed_rows[DIRECT_CASE_ID] = _direct_row_from_workflow_smoke(direct_smoke_payload)
    elif direct_evidence is not None:
        observed_rows[DIRECT_CASE_ID] = _direct_row_from_evidence(direct_evidence)

    selected = set(selected_case_ids or ())
    unknown = selected - {str(case["case_id"]) for case in REQUIRED_CASES}
    if unknown:
        raise ValueError(f"unknown selected case ids: {', '.join(sorted(unknown))}")
    cases = [case for case in REQUIRED_CASES if not selected or case["case_id"] in selected]
    rows = [observed_rows.get(case["case_id"]) or _not_observed_row(case) for case in cases]
    manifest = [item for item in case_manifest() if not selected or item["case_id"] in selected]
    return _matrix_report(rows, manifest=manifest)


def run_direct_execution_smoke(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    _require_source_wrapper_test_root(test_root)
    module = _load_workflow_closure_smoke()
    return module.run_workflow_smoke(
        test_root=test_root,
        project_name=project_name,
        provider=provider,
        ccb_test=ccb_test,
        timeout_s=timeout_s,
        reset=reset,
        keep_running=keep_running,
    )


def run_phase6_route_smoke(
    *,
    case_id: str,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    if case_id not in ROUTE_SMOKE_CASE_IDS:
        raise ValueError(f"unsupported route smoke case: {case_id}")
    _require_source_wrapper_test_root(test_root)
    module = _load_workflow_closure_smoke()
    return module.run_phase6_route_smoke(
        test_root=test_root,
        project_name=project_name,
        case_id=case_id,
        provider=provider,
        ccb_test=ccb_test,
        timeout_s=timeout_s,
        reset=reset,
        keep_running=keep_running,
    )


def run_phase6_execution_case_smoke(
    *,
    case_id: str,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    if case_id not in EXECUTION_SMOKE_CASE_IDS:
        raise ValueError(f"unsupported execution smoke case: {case_id}")
    _require_source_wrapper_test_root(test_root)
    module = _load_workflow_closure_smoke()
    return module.run_phase6_execution_case_smoke(
        test_root=test_root,
        project_name=project_name,
        case_id=case_id,
        provider=provider,
        ccb_test=ccb_test,
        timeout_s=timeout_s,
        reset=reset,
        keep_running=keep_running,
    )


def run_busy_release_smoke(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    timeout_s: int,
    reset: bool,
    keep_running: bool,
    busy_latency_ms: int = 3000,
) -> dict[str, Any]:
    _require_source_wrapper_test_root(test_root)
    module = _load_workflow_closure_smoke()
    prepared = module.prepare_project(
        test_root=test_root,
        project_name=project_name,
        provider=provider,
        ccb_test=ccb_test,
        reset=reset,
    )
    project_root = Path(prepared["project_root"])
    role_store = Path(prepared["role_store"])
    env = module._smoke_env(test_root=test_root, project_root=project_root, role_store=role_store)
    results: list[dict[str, Any]] = []
    loop_id = "p6busy"
    worker_agent = f"loop-{loop_id}-coder-1"
    reviewer_agent = f"loop-{loop_id}-code_reviewer-1"
    proposal_path = _write_busy_release_topology_proposal(
        project_root=project_root,
        loop_id=loop_id,
        worker_agent=worker_agent,
        reviewer_agent=reviewer_agent,
    )
    artifacts = module.write_artifacts(
        project_root=project_root,
        task_id=BUSY_RELEASE_CASE_ID,
        route="direct_execution",
        scenario="busy_release",
    )
    try:
        _append_workflow_command(module, results, "diagnose", [str(ccb_test), "--diagnose"], cwd=test_root, env=env)
        _append_workflow_command(
            module,
            results,
            "config_validate",
            [str(ccb_test), "--project", str(project_root), "config", "validate"],
            cwd=test_root,
            env=env,
        )
        _append_workflow_command(module, results, "start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env)
        _append_workflow_command(
            module,
            results,
            "task_create",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "plan",
                "task-create",
                "--plan",
                module.DEFAULT_PLAN,
                "--title",
                f"Workflow smoke task {BUSY_RELEASE_CASE_ID}",
                "--task-id",
                BUSY_RELEASE_CASE_ID,
                "--json",
            ],
            cwd=test_root,
            env=env,
        )
        for kind in ("task_packet", "execution_contract"):
            _append_workflow_command(
                module,
                results,
                f"artifact_{kind}",
                [
                    str(ccb_test),
                    "--project",
                    str(project_root),
                    "plan",
                    "task-artifact",
                    "--task",
                    BUSY_RELEASE_CASE_ID,
                    "--kind",
                    kind,
                    "--file",
                    artifacts[kind],
                    "--json",
                ],
                cwd=test_root,
                env=env,
            )
        _append_workflow_command(
            module,
            results,
            "route_direct_execution",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "plan",
                "task-artifact",
                "--task",
                BUSY_RELEASE_CASE_ID,
                "--kind",
                "orchestration_notes",
                "--file",
                artifacts["orchestration_notes"],
                "--route",
                "direct_execution",
                "--json",
            ],
            cwd=test_root,
            env=env,
        )
        _append_workflow_command(
            module,
            results,
            "ready_for_orchestration",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "plan",
                "task-status",
                "--task",
                BUSY_RELEASE_CASE_ID,
                "--status",
                "ready_for_orchestration",
                "--next-owner",
                "orchestrator",
                "--activation-reason",
                "phase6_busy_release_smoke",
                "--json",
            ],
            cwd=test_root,
            env=env,
        )
        _append_workflow_command(
            module,
            results,
            "script_bind_loop",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "plan",
                "task-bind-loop",
                "--task",
                BUSY_RELEASE_CASE_ID,
                "--loop",
                loop_id,
                "--json",
            ],
            cwd=test_root,
            env=env,
        )
        _append_workflow_command(
            module,
            results,
            "topology_propose",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "loop",
                "topology",
                "propose",
                "--loop-id",
                loop_id,
                "--from",
                str(proposal_path),
                "--proposal-id",
                "busy-release",
                "--json",
            ],
            cwd=test_root,
            env=env,
        )
        _append_workflow_command(
            module,
            results,
            "topology_commit",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "loop",
                "topology",
                "commit",
                "--loop-id",
                loop_id,
                "--proposal",
                "busy-release",
                "--apply",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=timeout_s + 60,
        )
        _append_workflow_command(
            module,
            results,
            "busy_worker_ask",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "ask",
                "--task-id",
                f"fake;latency_ms={max(1000, int(busy_latency_ms))}",
                worker_agent,
                "phase6",
                "busy",
                "release",
                "smoke",
            ],
            cwd=test_root,
            env=env,
        )
        _append_workflow_command(
            module,
            results,
            "topology_release_busy",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "loop",
                "topology",
                "release",
                "--loop-id",
                loop_id,
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=timeout_s + 60,
        )
        _append_workflow_command(
            module,
            results,
            "task_show_final",
            [str(ccb_test), "--project", str(project_root), "plan", "task-show", "--task", BUSY_RELEASE_CASE_ID, "--json"],
            cwd=test_root,
            env=env,
        )
        _append_workflow_command(
            module,
            results,
            "post_retained_ps",
            [str(ccb_test), "--project", str(project_root), "ps"],
            cwd=test_root,
            env=env,
        )
        job_id = _accepted_job_id(_result(results, "busy_worker_ask"))
        if job_id:
            watch_env = dict(env)
            watch_env["CCB_WATCH_TIMEOUT_S"] = str(max(10, int(timeout_s), int(busy_latency_ms / 1000) + 10))
            watch_env.setdefault("CCB_WATCH_POLL_INTERVAL_S", "0.1")
            _append_workflow_command(
                module,
                results,
                f"watch_{job_id}",
                [str(ccb_test), "--project", str(project_root), "pend", "--watch", job_id],
                cwd=test_root,
                env=watch_env,
                timeout=max(15, int(timeout_s), int(busy_latency_ms / 1000) + 15),
            )
        _append_workflow_command(
            module,
            results,
            "topology_release_idle",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "loop",
                "topology",
                "release",
                "--loop-id",
                loop_id,
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=timeout_s + 60,
        )
        _append_workflow_command(
            module,
            results,
            "post_idle_ps",
            [str(ccb_test), "--project", str(project_root), "ps"],
            cwd=test_root,
            env=env,
        )
    finally:
        if not keep_running:
            _append_workflow_command(
                module,
                results,
                "kill",
                [str(ccb_test), "--project", str(project_root), "kill", "-f"],
                cwd=test_root,
                env=env,
                allow_failure=True,
            )
    return _busy_release_evidence(
        results,
        project_root=project_root,
        loop_id=loop_id,
        worker_agent=worker_agent,
        reviewer_agent=reviewer_agent,
        proposal_source_path=proposal_path,
        artifact_paths=artifacts,
    )


def load_direct_evidence(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"direct evidence must be a JSON object: {path}")
    return payload


def write_matrix_report(
    report: dict[str, Any],
    output_dir: Path,
    *,
    markdown_report_path: Path | None = None,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "phase6_fake_matrix_report.json"
    rows_path = output_dir / "phase6_fake_matrix_rows.jsonl"
    paths = {
        "report_json": str(report_path),
        "rows_jsonl": str(rows_path),
    }
    if markdown_report_path is not None:
        paths["markdown_report"] = str(markdown_report_path)
    report["report_paths"] = paths
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in report["rows"]),
        encoding="utf-8",
    )
    if markdown_report_path is not None:
        write_history_report(report, markdown_report_path)
    return paths


def write_history_report(report: dict[str, Any], path: Path | None = None) -> str:
    target = path or default_history_report_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_markdown_report(report), encoding="utf-8")
    return str(target)


def default_history_report_path() -> Path:
    return HISTORY_DIR / f"phase6-real-capability-assessment-{time.strftime('%Y%m%d')}.md"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the Phase 6 fake-provider matrix evidence report.")
    parser.add_argument("--test-root", default=str(DEFAULT_TEST_ROOT))
    parser.add_argument("--project-name", default=f"phase6-fake-matrix-{int(time.time())}")
    parser.add_argument("--provider", default="fake")
    parser.add_argument("--ccb-test", default=str(DEFAULT_CCB_TEST))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-direct-execution", action="store_true")
    parser.add_argument("--run-needs-detail", action="store_true")
    parser.add_argument("--run-macro-adjustment", action="store_true")
    parser.add_argument("--run-blocked", action="store_true")
    parser.add_argument("--run-route-tranche", action="store_true")
    parser.add_argument("--run-partial-completion", action="store_true")
    parser.add_argument("--run-reviewer-reject-rework", action="store_true")
    parser.add_argument("--run-reviewer-cannot-accept", action="store_true")
    parser.add_argument("--run-execution-tranche", action="store_true")
    parser.add_argument("--run-busy-release", action="store_true")
    parser.add_argument("--busy-latency-ms", type=int, default=3000)
    parser.add_argument("--direct-evidence")
    parser.add_argument("--output-dir")
    parser.add_argument("--history-report-path")
    parser.add_argument("--write-history-report", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    if (args.run or args.run_direct_execution) and args.direct_evidence:
        raise SystemExit("--run/--run-direct-execution and --direct-evidence are mutually exclusive")

    direct_payload: dict[str, Any] | None = None
    direct_evidence: dict[str, Any] | None = None
    route_payloads: dict[str, dict[str, Any]] = {}
    case_evidence: dict[str, dict[str, Any]] = {}
    if args.run or args.run_direct_execution:
        direct_payload = run_direct_execution_smoke(
            test_root=Path(args.test_root),
            project_name=str(args.project_name),
            provider=str(args.provider),
            ccb_test=Path(args.ccb_test),
            timeout_s=int(args.timeout),
            reset=bool(args.reset),
            keep_running=bool(args.keep_running),
        )
    elif args.direct_evidence:
        direct_evidence = load_direct_evidence(Path(args.direct_evidence))

    route_case_ids = _selected_route_case_ids(args)
    for case_id in route_case_ids:
        route_payloads[case_id] = run_phase6_route_smoke(
            case_id=case_id,
            test_root=Path(args.test_root),
            project_name=_case_project_name(str(args.project_name), case_id),
            provider=str(args.provider),
            ccb_test=Path(args.ccb_test),
            timeout_s=int(args.timeout),
            reset=bool(args.reset),
            keep_running=bool(args.keep_running),
        )
    execution_case_ids = _selected_execution_case_ids(args)
    for case_id in execution_case_ids:
        route_payloads[case_id] = run_phase6_execution_case_smoke(
            case_id=case_id,
            test_root=Path(args.test_root),
            project_name=_case_project_name(str(args.project_name), case_id),
            provider=str(args.provider),
            ccb_test=Path(args.ccb_test),
            timeout_s=int(args.timeout),
            reset=bool(args.reset),
            keep_running=bool(args.keep_running),
        )
    if bool(args.run or args.run_busy_release):
        case_evidence[BUSY_RELEASE_CASE_ID] = run_busy_release_smoke(
            test_root=Path(args.test_root),
            project_name=_case_project_name(str(args.project_name), BUSY_RELEASE_CASE_ID),
            provider=str(args.provider),
            ccb_test=Path(args.ccb_test),
            timeout_s=int(args.timeout),
            reset=bool(args.reset),
            keep_running=bool(args.keep_running),
            busy_latency_ms=int(args.busy_latency_ms),
        )

    report = build_matrix_report(
        direct_evidence=direct_evidence,
        direct_smoke_payload=direct_payload,
        route_smoke_payloads=route_payloads or None,
        case_evidence=case_evidence or None,
        selected_case_ids=(
            [BUSY_RELEASE_CASE_ID]
            if args.run_busy_release
            and not args.run
            and not args.run_direct_execution
            and not args.run_route_tranche
            and not args.run_execution_tranche
            and not any(
                (
                    args.run_needs_detail,
                    args.run_macro_adjustment,
                    args.run_blocked,
                    args.run_partial_completion,
                    args.run_reviewer_reject_rework,
                    args.run_reviewer_cannot_accept,
                )
            )
            else None
        ),
    )
    history_report_path = (
        Path(args.history_report_path)
        if args.history_report_path
        else default_history_report_path()
        if args.write_history_report
        else None
    )
    if args.output_dir:
        write_matrix_report(report, Path(args.output_dir), markdown_report_path=history_report_path)
    elif history_report_path is not None:
        report.setdefault("report_paths", {})["markdown_report"] = str(history_report_path)
        write_history_report(report, history_report_path)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"phase6_fake_matrix_status: {report['phase6_fake_matrix_status']}")
        print(f"phase6a_pass: {str(report['phase6a_pass']).lower()}")
        print(f"observed_cases: {report['summary']['observed_case_count']}/{report['summary']['required_case_count']}")
        if report["summary"]["missing_case_ids"]:
            print("missing_cases: " + ", ".join(report["summary"]["missing_case_ids"]))
    return 0 if report["phase6_fake_matrix_status"] == "pass" else 1


def _selected_route_case_ids(args: argparse.Namespace) -> list[str]:
    selected: list[str] = []
    if bool(args.run or args.run_route_tranche):
        selected.extend(ROUTE_SMOKE_CASE_IDS)
    for flag, case_id in (
        ("run_needs_detail", "smoke-needs-detail-pass"),
        ("run_macro_adjustment", "smoke-macro-adjustment"),
        ("run_blocked", "smoke-blocked"),
    ):
        if bool(getattr(args, flag)):
            selected.append(case_id)
    return list(dict.fromkeys(selected))


def _selected_execution_case_ids(args: argparse.Namespace) -> list[str]:
    selected: list[str] = []
    if bool(args.run or args.run_execution_tranche):
        selected.extend(EXECUTION_SMOKE_CASE_IDS)
    for flag, case_id in (
        ("run_partial_completion", "smoke-partial-completion"),
        ("run_reviewer_reject_rework", "smoke-reviewer-reject-rework"),
        ("run_reviewer_cannot_accept", "smoke-reviewer-cannot-accept"),
    ):
        if bool(getattr(args, flag)):
            selected.append(case_id)
    return list(dict.fromkeys(selected))


def _case_project_name(base: str, case_id: str) -> str:
    if case_id == DIRECT_CASE_ID:
        return base
    return f"{base}-{case_id}"


def _matrix_report(
    rows: list[dict[str, Any]],
    *,
    manifest: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    missing_case_ids = [row["case_id"] for row in rows if row["case_status"] != "observed"]
    not_implemented_case_ids = [row["case_id"] for row in rows if not row["implemented"]]
    classification_counts = {name: 0 for name in sorted(CLASSIFICATIONS)}
    for row in rows:
        classification_counts[str(row["classification"])] += 1
    hard_failures = [
        row["case_id"]
        for row in rows
        if row["classification"] in {"system_failure", "role_failure", "provider_failure"}
    ]
    status = "incomplete" if missing_case_ids else "failed" if hard_failures else "pass"
    return {
        "phase6_fake_matrix_status": status,
        "phase6a_pass": status == "pass",
        "manifest": manifest if manifest is not None else case_manifest(),
        "rows": rows,
        "summary": {
            "required_case_count": len(rows),
            "observed_case_count": len(rows) - len(missing_case_ids),
            "implemented_case_count": sum(1 for row in rows if row["implemented"]),
            "missing_case_ids": missing_case_ids,
            "not_implemented_case_ids": not_implemented_case_ids,
            "hard_failure_case_ids": hard_failures,
            "classification_counts": classification_counts,
        },
    }


def _not_observed_row(case: dict[str, Any]) -> dict[str, Any]:
    implemented = bool(case["runner"])
    return _base_row(
        case,
        implemented=implemented,
        case_status="missing_evidence" if implemented else "not_implemented",
        observed_route=None,
        route_decision_correct=False,
        round_result="not_run",
        final_status="not_run",
        cleanup_status="not_run",
        classification="test_design_failure",
        artifact_paths={},
        runtime_paths={
            "workflow_closure_smoke": str(WORKFLOW_CLOSURE_SMOKE),
        }
        if implemented
        else {},
        authority_checks={
            "topology_dispatch_absent": None,
            "communication_edges_absent": None,
            "provider_reply_authority_parsing_absent": None,
        },
        ask_reachability=None,
        runtime_residue=_runtime_residue(None),
        notes=(
            "runner is wired but this report was built without source-wrapper evidence"
            if implemented
            else "case manifest is present but no Phase 6A runner is implemented yet"
        ),
    )


def _direct_row_from_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    if isinstance(evidence.get("run"), dict):
        return _direct_row_from_workflow_smoke(evidence["run"])
    if "workflow_smoke_status" in evidence or "summary" in evidence:
        return _direct_row_from_workflow_smoke(evidence)

    return _row_from_evidence(DIRECT_CASE_ID, evidence)


def _row_from_evidence(case_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    expected_case_id = _optional_str(evidence.get("case_id"))
    if expected_case_id and expected_case_id != case_id:
        raise ValueError(f"evidence case_id must be {case_id!r}: {expected_case_id!r}")
    case = _case_with_task_id(case_id, _optional_str(evidence.get("task_id")))
    observed_route = _optional_str(evidence.get("observed_route"))
    round_result = _str_or_unknown(evidence.get("round_result"))
    final_status = _str_or_unknown(evidence.get("final_status"))
    cleanup_status = _str_or_unknown(evidence.get("cleanup_result") or evidence.get("cleanup_status"))
    route_decision_correct = observed_route == case["expected_route"]
    artifact_paths = _dict_or_empty(evidence.get("artifact_paths"))
    runtime_paths = _dict_or_empty(evidence.get("runtime_paths"))
    row = _base_row(
        case,
        implemented=True,
        case_status="observed",
        observed_route=observed_route,
        route_decision_correct=route_decision_correct,
        round_result=round_result,
        final_status=final_status,
        cleanup_status=cleanup_status,
        classification="test_design_failure",
        artifact_paths=artifact_paths,
        runtime_paths=runtime_paths,
        authority_checks=_authority_checks_for_paths(
            evidence.get("authority_checks"),
            artifact_paths=artifact_paths,
            runtime_paths=runtime_paths,
            fallback_root=None,
        ),
        ask_reachability=_optional_bool(evidence.get("ask_reachability")),
        runtime_residue=_runtime_residue(evidence.get("runtime_residue")),
        failure_domain=_optional_str(evidence.get("failure_domain")),
        notes=_optional_str(evidence.get("notes")),
    )
    if case_id == BUSY_RELEASE_CASE_ID:
        row["retained_busy_evidence"] = _dict_or_empty(evidence.get("retained_busy_evidence"))
        row["idle_release_evidence"] = _dict_or_empty(evidence.get("idle_release_evidence"))
    _add_release_incomplete_evidence(row, evidence)
    row["classification"] = _classify_observed(row, case)
    return row


def _direct_row_from_workflow_smoke(payload: dict[str, Any]) -> dict[str, Any]:
    case = _case_with_task_id(DIRECT_CASE_ID, _optional_str(payload.get("task_id")))
    summary = _dict_or_empty(payload.get("summary"))
    checks = _dict_or_empty(summary.get("checks"))
    release = _dict_or_empty(summary.get("release"))
    runner_execute = _result_payload(payload, "runner_execute")
    topology = _dict_or_empty(runner_execute.get("topology"))
    observed_route = "direct_execution" if checks.get("route_direct_execution_imported") else None
    cleanup_status = _cleanup_status_from_workflow_summary(checks=checks, release=release)
    runtime_paths = {
        "project_root": str(payload.get("project_root") or ""),
        "workflow_closure_smoke": str(WORKFLOW_CLOSURE_SMOKE),
    }
    for key in ("desired_path", "observed_path", "proposal_path", "proposal_source_path"):
        value = _optional_str(topology.get(key))
        if value:
            runtime_paths[key] = value
    _add_config_runtime_path(runtime_paths)
    row = _base_row(
        case,
        implemented=True,
        case_status="observed",
        observed_route=observed_route,
        route_decision_correct=observed_route == case["expected_route"],
        round_result=_str_or_unknown(summary.get("round_result")),
        final_status=_str_or_unknown(summary.get("final_status")),
        cleanup_status=cleanup_status,
        classification="test_design_failure",
        artifact_paths={"round_json": str(summary.get("round_path") or "")},
        runtime_paths=runtime_paths,
        authority_checks=_authority_checks_for_paths(
            {
                "topology_dispatch_absent": _optional_bool(checks.get("topology_dispatch_absent")),
                "provider_reply_authority_parsing_absent": True,
            },
            artifact_paths={},
            runtime_paths=runtime_paths,
            fallback_root=_optional_str(payload.get("project_root")),
        ),
        ask_reachability=_optional_bool(checks.get("ask_reachability")),
        runtime_residue=_runtime_residue_from_workflow_checks(checks, runtime_paths=runtime_paths),
        notes=f"workflow_smoke_status={payload.get('workflow_smoke_status') or 'unknown'}",
    )
    _add_release_incomplete_evidence(row, release)
    row["classification"] = _classify_observed(row, case)
    return row


def _route_row_from_workflow_smoke(case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload_case_id = _optional_str(payload.get("phase6_case_id"))
    if payload_case_id and payload_case_id != case_id:
        raise ValueError(f"route smoke payload case_id must be {case_id!r}: {payload_case_id!r}")
    case = _case_with_task_id(case_id, _optional_str(payload.get("task_id")))
    summary = _dict_or_empty(payload.get("summary"))
    checks = _dict_or_empty(summary.get("checks"))
    release = _dict_or_empty(summary.get("release"))
    runner_execute = _result_payload(payload, "runner_execute")
    topology = _dict_or_empty(runner_execute.get("topology"))
    observed_route = _optional_str(summary.get("observed_route"))
    cleanup_status = _optional_str(summary.get("cleanup_status")) or _cleanup_status_from_workflow_summary(checks=checks, release=release)
    runtime_paths = {
        "project_root": str(payload.get("project_root") or ""),
        "workflow_closure_smoke": str(WORKFLOW_CLOSURE_SMOKE),
    }
    for key in ("desired_path", "observed_path", "proposal_path", "proposal_source_path"):
        value = _optional_str(topology.get(key))
        if value:
            runtime_paths[key] = value
    _add_config_runtime_path(runtime_paths)
    round_path = _optional_str(summary.get("round_path"))
    artifact_paths = {"round_json": round_path} if round_path else {}
    row = _base_row(
        case,
        implemented=True,
        case_status="observed",
        observed_route=observed_route,
        route_decision_correct=observed_route == case["expected_route"],
        round_result=_str_or_unknown(summary.get("round_result")),
        final_status=_str_or_unknown(summary.get("final_status")),
        cleanup_status=cleanup_status,
        classification="test_design_failure",
        artifact_paths=artifact_paths,
        runtime_paths=runtime_paths,
        authority_checks=_authority_checks_for_paths(
            {
                "topology_dispatch_absent": _optional_bool(checks.get("topology_dispatch_absent")),
                "communication_edges_absent": _optional_bool(checks.get("communication_edges_absent")),
                "provider_reply_authority_parsing_absent": _optional_bool(
                    checks.get("provider_reply_authority_parsing_absent")
                ),
            },
            artifact_paths=artifact_paths,
            runtime_paths=runtime_paths,
            fallback_root=_optional_str(payload.get("project_root")),
        ),
        ask_reachability=_optional_bool(checks.get("ask_reachability")),
        runtime_residue=_runtime_residue_from_workflow_checks(checks, runtime_paths=runtime_paths),
        notes=(
            f"workflow_smoke_status={payload.get('workflow_smoke_status') or 'unknown'}; "
            f"round_result_source={summary.get('round_result_source') or 'unknown'}"
        ),
    )
    _add_release_incomplete_evidence(row, release)
    row["classification"] = _classify_observed(row, case)
    return row


def _base_row(
    case: dict[str, Any],
    *,
    implemented: bool,
    case_status: str,
    observed_route: str | None,
    route_decision_correct: bool,
    round_result: str,
    final_status: str,
    cleanup_status: str,
    classification: str,
    artifact_paths: dict[str, Any],
    runtime_paths: dict[str, Any],
    authority_checks: dict[str, bool | None],
    ask_reachability: bool | None,
    runtime_residue: dict[str, Any],
    failure_domain: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "task_id": case["task_id"],
        "implemented": implemented,
        "case_status": case_status,
        "expected_route": case["expected_route"],
        "observed_route": observed_route,
        "route_decision_correct": route_decision_correct,
        "round_result": round_result,
        "final_status": final_status,
        "cleanup_result": cleanup_status,
        "cleanup_status": cleanup_status,
        "classification": classification,
        "expected_classification": case["expected_classification"],
        "artifact_paths": artifact_paths,
        "runtime_paths": runtime_paths,
        "authority_checks": authority_checks,
        "ask_reachability": ask_reachability,
        "runtime_residue": runtime_residue,
        "failure_domain": failure_domain,
        "notes": notes,
    }


def _classify_observed(row: dict[str, Any], case: dict[str, Any]) -> str:
    failure_domain = str(row.get("failure_domain") or "").strip()
    if failure_domain == "provider":
        return "provider_failure"
    if failure_domain == "role":
        return "role_failure"
    if _has_test_design_failure(row):
        return "test_design_failure"
    if not row["route_decision_correct"] or _has_authority_violation(row):
        return "system_failure"

    expected_round_results = set(case["expected_round_results"])
    expected_final_statuses = set(case["expected_final_statuses"])
    expected_cleanup_statuses = set(case["expected_cleanup_statuses"])
    round_ok = row["round_result"] in expected_round_results
    final_ok = row["final_status"] in expected_final_statuses
    cleanup_ok = row["cleanup_status"] in expected_cleanup_statuses
    if round_ok and final_ok and _has_bounded_release_incomplete(row):
        return "valid_non_success"
    if case["expected_classification"] == "pass":
        if round_ok and final_ok and row["cleanup_status"] in PASS_CLEANUP_STATUSES:
            return "pass"
        return "system_failure"
    if round_ok and final_ok and (cleanup_ok or row["cleanup_status"] in VALID_NON_SUCCESS_CLEANUP_STATUSES):
        return "valid_non_success"
    return "system_failure"


def _add_release_incomplete_evidence(row: dict[str, Any], source: Any) -> None:
    evidence = _dict_or_empty(source)
    release = _dict_or_empty(evidence.get("release"))
    release_blockers = _dict_or_empty(evidence.get("release_blockers") or release.get("release_blockers"))
    release_incomplete_agents = _str_list(
        evidence.get("release_incomplete_agents") or release.get("release_incomplete_agents")
    )
    if release_blockers and not release_incomplete_agents:
        release_incomplete_agents = sorted(str(agent) for agent in release_blockers)
    input_errors = _str_list(evidence.get("input_errors"))
    test_design_failures = _str_list(evidence.get("test_design_failures"))
    if release_blockers:
        row["release_blockers"] = release_blockers
    if release_incomplete_agents:
        row["release_incomplete_agents"] = release_incomplete_agents
    if input_errors:
        row["input_errors"] = input_errors
    if test_design_failures:
        row["test_design_failures"] = test_design_failures


def _has_test_design_failure(row: dict[str, Any]) -> bool:
    return bool(_str_list(row.get("input_errors")) or _str_list(row.get("test_design_failures")))


def _has_bounded_release_incomplete(row: dict[str, Any]) -> bool:
    if row.get("cleanup_status") != "release_incomplete":
        return False
    release_blockers = _dict_or_empty(row.get("release_blockers"))
    release_incomplete_agents = _str_list(row.get("release_incomplete_agents"))
    if not release_blockers or not release_incomplete_agents:
        return False
    for agent in release_incomplete_agents:
        blocker = release_blockers.get(agent)
        if not _has_bounded_release_blocker_reason(blocker):
            return False
    return True


def _has_bounded_release_blocker_reason(blocker: Any) -> bool:
    if isinstance(blocker, dict):
        reason_parts = [
            _optional_str(blocker.get(key)) or ""
            for key in (
                "reason",
                "status",
                "state",
                "policy",
                "kind",
                "profile",
                "desired_state",
                "observed_state",
                "lifecycle_state",
                "retain_reason",
                "provider_home_mode",
            )
        ]
        reason_text = " ".join(reason_parts).lower()
    else:
        reason_text = (_optional_str(blocker) or "").lower()
    if not reason_text:
        return False
    return any(marker in reason_text for marker in BOUNDED_RELEASE_INCOMPLETE_REASON_MARKERS)


def _has_authority_violation(row: dict[str, Any]) -> bool:
    checks = _dict_or_empty(row.get("authority_checks"))
    for key in ("topology_dispatch_absent", "communication_edges_absent", "provider_reply_authority_parsing_absent"):
        if checks.get(key) is False:
            return True
    return False


def _cleanup_status_from_workflow_summary(*, checks: dict[str, Any], release: dict[str, Any]) -> str:
    retained = _int_or_none(release.get("retained_count"))
    if retained and retained > 0:
        return "retained_busy"
    if release.get("loop_topology_status") == "released" or checks.get("release_status_released"):
        return "released"
    if checks.get("release_retained_zero"):
        return "released"
    return "unknown"


def _authority_checks(value: Any) -> dict[str, bool | None]:
    checks = _dict_or_empty(value)
    return {
        "topology_dispatch_absent": _optional_bool(checks.get("topology_dispatch_absent")),
        "communication_edges_absent": _optional_bool(checks.get("communication_edges_absent")),
        "provider_reply_authority_parsing_absent": _optional_bool(checks.get("provider_reply_authority_parsing_absent")),
    }


def _authority_checks_for_paths(
    value: Any,
    *,
    artifact_paths: dict[str, Any],
    runtime_paths: dict[str, Any],
    fallback_root: str | None,
) -> dict[str, bool | None]:
    checks = _authority_checks(value)
    desired_path = _mount_topology_desired_path(artifact_paths=artifact_paths, runtime_paths=runtime_paths)
    if desired_path is not None:
        checks["communication_edges_absent"] = _communication_edges_absent_from_topology(
            desired_path,
            fallback_root=fallback_root,
        )
    return checks


def _mount_topology_desired_path(*, artifact_paths: dict[str, Any], runtime_paths: dict[str, Any]) -> str | None:
    keys = (
        "agent_mount_topology_desired",
        "agent_mount_topology_desired_path",
        "mount_topology_desired",
        "mount_topology_desired_path",
        "topology_desired",
        "topology_desired_path",
        "desired_path",
    )
    for paths in (runtime_paths, artifact_paths):
        for key in keys:
            value = _optional_str(paths.get(key))
            if value:
                return value
    return None


def _communication_edges_absent_from_topology(path_text: str, *, fallback_root: str | None) -> bool:
    path = Path(path_text)
    if not path.is_absolute() and fallback_root:
        path = Path(fallback_root) / path
    if not path.is_file():
        return False
    payload = _read_json_object(path)
    if not payload:
        return False
    return (
        _absent_or_empty(payload, "edges", list)
        and _absent_or_empty(payload, "gates", list)
        and _absent_or_empty(payload, "artifacts", dict)
    )


def _absent_or_empty(payload: dict[str, Any], key: str, expected_type: type) -> bool:
    if key not in payload:
        return True
    value = payload.get(key)
    return isinstance(value, expected_type) and not value


def _runtime_residue(value: Any) -> dict[str, Any]:
    residue = _dict_or_empty(value)
    return {
        "dynamic_agents_absent": _optional_bool(residue.get("dynamic_agents_absent")),
        "config_dynamic_agents_absent": _optional_bool(residue.get("config_dynamic_agents_absent")),
        "observed_topology_residue_absent": _optional_bool(residue.get("observed_topology_residue_absent")),
    }


def _runtime_residue_from_workflow_checks(checks: dict[str, Any], *, runtime_paths: dict[str, Any]) -> dict[str, Any]:
    loop_id = _loop_id_from_runtime_paths(runtime_paths)
    agent_names = _dynamic_agent_names(loop_id)
    config_path = _runtime_path(runtime_paths, "ccb_config")
    observed_path = _runtime_path(runtime_paths, "observed_path")
    return {
        "dynamic_agents_absent": _optional_bool(checks.get("dynamic_agents_absent_from_ps")),
        "config_dynamic_agents_absent": _config_dynamic_residue_absent(config_path, agent_names),
        "observed_topology_residue_absent": _observed_topology_residue_absent(observed_path, agent_names),
    }


def _add_config_runtime_path(runtime_paths: dict[str, Any]) -> None:
    project_root = _optional_str(runtime_paths.get("project_root"))
    if not project_root or _optional_str(runtime_paths.get("ccb_config")):
        return
    runtime_paths["ccb_config"] = str(Path(project_root) / ".ccb" / "ccb.config")


def _runtime_path(runtime_paths: dict[str, Any], key: str) -> Path | None:
    value = _optional_str(runtime_paths.get(key))
    return Path(value) if value else None


def _loop_id_from_runtime_paths(runtime_paths: dict[str, Any]) -> str | None:
    for key in ("observed_path", "desired_path"):
        path = _runtime_path(runtime_paths, key)
        if path is None:
            continue
        payload = _read_json_object(path)
        loop_id = _optional_str(payload.get("loop_id"))
        if loop_id:
            return loop_id
        match = re.search(r"/loops/([^/]+)/", path.as_posix())
        if match:
            return match.group(1)
    return None


def _dynamic_agent_names(loop_id: str | None) -> tuple[str, ...]:
    if not loop_id:
        return ()
    return (f"loop-{loop_id}-coder-1", f"loop-{loop_id}-code_reviewer-1")


def _config_dynamic_residue_absent(path: Path | None, agent_names: tuple[str, ...]) -> bool:
    if path is None:
        return False
    return _config_agents_absent(path, agent_names)


def _observed_topology_residue_absent(path: Path | None, agent_names: tuple[str, ...]) -> bool:
    if path is None:
        return True
    return _topology_agents_absent(path, agent_names)


def _append_workflow_command(
    module: Any,
    results: list[dict[str, Any]],
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 60,
    allow_failure: bool = False,
) -> None:
    module._append(results, name, command, cwd=cwd, env=env, timeout=timeout, allow_failure=allow_failure)


def _write_busy_release_topology_proposal(
    *,
    project_root: Path,
    loop_id: str,
    worker_agent: str,
    reviewer_agent: str,
) -> Path:
    path = project_root / "drafts" / f"{BUSY_RELEASE_CASE_ID}-mount-topology.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    proposal = {
        "schema": "ccb.loop.agent_mount_topology.v1",
        "release_policy": {"policy": "auto", "idle_only": True},
        "windows": [
            {
                "name": "ccb-exec",
                "class": "execution",
                "max_panes": 6,
                "layout_policy": "append-or-create-window",
            }
        ],
        "agents": [
            {
                "id": worker_agent,
                "profile": "coder",
                "desired_state": "present",
                "window_name": "ccb-exec",
                "pane_order": 0,
                "lifecycle": "ephemeral",
                "release_policy": "auto",
            },
            {
                "id": reviewer_agent,
                "profile": "code_reviewer",
                "desired_state": "present",
                "window_name": "ccb-exec",
                "pane_order": 1,
                "lifecycle": "ephemeral",
                "release_policy": "auto",
            },
        ],
    }
    path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _busy_release_evidence(
    results: list[dict[str, Any]],
    *,
    project_root: Path,
    loop_id: str,
    worker_agent: str,
    reviewer_agent: str,
    proposal_source_path: Path,
    artifact_paths: dict[str, str],
) -> dict[str, Any]:
    payloads = {str(item.get("name") or ""): _json_payload(str(item.get("stdout") or "")) for item in results}
    retained_release = payloads.get("topology_release_busy") or {}
    idle_release = payloads.get("topology_release_idle") or {}
    final_task = payloads.get("task_show_final") or {}
    route_payload = payloads.get("route_direct_execution") or {}
    task_record = final_task.get("task") if isinstance(final_task.get("task"), dict) else {}
    desired_path = _optional_str(idle_release.get("desired_path")) or _optional_str(retained_release.get("desired_path"))
    observed_path = _optional_str(idle_release.get("observed_path")) or _optional_str(retained_release.get("observed_path"))
    retained_agents = [str(item) for item in tuple(retained_release.get("retained_agents") or ()) if str(item)]
    retain_reasons = _dict_or_empty(retained_release.get("retain_reasons"))
    busy_job_id = _accepted_job_id(_result(results, "busy_worker_ask"))
    final_status = _str_or_unknown(task_record.get("status") or final_task.get("status"))
    observed_route = _optional_str(_dict_or_empty(route_payload.get("artifact")).get("orchestrator_route"))
    release_status = _optional_str(retained_release.get("loop_topology_status"))
    cleanup_status = "retained_busy" if release_status == "retained_busy" else _str_or_unknown(release_status)
    post_idle_ps = str(_result(results, "post_idle_ps").get("stdout") or "")
    agent_names = (worker_agent, reviewer_agent)
    runtime_paths = {
        "project_root": str(project_root),
        "workflow_closure_smoke": str(WORKFLOW_CLOSURE_SMOKE),
        "proposal_source_path": str(proposal_source_path),
        "topology_events": str(project_root / ".ccb" / "runtime" / "loops" / loop_id / "agent_mount_topology.events.jsonl"),
        "ccb_config": str(project_root / ".ccb" / "ccb.config"),
    }
    if desired_path:
        runtime_paths["desired_path"] = desired_path
    if observed_path:
        runtime_paths["observed_path"] = observed_path
    authority_checks = _authority_checks_for_paths(
        {
            "topology_dispatch_absent": not (project_root / ".ccb" / "runtime" / "loops" / loop_id / "topology_dispatch.json").exists(),
            "provider_reply_authority_parsing_absent": True,
        },
        artifact_paths={},
        runtime_paths=runtime_paths,
        fallback_root=str(project_root),
    )
    return {
        "case_id": BUSY_RELEASE_CASE_ID,
        "task_id": BUSY_RELEASE_CASE_ID,
        "observed_route": observed_route,
        "round_result": "busy" if retained_agents and cleanup_status == "retained_busy" else "unknown",
        "final_status": final_status,
        "cleanup_result": cleanup_status,
        "artifact_paths": {
            "task_packet": artifact_paths.get("task_packet"),
            "execution_contract": artifact_paths.get("execution_contract"),
            "orchestration_notes": artifact_paths.get("orchestration_notes"),
        },
        "runtime_paths": runtime_paths,
        "authority_checks": authority_checks,
        "ask_reachability": bool(busy_job_id and retained_agents),
        "runtime_residue": {
            "dynamic_agents_absent": _agents_absent_from_text(post_idle_ps, agent_names),
            "config_dynamic_agents_absent": _config_agents_absent(project_root / ".ccb" / "ccb.config", agent_names),
            "observed_topology_residue_absent": _topology_agents_absent(Path(observed_path), agent_names)
            if observed_path
            else None,
        },
        "retained_busy_evidence": {
            "loop_id": loop_id,
            "busy_job_id": busy_job_id,
            "retained_agents": retained_agents,
            "retain_reasons": retain_reasons,
            "released_agents": list(retained_release.get("released_agents") or []),
            "task_current_loop": task_record.get("current_loop"),
            "task_status": task_record.get("status"),
        },
        "idle_release_evidence": {
            "loop_topology_status": idle_release.get("loop_topology_status"),
            "released_agents": list(idle_release.get("released_agents") or []),
            "retained_count": idle_release.get("retained_count"),
            "released_count": idle_release.get("released_count"),
        },
        "notes": "script-owned busy lifecycle evidence; no task-import-round busy authority mutation",
    }


def _result(results: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for item in results:
        if item.get("name") == name:
            return item
    return {}


def _accepted_job_id(result: dict[str, Any]) -> str:
    text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    match = re.search(r"\baccepted job=(job_[A-Za-z0-9]+)\b", text)
    return match.group(1) if match else ""


def _agents_absent_from_text(text: str, agent_names: tuple[str, ...]) -> bool:
    return all(agent not in text for agent in agent_names)


def _config_agents_absent(path: Path, agent_names: tuple[str, ...]) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return _agents_absent_from_text(text, agent_names)


def _topology_agents_absent(path: Path, agent_names: tuple[str, ...]) -> bool:
    payload = _read_json_object(path)
    if not payload:
        return False
    return not (_topology_agent_ids(payload) & set(agent_names))


def _topology_agent_ids(payload: dict[str, Any]) -> set[str]:
    names = {
        str(agent.get("id") or agent.get("name") or "")
        for agent in tuple(payload.get("agents") or ())
        if isinstance(agent, dict)
    }
    for node in tuple(payload.get("nodes") or ()):
        if not isinstance(node, dict):
            continue
        names.update(
            str(agent.get("id") or agent.get("name") or "")
            for agent in tuple(node.get("agents") or ())
            if isinstance(agent, dict)
        )
    return {name for name in names if name}


def _case(case_id: str) -> dict[str, Any]:
    for case in REQUIRED_CASES:
        if case["case_id"] == case_id:
            return case
    raise KeyError(case_id)


def _case_with_task_id(case_id: str, task_id: str | None) -> dict[str, Any]:
    case = dict(_case(case_id))
    if task_id:
        case["task_id"] = task_id
    return case


def _load_workflow_closure_smoke() -> Any:
    spec = importlib.util.spec_from_file_location("workflow_closure_smoke", WORKFLOW_CLOSURE_SMOKE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load workflow closure smoke script: {WORKFLOW_CLOSURE_SMOKE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result_payload(payload: dict[str, Any], name: str) -> dict[str, Any]:
    for item in payload.get("results") if isinstance(payload.get("results"), list) else ():
        if not isinstance(item, dict) or item.get("name") != name:
            continue
        return _json_payload(str(item.get("stdout") or ""))
    return {}


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _require_source_wrapper_test_root(test_root: Path) -> None:
    root = test_root.expanduser().resolve(strict=False)
    allowed = SOURCE_WRAPPER_ROOT.expanduser().resolve(strict=False)
    if root != allowed and allowed not in root.parents:
        raise ValueError(f"source-wrapper Phase 6 smokes must run under {allowed}: {root}")


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [text for item in value if (text := _optional_str(item))]


def _str_or_unknown(value: Any) -> str:
    text = _optional_str(value)
    return text or "unknown"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _markdown_report(report: dict[str, Any]) -> str:
    summary = _dict_or_empty(report.get("summary"))
    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    matrix_passed = report.get("phase6_fake_matrix_status") == "pass"
    title = (
        "# Phase 6 Fake-Provider Matrix Report"
        if matrix_passed
        else "# Phase 6 Fake-Provider Matrix Scaffold Report"
    )
    lines = [
        title,
        "",
        f"Date: {time.strftime('%Y-%m-%d')}",
        "",
        "## Status",
        "",
        f"- phase6_fake_matrix_status: `{report.get('phase6_fake_matrix_status')}`",
        f"- phase6a_pass: `{str(bool(report.get('phase6a_pass'))).lower()}`",
        f"- required_case_count: `{summary.get('required_case_count')}`",
        f"- observed_case_count: `{summary.get('observed_case_count')}`",
        f"- missing_case_ids: `{', '.join(str(item) for item in summary.get('missing_case_ids') or ())}`",
        "",
        "## Matrix Rows",
        "",
        "| Case | Status | Expected Route | Observed Route | Round | Final | Cleanup | Classification |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        lines.append(
            "| {case_id} | {case_status} | {expected_route} | {observed_route} | {round_result} | {final_status} | {cleanup_result} | {classification} |".format(
                case_id=_md_cell(row.get("case_id")),
                case_status=_md_cell(row.get("case_status")),
                expected_route=_md_cell(row.get("expected_route")),
                observed_route=_md_cell(row.get("observed_route")),
                round_result=_md_cell(row.get("round_result")),
                final_status=_md_cell(row.get("final_status")),
                cleanup_result=_md_cell(row.get("cleanup_result")),
                classification=_md_cell(row.get("classification")),
            )
        )
    if matrix_passed:
        lines.extend(
            [
                "",
                "## Reviewer Audit Notes",
                "",
                "- All eight required fake-provider matrix cases are observed in this integrated source-wrapper report.",
                "- `phase6a_pass=true` is matrix evidence for reviewer audit; final Phase 6A acceptance still requires reviewer/module-level sign-off.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Residual Gaps",
                "",
                "- Missing or not implemented cases remain `test_design_failure` and keep `phase6a_pass=false`.",
                "- `phase6a_pass=true` requires all eight implemented cases to be observed in one integrated source-wrapper report.",
                "",
            ]
        )
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|")


if __name__ == "__main__":
    raise SystemExit(main())
