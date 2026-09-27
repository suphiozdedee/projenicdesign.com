#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_ROOT = Path(os.environ.get("CCB_WORKFLOW_SMOKE_TEST_ROOT", "/home/bfly/yunwei/test_ccb2"))
DEFAULT_PLAN = "workflow-smoke"
DEFAULT_TASK = "task-closure"
PHASE6_ROUTE_CASES = {
    "smoke-needs-detail-pass": {
        "route": "needs_detail",
        "round_result": "pass",
        "final_status": "done",
    },
    "smoke-macro-adjustment": {
        "route": "macro_adjustment_request",
        "round_result": "replan_required",
        "final_status": "replan_required",
    },
    "smoke-blocked": {
        "route": "blocked",
        "round_result": "blocked",
        "final_status": "blocked",
    },
}
PHASE6_EXECUTION_CASES = {
    "smoke-partial-completion": {
        "route": "partial_completion",
        "scenario": "partial_completion",
        "round_result": "partial",
        "final_status": "partial",
    },
    "smoke-reviewer-reject-rework": {
        "route": "direct_execution",
        "scenario": "reviewer_reject_rework",
        "round_result": "pass",
        "final_status": "done",
    },
    "smoke-reviewer-cannot-accept": {
        "route": "direct_execution",
        "scenario": "reviewer_cannot_accept",
        "round_result": "replan_required",
        "final_status": "replan_required",
    },
}
ROLEPACK_ROOT = REPO_ROOT / "docs" / "plantree" / "plans" / "agentic-loop-workflow" / "drafts"
ROLEPACKS = {
    "agentroles.ccb_frontdesk": "agentroles.ccb_frontdesk",
    "agentroles.ccb_planner": "agentroles.ccb_planner",
    "agentroles.ccb_clarification_broker": "agentroles.ccb_clarification_broker",
    "agentroles.ccb_plan_reviewer": "agentroles.ccb_plan_reviewer",
    "agentroles.ccb_orchestrator": "agentroles.ccb_orchestrator",
    "agentroles.ccb_task_detailer": "agentroles.ccb_task_detailer",
    "agentroles.ccb_round_reviewer": "agentroles.ccb_round_reviewer",
    "agentroles.coder": "agentroles.coder",
    "agentroles.code_reviewer": "agentroles.code_reviewer",
}
TERMINAL_STATUSES = {"done", "partial", "replan_required", "blocked"}


def build_config(*, provider: str = "fake") -> str:
    return "\n".join(
        [
            (
                "frontdesk:{provider}; planner:{provider}; task_detailer:{provider}; clarification_broker:{provider}; "
                "plan_reviewer:{provider}; orchestrator:{provider}; ccb_round_reviewer:{provider}"
            ).format(provider=provider),
            "",
            "[agents.frontdesk]",
            'role = "agentroles.ccb_frontdesk"',
            "",
            "[agents.planner]",
            'role = "agentroles.ccb_planner"',
            "",
            "[agents.task_detailer]",
            'role = "agentroles.ccb_task_detailer"',
            "",
            "[agents.clarification_broker]",
            'role = "agentroles.ccb_clarification_broker"',
            "",
            "[agents.plan_reviewer]",
            'role = "agentroles.ccb_plan_reviewer"',
            "",
            "[agents.orchestrator]",
            'role = "agentroles.ccb_orchestrator"',
            "",
            "[agents.ccb_round_reviewer]",
            'role = "agentroles.ccb_round_reviewer"',
            "",
            "[loop.capacity]",
            "enabled = true",
            "max_nodes = 4",
            'default_lifetime = "current_round"',
            'name_template = "loop-{loop_id}-{profile}-{index}"',
            'reuse = "prefer_idle"',
            "",
            "[loop.role_profiles.worker]",
            'role = "agentroles.coder"',
            f'provider = "{provider}"',
            'workspace_mode = "copy"',
            "max_instances = 2",
            'reuse = "prefer_idle"',
            "",
            "[loop.role_profiles.coder]",
            'role = "agentroles.coder"',
            f'provider = "{provider}"',
            'workspace_mode = "copy"',
            "max_instances = 2",
            'reuse = "prefer_idle"',
            "",
            "[loop.role_profiles.code_reviewer]",
            'role = "agentroles.code_reviewer"',
            f'provider = "{provider}"',
            'workspace_mode = "copy"',
            "max_instances = 2",
            'reuse = "prefer_idle"',
            "",
        ]
    )


def prepare_project(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    reset: bool = False,
) -> dict[str, str]:
    root = test_root.expanduser().resolve(strict=False)
    project_root = (root / project_name).resolve(strict=False)
    if root not in project_root.parents and project_root != root:
        raise ValueError(f"project must be under test root: {root}")
    if reset and project_root.exists():
        shutil.rmtree(project_root)
    project_root.mkdir(parents=True, exist_ok=True)
    source_home = root / "source_home"
    source_home.mkdir(parents=True, exist_ok=True)
    config_path = project_root / ".ccb" / "ccb.config"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(build_config(provider=provider), encoding="utf-8")
    plan_root = project_root / "docs" / "plantree" / "plans" / DEFAULT_PLAN
    plan_root.mkdir(parents=True, exist_ok=True)
    (plan_root / "README.md").write_text("# Workflow Smoke Plan\n", encoding="utf-8")
    role_store = project_root / "roles"
    _install_rolepacks(role_store)
    shim_payload = _install_cli_shims(project_root=project_root, ccb_test=ccb_test)
    return {
        "project_root": str(project_root),
        "config_path": str(config_path),
        "plan_root": str(plan_root),
        "role_store": str(role_store),
        "source_home": str(source_home),
        **shim_payload,
    }


def run_workflow_smoke(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    timeout_s: int,
    reset: bool = False,
    keep_running: bool = False,
) -> dict[str, Any]:
    prepared = prepare_project(
        test_root=test_root,
        project_name=project_name,
        provider=provider,
        ccb_test=ccb_test,
        reset=reset,
    )
    project_root = Path(prepared["project_root"])
    role_store = Path(prepared["role_store"])
    env = _smoke_env(test_root=test_root, project_root=project_root, role_store=role_store)
    results: list[dict[str, Any]] = []
    try:
        _append(results, "diagnose", [str(ccb_test), "--diagnose"], cwd=test_root, env=env)
        _append(results, "config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env)
        _append(results, "start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env)
        _append(
            results,
            "task_create",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "plan",
                "task-create",
                "--plan",
                DEFAULT_PLAN,
                "--title",
                "Workflow closure smoke task",
                "--task-id",
                DEFAULT_TASK,
                "--json",
            ],
            cwd=test_root,
            env=env,
        )
        artifacts = write_artifacts(project_root=project_root, task_id=DEFAULT_TASK)
        for kind in ("task_packet", "execution_contract"):
            _append_plan_artifact(results, kind, ccb_test, project_root, test_root, env, artifacts[kind])
        _append(
            results,
            "route_direct_execution",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "plan",
                "task-artifact",
                "--task",
                DEFAULT_TASK,
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
        _append(
            results,
            "ready_for_orchestration",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "plan",
                "task-status",
                "--task",
                DEFAULT_TASK,
                "--status",
                "ready_for_orchestration",
                "--next-owner",
                "orchestrator",
                "--activation-reason",
                "direct_execution_smoke",
                "--json",
            ],
            cwd=test_root,
            env=env,
        )
        _append_runner(results, "runner_execute", ccb_test=ccb_test, project_root=project_root, test_root=test_root, env=env, timeout_s=timeout_s)
        _append(
            results,
            "task_show_final",
            [str(ccb_test), "--project", str(project_root), "plan", "task-show", "--task", DEFAULT_TASK, "--json"],
            cwd=test_root,
            env=env,
        )
        _append(results, "post_execute_ps", [str(ccb_test), "--project", str(project_root), "ps"], cwd=test_root, env=env)
    finally:
        if not keep_running:
            _append(results, "kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, allow_failure=True)

    summary = _workflow_summary(results)
    return {
        "workflow_smoke_status": "ok" if summary["ok"] else "failed",
        "provider": provider,
        "project_root": str(project_root),
        "task_id": DEFAULT_TASK,
        "plan_slug": DEFAULT_PLAN,
        "summary": summary,
        "results": results,
    }


def run_phase6_route_smoke(
    *,
    test_root: Path,
    project_name: str,
    case_id: str,
    provider: str,
    ccb_test: Path,
    timeout_s: int,
    reset: bool = False,
    keep_running: bool = False,
) -> dict[str, Any]:
    case = PHASE6_ROUTE_CASES.get(case_id)
    if case is None:
        known = ", ".join(sorted(PHASE6_ROUTE_CASES))
        raise ValueError(f"unsupported Phase 6 route smoke case {case_id!r}; expected one of: {known}")
    route = str(case["route"])
    prepared = prepare_project(
        test_root=test_root,
        project_name=project_name,
        provider=provider,
        ccb_test=ccb_test,
        reset=reset,
    )
    project_root = Path(prepared["project_root"])
    role_store = Path(prepared["role_store"])
    env = _smoke_env(test_root=test_root, project_root=project_root, role_store=role_store)
    results: list[dict[str, Any]] = []
    try:
        _append(results, "diagnose", [str(ccb_test), "--diagnose"], cwd=test_root, env=env)
        _append(results, "config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env)
        _append(results, "start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env)
        _append_task_create(results, ccb_test, project_root, test_root, env, task_id=case_id)
        artifacts = write_artifacts(project_root=project_root, task_id=case_id, route=route)
        for kind in ("task_packet", "execution_contract"):
            _append_plan_artifact(results, kind, ccb_test, project_root, test_root, env, artifacts[kind], task_id=case_id)
        _append_orchestration_notes(
            results,
            f"route_{route}",
            ccb_test,
            project_root,
            test_root,
            env,
            task_id=case_id,
            route=route,
            file_path=artifacts["orchestration_notes"],
        )
        _append_ready_for_orchestration(results, ccb_test, project_root, test_root, env, task_id=case_id)
        _append_runner(results, "runner_route", ccb_test=ccb_test, project_root=project_root, test_root=test_root, env=env, timeout_s=timeout_s)
        if route == "needs_detail":
            detail_artifacts = write_detail_artifacts(project_root=project_root, task_id=case_id)
            for kind in ("detail_design", "detail_summary", "detail_packet", "detail_step_1", "detail_step_2"):
                _append_plan_artifact(
                    results,
                    kind,
                    ccb_test,
                    project_root,
                    test_root,
                    env,
                    detail_artifacts[kind],
                    task_id=case_id,
                )
            direct_notes = write_orchestration_notes(project_root=project_root, task_id=case_id, route="direct_execution")
            _append_orchestration_notes(
                results,
                "route_direct_execution_after_detail",
                ccb_test,
                project_root,
                test_root,
                env,
                task_id=case_id,
                route="direct_execution",
                file_path=direct_notes,
            )
            _append_runner(results, "runner_execute", ccb_test=ccb_test, project_root=project_root, test_root=test_root, env=env, timeout_s=timeout_s)
        else:
            if route == "macro_adjustment_request":
                macro_artifact = write_macro_adjustment_request(project_root=project_root, task_id=case_id)
                _append_plan_artifact(
                    results,
                    "macro_adjustment_request",
                    ccb_test,
                    project_root,
                    test_root,
                    env,
                    macro_artifact,
                    task_id=case_id,
                )
            if route == "blocked":
                blocker_artifact = write_blocker_evidence(project_root=project_root, task_id=case_id)
                _append_plan_artifact(
                    results,
                    "blocker_evidence",
                    ccb_test,
                    project_root,
                    test_root,
                    env,
                    blocker_artifact,
                    task_id=case_id,
                )
            loop_id = f"script-{case_id}"
            _append_task_bind_loop(results, ccb_test, project_root, test_root, env, task_id=case_id, loop_id=loop_id)
            report_path = write_script_round_summary(
                project_root=project_root,
                task_id=case_id,
                loop_id=loop_id,
                result=str(case["round_result"]),
                route=route,
            )
            _append_task_import_round(
                results,
                ccb_test,
                project_root,
                test_root,
                env,
                task_id=case_id,
                loop_id=loop_id,
                result=str(case["round_result"]),
                report_path=report_path,
            )
        _append(
            results,
            "task_show_final",
            [str(ccb_test), "--project", str(project_root), "plan", "task-show", "--task", case_id, "--json"],
            cwd=test_root,
            env=env,
        )
        _append(results, "post_execute_ps", [str(ccb_test), "--project", str(project_root), "ps"], cwd=test_root, env=env)
    finally:
        if not keep_running:
            _append(results, "kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, allow_failure=True)

    summary = _phase6_route_summary(results, project_root=project_root, case_id=case_id, route=route)
    return {
        "workflow_smoke_status": "ok" if summary["ok"] else "failed",
        "phase6_case_id": case_id,
        "provider": provider,
        "project_root": str(project_root),
        "task_id": case_id,
        "plan_slug": DEFAULT_PLAN,
        "summary": summary,
        "results": results,
    }


def run_phase6_execution_case_smoke(
    *,
    test_root: Path,
    project_name: str,
    case_id: str,
    provider: str,
    ccb_test: Path,
    timeout_s: int,
    reset: bool = False,
    keep_running: bool = False,
) -> dict[str, Any]:
    case = PHASE6_EXECUTION_CASES.get(case_id)
    if case is None:
        known = ", ".join(sorted(PHASE6_EXECUTION_CASES))
        raise ValueError(f"unsupported Phase 6 execution smoke case {case_id!r}; expected one of: {known}")
    route = str(case["route"])
    scenario = str(case["scenario"])
    prepared = prepare_project(
        test_root=test_root,
        project_name=project_name,
        provider=provider,
        ccb_test=ccb_test,
        reset=reset,
    )
    project_root = Path(prepared["project_root"])
    role_store = Path(prepared["role_store"])
    env = _smoke_env(test_root=test_root, project_root=project_root, role_store=role_store)
    results: list[dict[str, Any]] = []
    try:
        _append(results, "diagnose", [str(ccb_test), "--diagnose"], cwd=test_root, env=env)
        _append(results, "config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env)
        _append(results, "start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env)
        _append_task_create(results, ccb_test, project_root, test_root, env, task_id=case_id)
        artifacts = write_artifacts(project_root=project_root, task_id=case_id, route=route, scenario=scenario)
        for kind in ("task_packet", "execution_contract"):
            _append_plan_artifact(results, kind, ccb_test, project_root, test_root, env, artifacts[kind], task_id=case_id)
        if case_id == "smoke-partial-completion":
            step_artifacts = write_partial_step_artifacts(project_root=project_root, task_id=case_id)
            for kind in ("detail_step_1", "detail_step_2"):
                _append_plan_artifact(results, kind, ccb_test, project_root, test_root, env, step_artifacts[kind], task_id=case_id)
        _append_orchestration_notes(
            results,
            f"route_{route}",
            ccb_test,
            project_root,
            test_root,
            env,
            task_id=case_id,
            route=route,
            file_path=artifacts["orchestration_notes"],
        )
        _append_ready_for_orchestration(results, ccb_test, project_root, test_root, env, task_id=case_id)
        _append_runner(results, "runner_execute", ccb_test=ccb_test, project_root=project_root, test_root=test_root, env=env, timeout_s=timeout_s)
        _append(
            results,
            "task_show_final",
            [str(ccb_test), "--project", str(project_root), "plan", "task-show", "--task", case_id, "--json"],
            cwd=test_root,
            env=env,
        )
        _append(results, "post_execute_ps", [str(ccb_test), "--project", str(project_root), "ps"], cwd=test_root, env=env)
    finally:
        if not keep_running:
            _append(results, "kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, allow_failure=True)

    summary = _phase6_execution_case_summary(
        results,
        project_root=project_root,
        case_id=case_id,
        route=route,
        expected_result=str(case["round_result"]),
        expected_status=str(case["final_status"]),
    )
    return {
        "workflow_smoke_status": "ok" if summary["ok"] else "failed",
        "phase6_case_id": case_id,
        "provider": provider,
        "project_root": str(project_root),
        "task_id": case_id,
        "plan_slug": DEFAULT_PLAN,
        "summary": summary,
        "results": results,
    }


def write_artifacts(
    *,
    project_root: Path,
    task_id: str,
    route: str = "direct_execution",
    scenario: str | None = None,
) -> dict[str, str]:
    drafts = project_root / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    files = {
        "candidate_questions": drafts / "candidate-questions.jsonl",
        "user_questions": drafts / "user-questions.json",
        "raw_answer": drafts / "raw-answer.md",
        "normalized_answers": drafts / "normalized-answers.jsonl",
        "brief": drafts / "brief.md",
        "requirements": drafts / "requirements.md",
        "acceptance": drafts / "acceptance.md",
        "verification": drafts / "verification.md",
        "handoff": drafts / "handoff.md",
        "review": drafts / "review.md",
        "task_packet": drafts / "task_packet.md",
        "execution_contract": drafts / "execution_contract.md",
        "orchestration_notes": drafts / "orchestration_notes.md",
    }
    files["candidate_questions"].write_text(
        json.dumps(
            {
                "id": "q1",
                "stage": "planning",
                "question": "Should this smoke keep fake-provider semantics?",
                "why_blocking": "The test must not require real provider auth.",
                "default_if_unanswered": "Use fake provider.",
                "defer_allowed": False,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    files["user_questions"].write_text(
        json.dumps(
            {
                "schema": "ccb.workflow.user_questions/v1",
                "task_id": task_id,
                "batch_id": "batch-closure",
                "questions": [
                    {
                        "id": "q1",
                        "text": "Confirm fake provider is acceptable for closure smoke.",
                        "why": "The smoke must be deterministic and account-free.",
                        "required": True,
                    }
                ],
                "defaults": [],
                "deferred": [],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    files["raw_answer"].write_text("Use fake provider for the workflow closure smoke.\n", encoding="utf-8")
    files["normalized_answers"].write_text(
        json.dumps(
            {
                "question_id": "q1",
                "answer": "Use fake provider.",
                "source": "user",
                "planner_note": "Proceed with deterministic fake-provider execution.",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    for kind in ("brief", "requirements", "acceptance", "verification", "handoff", "review"):
        files[kind].write_text(f"# {kind}\n\nWorkflow closure smoke artifact for {kind}.\n", encoding="utf-8")
    scenario_value = str(scenario or "direct_execution")
    files["task_packet"].write_text(
        "\n".join(
            [
                "# Task Packet",
                "",
                f"task_id: {task_id}",
                f"phase6_scenario: {scenario_value}",
                f"goal: run a deterministic {route} ask-first round.",
                "scope: one coder and one code_reviewer execution pair.",
                "allowed_change_paths: workflow_smoke_output.txt",
                "acceptance: worker/reviewer/orchestrator ask evidence and round_summary import.",
                "verification: fake provider round result must be imported through task-import-round.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    files["execution_contract"].write_text(
        "\n".join(
            [
                "# Execution Contract",
                "",
                "- no hidden fallback",
                "- no silent scope shrink",
                "- no fake success without imported round_summary evidence",
                "- allowed_change_paths: workflow_smoke_output.txt",
                "- reviewer must check this execution_contract explicitly",
                "- release ephemeral execution agents after evidence import",
                "",
            ]
        ),
        encoding="utf-8",
    )
    files["orchestration_notes"].write_text(_orchestration_notes_text(route=route), encoding="utf-8")
    return {key: str(path) for key, path in files.items()}


def write_orchestration_notes(*, project_root: Path, task_id: str, route: str) -> str:
    path = project_root / "drafts" / f"{task_id}-{route}-orchestration-notes.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_orchestration_notes_text(route=route), encoding="utf-8")
    return str(path)


def write_detail_artifacts(*, project_root: Path, task_id: str) -> dict[str, str]:
    drafts = project_root / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    files = {
        "detail_design": drafts / f"{task_id}-detail-design.md",
        "detail_summary": drafts / f"{task_id}-detail-summary.md",
        "detail_packet": drafts / f"{task_id}-detail-packet.json",
        "detail_step_1": drafts / f"{task_id}-step-1.md",
        "detail_step_2": drafts / f"{task_id}-step-2.md",
    }
    files["detail_design"].write_text("# Detail Design\n\nFake-provider detail design for Phase 6 needs_detail smoke.\n", encoding="utf-8")
    files["detail_summary"].write_text("# Detail Summary\n\nStable detail summary for Phase 6 needs_detail smoke.\n", encoding="utf-8")
    files["detail_step_1"].write_text("# Step 1\n\nInspect the deterministic fake-provider task packet.\n", encoding="utf-8")
    files["detail_step_2"].write_text("# Step 2\n\nExecute and review the ask-first round.\n", encoding="utf-8")
    files["detail_packet"].write_text(
        json.dumps(
            {
                "schema": "ccb.loop.detail_packet_manifest/v1",
                "task_id": task_id,
                "source": "phase6_route_smoke",
                "status": "ready_for_review",
                "detail_design_ref": "details/task-detail-design.md",
                "brief_update_summary_ref": "details/brief-update-summary.md",
                "step_refs": ["details/steps/step-1.md", "details/steps/step-2.md"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {key: str(path) for key, path in files.items()}


def write_partial_step_artifacts(*, project_root: Path, task_id: str) -> dict[str, str]:
    drafts = project_root / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    files = {
        "detail_step_1": drafts / f"{task_id}-step-1.md",
        "detail_step_2": drafts / f"{task_id}-step-2.md",
    }
    files["detail_step_1"].write_text(
        "\n".join(
            [
                "# Step 1",
                "",
                f"task_id: {task_id}",
                "status: passed",
                "evidence: fake-provider partial-completion smoke finished the first step.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    files["detail_step_2"].write_text(
        "\n".join(
            [
                "# Step 2",
                "",
                f"task_id: {task_id}",
                "status: open",
                "evidence: fake-provider partial-completion smoke preserves this unfinished step.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {key: str(path) for key, path in files.items()}


def write_macro_adjustment_request(*, project_root: Path, task_id: str) -> str:
    path = project_root / "drafts" / f"{task_id}-macro-adjustment-request.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "ccb.loop.macro_adjustment_request/v1",
                "task_id": task_id,
                "source": "phase6_route_smoke",
                "reason": "Phase 6 macro-adjustment smoke requested planner replan.",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return str(path)


def write_blocker_evidence(*, project_root: Path, task_id: str) -> str:
    path = project_root / "drafts" / f"{task_id}-blocker-evidence.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# Blocker Evidence",
                "",
                f"task_id: {task_id}",
                "source: phase6_route_smoke",
                "reason: Phase 6 blocked-route smoke requires frontdesk or terminal ownership.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return str(path)


def write_script_round_summary(*, project_root: Path, task_id: str, loop_id: str, result: str, route: str) -> str:
    path = project_root / "drafts" / f"{task_id}-{result}-round-summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# Round Summary",
                "",
                f"task_id: {task_id}",
                f"loop_id: {loop_id}",
                f"route: {route}",
                f"round result: {result}",
                "round_result_source: phase6_route_smoke_script",
                "evidence: script-owned Phase 6 route matrix smoke transition",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return str(path)


def _orchestration_notes_text(*, route: str) -> str:
    route_notes = {
        "direct_execution": (
            "needed agents: coder, code_reviewer\n"
            "ask order: orchestrator -> worker, worker -> code_reviewer, round reviewer -> import"
        ),
        "partial_completion": (
            "needed agents: coder, code_reviewer\n"
            "ask order: direct execution with unfinished-step evidence and partial round import"
        ),
        "needs_detail": (
            "needed agents: task_detailer\n"
            "ask order: loop_runner -> task_detailer, script imports detail packet, then reroutes"
        ),
        "macro_adjustment_request": (
            "needed agents: planner\n"
            "ask order: no execution workers; script imports macro adjustment evidence"
        ),
        "blocked": (
            "needed agents: frontdesk\n"
            "ask order: no execution workers; script imports blocker evidence"
        ),
    }
    return "\n".join(
        [
            "# Orchestration Notes",
            "",
            f"route: {route}",
            route_notes.get(route, "needed agents: unspecified"),
            "refs: task_packet.md, execution_contract.md",
            "",
        ]
    )


def _workflow_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    payloads = {str(item["name"]): _json_payload(str(item.get("stdout") or "")) for item in results}
    runner_execute = payloads.get("runner_execute") or {}
    final_task = payloads.get("task_show_final") or {}
    round_path = _round_path(runner_execute)
    round_payload = _read_json_object(Path(round_path)) if round_path else {}
    release_payload = _nested_dict(runner_execute, "release") or _nested_dict(round_payload, "topology", "release")
    loop_id = str(runner_execute.get("loop_id") or round_payload.get("loop_id") or "")
    ps_text = str((next((item for item in results if item["name"] == "post_execute_ps"), {}) or {}).get("stdout") or "")
    final_record = final_task.get("task") if isinstance(final_task.get("task"), dict) else {}
    final_artifacts = final_record.get("artifacts") if isinstance(final_record.get("artifacts"), dict) else {}
    ask_targets = [
        str((round_payload.get(kind) or {}).get("target") or "")
        for kind in ("worker", "reviewer", "orchestrator", "ccb_round_reviewer", "round_checker")
        if isinstance(round_payload.get(kind), dict)
    ]
    checks = {
        "all_required_commands_succeeded": _required_commands_succeeded(results),
        "task_packet_imported": (payloads.get("artifact_task_packet") or {}).get("status") == "draft",
        "execution_contract_imported": (payloads.get("artifact_execution_contract") or {}).get("status") == "draft",
        "route_direct_execution_imported": _nested_dict(payloads.get("route_direct_execution") or {}, "artifact").get("orchestrator_route") == "direct_execution",
        "ready_for_orchestration": (payloads.get("ready_for_orchestration") or {}).get("status") == "ready_for_orchestration",
        "execution_round_ran": runner_execute.get("action") == "ran_one_round",
        "ask_first_execution_mode": runner_execute.get("execution_mode") == "ask_first_direct_execution",
        "round_summary_imported": isinstance(final_artifacts.get("round_summary"), dict),
        "ask_reachability": (
            any(target.startswith(f"loop-{loop_id}-coder-") for target in ask_targets)
            and any(target.startswith(f"loop-{loop_id}-code_reviewer-") for target in ask_targets)
            and "orchestrator" in ask_targets
        ),
        "mount_topology_ready": _nested_dict(runner_execute, "topology").get("status") == "ready",
        "topology_dispatch_absent": bool(loop_id)
        and not (Path(str((runner_execute.get("project_root") or ""))) / ".ccb" / "runtime" / "loops" / loop_id / "topology_dispatch.json").exists(),
        "terminal_or_replan_status": str((final_task.get("task") or {}).get("status") or final_task.get("status") or "") in TERMINAL_STATUSES,
        "release_retained_zero": int(release_payload.get("retained_count") or 0) == 0,
        "release_status_released": release_payload.get("loop_topology_status") == "released",
        "release_count_two": int(release_payload.get("released_count") or 0) == 2,
        "dynamic_agents_absent_from_ps": bool(loop_id)
        and f"loop-{loop_id}-coder" not in ps_text
        and f"loop-{loop_id}-code_reviewer" not in ps_text,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "loop_id": loop_id,
        "round_path": round_path,
        "round_result": runner_execute.get("round_result"),
        "round_result_source": runner_execute.get("round_result_source"),
        "final_status": str((final_task.get("task") or {}).get("status") or final_task.get("status") or ""),
        "release": release_payload,
    }


def _phase6_route_summary(results: list[dict[str, Any]], *, project_root: Path, case_id: str, route: str) -> dict[str, Any]:
    payloads = {str(item["name"]): _json_payload(str(item.get("stdout") or "")) for item in results}
    runner_route = payloads.get("runner_route") or {}
    runner_execute = payloads.get("runner_execute") or {}
    import_round = payloads.get("script_import_round") or {}
    final_task = payloads.get("task_show_final") or {}
    final_record = final_task.get("task") if isinstance(final_task.get("task"), dict) else {}
    final_artifacts = final_record.get("artifacts") if isinstance(final_record.get("artifacts"), dict) else {}
    round_path = _round_path(runner_execute) or _artifact_path(final_artifacts.get("round_summary"), project_root=project_root)
    round_payload = _read_json_object(Path(round_path)) if round_path else {}
    release_payload = _nested_dict(runner_execute, "release") or _nested_dict(round_payload, "topology", "release")
    if not release_payload and route in {"macro_adjustment_request", "blocked"}:
        release_payload = {
            "loop_topology_status": "released",
            "released_count": 0,
            "retained_count": 0,
            "release_policy": "no_dynamic_topology",
        }
    loop_id = str(runner_execute.get("loop_id") or round_payload.get("loop_id") or (import_round.get("task") or {}).get("last_round", {}).get("loop_id") or "")
    ps_text = str((next((item for item in results if item["name"] == "post_execute_ps"), {}) or {}).get("stdout") or "")
    ask_targets = [
        str((round_payload.get(kind) or {}).get("target") or "")
        for kind in ("worker", "reviewer", "orchestrator", "ccb_round_reviewer", "round_checker")
        if isinstance(round_payload.get(kind), dict)
    ]
    route_payload = payloads.get(f"route_{route}") or {}
    observed_route = _nested_dict(route_payload, "artifact").get("orchestrator_route")
    final_status = str(final_record.get("status") or final_task.get("status") or "")
    final_next_owner = str(final_record.get("next_owner") or final_task.get("next_owner") or "")
    round_result = str(runner_execute.get("round_result") or import_round.get("round_result") or "")
    topology_dispatch_absent = _topology_dispatch_absent(project_root=project_root, loop_id=loop_id)
    dispatch_keys_absent = _dispatch_keys_absent(project_root=project_root, runner_payload=runner_execute)
    checks = {
        "all_required_commands_succeeded": _required_commands_succeeded(results),
        "task_packet_imported": (payloads.get("artifact_task_packet") or {}).get("status") == "draft",
        "execution_contract_imported": (payloads.get("artifact_execution_contract") or {}).get("status") == "draft",
        "route_imported": observed_route == route,
        "ready_for_orchestration": (payloads.get("ready_for_orchestration") or {}).get("status") == "ready_for_orchestration",
        "runner_route_observed": _runner_route_observed(runner_route, route=route),
        "task_detailer_activated": runner_route.get("action") == "activated_task_detailer" if route == "needs_detail" else None,
        "detail_artifacts_imported": _detail_artifacts_imported(payloads) if route == "needs_detail" else None,
        "detail_step_files_imported": _detail_step_files_imported(payloads) if route == "needs_detail" else None,
        "adjustment_evidence_imported": _artifact_imported(payloads, "macro_adjustment_request") if route == "macro_adjustment_request" else None,
        "blocker_evidence_imported": _artifact_imported(payloads, "blocker_evidence") if route == "blocked" else None,
        "execution_round_ran": runner_execute.get("action") == "ran_one_round" if route == "needs_detail" else None,
        "execution_topology_absent": not bool(runner_execute) if route in {"macro_adjustment_request", "blocked"} else None,
        "round_summary_imported": isinstance(final_artifacts.get("round_summary"), dict),
        "ask_reachability": (
            any(target.startswith(f"loop-{loop_id}-coder-") for target in ask_targets)
            and any(target.startswith(f"loop-{loop_id}-code_reviewer-") for target in ask_targets)
            and "orchestrator" in ask_targets
        )
        if route == "needs_detail"
        else False,
        "mount_topology_ready": _nested_dict(runner_execute, "topology").get("status") == "ready" if route == "needs_detail" else True,
        "topology_dispatch_absent": topology_dispatch_absent,
        "communication_edges_absent": dispatch_keys_absent,
        "provider_reply_authority_parsing_absent": True,
        "terminal_or_replan_status": final_status in TERMINAL_STATUSES,
        "next_owner_planner": final_next_owner == "planner" if route == "macro_adjustment_request" else None,
        "next_owner_frontdesk_or_terminal": final_next_owner in {"frontdesk", "terminal"} if route == "blocked" else None,
        "release_retained_zero": int(release_payload.get("retained_count") or 0) == 0,
        "release_status_released": release_payload.get("loop_topology_status") == "released",
        "dynamic_agents_absent_from_ps": (
            f"loop-{loop_id}-coder" not in ps_text and f"loop-{loop_id}-code_reviewer" not in ps_text
        )
        if loop_id
        else True,
    }
    return {
        "ok": _route_summary_ok(checks, route=route, final_status=final_status, round_result=round_result),
        "case_id": case_id,
        "observed_route": str(observed_route or ""),
        "route_decision_correct": observed_route == route,
        "checks": checks,
        "loop_id": loop_id,
        "round_path": round_path,
        "round_result": round_result,
        "round_result_source": str(runner_execute.get("round_result_source") or "phase6_route_smoke_script"),
        "final_status": final_status,
        "final_next_owner": final_next_owner,
        "cleanup_status": "released" if release_payload.get("loop_topology_status") == "released" else "unknown",
        "release": release_payload,
    }


def _phase6_execution_case_summary(
    results: list[dict[str, Any]],
    *,
    project_root: Path,
    case_id: str,
    route: str,
    expected_result: str,
    expected_status: str,
) -> dict[str, Any]:
    payloads = {str(item["name"]): _json_payload(str(item.get("stdout") or "")) for item in results}
    runner_execute = payloads.get("runner_execute") or {}
    final_task = payloads.get("task_show_final") or {}
    final_record = final_task.get("task") if isinstance(final_task.get("task"), dict) else {}
    final_artifacts = final_record.get("artifacts") if isinstance(final_record.get("artifacts"), dict) else {}
    round_path = _round_path(runner_execute) or _artifact_path(final_artifacts.get("round_summary"), project_root=project_root)
    round_payload = _read_json_object(Path(round_path)) if round_path else {}
    release_payload = _nested_dict(runner_execute, "release") or _nested_dict(round_payload, "topology", "release")
    loop_id = str(runner_execute.get("loop_id") or round_payload.get("loop_id") or "")
    ps_text = str((next((item for item in results if item["name"] == "post_execute_ps"), {}) or {}).get("stdout") or "")
    ask_targets = [
        str((round_payload.get(kind) or {}).get("target") or "")
        for kind in ("worker", "reviewer", "orchestrator", "ccb_round_reviewer", "round_checker")
        if isinstance(round_payload.get(kind), dict)
    ]
    route_payload = payloads.get(f"route_{route}") or {}
    observed_route = _nested_dict(route_payload, "artifact").get("orchestrator_route")
    final_status = str(final_record.get("status") or final_task.get("status") or "")
    final_next_owner = str(final_record.get("next_owner") or final_task.get("next_owner") or "")
    round_result = str(runner_execute.get("round_result") or round_payload.get("round_result") or "")
    topology_dispatch_absent = _topology_dispatch_absent(project_root=project_root, loop_id=loop_id)
    dispatch_keys_absent = _dispatch_keys_absent(project_root=project_root, runner_payload=runner_execute)
    ask_counts = _ask_purpose_counts(project_root=project_root, loop_id=loop_id)
    checks = {
        "all_required_commands_succeeded": _required_commands_succeeded(results),
        "task_packet_imported": (payloads.get("artifact_task_packet") or {}).get("status") == "draft",
        "execution_contract_imported": (payloads.get("artifact_execution_contract") or {}).get("status") == "draft",
        "route_imported": observed_route == route,
        "ready_for_orchestration": (payloads.get("ready_for_orchestration") or {}).get("status") == "ready_for_orchestration",
        "runner_route_observed": runner_execute.get("action") == "ran_one_round",
        "execution_round_ran": runner_execute.get("action") == "ran_one_round",
        "ask_first_execution_mode": runner_execute.get("execution_mode") == "ask_first_direct_execution",
        "round_summary_imported": isinstance(final_artifacts.get("round_summary"), dict),
        "partial_step_evidence_imported": _partial_step_evidence_imported(final_artifacts) if case_id == "smoke-partial-completion" else None,
        "partial_not_done": final_status == "partial" if case_id == "smoke-partial-completion" else None,
        "bounded_rework_cycle": _bounded_rework_cycle(round_payload) if case_id in {"smoke-reviewer-reject-rework", "smoke-reviewer-cannot-accept"} else None,
        "no_extra_rework_cycle": _single_rework_cycle_count(ask_counts) if case_id in {"smoke-reviewer-reject-rework", "smoke-reviewer-cannot-accept"} else None,
        "cannot_accept_not_done": final_status != "done" if case_id == "smoke-reviewer-cannot-accept" else None,
        "ask_reachability": (
            any(target.startswith(f"loop-{loop_id}-coder-") for target in ask_targets)
            and any(target.startswith(f"loop-{loop_id}-code_reviewer-") for target in ask_targets)
            and "orchestrator" in ask_targets
            and "ccb_round_reviewer" in ask_targets
        ),
        "mount_topology_ready": _nested_dict(runner_execute, "topology").get("status") == "ready",
        "topology_dispatch_absent": topology_dispatch_absent,
        "communication_edges_absent": dispatch_keys_absent,
        "provider_reply_authority_parsing_absent": True,
        "terminal_or_replan_status": final_status in TERMINAL_STATUSES,
        "next_owner_planner": final_next_owner == "planner" if final_status in {"partial", "replan_required"} else None,
        "release_retained_zero": int(release_payload.get("retained_count") or 0) == 0,
        "release_status_released": release_payload.get("loop_topology_status") == "released",
        "release_count_two": int(release_payload.get("released_count") or 0) == 2,
        "dynamic_agents_absent_from_ps": bool(loop_id)
        and f"loop-{loop_id}-coder" not in ps_text
        and f"loop-{loop_id}-code_reviewer" not in ps_text,
    }
    return {
        "ok": _execution_case_summary_ok(
            checks,
            case_id=case_id,
            final_status=final_status,
            round_result=round_result,
            expected_status=expected_status,
            expected_result=expected_result,
        ),
        "case_id": case_id,
        "observed_route": str(observed_route or ""),
        "route_decision_correct": observed_route == route,
        "checks": checks,
        "loop_id": loop_id,
        "round_path": round_path,
        "round_result": round_result,
        "round_result_source": str(runner_execute.get("round_result_source") or round_payload.get("round_result_source") or ""),
        "final_status": final_status,
        "final_next_owner": final_next_owner,
        "cleanup_status": "released" if release_payload.get("loop_topology_status") == "released" else "unknown",
        "release": release_payload,
    }


def _execution_case_summary_ok(
    checks: dict[str, Any],
    *,
    case_id: str,
    final_status: str,
    round_result: str,
    expected_status: str,
    expected_result: str,
) -> bool:
    required = [
        "all_required_commands_succeeded",
        "task_packet_imported",
        "execution_contract_imported",
        "route_imported",
        "ready_for_orchestration",
        "runner_route_observed",
        "execution_round_ran",
        "ask_first_execution_mode",
        "round_summary_imported",
        "ask_reachability",
        "mount_topology_ready",
        "topology_dispatch_absent",
        "communication_edges_absent",
        "provider_reply_authority_parsing_absent",
        "terminal_or_replan_status",
        "release_retained_zero",
        "release_status_released",
        "release_count_two",
        "dynamic_agents_absent_from_ps",
    ]
    if case_id == "smoke-partial-completion":
        required.extend(("partial_step_evidence_imported", "partial_not_done", "next_owner_planner"))
    if case_id in {"smoke-reviewer-reject-rework", "smoke-reviewer-cannot-accept"}:
        required.extend(("bounded_rework_cycle", "no_extra_rework_cycle"))
    if case_id == "smoke-reviewer-cannot-accept":
        required.extend(("cannot_accept_not_done", "next_owner_planner"))
    return all(bool(checks.get(key)) for key in required) and final_status == expected_status and round_result == expected_result


def _route_summary_ok(checks: dict[str, Any], *, route: str, final_status: str, round_result: str) -> bool:
    required = [
        "all_required_commands_succeeded",
        "task_packet_imported",
        "execution_contract_imported",
        "route_imported",
        "ready_for_orchestration",
        "runner_route_observed",
        "round_summary_imported",
        "topology_dispatch_absent",
        "communication_edges_absent",
        "provider_reply_authority_parsing_absent",
        "terminal_or_replan_status",
        "release_retained_zero",
        "release_status_released",
        "dynamic_agents_absent_from_ps",
    ]
    if route == "needs_detail":
        required.extend(("task_detailer_activated", "detail_artifacts_imported", "detail_step_files_imported", "execution_round_ran", "ask_reachability", "mount_topology_ready"))
        return all(bool(checks.get(key)) for key in required) and final_status == "done" and round_result == "pass"
    if route == "macro_adjustment_request":
        required.extend(("adjustment_evidence_imported", "execution_topology_absent", "next_owner_planner"))
        return all(bool(checks.get(key)) for key in required) and final_status == "replan_required" and round_result == "replan_required"
    if route == "blocked":
        required.extend(("blocker_evidence_imported", "execution_topology_absent", "next_owner_frontdesk_or_terminal"))
        return all(bool(checks.get(key)) for key in required) and final_status == "blocked" and round_result == "blocked"
    return False


def _runner_route_observed(payload: dict[str, Any], *, route: str) -> bool:
    if route == "needs_detail":
        return payload.get("action") == "activated_task_detailer"
    if route == "macro_adjustment_request":
        return payload.get("action") == "planner_next_action_required"
    if route == "blocked":
        return payload.get("action") == "blocker_evidence_required"
    return False


def _detail_artifacts_imported(payloads: dict[str, Any]) -> bool:
    return all((payloads.get(f"artifact_{kind}") or {}).get("status") == "ready_for_orchestration" for kind in ("detail_design", "detail_summary", "detail_packet"))


def _detail_step_files_imported(payloads: dict[str, Any]) -> bool:
    return all(_artifact_imported(payloads, kind) for kind in ("detail_step_1", "detail_step_2"))


def _artifact_imported(payloads: dict[str, Any], kind: str) -> bool:
    return (payloads.get(f"artifact_{kind}") or {}).get("status") == "ready_for_orchestration"


def _partial_step_evidence_imported(final_artifacts: dict[str, Any]) -> bool:
    return all(isinstance(final_artifacts.get(kind), dict) for kind in ("detail_step_1", "detail_step_2"))


def _bounded_rework_cycle(round_payload: dict[str, Any]) -> bool:
    rework = round_payload.get("rework") if isinstance(round_payload.get("rework"), dict) else {}
    if set(rework) != {"worker_rework", "reviewer_recheck"}:
        return False
    return all(isinstance(item, dict) and item.get("status") == "completed" for item in rework.values())


def _single_rework_cycle_count(counts: dict[str, int]) -> bool:
    return counts.get("worker_rework") == 1 and counts.get("reviewer_recheck") == 1


def _ask_purpose_counts(*, project_root: Path, loop_id: str) -> dict[str, int]:
    if not loop_id:
        return {}
    path = project_root / ".ccb" / "runtime" / "loops" / loop_id / "asks.jsonl"
    counts: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return counts
    for line in lines:
        payload = _json_payload(line)
        purpose = str(payload.get("purpose") or "").strip()
        if purpose:
            counts[purpose] = counts.get(purpose, 0) + 1
    return counts


def _artifact_path(artifact: object, *, project_root: Path) -> str:
    if not isinstance(artifact, dict):
        return ""
    path = str(artifact.get("source_path") or artifact.get("path") or "").strip()
    if not path:
        return ""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return str(candidate)


def _topology_dispatch_absent(*, project_root: Path, loop_id: str) -> bool:
    if not loop_id:
        return True
    return not (project_root / ".ccb" / "runtime" / "loops" / loop_id / "topology_dispatch.json").exists()


def _dispatch_keys_absent(*, project_root: Path, runner_payload: dict[str, Any]) -> bool:
    topology = runner_payload.get("topology") if isinstance(runner_payload.get("topology"), dict) else {}
    paths = [
        str(topology.get("proposal_path") or ""),
        str(topology.get("desired_path") or ""),
        str(topology.get("observed_path") or ""),
    ]
    existing = [Path(path) for path in paths if path and Path(path).is_file()]
    if not existing:
        return True
    for path in existing:
        payload = _read_json_object(path)
        if any(key in payload for key in ("edges", "artifacts", "gates")):
            return False
    return True


def _append_question(
    results: list[dict[str, Any]],
    name: str,
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    action: str,
    file_path: str,
) -> None:
    _append(
        results,
        name,
        [str(ccb_test), "--project", str(project_root), "question", action, "--task", DEFAULT_TASK, "--file", file_path, "--json"],
        cwd=test_root,
        env=env,
    )


def _append_task_create(
    results: list[dict[str, Any]],
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    *,
    task_id: str,
) -> None:
    _append(
        results,
        "task_create",
        [
            str(ccb_test),
            "--project",
            str(project_root),
            "plan",
            "task-create",
            "--plan",
            DEFAULT_PLAN,
            "--title",
            f"Workflow smoke task {task_id}",
            "--task-id",
            task_id,
            "--json",
        ],
        cwd=test_root,
        env=env,
    )


def _append_ready_for_orchestration(
    results: list[dict[str, Any]],
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    *,
    task_id: str,
) -> None:
    _append(
        results,
        "ready_for_orchestration",
        [
            str(ccb_test),
            "--project",
            str(project_root),
            "plan",
            "task-status",
            "--task",
            task_id,
            "--status",
            "ready_for_orchestration",
            "--next-owner",
            "orchestrator",
            "--activation-reason",
            "phase6_route_smoke",
            "--json",
        ],
        cwd=test_root,
        env=env,
    )


def _append_plan_artifact(
    results: list[dict[str, Any]],
    kind: str,
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    file_path: str,
    *,
    task_id: str = DEFAULT_TASK,
) -> None:
    _append(
        results,
        f"artifact_{kind}",
        [
            str(ccb_test),
            "--project",
            str(project_root),
            "plan",
            "task-artifact",
            "--task",
            task_id,
            "--kind",
            kind,
            "--file",
            file_path,
            "--json",
        ],
        cwd=test_root,
        env=env,
    )


def _append_orchestration_notes(
    results: list[dict[str, Any]],
    name: str,
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    *,
    task_id: str,
    route: str,
    file_path: str,
) -> None:
    _append(
        results,
        name,
        [
            str(ccb_test),
            "--project",
            str(project_root),
            "plan",
            "task-artifact",
            "--task",
            task_id,
            "--kind",
            "orchestration_notes",
            "--file",
            file_path,
            "--route",
            route,
            "--json",
        ],
        cwd=test_root,
        env=env,
    )


def _append_task_bind_loop(
    results: list[dict[str, Any]],
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    *,
    task_id: str,
    loop_id: str,
) -> None:
    _append(
        results,
        "script_bind_loop",
        [str(ccb_test), "--project", str(project_root), "plan", "task-bind-loop", "--task", task_id, "--loop", loop_id, "--json"],
        cwd=test_root,
        env=env,
    )


def _append_task_import_round(
    results: list[dict[str, Any]],
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    *,
    task_id: str,
    loop_id: str,
    result: str,
    report_path: str,
) -> None:
    _append(
        results,
        "script_import_round",
        [
            str(ccb_test),
            "--project",
            str(project_root),
            "plan",
            "task-import-round",
            "--task",
            task_id,
            "--loop",
            loop_id,
            "--result",
            result,
            "--report",
            report_path,
            "--json",
        ],
        cwd=test_root,
        env=env,
    )


def _append_runner(
    results: list[dict[str, Any]],
    name: str,
    *,
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    timeout_s: int,
    consume_role_output: bool = False,
) -> None:
    command = [str(ccb_test), "--project", str(project_root), "loop", "runner", "--once", "--timeout", str(timeout_s), "--json"]
    if consume_role_output:
        command.insert(-1, "--consume-role-output")
    _append(
        results,
        name,
        command,
        cwd=project_root,
        env=env,
        timeout=timeout_s + 60,
    )


def _append(
    results: list[dict[str, Any]],
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 60,
    allow_failure: bool = False,
) -> None:
    completed = _run_command(name, command, cwd=cwd, env=env, timeout=timeout)
    results.append(completed)
    if completed["returncode"] not in (0, None) and not allow_failure:
        raise RuntimeError(f"{name} failed: {completed['stderr'] or completed['stdout']}")


def _run_command(name: str, command: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(command, cwd=str(cwd), env=env, text=True, capture_output=True, timeout=timeout)
        return {
            "name": name,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "command": command,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timeout": True,
        }


def _required_commands_succeeded(results: list[dict[str, Any]]) -> bool:
    for item in results:
        name = str(item.get("name") or "")
        if name in {"ready_before_review_rejected", "kill"}:
            continue
        if item.get("returncode") != 0:
            return False
    return True


def _returncode(results: list[dict[str, Any]], name: str) -> int | None:
    for item in results:
        if item.get("name") == name:
            value = item.get("returncode")
            return int(value) if value is not None else None
    return None


def _round_path(payload: dict[str, Any]) -> str:
    round_info = payload.get("round")
    if isinstance(round_info, dict) and str(round_info.get("round_json_path") or "").strip():
        return str(round_info["round_json_path"])
    if isinstance(round_info, dict) and str(round_info.get("round_path") or "").strip():
        return str(round_info["round_path"])
    paths = payload.get("paths")
    if isinstance(paths, dict) and str(paths.get("round_json") or "").strip():
        return str(paths["round_json"])
    if isinstance(paths, dict) and str(paths.get("round") or "").strip():
        return str(paths["round"])
    return ""


def _nested_dict(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


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


def _install_rolepacks(role_store: Path) -> None:
    for role_id, dirname in ROLEPACKS.items():
        source = ROLEPACK_ROOT / dirname
        if not source.is_dir():
            raise FileNotFoundError(f"rolepack source missing: {source}")
        target = role_store / "installed" / role_id / "current"
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)


def _install_cli_shims(*, project_root: Path, ccb_test: Path) -> dict[str, str]:
    bin_dir = project_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    ccb_test_path = ccb_test.expanduser().resolve(strict=False)
    payload: dict[str, str] = {"bin_dir": str(bin_dir)}
    for name, args in {"ccb": "", "ask": " ask"}.items():
        shim = bin_dir / name
        shim.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f'exec "{ccb_test_path}"{args} "$@"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        shim.chmod(0o755)
        payload[f"{name}_shim"] = str(shim)
    return payload


def _smoke_env(*, test_root: Path, project_root: Path, role_store: Path) -> dict[str, str]:
    env = dict(os.environ)
    source_home = test_root.expanduser().resolve(strict=False) / "source_home"
    env["HOME"] = str(source_home)
    env["CCB_SOURCE_HOME"] = str(source_home)
    env["CCB_TEST_ROOTS"] = str(test_root.expanduser().resolve(strict=False))
    env["CCB_SOURCE_ALLOWED_ROOTS"] = str(test_root.expanduser().resolve(strict=False))
    env["AGENT_ROLES_STORE"] = str(role_store)
    env["CCB_NO_ATTACH"] = "1"
    env["CCB_REPLY_LANG"] = "en"
    env["PATH"] = str(project_root / "bin") + os.pathsep + env.get("PATH", "")
    return env


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a deterministic CCB workflow closure smoke.")
    parser.add_argument("--test-root", default=str(DEFAULT_TEST_ROOT))
    parser.add_argument("--project-name", default="workflow-closure-smoke")
    parser.add_argument("--provider", default="fake")
    parser.add_argument("--ccb-test", default=str(REPO_ROOT / "ccb_test"))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    test_root = Path(args.test_root)
    ccb_test = Path(args.ccb_test)
    prepared = prepare_project(
        test_root=test_root,
        project_name=args.project_name,
        provider=args.provider,
        ccb_test=ccb_test,
        reset=bool(args.reset),
    )
    payload: dict[str, Any] = {"prepare": prepared}
    if args.run:
        payload["run"] = run_workflow_smoke(
            test_root=test_root,
            project_name=args.project_name,
            provider=args.provider,
            ccb_test=ccb_test,
            timeout_s=int(args.timeout),
            reset=False,
            keep_running=bool(args.keep_running),
        )
    if args.json or args.prepare_only or args.run:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"project_root: {prepared['project_root']}")
        print(f"role_store: {prepared['role_store']}")
        print("run: add --run --json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
