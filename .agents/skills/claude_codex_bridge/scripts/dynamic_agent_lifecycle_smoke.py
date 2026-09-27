#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dynamic_layout_smoke as layout_smoke  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_ROOT = Path(os.environ.get("CCB_DYNAMIC_AGENT_LIFECYCLE_SMOKE_TEST_ROOT", "/home/bfly/yunwei/test_ccb2"))
DEFAULT_CCB_TEST = REPO_ROOT / "ccb_test"
DEFAULT_COMMAND_TIMEOUT_S = int(os.environ.get("CCB_DYNAMIC_AGENT_LIFECYCLE_SMOKE_COMMAND_TIMEOUT_S", "60"))
REAL_RUN_ENV = "CCB_DYNAMIC_AGENT_LIFECYCLE_SMOKE_RUN_REAL"


def build_lifecycle_config(*, provider: str = "fake") -> str:
    return "\n".join(
        [
            "version = 2",
            'entry_window = "main"',
            "",
            "[windows]",
            f'main = "frontdesk:{provider}"',
            f'plan-orchestrate = "planner:{provider}"',
            "",
        ]
    )


def prepare_lifecycle_project(
    *,
    test_root: Path,
    project_name: str,
    provider: str = "fake",
    reset: bool = False,
) -> dict[str, str]:
    project_root = layout_smoke._project_root(test_root, project_name)
    if reset and project_root.exists():
        shutil.rmtree(project_root)
    (project_root / ".ccb").mkdir(parents=True, exist_ok=True)
    (project_root / ".ccb" / "ccb.config").write_text(build_lifecycle_config(provider=provider), encoding="utf-8")
    role_store = project_root / "roles"
    layout_smoke._write_minimal_role(role_store, "agentroles.ccb_planner", default_agent_name="planner")
    layout_smoke._write_minimal_role(role_store, "agentroles.code_reviewer", default_agent_name="code_reviewer")
    return {"project_root": str(project_root), "role_store": str(role_store)}


def run_lifecycle_policy_smoke(
    *,
    test_root: Path,
    project_name: str,
    ccb_test: Path,
    provider: str = "fake",
    provider_home_mode: str = "source-home",
    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
    reset: bool = False,
    keep_running: bool = False,
) -> dict[str, Any]:
    test_root = test_root.expanduser().resolve(strict=False)
    test_root.mkdir(parents=True, exist_ok=True)
    preflight_payload = layout_smoke.preflight(
        test_root=test_root,
        provider=provider,
        ccb_test=ccb_test,
        provider_home_mode=provider_home_mode,
    )
    preflight_payload["checks"]["lifecycle_real_run_opt_in"] = os.environ.get(REAL_RUN_ENV) == "1"
    if provider != "fake" and os.environ.get(REAL_RUN_ENV) != "1":
        raise RuntimeError(f"real provider dynamic agent lifecycle smoke requires {REAL_RUN_ENV}=1")

    prepared = prepare_lifecycle_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    provider_home = layout_smoke._provider_home(test_root=test_root, mode=provider_home_mode)
    provider_home.mkdir(parents=True, exist_ok=True)
    env = layout_smoke._env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    commands: list[dict[str, Any]] = []
    expected_failure_commands: list[dict[str, Any]] = []
    try:
        commands.append(layout_smoke._run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(layout_smoke._run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        add_planner = layout_smoke._run_json(
            "add_planner_helper",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "add",
                f"planner_helper:{provider}",
                "--role",
                "agentroles.ccb_planner",
                "--window-class",
                "plan-orchestrate",
                "--hidden",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(add_planner)
        show_planner_added = layout_smoke._run_json(
            "show_planner_helper_after_add",
            [str(ccb_test), "--project", str(project_root), "agent", "show", "planner_helper", "--json"],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(show_planner_added)
        planner_ask_1 = layout_smoke._run(
            "ask_planner_helper_before_park",
            [str(ccb_test), "--project", str(project_root), "ask", "planner_helper"],
            cwd=test_root,
            env=env,
            input_text="dynamic lifecycle smoke ping planner before park\n",
            timeout=command_timeout_s,
        )
        commands.append(planner_ask_1)
        commands.extend(
            layout_smoke._watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(planner_ask_1,),
                timeout=command_timeout_s,
            )
        )
        park_planner = layout_smoke._run_json(
            "release_planner_helper_auto",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "release",
                "planner_helper",
                "--idle-only",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(park_planner)
        show_planner_parked = layout_smoke._run_json(
            "show_planner_helper_after_park",
            [str(ccb_test), "--project", str(project_root), "agent", "show", "planner_helper", "--json"],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(show_planner_parked)
        parked_ask = layout_smoke._run(
            "ask_planner_helper_while_parked",
            [str(ccb_test), "--project", str(project_root), "ask", "planner_helper"],
            cwd=test_root,
            env=env,
            input_text="dynamic lifecycle smoke should be rejected while parked\n",
            timeout=command_timeout_s,
        )
        commands.append(parked_ask)
        expected_failure_commands.append(parked_ask)
        resume_planner = layout_smoke._run_json(
            "resume_planner_helper_hidden",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "resume",
                "planner_helper",
                "--hidden",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(resume_planner)
        show_planner_resumed = layout_smoke._run_json(
            "show_planner_helper_after_resume",
            [str(ccb_test), "--project", str(project_root), "agent", "show", "planner_helper", "--json"],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(show_planner_resumed)
        planner_ask_2 = layout_smoke._run(
            "ask_planner_helper_after_resume",
            [str(ccb_test), "--project", str(project_root), "ask", "planner_helper"],
            cwd=test_root,
            env=env,
            input_text="dynamic lifecycle smoke ping planner after resume\n",
            timeout=command_timeout_s,
        )
        commands.append(planner_ask_2)
        commands.extend(
            layout_smoke._watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(planner_ask_2,),
                timeout=command_timeout_s,
            )
        )
        add_reviewer = layout_smoke._run_json(
            "add_reviewer_helper",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "add",
                f"reviewer_helper:{provider}",
                "--role",
                "agentroles.code_reviewer",
                "--window-class",
                "plan-orchestrate",
                "--hidden",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(add_reviewer)
        reviewer_ask = layout_smoke._run(
            "ask_reviewer_helper",
            [str(ccb_test), "--project", str(project_root), "ask", "reviewer_helper"],
            cwd=test_root,
            env=env,
            input_text="dynamic lifecycle smoke ping reviewer before release\n",
            timeout=command_timeout_s,
        )
        commands.append(reviewer_ask)
        commands.extend(
            layout_smoke._watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(reviewer_ask,),
                timeout=command_timeout_s,
            )
        )
        release_reviewer = layout_smoke._run_json(
            "release_reviewer_helper_auto",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "release",
                "reviewer_helper",
                "--idle-only",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(release_reviewer)
        layout_after_reviewer = layout_smoke._run_json(
            "layout_after_reviewer_release",
            [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(layout_after_reviewer)
        cleanup_planner = layout_smoke._run_json(
            "cleanup_planner_helper_unload",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "remove",
                "planner_helper",
                "--policy",
                "unload",
                "--idle-only",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(cleanup_planner)
        layout_after_cleanup = layout_smoke._run_json(
            "layout_after_cleanup",
            [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(layout_after_cleanup)

        add_planner_payload = layout_smoke._payload(add_planner)
        show_added_payload = layout_smoke._payload(show_planner_added)
        park_payload = layout_smoke._payload(park_planner)
        parked_show_payload = layout_smoke._payload(show_planner_parked)
        resume_payload = layout_smoke._payload(resume_planner)
        resumed_show_payload = layout_smoke._payload(show_planner_resumed)
        add_reviewer_payload = layout_smoke._payload(add_reviewer)
        release_reviewer_payload = layout_smoke._payload(release_reviewer)
        cleanup_payload = layout_smoke._payload(cleanup_planner)
        planner_pane = str(add_planner_payload.get("pane_id") or show_added_payload.get("pane_id") or "")
        reviewer_pane = str(add_reviewer_payload.get("pane_id") or "")
        successful_commands = [item for item in commands if item not in expected_failure_commands]
        checks = {
            "planner_role_class_long_lived": add_planner_payload.get("role_class") == "long_lived_interactive",
            "planner_add_agent": add_planner_payload.get("apply", {}).get("plan_class") == "add_agent",
            "planner_ask_before_park_accepted": layout_smoke._accepted(planner_ask_1),
            "planner_auto_policy_park": park_payload.get("requested_policy") == "auto"
            and park_payload.get("resolved_policy") == "park"
            and park_payload.get("lifecycle_state") == "parked",
            "planner_park_dispatch_disabled": park_payload.get("dispatch_disabled") is True
            and parked_show_payload.get("dispatch_disabled") is True,
            "planner_park_view_only": park_payload.get("apply", {}).get("plan_class") == "view_only_change",
            "planner_pane_preserved_on_park": bool(planner_pane)
            and str(park_payload.get("pane_id") or "") == planner_pane
            and str(parked_show_payload.get("pane_id") or "") == planner_pane,
            "parked_ask_rejected": int(parked_ask.get("returncode") or 0) != 0
            and not layout_smoke._accepted(parked_ask),
            "planner_resume_hidden": resume_payload.get("lifecycle_state") == "hidden"
            and resume_payload.get("dispatch_disabled") is False
            and resumed_show_payload.get("dispatch_disabled") is False,
            "planner_pane_preserved_on_resume": bool(planner_pane)
            and str(resume_payload.get("pane_id") or "") == planner_pane
            and str(resumed_show_payload.get("pane_id") or "") == planner_pane,
            "planner_ask_after_resume_accepted": layout_smoke._accepted(planner_ask_2),
            "reviewer_role_class_short_lived": add_reviewer_payload.get("role_class") == "short_lived_execution",
            "reviewer_add_agent": add_reviewer_payload.get("apply", {}).get("plan_class") == "add_agent",
            "reviewer_ask_accepted": layout_smoke._accepted(reviewer_ask),
            "reviewer_auto_policy_unload": release_reviewer_payload.get("requested_policy") == "auto"
            and release_reviewer_payload.get("resolved_policy") == "unload"
            and release_reviewer_payload.get("lifecycle_state") == "unloaded",
            "reviewer_remove_agent": release_reviewer_payload.get("apply", {}).get("plan_class") == "remove_agent",
            "reviewer_pane_removed": bool(reviewer_pane)
            and release_reviewer_payload.get("apply", {}).get("namespace_removed_agents", {}).get("reviewer_helper") == reviewer_pane,
            "reviewer_released_from_layout": "reviewer_helper" not in {
                agent for agents in layout_smoke._window_agents(layout_after_reviewer).values() for agent in agents
            },
            "planner_cleanup_unloaded": cleanup_payload.get("resolved_policy") == "unload"
            and cleanup_payload.get("lifecycle_state") == "unloaded",
            "layout_clean_after_cleanup": layout_smoke._payload(layout_after_cleanup).get("dynamic_agent_count") == 0
            and layout_smoke._window_agents(layout_after_cleanup) == {
                "main": ["frontdesk"],
                "plan-orchestrate": ["planner"],
            },
            "asks_terminal": layout_smoke._watch_commands_terminal(commands),
        }
        status = "ok" if all(checks.values()) and layout_smoke._all_success(successful_commands) else "failed"
        return {
            "dynamic_agent_lifecycle_smoke_status": status,
            "provider": provider,
            "provider_home_mode": provider_home_mode,
            "preflight": preflight_payload,
            "project_root": str(project_root),
            "checks": checks,
            "commands": commands,
        }
    finally:
        if not keep_running:
            commands.append(layout_smoke._run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def compact_lifecycle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "dynamic_agent_lifecycle_smoke_status": payload.get("dynamic_agent_lifecycle_smoke_status"),
        "provider": payload.get("provider"),
        "provider_home_mode": payload.get("provider_home_mode"),
        "preflight": payload.get("preflight"),
        "project_root": payload.get("project_root"),
        "checks": payload.get("checks"),
        "commands": [
            layout_smoke._compact_command(item)
            for item in payload.get("commands", [])
            if isinstance(item, dict)
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CCB dynamic agent lifecycle policy smoke tests.")
    parser.add_argument("--test-root", type=Path, default=DEFAULT_TEST_ROOT)
    parser.add_argument("--project-name", default="dynamic-agent-lifecycle-smoke")
    parser.add_argument("--ccb-test", type=Path, default=DEFAULT_CCB_TEST)
    parser.add_argument("--provider", default="fake")
    parser.add_argument("--provider-home-mode", choices=("source-home", "real-home"), default="source-home")
    parser.add_argument("--command-timeout", type=int, default=DEFAULT_COMMAND_TIMEOUT_S)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--full-output", action="store_true", help="Print complete command stdout and JSON payloads.")
    args = parser.parse_args(argv)

    payload = run_lifecycle_policy_smoke(
        test_root=args.test_root,
        project_name=args.project_name,
        ccb_test=args.ccb_test,
        provider=args.provider,
        provider_home_mode=args.provider_home_mode,
        command_timeout_s=args.command_timeout,
        reset=args.reset,
        keep_running=args.keep_running,
    )
    output = payload if args.full_output else compact_lifecycle_payload(payload)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if payload.get("dynamic_agent_lifecycle_smoke_status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
