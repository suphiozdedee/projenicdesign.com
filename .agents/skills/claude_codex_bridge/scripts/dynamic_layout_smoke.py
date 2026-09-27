#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_ROOT = Path(os.environ.get("CCB_DYNAMIC_LAYOUT_SMOKE_TEST_ROOT", "/home/bfly/yunwei/test_ccb2"))
DEFAULT_CCB_TEST = REPO_ROOT / "ccb_test"
DEFAULT_COMMAND_TIMEOUT_S = int(os.environ.get("CCB_DYNAMIC_LAYOUT_SMOKE_COMMAND_TIMEOUT_S", "60"))
REAL_RUN_ENV = "CCB_DYNAMIC_LAYOUT_SMOKE_RUN_REAL"
FLOW_NAMES = (
    "multi-node",
    "multi-window-continuous",
    "batch-release",
    "same-window",
    "same-window-continuous",
    "single-agent-window",
    "move-agent",
    "move-shared-source",
    "mixed-move-add",
    "batch-move-window-class",
    "batch-move-execution-node",
    "window-class",
    "arrange-window",
    "window-class-continuous",
    "resolve-preflight",
)
PROVIDER_EXECUTABLES = {
    "codex": "codex",
    "claude": "claude",
    "fake": "fake",
    "gemini": "gemini",
}


def build_multi_node_config(*, provider: str = "fake") -> str:
    return "\n".join(
        [
            "version = 2",
            'entry_window = "main"',
            "",
            "[windows]",
            f'main = "orchestrator:{provider}"',
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
            'workspace_mode = "inplace"',
            "max_instances = 2",
            'reuse = "prefer_idle"',
            "",
            "[loop.role_profiles.code_reviewer]",
            'role = "agentroles.code_reviewer"',
            f'provider = "{provider}"',
            'workspace_mode = "inplace"',
            "max_instances = 2",
            "",
        ]
    )


def build_same_window_config(*, provider: str = "fake") -> str:
    return "\n".join(
        [
            "version = 2",
            'entry_window = "main"',
            "",
            "[windows]",
            f'main = "main:{provider}"',
            "",
        ]
    )


def build_single_agent_window_config(*, provider: str = "fake") -> str:
    return build_same_window_config(provider=provider)


def build_window_class_config(*, provider: str = "fake") -> str:
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


def build_mixed_move_add_initial_config(*, provider: str = "fake") -> str:
    return "\n".join(
        [
            "version = 2",
            'entry_window = "main"',
            "",
            "[windows]",
            f'main = "main:{provider}"',
            f'review = "zeta:{provider}, alpha:{provider}"',
            "",
            "[ui.sidebar]",
            'mode = "every_window"',
            'width = "15%"',
            "bottom_height = 20",
            "",
        ]
    )


def build_mixed_move_add_target_config(*, provider: str = "fake") -> str:
    return "\n".join(
        [
            "version = 2",
            'entry_window = "main"',
            "",
            "[windows]",
            f'main = "main:{provider}"',
            f'archive = "zeta:{provider}, alpha:{provider}, beta:{provider}"',
            "",
            "[ui.sidebar]",
            'mode = "every_window"',
            'width = "15%"',
            "bottom_height = 20",
            "",
        ]
    )


def build_batch_move_window_class_config(*, provider: str = "fake") -> str:
    return "\n".join(
        [
            "version = 2",
            'entry_window = "main"',
            "",
            "[windows]",
            f'main = "frontdesk:{provider}"',
            (
                'plan-orchestrate = "'
                + ", ".join(f"p{index}:{provider}" for index in range(1, 6))
                + '"'
            ),
            "",
        ]
    )


def build_resolve_preflight_config(*, provider: str = "fake", static_provider: str | None = None) -> str:
    filler_provider = static_provider or provider
    return "\n".join(
        [
            "version = 2",
            'entry_window = "main"',
            "",
            "[windows]",
            f'main = "frontdesk:{filler_provider}"',
            (
                'plan-orchestrate = "'
                + ", ".join(f"p{index}:{filler_provider}" for index in range(1, 7))
                + '"'
            ),
            "",
            "[loop.capacity]",
            "enabled = true",
            "max_nodes = 2",
            'default_lifetime = "current_round"',
            'name_template = "loop-{loop_id}-{profile}-{index}"',
            'reuse = "prefer_idle"',
            "",
            "[loop.role_profiles.worker]",
            'role = "agentroles.coder"',
            f'provider = "{provider}"',
            'workspace_mode = "inplace"',
            "max_instances = 1",
            "",
            "[loop.role_profiles.code_reviewer]",
            'role = "agentroles.code_reviewer"',
            f'provider = "{provider}"',
            'workspace_mode = "inplace"',
            "max_instances = 1",
            "",
        ]
    )


def prepare_multi_node_project(*, test_root: Path, project_name: str, provider: str = "fake", reset: bool = False) -> dict[str, str]:
    project_root = _project_root(test_root, project_name)
    if reset and project_root.exists():
        shutil.rmtree(project_root)
    (project_root / ".ccb").mkdir(parents=True, exist_ok=True)
    (project_root / ".ccb" / "ccb.config").write_text(build_multi_node_config(provider=provider), encoding="utf-8")
    role_store = project_root / "roles"
    _write_minimal_role(role_store, "agentroles.coder", default_agent_name="worker")
    _write_minimal_role(role_store, "agentroles.code_reviewer", default_agent_name="code_reviewer")
    return {"project_root": str(project_root), "role_store": str(role_store)}


def prepare_same_window_project(*, test_root: Path, project_name: str, provider: str = "fake", reset: bool = False) -> dict[str, str]:
    project_root = _project_root(test_root, project_name)
    if reset and project_root.exists():
        shutil.rmtree(project_root)
    (project_root / ".ccb").mkdir(parents=True, exist_ok=True)
    (project_root / ".ccb" / "ccb.config").write_text(build_same_window_config(provider=provider), encoding="utf-8")
    role_store = project_root / "roles"
    _write_minimal_role(role_store, "agentroles.general", default_agent_name="general")
    return {"project_root": str(project_root), "role_store": str(role_store)}


def prepare_single_agent_window_project(*, test_root: Path, project_name: str, provider: str = "fake", reset: bool = False) -> dict[str, str]:
    project_root = _project_root(test_root, project_name)
    if reset and project_root.exists():
        shutil.rmtree(project_root)
    (project_root / ".ccb").mkdir(parents=True, exist_ok=True)
    (project_root / ".ccb" / "ccb.config").write_text(build_single_agent_window_config(provider=provider), encoding="utf-8")
    role_store = project_root / "roles"
    _write_minimal_role(role_store, "agentroles.general", default_agent_name="general")
    return {"project_root": str(project_root), "role_store": str(role_store)}


def prepare_window_class_project(*, test_root: Path, project_name: str, provider: str = "fake", reset: bool = False) -> dict[str, str]:
    project_root = _project_root(test_root, project_name)
    if reset and project_root.exists():
        shutil.rmtree(project_root)
    (project_root / ".ccb").mkdir(parents=True, exist_ok=True)
    (project_root / ".ccb" / "ccb.config").write_text(build_window_class_config(provider=provider), encoding="utf-8")
    role_store = project_root / "roles"
    _write_minimal_role(role_store, "agentroles.general", default_agent_name="general")
    return {"project_root": str(project_root), "role_store": str(role_store)}


def prepare_mixed_move_add_project(*, test_root: Path, project_name: str, provider: str = "fake", reset: bool = False) -> dict[str, str]:
    project_root = _project_root(test_root, project_name)
    if reset and project_root.exists():
        shutil.rmtree(project_root)
    (project_root / ".ccb").mkdir(parents=True, exist_ok=True)
    (project_root / ".ccb" / "ccb.config").write_text(build_mixed_move_add_initial_config(provider=provider), encoding="utf-8")
    role_store = project_root / "roles"
    _write_minimal_role(role_store, "agentroles.general", default_agent_name="general")
    return {"project_root": str(project_root), "role_store": str(role_store)}


def prepare_batch_move_window_class_project(*, test_root: Path, project_name: str, provider: str = "fake", reset: bool = False) -> dict[str, str]:
    project_root = _project_root(test_root, project_name)
    if reset and project_root.exists():
        shutil.rmtree(project_root)
    (project_root / ".ccb").mkdir(parents=True, exist_ok=True)
    (project_root / ".ccb" / "ccb.config").write_text(build_batch_move_window_class_config(provider=provider), encoding="utf-8")
    role_store = project_root / "roles"
    _write_minimal_role(role_store, "agentroles.general", default_agent_name="general")
    return {"project_root": str(project_root), "role_store": str(role_store)}


def prepare_resolve_preflight_project(
    *,
    test_root: Path,
    project_name: str,
    provider: str = "fake",
    static_provider: str | None = None,
    reset: bool = False,
) -> dict[str, str]:
    project_root = _project_root(test_root, project_name)
    if reset and project_root.exists():
        shutil.rmtree(project_root)
    (project_root / ".ccb").mkdir(parents=True, exist_ok=True)
    (project_root / ".ccb" / "ccb.config").write_text(
        build_resolve_preflight_config(provider=provider, static_provider=static_provider),
        encoding="utf-8",
    )
    role_store = project_root / "roles"
    _write_minimal_role(role_store, "agentroles.general", default_agent_name="general")
    _write_minimal_role(role_store, "agentroles.coder", default_agent_name="worker")
    _write_minimal_role(role_store, "agentroles.code_reviewer", default_agent_name="code_reviewer")
    return {"project_root": str(project_root), "role_store": str(role_store)}


def run_dynamic_layout_smoke(
    *,
    test_root: Path,
    project_prefix: str,
    ccb_test: Path,
    provider: str = "fake",
    flows: tuple[str, ...] | None = None,
    provider_home_mode: str = "source-home",
    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
    resolve_preflight_static_provider: str | None = None,
    prepare_only: bool = False,
    reset: bool = False,
    keep_running: bool = False,
) -> dict[str, Any]:
    test_root = test_root.expanduser().resolve(strict=False)
    test_root.mkdir(parents=True, exist_ok=True)
    flow_names = _normalize_flows(flows)
    preflight_payload = preflight(test_root=test_root, provider=provider, ccb_test=ccb_test, provider_home_mode=provider_home_mode)
    if provider != "fake" and not prepare_only and os.environ.get(REAL_RUN_ENV) != "1":
        raise RuntimeError(f"real provider dynamic layout smoke requires {REAL_RUN_ENV}=1")
    if prepare_only:
        prepared = _prepare_selected_projects(
            test_root=test_root,
            project_prefix=project_prefix,
            provider=provider,
            flows=flow_names,
            resolve_preflight_static_provider=resolve_preflight_static_provider,
            reset=reset,
        )
        return {
            "dynamic_layout_smoke_status": "prepared",
            "provider": provider,
            "provider_home_mode": provider_home_mode,
            "resolve_preflight_static_provider": resolve_preflight_static_provider,
            "flows": list(flow_names),
            "preflight": preflight_payload,
            "prepared": prepared,
        }
    provider_home = _provider_home(test_root=test_root, mode=provider_home_mode)
    provider_home.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    if "multi-node" in flow_names:
        results.append(
            _run_multi_node_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-multi-node",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "multi-window-continuous" in flow_names:
        results.append(
            _run_multi_window_continuous_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-multi-window-continuous",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "batch-release" in flow_names:
        results.append(
            _run_batch_release_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-batch-release",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "same-window" in flow_names:
        results.append(
            _run_same_window_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-same-window",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "same-window-continuous" in flow_names:
        results.append(
            _run_same_window_continuous_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-same-window-continuous",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "single-agent-window" in flow_names:
        results.append(
            _run_single_agent_window_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-single-agent-window",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "move-agent" in flow_names:
        results.append(
            _run_move_agent_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-move-agent",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "move-shared-source" in flow_names:
        results.append(
            _run_move_shared_source_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-move-shared-source",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "mixed-move-add" in flow_names:
        results.append(
            _run_mixed_move_add_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-mixed-move-add",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "batch-move-window-class" in flow_names:
        results.append(
            _run_batch_move_window_class_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-batch-move-window-class",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "batch-move-execution-node" in flow_names:
        results.append(
            _run_batch_move_execution_node_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-batch-move-execution-node",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "window-class" in flow_names:
        results.append(
            _run_window_class_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-window-class",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "arrange-window" in flow_names:
        results.append(
            _run_arrange_window_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-arrange-window",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "window-class-continuous" in flow_names:
        results.append(
            _run_window_class_continuous_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-window-class-continuous",
                provider=provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    if "resolve-preflight" in flow_names:
        results.append(
            _run_resolve_preflight_flow(
                test_root=test_root,
                project_name=f"{project_prefix}-resolve-preflight",
                provider=provider,
                static_provider=resolve_preflight_static_provider,
                ccb_test=ccb_test,
                provider_home=provider_home,
                command_timeout_s=command_timeout_s,
                reset=reset,
                keep_running=keep_running,
            )
        )
    checks = {item["flow"]: item.get("flow_status") == "ok" for item in results}
    return {
        "dynamic_layout_smoke_status": "ok" if all(checks.values()) else "failed",
        "provider": provider,
        "provider_home_mode": provider_home_mode,
        "resolve_preflight_static_provider": resolve_preflight_static_provider,
        "flows": list(flow_names),
        "preflight": preflight_payload,
        "checks": checks,
        "results": results,
    }


def run_dynamic_layout_provider_matrix(
    *,
    test_root: Path,
    project_prefix: str,
    ccb_test: Path,
    providers: tuple[str, ...],
    flows: tuple[str, ...] | None = None,
    provider_home_mode: str = "source-home",
    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
    resolve_preflight_static_provider: str | None = None,
    prepare_only: bool = False,
    reset: bool = False,
    keep_running: bool = False,
) -> dict[str, Any]:
    provider_names = _normalize_providers(providers)
    results = []
    for provider in provider_names:
        results.append(
            run_dynamic_layout_smoke(
                test_root=test_root,
                project_prefix=f"{project_prefix}-{_provider_slug(provider)}",
                ccb_test=ccb_test,
                provider=provider,
                flows=flows,
                provider_home_mode=provider_home_mode,
                command_timeout_s=command_timeout_s,
                resolve_preflight_static_provider=resolve_preflight_static_provider,
                prepare_only=prepare_only,
                reset=reset,
                keep_running=keep_running,
            )
        )
    checks = {str(item.get("provider") or ""): item.get("dynamic_layout_smoke_status") in {"ok", "prepared"} for item in results}
    status = "prepared" if prepare_only and all(checks.values()) else ("ok" if all(checks.values()) else "failed")
    return {
        "dynamic_layout_smoke_status": status,
        "providers": list(provider_names),
        "provider_home_mode": provider_home_mode,
        "resolve_preflight_static_provider": resolve_preflight_static_provider,
        "flows": list(_normalize_flows(flows)),
        "checks": checks,
        "provider_results": results,
    }


def _run_multi_node_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_multi_node_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        ensure = _run_json(
            "ensure_multi_node",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "loop",
                "capacity",
                "ensure",
                "--loop-id",
                "round2",
                "--profile",
                "worker=2",
                "--profile",
                "code_reviewer=2",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(ensure)
        before = _run_json("layout_before_release", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(before)
        worker_ask = _run("ask_worker1", [str(ccb_test), "--project", str(project_root), "ask", "loop-round2-worker-1"], cwd=test_root, env=env, input_text="dynamic layout smoke ping worker1\n", timeout=command_timeout_s)
        reviewer_ask = _run("ask_reviewer2", [str(ccb_test), "--project", str(project_root), "ask", "loop-round2-code_reviewer-2"], cwd=test_root, env=env, input_text="dynamic layout smoke ping reviewer2\n", timeout=command_timeout_s)
        commands.extend([worker_ask, reviewer_ask])
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(worker_ask, reviewer_ask),
                timeout=command_timeout_s,
            )
        )
        release = _run_json(
            "release_multi_node",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "loop",
                "capacity",
                "release",
                "--loop-id",
                "round2",
                "--idle-only",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(release)
        after = _run_json("layout_after_release", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after)
        checks = {
            "ensure_add_window": _payload(ensure).get("apply", {}).get("plan_class") == "add_window",
            "four_loop_agents": _payload(before).get("loop_agent_count") == 4,
            "two_node_windows": _has_windows(
                before,
                {
                "node-round2-node1": ["loop-round2-worker-1", "loop-round2-code_reviewer-1"],
                "node-round2-node2": ["loop-round2-worker-2", "loop-round2-code_reviewer-2"],
                },
            ),
            "asks_accepted": _accepted(worker_ask) and _accepted(reviewer_ask),
            "asks_terminal": _watch_commands_terminal(commands),
            "release_removed_four": _payload(release).get("released_count") == 4,
            "loop_agents_cleaned": _payload(after).get("loop_agent_count") == 0,
            "returned_to_main": _window_agents(after) == {"main": ["orchestrator"]},
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "multi_node_capacity", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_multi_window_continuous_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_same_window_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    targets = tuple((f"helper{index}", f"review{index}") for index in range(1, 4))
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        for helper, window in targets:
            commands.append(
                _run_json(
                    f"add_{helper}_{window}",
                    [
                        str(ccb_test),
                        "--project",
                        str(project_root),
                        "agent",
                        "add",
                        f"{helper}:{provider}",
                        "--role",
                        "agentroles.general",
                        "--window",
                        window,
                        "--hidden",
                        "--json",
                    ],
                    cwd=test_root,
                    env=env,
                    timeout=command_timeout_s,
                )
            )
        after_add = _run_json("layout_after_add_windows", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_add)
        helper_ask = _run("ask_helper2_before_window_release", [str(ccb_test), "--project", str(project_root), "ask", "helper2"], cwd=test_root, env=env, input_text="multi-window-continuous smoke ping helper2\n", timeout=command_timeout_s)
        commands.append(helper_ask)
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(helper_ask,),
                timeout=command_timeout_s,
            )
        )
        releases: list[dict[str, Any]] = []
        for helper, _window in reversed(targets):
            release = _run_json(
                f"remove_{helper}",
                [
                    str(ccb_test),
                    "--project",
                    str(project_root),
                    "agent",
                    "remove",
                    helper,
                    "--policy",
                    "unload",
                    "--idle-only",
                    "--json",
                ],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
            releases.append(release)
            commands.append(release)
        after_release = _run_json("layout_after_remove_windows", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_release)
        commands.append(_run("ask_main_after_window_release", [str(ccb_test), "--project", str(project_root), "ask", "main"], cwd=test_root, env=env, input_text="multi-window-continuous smoke ping main\n", timeout=command_timeout_s))
        after_add_panes = _agent_panes(after_add)
        after_release_panes = _agent_panes(after_release)
        release_apply = [dict(_payload(item).get("apply") or {}) for item in releases]
        expected_windows = {"main": ["main"], **{window: [helper] for helper, window in targets}}
        removed_windows = {
            helper: apply.get("namespace_removed_windows")
            for (helper, _window), apply in zip(reversed(targets), release_apply)
        }
        removed_agents = {
            helper: apply.get("namespace_removed_agents", {}).get(helper)
            for (helper, _window), apply in zip(reversed(targets), release_apply)
        }
        checks = {
            "add_window_plans": [_payload(item).get("apply", {}).get("plan_class") for item in commands[2:5]] == ["add_window"] * 3,
            "grew_to_four_windows": _window_agents(after_add) == expected_windows,
            "observed_grew_to_four_windows": _observed_window_agent_pane_counts(after_add) == {
                "main": 1,
                "review1": 1,
                "review2": 1,
                "review3": 1,
            },
            "observed_window_geometry": _observed_all_windows_have_geometry(after_add, ("main", "review1", "review2", "review3")),
            "helper_ask_accepted": _accepted(helper_ask),
            "helper_ask_terminal": _watch_commands_terminal(commands),
            "release_remove_agent_plans": [apply.get("plan_class") for apply in release_apply] == ["remove_agent"] * 3,
            "removed_windows_match": all(removed_windows[helper] == [window] for helper, window in targets),
            "removed_agent_panes_match": all(removed_agents[helper] == after_add_panes.get(helper) for helper, _window in targets),
            "main_pane_preserved": after_release_panes.get("main") == after_add_panes.get("main"),
            "returned_to_main_window": _window_agents(after_release) == {"main": ["main"]},
            "observed_returned_to_main_pane": _observed_window_agent_pane_counts(after_release) == {"main": 1},
            "dynamic_agents_cleaned": _payload(after_release).get("dynamic_agent_count") == 0,
            "ask_main_accepted": _accepted(commands[-1]),
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "multi_window_continuous_add_remove", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_batch_release_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_same_window_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    additions = (
        ("helper1", "main"),
        ("helper2", "review2"),
        ("helper3", "review3"),
    )
    release_targets = ("helper2", "helper3")
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        for helper, window in additions:
            commands.append(
                _run_json(
                    f"add_{helper}_{window}",
                    [
                        str(ccb_test),
                        "--project",
                        str(project_root),
                        "agent",
                        "add",
                        f"{helper}:{provider}",
                        "--role",
                        "agentroles.general",
                        "--window",
                        window,
                        "--hidden",
                        "--json",
                    ],
                    cwd=test_root,
                    env=env,
                    timeout=command_timeout_s,
                )
            )
        before = _run_json("layout_before_batch_release", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(before)
        release = _run_json(
            "batch_remove_helper2_helper3",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "remove",
                "--agents",
                ",".join(release_targets),
                "--policy",
                "unload",
                "--idle-only",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(release)
        after = _run_json("layout_after_batch_release", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after)
        survivor_ask = _run(
            "ask_helper1_after_batch_release",
            [str(ccb_test), "--project", str(project_root), "ask", "helper1"],
            cwd=test_root,
            env=env,
            input_text="batch-release smoke ping helper1\n",
            timeout=command_timeout_s,
        )
        commands.append(survivor_ask)
        main_ask = _run(
            "ask_main_after_batch_release",
            [str(ccb_test), "--project", str(project_root), "ask", "main"],
            cwd=test_root,
            env=env,
            input_text="batch-release smoke ping main\n",
            timeout=command_timeout_s,
        )
        commands.append(main_ask)
        before_panes = _agent_panes(before)
        after_panes = _agent_panes(after)
        release_payload = _payload(release)
        apply_payload = dict(release_payload.get("apply") or {})
        removed_agents = dict(apply_payload.get("namespace_removed_agents") or {})
        checks = {
            "add_plans": [_payload(item).get("apply", {}).get("plan_class") for item in commands[2:5]] == ["add_agent", "add_window", "add_window"],
            "before_windows": _window_agents(before) == {
                "main": ["main", "helper1"],
                "review2": ["helper2"],
                "review3": ["helper3"],
            },
            "batch_status_removed": release_payload.get("agent_lifecycle_status") == "removed",
            "batch_remove_agent_plan": apply_payload.get("plan_class") == "remove_agent",
            "batch_removed_agents": set(removed_agents) == set(release_targets),
            "batch_removed_agent_panes_match": all(removed_agents.get(agent) == before_panes.get(agent) for agent in release_targets),
            "batch_removed_windows": sorted(apply_payload.get("namespace_removed_windows") or ()) == ["review2", "review3"],
            "survivor_panes_preserved": after_panes.get("main") == before_panes.get("main")
            and after_panes.get("helper1") == before_panes.get("helper1"),
            "after_windows": _window_agents(after) == {"main": ["main", "helper1"]},
            "after_dynamic_count_one": _payload(after).get("dynamic_agent_count") == 1,
            "survivor_ask_accepted": _accepted(survivor_ask),
            "main_ask_accepted": _accepted(main_ask),
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "batch_release_multi_window", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_same_window_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_same_window_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        for helper in ("helper1", "helper2", "helper3"):
            commands.append(
                _run_json(
                    f"add_{helper}",
                    [
                        str(ccb_test),
                        "--project",
                        str(project_root),
                        "agent",
                        "add",
                        f"{helper}:{provider}",
                        "--role",
                        "agentroles.general",
                        "--window",
                        "main",
                        "--hidden",
                        "--json",
                    ],
                    cwd=test_root,
                    env=env,
                    timeout=command_timeout_s,
                )
            )
        before = _run_json("layout_before_middle_release", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(before)
        release = _run_json(
            "remove_middle_helper",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "remove",
                "helper2",
                "--policy",
                "unload",
                "--idle-only",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(release)
        after = _run_json("layout_after_middle_release", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after)
        commands.append(_run("ask_helper1", [str(ccb_test), "--project", str(project_root), "ask", "helper1"], cwd=test_root, env=env, input_text="same-window smoke ping helper1\n", timeout=command_timeout_s))
        commands.append(_run("ask_helper3", [str(ccb_test), "--project", str(project_root), "ask", "helper3"], cwd=test_root, env=env, input_text="same-window smoke ping helper3\n", timeout=command_timeout_s))
        before_panes = _agent_panes(before)
        after_panes = _agent_panes(after)
        checks = {
            "add_agent_panes": [_payload(item).get("apply", {}).get("plan_class") for item in commands[2:5]] == ["add_agent", "add_agent", "add_agent"],
            "before_order": _window_agents(before).get("main") == ["main", "helper1", "helper2", "helper3"],
            "remove_agent_plan": _payload(release).get("apply", {}).get("plan_class") == "remove_agent",
            "removed_middle_pane": _payload(release).get("applied", {}).get("removed_pane_id") == before_panes.get("helper2"),
            "reflowed_main_window": _payload(release).get("apply", {}).get("namespace_reflowed_windows") == ["main"],
            "survivor_panes_preserved": after_panes.get("helper1") == before_panes.get("helper1") and after_panes.get("helper3") == before_panes.get("helper3"),
            "after_order": _window_agents(after).get("main") == ["main", "helper1", "helper3"],
            "asks_accepted": _accepted(commands[-2]) and _accepted(commands[-1]),
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "same_window_middle_release", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_same_window_continuous_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_same_window_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    helpers = tuple(f"helper{index}" for index in range(1, 6))
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        for helper in helpers:
            commands.append(
                _run_json(
                    f"add_{helper}",
                    [
                        str(ccb_test),
                        "--project",
                        str(project_root),
                        "agent",
                        "add",
                        f"{helper}:{provider}",
                        "--role",
                        "agentroles.general",
                        "--window",
                        "main",
                        "--hidden",
                        "--json",
                    ],
                    cwd=test_root,
                    env=env,
                    timeout=command_timeout_s,
                )
            )
        after_add = _run_json("layout_after_grow_to_six", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_add)
        helper_ask = _run("ask_helper3_before_shrink", [str(ccb_test), "--project", str(project_root), "ask", "helper3"], cwd=test_root, env=env, input_text="same-window-continuous smoke ping helper3\n", timeout=command_timeout_s)
        commands.append(helper_ask)
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(helper_ask,),
                timeout=command_timeout_s,
            )
        )
        releases: list[dict[str, Any]] = []
        for helper in reversed(helpers):
            release = _run_json(
                f"remove_{helper}",
                [
                    str(ccb_test),
                    "--project",
                    str(project_root),
                    "agent",
                    "remove",
                    helper,
                    "--policy",
                    "unload",
                    "--idle-only",
                    "--json",
                ],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
            releases.append(release)
            commands.append(release)
        after_release = _run_json("layout_after_shrink_to_one", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_release)
        commands.append(_run("ask_main_after_shrink", [str(ccb_test), "--project", str(project_root), "ask", "main"], cwd=test_root, env=env, input_text="same-window-continuous smoke ping main\n", timeout=command_timeout_s))
        after_add_panes = _agent_panes(after_add)
        after_release_panes = _agent_panes(after_release)
        release_payloads = [_payload(item) for item in releases]
        release_apply = [dict(payload.get("apply") or {}) for payload in release_payloads]
        removed_agents = {
            helper: apply.get("namespace_removed_agents", {}).get(helper)
            for helper, apply in zip(reversed(helpers), release_apply)
        }
        checks = {
            "grow_add_agent_plans": [_payload(item).get("apply", {}).get("plan_class") for item in commands[2:7]] == ["add_agent"] * 5,
            "grew_to_six_order": _window_agents(after_add).get("main") == ["main", *helpers],
            "observed_grew_to_six_panes": _observed_window_agent_pane_count(after_add, "main") == 6,
            "observed_grow_geometry": _observed_panes_have_geometry(_observed_window_agent_panes(after_add, "main")),
            "observed_grow_indexes_contiguous": _observed_pane_indexes_contiguous(_observed_window_agent_panes(after_add, "main")),
            "observed_grow_min_width": _observed_panes_min_width(_observed_window_agent_panes(after_add, "main")) >= 8,
            "observed_grow_fixed_columns": _observed_panes_match_fixed_columns(after_add, "main", ("main", *helpers)),
            "helper_ask_accepted": _accepted(helper_ask),
            "helper_ask_terminal": _watch_commands_terminal(commands),
            "release_remove_agent_plans": [apply.get("plan_class") for apply in release_apply] == ["remove_agent"] * 5,
            "release_reflowed_main": all(apply.get("namespace_reflowed_windows") == ["main"] for apply in release_apply),
            "removed_helper_panes_match": all(removed_agents[helper] == after_add_panes.get(helper) for helper in helpers),
            "main_pane_preserved": after_release_panes.get("main") == after_add_panes.get("main"),
            "shrunk_to_one_order": _window_agents(after_release) == {"main": ["main"]},
            "observed_shrunk_to_one_pane": _observed_window_agent_pane_count(after_release, "main") == 1,
            "observed_shrink_geometry": _observed_panes_have_geometry(_observed_window_agent_panes(after_release, "main")),
            "dynamic_agents_cleaned": _payload(after_release).get("dynamic_agent_count") == 0,
            "ask_main_accepted": _accepted(commands[-1]),
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "same_window_continuous_1_to_6_to_1", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_single_agent_window_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_single_agent_window_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        add = _run_json(
            "add_single_window_helper",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "add",
                f"helper:{provider}",
                "--role",
                "agentroles.general",
                "--window",
                "review",
                "--hidden",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(add)
        before = _run_json("layout_before_single_window_release", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(before)
        release = _run_json(
            "remove_single_window_helper",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "remove",
                "helper",
                "--policy",
                "unload",
                "--idle-only",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(release)
        after = _run_json("layout_after_single_window_release", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after)
        commands.append(_run("ask_main", [str(ccb_test), "--project", str(project_root), "ask", "main"], cwd=test_root, env=env, input_text="single-agent-window smoke ping main\n", timeout=command_timeout_s))
        before_panes = _agent_panes(before)
        release_payload = _payload(release)
        apply_payload = dict(release_payload.get("apply") or {})
        checks = {
            "add_window_plan": _payload(add).get("apply", {}).get("plan_class") == "add_window",
            "review_window_added": _window_agents(before).get("review") == ["helper"],
            "helper_pane_recorded": bool(before_panes.get("helper")),
            "remove_agent_plan": apply_payload.get("plan_class") == "remove_agent",
            "removed_helper_pane": apply_payload.get("namespace_removed_agents", {}).get("helper") == before_panes.get("helper"),
            "removed_review_window": apply_payload.get("namespace_removed_windows") == ["review"],
            "after_only_main_window": _window_agents(after) == {"main": ["main"]},
            "dynamic_agents_cleaned": _payload(after).get("dynamic_agent_count") == 0,
            "ask_main_accepted": _accepted(commands[-1]),
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "single_agent_window_release", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_move_agent_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_same_window_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        add = _run_json(
            "add_move_helper_to_main",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "add",
                f"helper:{provider}",
                "--role",
                "agentroles.general",
                "--window",
                "main",
                "--hidden",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(add)
        before_move = _run_json("layout_before_move_agent", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(before_move)
        pre_move_ask = _run(
            "ask_helper_before_move",
            [str(ccb_test), "--project", str(project_root), "ask", "helper"],
            cwd=test_root,
            env=env,
            input_text="move-agent smoke ping before move\n",
            timeout=command_timeout_s,
        )
        commands.append(pre_move_ask)
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(pre_move_ask,),
                timeout=command_timeout_s,
            )
        )
        move = _run_json(
            "move_helper_to_review_window",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "move",
                "helper",
                "--window",
                "review",
                "--reason",
                "dynamic layout move smoke",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(move)
        after_move = _run_json("layout_after_move_agent", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_move)
        post_move_ask = _run(
            "ask_helper_after_move",
            [str(ccb_test), "--project", str(project_root), "ask", "helper"],
            cwd=test_root,
            env=env,
            input_text="move-agent smoke ping after move\n",
            timeout=command_timeout_s,
        )
        commands.append(post_move_ask)
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(post_move_ask,),
                timeout=command_timeout_s,
            )
        )
        move_back = _run_json(
            "move_helper_back_to_main",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "move",
                "helper",
                "--window",
                "main",
                "--reason",
                "dynamic layout move smoke return",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(move_back)
        after_return = _run_json("layout_after_move_agent_return", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_return)
        return_ask = _run(
            "ask_helper_after_return",
            [str(ccb_test), "--project", str(project_root), "ask", "helper"],
            cwd=test_root,
            env=env,
            input_text="move-agent smoke ping after return\n",
            timeout=command_timeout_s,
        )
        commands.append(return_ask)
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(return_ask,),
                timeout=command_timeout_s,
            )
        )
        release = _run_json(
            "remove_moved_helper",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "remove",
                "helper",
                "--policy",
                "unload",
                "--idle-only",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(release)
        after_release = _run_json("layout_after_move_cleanup", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_release)
        before_panes = _agent_panes(before_move)
        after_panes = _agent_panes(after_move)
        after_return_panes = _agent_panes(after_return)
        helper_pane = before_panes.get("helper")
        move_apply = dict(_payload(move).get("apply") or {})
        move_back_apply = dict(_payload(move_back).get("apply") or {})
        release_apply = dict(_payload(release).get("apply") or {})
        checks = {
            "add_agent_plan": _payload(add).get("apply", {}).get("plan_class") == "add_agent",
            "before_move_order": _window_agents(before_move) == {"main": ["main", "helper"]},
            "helper_pane_recorded_before_move": bool(helper_pane),
            "pre_move_ask_accepted": _accepted(pre_move_ask),
            "pre_move_ask_terminal": _watch_commands_terminal(commands),
            "move_plan_class": move_apply.get("plan_class") == "move_agent",
            "move_apply_status": move_apply.get("apply_status") == "applied",
            "move_preserved_helper_pane": move_apply.get("namespace_moved_agents", {}).get("helper") == helper_pane
            and after_panes.get("helper") == helper_pane,
            "move_window_evidence": move_apply.get("namespace_moved_agent_windows", {}).get("helper") == "review",
            "move_reflowed_windows": move_apply.get("namespace_reflowed_windows") == ["main", "review"],
            "after_move_order": _window_agents(after_move) == {"main": ["main"], "review": ["helper"]},
            "post_move_ask_accepted": _accepted(post_move_ask),
            "post_move_ask_terminal": _watch_commands_terminal(commands),
            "return_move_plan_class": move_back_apply.get("plan_class") == "move_agent",
            "return_move_apply_status": move_back_apply.get("apply_status") == "applied",
            "return_preserved_helper_pane": move_back_apply.get("namespace_moved_agents", {}).get("helper") == helper_pane
            and after_return_panes.get("helper") == helper_pane,
            "return_window_evidence": move_back_apply.get("namespace_moved_agent_windows", {}).get("helper") == "main",
            "return_removed_review_window": move_back_apply.get("namespace_removed_windows") == ["review"],
            "return_reflowed_main": move_back_apply.get("namespace_reflowed_windows") == ["main"],
            "after_return_order": _window_agents(after_return) == {"main": ["main", "helper"]},
            "return_ask_accepted": _accepted(return_ask),
            "return_ask_terminal": _watch_commands_terminal(commands),
            "release_remove_agent_plan": release_apply.get("plan_class") == "remove_agent",
            "release_removed_helper_pane": release_apply.get("namespace_removed_agents", {}).get("helper") == helper_pane,
            "release_kept_main_window": release_apply.get("namespace_removed_windows") == [],
            "after_release_only_main": _window_agents(after_release) == {"main": ["main"]},
            "dynamic_agents_cleaned": _payload(after_release).get("dynamic_agent_count") == 0,
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "move_agent_to_new_window", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_move_shared_source_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_same_window_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        add_results = []
        for helper in ("helper1", "helper2"):
            add = _run_json(
                f"add_{helper}_to_review",
                [
                    str(ccb_test),
                    "--project",
                    str(project_root),
                    "agent",
                    "add",
                    f"{helper}:{provider}",
                    "--role",
                    "agentroles.general",
                    "--window",
                    "review",
                    "--hidden",
                    "--json",
                ],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
            commands.append(add)
            add_results.append(add)
        before_move = _run_json("layout_before_shared_source_move", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(before_move)
        pre_move_stay_ask = _run(
            "ask_helper2_before_shared_source_move",
            [str(ccb_test), "--project", str(project_root), "ask", "helper2"],
            cwd=test_root,
            env=env,
            input_text="move-shared-source smoke ping helper2 before move\n",
            timeout=command_timeout_s,
        )
        commands.append(pre_move_stay_ask)
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(pre_move_stay_ask,),
                timeout=command_timeout_s,
            )
        )
        move = _run_json(
            "move_helper1_to_main_from_shared_source",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "move",
                "helper1",
                "--window",
                "main",
                "--reason",
                "dynamic layout shared-source move smoke",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(move)
        after_move = _run_json("layout_after_shared_source_move", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_move)
        post_move_moved_ask = _run(
            "ask_helper1_after_shared_source_move",
            [str(ccb_test), "--project", str(project_root), "ask", "helper1"],
            cwd=test_root,
            env=env,
            input_text="move-shared-source smoke ping helper1 after move\n",
            timeout=command_timeout_s,
        )
        post_move_stay_ask = _run(
            "ask_helper2_after_shared_source_move",
            [str(ccb_test), "--project", str(project_root), "ask", "helper2"],
            cwd=test_root,
            env=env,
            input_text="move-shared-source smoke ping helper2 after move\n",
            timeout=command_timeout_s,
        )
        commands.extend([post_move_moved_ask, post_move_stay_ask])
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(post_move_moved_ask, post_move_stay_ask),
                timeout=command_timeout_s,
            )
        )
        move_back = _run_json(
            "move_helper1_back_to_shared_source",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "move",
                "helper1",
                "--window",
                "review",
                "--reason",
                "dynamic layout shared-source move return",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(move_back)
        after_return = _run_json("layout_after_shared_source_return", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_return)
        return_ask = _run(
            "ask_helper1_after_shared_source_return",
            [str(ccb_test), "--project", str(project_root), "ask", "helper1"],
            cwd=test_root,
            env=env,
            input_text="move-shared-source smoke ping helper1 after return\n",
            timeout=command_timeout_s,
        )
        commands.append(return_ask)
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(return_ask,),
                timeout=command_timeout_s,
            )
        )
        releases = []
        for helper in ("helper1", "helper2"):
            release = _run_json(
                f"remove_shared_source_{helper}",
                [
                    str(ccb_test),
                    "--project",
                    str(project_root),
                    "agent",
                    "remove",
                    helper,
                    "--policy",
                    "unload",
                    "--idle-only",
                    "--json",
                ],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
            commands.append(release)
            releases.append(release)
        after_cleanup = _run_json("layout_after_shared_source_cleanup", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_cleanup)
        before_panes = _agent_panes(before_move)
        after_move_panes = _agent_panes(after_move)
        after_return_panes = _agent_panes(after_return)
        moved_pane = before_panes.get("helper1")
        stay_pane = before_panes.get("helper2")
        move_apply = dict(_payload(move).get("apply") or {})
        move_back_apply = dict(_payload(move_back).get("apply") or {})
        first_release_apply = dict(_payload(releases[0]).get("apply") or {}) if releases else {}
        final_release_apply = dict(_payload(releases[-1]).get("apply") or {}) if releases else {}
        checks = {
            "first_add_window_plan": _payload(add_results[0]).get("apply", {}).get("plan_class") == "add_window",
            "second_add_agent_plan": _payload(add_results[1]).get("apply", {}).get("plan_class") == "add_agent",
            "before_move_order": _window_agents(before_move) == {"main": ["main"], "review": ["helper1", "helper2"]},
            "moved_pane_recorded": bool(moved_pane),
            "stay_pane_recorded": bool(stay_pane),
            "pre_move_stay_ask_accepted": _accepted(pre_move_stay_ask),
            "pre_move_stay_ask_terminal": _watch_commands_terminal(commands),
            "move_plan_class": move_apply.get("plan_class") == "move_agent",
            "move_apply_status": move_apply.get("apply_status") == "applied",
            "move_preserved_moved_pane": move_apply.get("namespace_moved_agents", {}).get("helper1") == moved_pane
            and after_move_panes.get("helper1") == moved_pane,
            "move_preserved_stay_pane": after_move_panes.get("helper2") == stay_pane,
            "move_source_window_retained": not move_apply.get("namespace_removed_windows"),
            "move_reflowed_source_and_target": move_apply.get("namespace_reflowed_windows") == ["main", "review"],
            "after_move_order": _window_agents(after_move) == {"main": ["main", "helper1"], "review": ["helper2"]},
            "post_move_moved_ask_accepted": _accepted(post_move_moved_ask),
            "post_move_stay_ask_accepted": _accepted(post_move_stay_ask),
            "post_move_asks_terminal": _watch_commands_terminal(commands),
            "return_move_plan_class": move_back_apply.get("plan_class") == "move_agent",
            "return_move_apply_status": move_back_apply.get("apply_status") == "applied",
            "return_preserved_moved_pane": move_back_apply.get("namespace_moved_agents", {}).get("helper1") == moved_pane
            and after_return_panes.get("helper1") == moved_pane,
            "return_preserved_stay_pane": after_return_panes.get("helper2") == stay_pane,
            "return_kept_review_window": not move_back_apply.get("namespace_removed_windows"),
            "after_return_order": _window_agents(after_return) == {"main": ["main"], "review": ["helper2", "helper1"]},
            "return_ask_accepted": _accepted(return_ask),
            "return_ask_terminal": _watch_commands_terminal(commands),
            "first_release_kept_review_window": first_release_apply.get("plan_class") == "remove_agent"
            and not first_release_apply.get("namespace_removed_windows"),
            "final_release_removed_review_window": final_release_apply.get("plan_class") == "remove_agent"
            and final_release_apply.get("namespace_removed_windows") == ["review"],
            "after_cleanup_only_main": _window_agents(after_cleanup) == {"main": ["main"]},
            "dynamic_agents_cleaned": _payload(after_cleanup).get("dynamic_agent_count") == 0,
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "move_agent_shared_source", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_mixed_move_add_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_mixed_move_add_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        before_reload = _run_json("layout_before_mixed_move_add_reload", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(before_reload)
        (project_root / ".ccb" / "ccb.config").write_text(build_mixed_move_add_target_config(provider=provider), encoding="utf-8")
        commands.append(
            {
                "name": "write_mixed_move_add_target_config",
                "command": ["write", str(project_root / ".ccb" / "ccb.config")],
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "timeout": False,
            }
        )
        reload_result = _run("reload_mixed_move_add_config", [str(ccb_test), "--project", str(project_root), "reload"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(reload_result)
        after_reload = _run_json("layout_after_mixed_move_add_reload", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_reload)
        zeta_ask = _run(
            "ask_zeta_after_mixed_move_add",
            [str(ccb_test), "--project", str(project_root), "ask", "zeta"],
            cwd=test_root,
            env=env,
            input_text="mixed-move-add smoke ping zeta\n",
            timeout=command_timeout_s,
        )
        alpha_ask = _run(
            "ask_alpha_after_mixed_move_add",
            [str(ccb_test), "--project", str(project_root), "ask", "alpha"],
            cwd=test_root,
            env=env,
            input_text="mixed-move-add smoke ping alpha\n",
            timeout=command_timeout_s,
        )
        beta_ask = _run(
            "ask_beta_after_mixed_move_add",
            [str(ccb_test), "--project", str(project_root), "ask", "beta"],
            cwd=test_root,
            env=env,
            input_text="mixed-move-add smoke ping beta\n",
            timeout=command_timeout_s,
        )
        commands.extend([zeta_ask, alpha_ask, beta_ask])
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(zeta_ask, alpha_ask, beta_ask),
                timeout=command_timeout_s,
            )
        )
        before_panes = _agent_panes(before_reload)
        after_panes = _agent_panes(after_reload)
        reload_stdout = str(reload_result.get("stdout") or "")
        checks = {
            "before_windows": _window_agents(before_reload) == {"main": ["main"], "review": ["zeta", "alpha"]},
            "reload_published": "reload_status: published" in reload_stdout,
            "reload_move_plan": "plan_class: move_agent" in reload_stdout,
            "reload_namespace_planned_mixed_steps": "action=create_agent_pane window=archive agent=beta" in reload_stdout
            and reload_stdout.count("action=move_agent_pane window=review") == 2
            and "action=kill_window window=review" in reload_stdout,
            "after_windows": _window_agents(after_reload) == {"main": ["main"], "archive": ["zeta", "alpha", "beta"]},
            "moved_panes_preserved": after_panes.get("zeta") == before_panes.get("zeta")
            and after_panes.get("alpha") == before_panes.get("alpha"),
            "new_beta_pane_created": bool(after_panes.get("beta")) and after_panes.get("beta") not in set(before_panes.values()),
            "review_window_removed": "review" not in _window_agents(after_reload),
            "zeta_ask_accepted": _accepted(zeta_ask),
            "alpha_ask_accepted": _accepted(alpha_ask),
            "beta_ask_accepted": _accepted(beta_ask),
            "asks_terminal": _watch_commands_terminal(commands),
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "mixed_move_add_explicit_windows", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_batch_move_window_class_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_batch_move_window_class_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    helpers = ("zeta", "alpha")
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        adds: list[dict[str, Any]] = []
        for helper in helpers:
            add = _run_json(
                f"add_{helper}_to_review",
                [
                    str(ccb_test),
                    "--project",
                    str(project_root),
                    "agent",
                    "add",
                    f"{helper}:{provider}",
                    "--role",
                    "agentroles.general",
                    "--window",
                    "review",
                    "--hidden",
                    "--json",
                ],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
            adds.append(add)
            commands.append(add)
        before_move = _run_json("layout_before_batch_move_window_class", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(before_move)
        move = _run_json(
            "move_zeta_alpha_to_window_class",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "move",
                "--agents",
                ",".join(helpers),
                "--window-class",
                "plan-orchestrate",
                "--reason",
                "dynamic layout batch window-class move smoke",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(move)
        after_move = _run_json("layout_after_batch_move_window_class", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_move)
        zeta_ask = _run(
            "ask_zeta_after_batch_move_window_class",
            [str(ccb_test), "--project", str(project_root), "ask", "zeta"],
            cwd=test_root,
            env=env,
            input_text="batch-move-window-class smoke ping zeta\n",
            timeout=command_timeout_s,
        )
        alpha_ask = _run(
            "ask_alpha_after_batch_move_window_class",
            [str(ccb_test), "--project", str(project_root), "ask", "alpha"],
            cwd=test_root,
            env=env,
            input_text="batch-move-window-class smoke ping alpha\n",
            timeout=command_timeout_s,
        )
        commands.extend([zeta_ask, alpha_ask])
        before_panes = _agent_panes(before_move)
        after_panes = _agent_panes(after_move)
        move_payload = _payload(move)
        move_apply = dict(move_payload.get("apply") or {})
        moved_agents = dict(move_apply.get("namespace_moved_agents") or {})
        moved_windows = dict(move_apply.get("namespace_moved_agent_windows") or {})
        checks = {
            "add_plans": [_payload(item).get("apply", {}).get("plan_class") for item in adds] == ["add_window", "add_agent"],
            "before_windows": _window_agents(before_move)
            == {
                "main": ["frontdesk"],
                "plan-orchestrate": ["p1", "p2", "p3", "p4", "p5"],
                "review": ["zeta", "alpha"],
            },
            "move_status_active": move_payload.get("agent_lifecycle_status") == "active",
            "move_target_windows": move_payload.get("target_window_names") == ["plan-orchestrate", "plan-orchestrate-2"],
            "move_plan_class": move_apply.get("plan_class") == "move_agent",
            "moved_agent_panes_match": all(moved_agents.get(agent) == before_panes.get(agent) for agent in helpers),
            "moved_window_evidence": moved_windows == {"zeta": "plan-orchestrate", "alpha": "plan-orchestrate-2"},
            "removed_review_window": move_apply.get("namespace_removed_windows") == ["review"],
            "after_panes_preserved": all(after_panes.get(agent) == before_panes.get(agent) for agent in helpers),
            "after_windows": _window_agents(after_move)
            == {
                "main": ["frontdesk"],
                "plan-orchestrate": ["p1", "p2", "p3", "p4", "p5", "zeta"],
                "plan-orchestrate-2": ["alpha"],
            },
            "zeta_ask_accepted": _accepted(zeta_ask),
            "alpha_ask_accepted": _accepted(alpha_ask),
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "batch_move_window_class", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_batch_move_execution_node_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_same_window_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    agents = ("worker", "checker")
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        adds: list[dict[str, Any]] = []
        for agent in agents:
            add = _run_json(
                f"add_{agent}_to_review",
                [
                    str(ccb_test),
                    "--project",
                    str(project_root),
                    "agent",
                    "add",
                    f"{agent}:{provider}",
                    "--role",
                    "agentroles.general",
                    "--window",
                    "review",
                    "--hidden",
                    "--json",
                ],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
            adds.append(add)
            commands.append(add)
        before_move = _run_json("layout_before_batch_move_execution_node", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(before_move)
        move = _run_json(
            "move_worker_checker_to_execution_node",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "move",
                "--agents",
                ",".join(agents),
                "--loop-id",
                "round1",
                "--node-id",
                "node1",
                "--reason",
                "dynamic layout batch execution-node move smoke",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(move)
        after_move = _run_json("layout_after_batch_move_execution_node", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_move)
        worker_ask = _run(
            "ask_worker_after_batch_move_execution_node",
            [str(ccb_test), "--project", str(project_root), "ask", "worker"],
            cwd=test_root,
            env=env,
            input_text="batch-move-execution-node smoke ping worker\n",
            timeout=command_timeout_s,
        )
        checker_ask = _run(
            "ask_checker_after_batch_move_execution_node",
            [str(ccb_test), "--project", str(project_root), "ask", "checker"],
            cwd=test_root,
            env=env,
            input_text="batch-move-execution-node smoke ping checker\n",
            timeout=command_timeout_s,
        )
        commands.extend([worker_ask, checker_ask])
        before_panes = _agent_panes(before_move)
        after_panes = _agent_panes(after_move)
        move_payload = _payload(move)
        move_apply = dict(move_payload.get("apply") or {})
        moved_agents = dict(move_apply.get("namespace_moved_agents") or {})
        moved_windows = dict(move_apply.get("namespace_moved_agent_windows") or {})
        checks = {
            "add_plans": [_payload(item).get("apply", {}).get("plan_class") for item in adds] == ["add_window", "add_agent"],
            "before_windows": _window_agents(before_move) == {"main": ["main"], "review": ["worker", "checker"]},
            "move_status_active": move_payload.get("agent_lifecycle_status") == "active",
            "move_target_window": move_payload.get("target_window_name") == "node-round1-node1",
            "move_target_windows": move_payload.get("target_window_names") == ["node-round1-node1"],
            "move_plan_class": move_apply.get("plan_class") == "move_agent",
            "moved_agent_panes_match": all(moved_agents.get(agent) == before_panes.get(agent) for agent in agents),
            "moved_window_evidence": moved_windows == {"worker": "node-round1-node1", "checker": "node-round1-node1"},
            "removed_review_window": move_apply.get("namespace_removed_windows") == ["review"],
            "after_panes_preserved": all(after_panes.get(agent) == before_panes.get(agent) for agent in agents),
            "after_windows": _window_agents(after_move) == {"main": ["main"], "node-round1-node1": ["worker", "checker"]},
            "worker_ask_accepted": _accepted(worker_ask),
            "checker_ask_accepted": _accepted(checker_ask),
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "batch_move_execution_node", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_window_class_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_window_class_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        for helper in ("planner_helper1", "planner_helper2", "planner_helper3"):
            commands.append(
                _run_json(
                    f"add_{helper}",
                    [
                        str(ccb_test),
                        "--project",
                        str(project_root),
                        "agent",
                        "add",
                        f"{helper}:{provider}",
                        "--role",
                        "agentroles.general",
                        "--window-class",
                        "plan-orchestrate",
                        "--hidden",
                        "--json",
                    ],
                    cwd=test_root,
                    env=env,
                    timeout=command_timeout_s,
                )
            )
        before = _run_json("layout_before_window_class_release", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(before)
        release = _run_json(
            "remove_middle_window_class_helper",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "remove",
                "planner_helper2",
                "--policy",
                "unload",
                "--idle-only",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(release)
        after = _run_json("layout_after_window_class_release", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after)
        commands.append(
            _run(
                "ask_planner_helper1",
                [str(ccb_test), "--project", str(project_root), "ask", "planner_helper1"],
                cwd=test_root,
                env=env,
                input_text="window-class smoke ping planner_helper1\n",
                timeout=command_timeout_s,
            )
        )
        commands.append(
            _run(
                "ask_planner_helper3",
                [str(ccb_test), "--project", str(project_root), "ask", "planner_helper3"],
                cwd=test_root,
                env=env,
                input_text="window-class smoke ping planner_helper3\n",
                timeout=command_timeout_s,
            )
        )
        before_panes = _agent_panes(before)
        after_panes = _agent_panes(after)
        checks = {
            "add_agent_panes": [_payload(item).get("apply", {}).get("plan_class") for item in commands[2:5]] == ["add_agent", "add_agent", "add_agent"],
            "before_main_order": _window_agents(before).get("main") == ["frontdesk"],
            "before_plan_order": _window_agents(before).get("plan-orchestrate")
            == ["planner", "planner_helper1", "planner_helper2", "planner_helper3"],
            "remove_agent_plan": _payload(release).get("apply", {}).get("plan_class") == "remove_agent",
            "removed_middle_pane": _payload(release).get("applied", {}).get("removed_pane_id") == before_panes.get("planner_helper2"),
            "reflowed_plan_window": _payload(release).get("apply", {}).get("namespace_reflowed_windows") == ["plan-orchestrate"],
            "survivor_panes_preserved": after_panes.get("planner_helper1") == before_panes.get("planner_helper1")
            and after_panes.get("planner_helper3") == before_panes.get("planner_helper3"),
            "after_main_order": _window_agents(after).get("main") == ["frontdesk"],
            "after_plan_order": _window_agents(after).get("plan-orchestrate") == ["planner", "planner_helper1", "planner_helper3"],
            "asks_accepted": _accepted(commands[-2]) and _accepted(commands[-1]),
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "window_class_middle_release", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_arrange_window_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_window_class_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    helpers = tuple(f"planner_helper{index}" for index in range(1, 5))
    plan_agents = ("planner", *helpers)
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        adds: list[dict[str, Any]] = []
        for helper in helpers:
            add = _run_json(
                f"add_{helper}",
                [
                    str(ccb_test),
                    "--project",
                    str(project_root),
                    "agent",
                    "add",
                    f"{helper}:{provider}",
                    "--role",
                    "agentroles.general",
                    "--window-class",
                    "plan-orchestrate",
                    "--hidden",
                    "--json",
                ],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
            adds.append(add)
            commands.append(add)
        before_disturb = _run_json("layout_before_arrange_disturb", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(before_disturb)
        disturb = _disturb_window_layout(
            "disturb_plan_orchestrate_even_horizontal",
            before_disturb,
            window_name="plan-orchestrate",
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(disturb)
        after_disturb = _run_json("layout_after_arrange_disturb", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_disturb)
        arrange = _run_json(
            "arrange_plan_orchestrate",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "layout",
                "arrange",
                "--window",
                "plan-orchestrate",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(arrange)
        after_arrange = _run_json("layout_after_arrange", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_arrange)
        helper_ask = _run(
            "ask_planner_helper3_after_arrange",
            [str(ccb_test), "--project", str(project_root), "ask", "planner_helper3"],
            cwd=test_root,
            env=env,
            input_text="arrange-window smoke ping planner_helper3\n",
            timeout=command_timeout_s,
        )
        commands.append(helper_ask)
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(helper_ask,),
                timeout=command_timeout_s,
            )
        )
        releases: list[dict[str, Any]] = []
        for helper in reversed(helpers):
            release = _run_json(
                f"remove_{helper}",
                [
                    str(ccb_test),
                    "--project",
                    str(project_root),
                    "agent",
                    "remove",
                    helper,
                    "--policy",
                    "unload",
                    "--idle-only",
                    "--json",
                ],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
            releases.append(release)
            commands.append(release)
        after_release = _run_json("layout_after_arrange_cleanup", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_release)

        before_panes = _agent_panes(before_disturb)
        arrange_panes = _agent_panes(arrange)
        after_arrange_panes = _agent_panes(after_arrange)
        release_apply = [dict(_payload(item).get("apply") or {}) for item in releases]
        checks = {
            "add_agent_plans": [_payload(item).get("apply", {}).get("plan_class") for item in adds] == ["add_agent"] * len(helpers),
            "before_fixed_columns": _observed_panes_match_fixed_columns(before_disturb, "plan-orchestrate", plan_agents),
            "disturb_command_success": int(disturb.get("returncode") or 0) == 0,
            "disturb_kept_agent_order": _window_agents(after_disturb).get("plan-orchestrate") == list(plan_agents),
            "disturb_made_non_fixed": not _observed_panes_match_fixed_columns(after_disturb, "plan-orchestrate", plan_agents),
            "arrange_status_ok": _payload(arrange).get("arrange_status") == "ok",
            "arrange_reflowed_plan": _payload(arrange).get("reflowed_windows") == ["plan-orchestrate"]
            and not _payload(arrange).get("reflow_errors"),
            "arrange_fixed_columns": _observed_panes_match_fixed_columns(arrange, "plan-orchestrate", plan_agents)
            and _observed_panes_match_fixed_columns(after_arrange, "plan-orchestrate", plan_agents),
            "agent_order_preserved": _window_agents(after_arrange).get("plan-orchestrate") == list(plan_agents),
            "pane_ids_preserved": all(
                before_panes.get(agent) == arrange_panes.get(agent) == after_arrange_panes.get(agent)
                for agent in plan_agents
            ),
            "helper_ask_accepted": _accepted(helper_ask),
            "helper_ask_terminal": _watch_commands_terminal(commands),
            "release_remove_agent_plans": [apply.get("plan_class") for apply in release_apply] == ["remove_agent"] * len(helpers),
            "after_release_static_windows": _window_agents(after_release) == {
                "main": ["frontdesk"],
                "plan-orchestrate": ["planner"],
            },
            "dynamic_agents_cleaned": _payload(after_release).get("dynamic_agent_count") == 0,
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "arrange_window_disturb_restore", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_window_class_continuous_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_window_class_project(test_root=test_root, project_name=project_name, provider=provider, reset=reset)
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    helpers = tuple(f"planner_helper{index}" for index in range(1, 8))
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        adds: list[dict[str, Any]] = []
        for helper in helpers:
            add = _run_json(
                f"add_{helper}",
                [
                    str(ccb_test),
                    "--project",
                    str(project_root),
                    "agent",
                    "add",
                    f"{helper}:{provider}",
                    "--role",
                    "agentroles.general",
                    "--window-class",
                    "plan-orchestrate",
                    "--hidden",
                    "--json",
                ],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
            adds.append(add)
            commands.append(add)
        after_add = _run_json("layout_after_window_class_grow_to_eight", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_add)
        helper_ask = _run(
            "ask_planner_helper7_before_release",
            [str(ccb_test), "--project", str(project_root), "ask", "planner_helper7"],
            cwd=test_root,
            env=env,
            input_text="window-class-continuous smoke ping planner_helper7\n",
            timeout=command_timeout_s,
        )
        commands.append(helper_ask)
        commands.extend(
            _watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(helper_ask,),
                timeout=command_timeout_s,
            )
        )
        releases: list[dict[str, Any]] = []
        for helper in reversed(helpers):
            release = _run_json(
                f"remove_{helper}",
                [
                    str(ccb_test),
                    "--project",
                    str(project_root),
                    "agent",
                    "remove",
                    helper,
                    "--policy",
                    "unload",
                    "--idle-only",
                    "--json",
                ],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
            releases.append(release)
            commands.append(release)
        after_release = _run_json("layout_after_window_class_shrink_to_planner", [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"], cwd=test_root, env=env, timeout=command_timeout_s)
        commands.append(after_release)
        add_payloads = [_payload(item) for item in adds]
        release_payloads = [_payload(item) for item in releases]
        release_apply = [dict(payload.get("apply") or {}) for payload in release_payloads]
        after_add_panes = _agent_panes(after_add)
        checks = {
            "add_plan_sequence": [payload.get("apply", {}).get("plan_class") for payload in add_payloads]
            == ["add_agent", "add_agent", "add_agent", "add_agent", "add_agent", "add_window", "add_agent"],
            "page1_order": _window_agents(after_add).get("plan-orchestrate")
            == ["planner", "planner_helper1", "planner_helper2", "planner_helper3", "planner_helper4", "planner_helper5"],
            "page2_order": _window_agents(after_add).get("plan-orchestrate-2") == ["planner_helper6", "planner_helper7"],
            "page1_observed_fixed_columns": _observed_panes_match_fixed_columns(
                after_add,
                "plan-orchestrate",
                ("planner", "planner_helper1", "planner_helper2", "planner_helper3", "planner_helper4", "planner_helper5"),
            ),
            "page2_observed_fixed_columns": _observed_panes_match_fixed_columns(
                after_add,
                "plan-orchestrate-2",
                ("planner_helper6", "planner_helper7"),
            ),
            "helper7_pane_recorded": bool(after_add_panes.get("planner_helper7")),
            "helper7_ask_accepted": _accepted(helper_ask),
            "helper7_ask_terminal": _watch_commands_terminal(commands),
            "release_remove_agent_plans": [apply.get("plan_class") for apply in release_apply] == ["remove_agent"] * 7,
            "page2_removed_when_empty": any("plan-orchestrate-2" in apply.get("namespace_removed_windows", []) for apply in release_apply),
            "after_main_order": _window_agents(after_release).get("main") == ["frontdesk"],
            "after_plan_order": _window_agents(after_release).get("plan-orchestrate") == ["planner"],
            "after_page2_removed": "plan-orchestrate-2" not in _window_agents(after_release),
            "dynamic_agents_cleaned": _payload(after_release).get("dynamic_agent_count") == 0,
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "window_class_continuous_1_to_8_to_1", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _run_resolve_preflight_flow(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    static_provider: str | None,
    ccb_test: Path,
    provider_home: Path,
    command_timeout_s: int,
    reset: bool,
    keep_running: bool,
) -> dict[str, Any]:
    prepared = prepare_resolve_preflight_project(
        test_root=test_root,
        project_name=project_name,
        provider=provider,
        static_provider=static_provider,
        reset=reset,
    )
    project_root = Path(prepared["project_root"])
    env = _env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    commands: list[dict[str, Any]] = []
    try:
        commands.append(_run("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"], cwd=test_root, env=env, timeout=command_timeout_s))
        commands.append(_run("start", [str(ccb_test), "--project", str(project_root)], cwd=test_root, env=env, timeout=command_timeout_s))
        class_resolve = _run_json(
            "resolve_window_class_overflow",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "layout",
                "resolve",
                "review_helper1",
                "--window-class",
                "plan-orchestrate",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(class_resolve)
        class_add = _run_json(
            "add_window_class_overflow",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "add",
                f"review_helper1:{provider}",
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
        commands.append(class_add)
        class_show = _run_json(
            "show_window_class_overflow",
            [str(ccb_test), "--project", str(project_root), "agent", "show", "review_helper1", "--json"],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(class_show)
        class_status = _run_json(
            "layout_after_window_class_add",
            [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(class_status)
        class_release = _run_json(
            "release_window_class_overflow",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "agent",
                "release",
                "review_helper1",
                "--idle-only",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(class_release)
        class_after = _run_json(
            "layout_after_window_class_release",
            [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(class_after)
        node_resolve = _run_json(
            "resolve_execution_node",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "layout",
                "resolve",
                "loop-round3-worker-1",
                "--loop-id",
                "round3",
                "--node-id",
                "node1",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(node_resolve)
        capacity_ensure = _run_json(
            "ensure_execution_node_capacity",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "loop",
                "capacity",
                "ensure",
                "--loop-id",
                "round3",
                "--profile",
                "worker=1",
                "--profile",
                "code_reviewer=1",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(capacity_ensure)
        node_status = _run_json(
            "layout_after_execution_node_ensure",
            [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(node_status)
        capacity_release = _run_json(
            "release_execution_node_capacity",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "loop",
                "capacity",
                "release",
                "--loop-id",
                "round3",
                "--idle-only",
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(capacity_release)
        node_after = _run_json(
            "layout_after_execution_node_release",
            [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(node_after)
        class_resolve_payload = _payload(class_resolve)
        class_add_payload = _payload(class_add)
        class_release_payload = _payload(class_release)
        node_resolve_payload = _payload(node_resolve)
        checks = {
            "class_resolve_overflow": class_resolve_payload.get("layout_status") == "ok"
            and class_resolve_payload.get("addable") is True
            and class_resolve_payload.get("placement_mode") == "window_class"
            and class_resolve_payload.get("resolved_window_name") == "plan-orchestrate-2"
            and class_resolve_payload.get("will_create_window") is True,
            "class_add_matches_resolve": class_add_payload.get("resolved_window_name") == class_resolve_payload.get("resolved_window_name"),
            "class_add_window_plan": class_add_payload.get("apply", {}).get("plan_class") == "add_window",
            "class_show_matches": _payload(class_show).get("resolved_window_name") == "plan-orchestrate-2",
            "class_window_visible": _window_agents(class_status).get("plan-orchestrate-2") == ["review_helper1"],
            "class_release_unloaded": class_release_payload.get("resolved_policy") == "unload"
            and class_release_payload.get("lifecycle_state") == "unloaded",
            "class_release_removed_window": "plan-orchestrate-2" in class_release_payload.get("apply", {}).get("namespace_removed_windows", []),
            "class_after_clean": "plan-orchestrate-2" not in _window_agents(class_after)
            and _payload(class_after).get("dynamic_agent_count") == 0,
            "node_resolve_execution_window": node_resolve_payload.get("layout_status") == "ok"
            and node_resolve_payload.get("placement_mode") == "execution_node"
            and node_resolve_payload.get("resolved_window_name") == "node-round3-node1"
            and node_resolve_payload.get("will_create_window") is True,
            "capacity_add_window_plan": _payload(capacity_ensure).get("apply", {}).get("plan_class") == "add_window",
            "node_window_visible": _has_windows(
                node_status,
                {
                    "node-round3-node1": [
                        "loop-round3-worker-1",
                        "loop-round3-code_reviewer-1",
                    ],
                },
            ),
            "capacity_release_clean": _payload(capacity_release).get("released_count") == 2
            and _payload(node_after).get("loop_agent_count") == 0
            and "node-round3-node1" not in _window_agents(node_after),
        }
        status = "ok" if all(checks.values()) and _all_success(commands) else "failed"
        return {"flow": "resolve_preflight_chain", "flow_status": status, "checks": checks, "commands": commands}
    finally:
        if not keep_running:
            commands.append(_run("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], cwd=test_root, env=env, timeout=command_timeout_s))


def _project_root(test_root: Path, project_name: str) -> Path:
    root = test_root.expanduser().resolve(strict=False)
    project_root = (root / project_name).resolve(strict=False)
    if root not in project_root.parents and project_root != root:
        raise ValueError(f"project must be under test root: {root}")
    return project_root


def preflight(*, test_root: Path, provider: str, ccb_test: Path, provider_home_mode: str) -> dict[str, Any]:
    provider_home = _provider_home(test_root=test_root, mode=provider_home_mode)
    executable = PROVIDER_EXECUTABLES.get(provider, provider)
    provider_path = shutil.which(executable)
    checks = {
        "ccb_test_exists": ccb_test.exists(),
        "test_root_exists": test_root.expanduser().resolve(strict=False).is_dir(),
        "provider": provider,
        "provider_executable": executable,
        "provider_executable_path": provider_path,
        "provider_executable_found": provider == "fake" or provider_path is not None,
        "provider_home_mode": provider_home_mode,
        "provider_home": str(provider_home),
        "provider_auth_exists": _provider_auth_exists(provider=provider, home=provider_home),
        "real_run_opt_in": os.environ.get(REAL_RUN_ENV) == "1",
    }
    required = ("ccb_test_exists", "test_root_exists", "provider_executable_found")
    return {
        "preflight_status": "ok" if all(bool(checks[key]) for key in required) else "blocked",
        "checks": checks,
    }


def _prepare_selected_projects(
    *,
    test_root: Path,
    project_prefix: str,
    provider: str,
    flows: tuple[str, ...],
    resolve_preflight_static_provider: str | None,
    reset: bool,
) -> list[dict[str, str]]:
    prepared: list[dict[str, str]] = []
    if "multi-node" in flows:
        prepared.append(
            prepare_multi_node_project(
                test_root=test_root,
                project_name=f"{project_prefix}-multi-node",
                provider=provider,
                reset=reset,
            )
        )
    if "multi-window-continuous" in flows:
        prepared.append(
            prepare_same_window_project(
                test_root=test_root,
                project_name=f"{project_prefix}-multi-window-continuous",
                provider=provider,
                reset=reset,
            )
        )
    if "batch-release" in flows:
        prepared.append(
            prepare_same_window_project(
                test_root=test_root,
                project_name=f"{project_prefix}-batch-release",
                provider=provider,
                reset=reset,
            )
        )
    if "same-window" in flows:
        prepared.append(
            prepare_same_window_project(
                test_root=test_root,
                project_name=f"{project_prefix}-same-window",
                provider=provider,
                reset=reset,
            )
        )
    if "same-window-continuous" in flows:
        prepared.append(
            prepare_same_window_project(
                test_root=test_root,
                project_name=f"{project_prefix}-same-window-continuous",
                provider=provider,
                reset=reset,
            )
        )
    if "single-agent-window" in flows:
        prepared.append(
            prepare_single_agent_window_project(
                test_root=test_root,
                project_name=f"{project_prefix}-single-agent-window",
                provider=provider,
                reset=reset,
            )
        )
    if "move-agent" in flows:
        prepared.append(
            prepare_same_window_project(
                test_root=test_root,
                project_name=f"{project_prefix}-move-agent",
                provider=provider,
                reset=reset,
            )
        )
    if "move-shared-source" in flows:
        prepared.append(
            prepare_same_window_project(
                test_root=test_root,
                project_name=f"{project_prefix}-move-shared-source",
                provider=provider,
                reset=reset,
            )
        )
    if "mixed-move-add" in flows:
        prepared.append(
            prepare_mixed_move_add_project(
                test_root=test_root,
                project_name=f"{project_prefix}-mixed-move-add",
                provider=provider,
                reset=reset,
            )
        )
    if "batch-move-window-class" in flows:
        prepared.append(
            prepare_batch_move_window_class_project(
                test_root=test_root,
                project_name=f"{project_prefix}-batch-move-window-class",
                provider=provider,
                reset=reset,
            )
        )
    if "batch-move-execution-node" in flows:
        prepared.append(
            prepare_same_window_project(
                test_root=test_root,
                project_name=f"{project_prefix}-batch-move-execution-node",
                provider=provider,
                reset=reset,
            )
        )
    if "window-class" in flows:
        prepared.append(
            prepare_window_class_project(
                test_root=test_root,
                project_name=f"{project_prefix}-window-class",
                provider=provider,
                reset=reset,
            )
        )
    if "arrange-window" in flows:
        prepared.append(
            prepare_window_class_project(
                test_root=test_root,
                project_name=f"{project_prefix}-arrange-window",
                provider=provider,
                reset=reset,
            )
        )
    if "window-class-continuous" in flows:
        prepared.append(
            prepare_window_class_project(
                test_root=test_root,
                project_name=f"{project_prefix}-window-class-continuous",
                provider=provider,
                reset=reset,
            )
        )
    if "resolve-preflight" in flows:
        prepared.append(
            prepare_resolve_preflight_project(
                test_root=test_root,
                project_name=f"{project_prefix}-resolve-preflight",
                provider=provider,
                static_provider=resolve_preflight_static_provider,
                reset=reset,
            )
        )
    return prepared


def _normalize_flows(flows: tuple[str, ...] | None) -> tuple[str, ...]:
    if not flows:
        return FLOW_NAMES
    unknown = sorted({item for item in flows if item not in FLOW_NAMES})
    if unknown:
        raise ValueError(f"unknown flow(s): {', '.join(unknown)}")
    return tuple(dict.fromkeys(flows))


def _normalize_providers(providers: tuple[str, ...] | None) -> tuple[str, ...]:
    selected = []
    for provider in providers or ("fake",):
        text = str(provider or "").strip()
        if text and text not in selected:
            selected.append(text)
    return tuple(selected or ("fake",))


def _provider_slug(provider: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(provider or "provider")).strip("-")
    return slug or "provider"


def _provider_home(*, test_root: Path, mode: str) -> Path:
    if mode == "source-home":
        return test_root.expanduser().resolve(strict=False) / "source_home"
    if mode == "real-home":
        return _real_user_home()
    raise ValueError(f"unsupported provider home mode: {mode}")


def _real_user_home() -> Path:
    override = os.environ.get("CCB_REAL_HOME") or os.environ.get("REAL_USER_HOME")
    if override:
        return Path(override).expanduser().resolve(strict=False)
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir).expanduser().resolve(strict=False)
    except Exception:
        return Path.home().expanduser().resolve(strict=False)


def _provider_auth_exists(*, provider: str, home: Path) -> bool | None:
    if provider == "codex":
        return any(
            path.is_file()
            for path in (
                home / ".codex" / "auth.json",
                home / ".codex" / "home" / "auth.json",
            )
        )
    return None


def _write_minimal_role(role_store: Path, role_id: str, *, default_agent_name: str) -> None:
    target = role_store / "installed" / role_id / "current" / "role.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                f'id = "{role_id}"',
                'version = "0.1.0"',
                "",
                "[identity]",
                f'default_agent_name = "{default_agent_name}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _env(*, provider_home: Path, role_store: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(provider_home)
    env["CCB_SOURCE_HOME"] = str(provider_home)
    env["AGENT_ROLES_STORE"] = str(role_store)
    env["CCB_NO_ATTACH"] = "1"
    env["CCB_WATCH_TIMEOUT_S"] = "10"
    env["CCB_WATCH_POLL_INTERVAL_S"] = "0.1"
    return env


def _run_json(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    input_text: str | None = None,
) -> dict[str, Any]:
    result = _run(name, command, cwd=cwd, env=env, input_text=input_text, timeout=timeout)
    try:
        payload = json.loads(str(result.get("stdout") or ""))
    except json.JSONDecodeError:
        payload = None
    result["payload"] = payload if isinstance(payload, dict) else {}
    return result


def _run(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    input_text: str | None = None,
) -> dict[str, Any]:
    run_command, run_cwd = _project_local_ask_command(command, cwd)
    try:
        completed = subprocess.run(
            run_command,
            cwd=str(run_cwd),
            env=env,
            text=True,
            input=input_text,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "command": run_command,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timeout": True,
        }
    return {
        "name": name,
        "command": run_command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "timeout": False,
    }


def _project_local_ask_command(command: list[str], cwd: Path) -> tuple[list[str], Path]:
    try:
        project_index = command.index("--project")
    except ValueError:
        return command, cwd
    if project_index + 2 >= len(command):
        return command, cwd
    if command[project_index + 2] != "ask":
        return command, cwd
    project_root = Path(command[project_index + 1]).expanduser()
    return command[:project_index] + command[project_index + 2 :], project_root


def _disturb_window_layout(
    name: str,
    layout_status_result: dict[str, Any],
    *,
    window_name: str,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    namespace = _payload(layout_status_result).get("namespace")
    namespace = namespace if isinstance(namespace, dict) else {}
    socket_path = str(namespace.get("tmux_socket_path") or "")
    session_name = str(namespace.get("tmux_session_name") or "")
    if not socket_path or not session_name:
        return {
            "name": name,
            "command": [],
            "returncode": 1,
            "stdout": "",
            "stderr": "layout status did not include tmux socket/session for disturbance\n",
            "timeout": False,
        }
    return _run(
        name,
        [
            "tmux",
            "-S",
            socket_path,
            "select-layout",
            "-t",
            f"{session_name}:{window_name}",
            "even-horizontal",
        ],
        cwd=cwd,
        env=env,
        timeout=timeout,
    )


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else {}


def compact_smoke_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a small CLI-friendly copy of a full smoke result payload."""
    return {
        "dynamic_layout_smoke_status": payload.get("dynamic_layout_smoke_status"),
        "provider": payload.get("provider"),
        "providers": payload.get("providers"),
        "provider_home_mode": payload.get("provider_home_mode"),
        "resolve_preflight_static_provider": payload.get("resolve_preflight_static_provider"),
        "flows": payload.get("flows"),
        "preflight": payload.get("preflight"),
        "prepared": payload.get("prepared"),
        "checks": payload.get("checks"),
        "results": [_compact_flow_result(item) for item in payload.get("results", []) if isinstance(item, dict)],
        "provider_results": [
            compact_smoke_payload(item)
            for item in payload.get("provider_results", [])
            if isinstance(item, dict)
        ],
    }


def _compact_flow_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "flow": result.get("flow"),
        "flow_status": result.get("flow_status"),
        "checks": result.get("checks"),
        "commands": [_compact_command(item) for item in result.get("commands", []) if isinstance(item, dict)],
    }


def _compact_command(command: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "name": command.get("name"),
        "returncode": command.get("returncode"),
    }
    if command.get("timeout"):
        summary["timeout"] = True
    payload_summary = _compact_payload_summary(_payload(command))
    if payload_summary:
        summary["payload"] = payload_summary
    stdout_excerpt = _line_excerpt(str(command.get("stdout") or ""))
    stderr_excerpt = _line_excerpt(str(command.get("stderr") or ""))
    if stdout_excerpt:
        summary["stdout_excerpt"] = stdout_excerpt
    if stderr_excerpt:
        summary["stderr_excerpt"] = stderr_excerpt
    return summary


def _compact_payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    keys = (
        "action",
        "layout_status",
        "arrange_status",
        "loop_capacity_status",
        "agent_lifecycle_status",
        "agent_count",
        "runtime_agent_count",
        "loop_agent_count",
        "dynamic_agent_count",
        "window_count",
        "pane_count",
        "placement_mode",
        "resolved_window_name",
        "target_surface",
        "target_window_exists",
        "will_create_window",
        "addable",
        "resolved_policy",
        "released_count",
        "retained_count",
        "retained_busy",
        "window_name",
        "reason",
        "reflowed_windows",
        "reflow_errors",
    )
    summary = {key: payload.get(key) for key in keys if key in payload}
    apply_payload = payload.get("apply")
    if isinstance(apply_payload, dict):
        summary["apply"] = {
            key: apply_payload.get(key)
            for key in ("plan_class", "apply_status", "reload_status")
            if key in apply_payload
        }
        if apply_payload.get("namespace_removed_agents"):
            summary["apply"]["namespace_removed_agents"] = dict(apply_payload.get("namespace_removed_agents") or {})
        if apply_payload.get("namespace_removed_windows"):
            summary["apply"]["namespace_removed_windows"] = list(apply_payload.get("namespace_removed_windows") or ())
        if apply_payload.get("namespace_moved_agents"):
            summary["apply"]["namespace_moved_agents"] = dict(apply_payload.get("namespace_moved_agents") or {})
        if apply_payload.get("namespace_moved_agent_windows"):
            summary["apply"]["namespace_moved_agent_windows"] = dict(apply_payload.get("namespace_moved_agent_windows") or {})
        if apply_payload.get("namespace_reflowed_windows"):
            summary["apply"]["namespace_reflowed_windows"] = list(apply_payload.get("namespace_reflowed_windows") or ())
        if apply_payload.get("namespace_reflow_errors"):
            summary["apply"]["namespace_reflow_errors"] = dict(apply_payload.get("namespace_reflow_errors") or {})
    windows = payload.get("windows")
    if isinstance(windows, list):
        summary["windows"] = [
            _compact_window_summary(raw)
            for raw in windows
            if isinstance(raw, dict)
        ]
    return summary


def _compact_window_summary(raw: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "name": raw.get("name"),
        "agents": raw.get("agent_names"),
        "pane_count": raw.get("pane_count"),
    }
    if "runtime_pane_count" in raw:
        summary["runtime_pane_count"] = raw.get("runtime_pane_count")
    observed = raw.get("observed")
    panes = observed.get("panes") if isinstance(observed, dict) else None
    if isinstance(panes, list):
        compact_panes = []
        for pane in panes:
            if not isinstance(pane, dict):
                continue
            compact_pane = {
                "pane_id": pane.get("pane_id"),
                "pane_index": pane.get("pane_index"),
                "pane_left": pane.get("pane_left"),
                "pane_top": pane.get("pane_top"),
                "pane_width": pane.get("pane_width"),
                "pane_height": pane.get("pane_height"),
            }
            agent = pane.get("ccb_agent") or pane.get("ccb_slot")
            if agent:
                compact_pane["agent"] = agent
            compact_panes.append(compact_pane)
        summary["observed_panes"] = compact_panes
    return summary


def _line_excerpt(text: str, *, max_lines: int = 3, max_chars: int = 240) -> list[str]:
    if not text:
        return []
    lines = [line for line in text.strip().splitlines() if line][:max_lines]
    return [line[:max_chars] for line in lines]


def _window_agents(result: dict[str, Any]) -> dict[str, list[str]]:
    payload = _payload(result)
    windows = payload.get("windows")
    if not isinstance(windows, list):
        return {}
    values: dict[str, list[str]] = {}
    for raw_window in windows:
        if not isinstance(raw_window, dict):
            continue
        name = str(raw_window.get("name") or "")
        agents = raw_window.get("agent_names")
        if name and isinstance(agents, list):
            values[name] = [str(item) for item in agents]
    return values


def _has_windows(result: dict[str, Any], expected: dict[str, list[str]]) -> bool:
    actual = _window_agents(result)
    return all(actual.get(name) == agents for name, agents in expected.items())


def _agent_panes(result: dict[str, Any]) -> dict[str, str]:
    payload = _payload(result)
    panes: dict[str, str] = {}
    for raw_window in payload.get("windows") if isinstance(payload.get("windows"), list) else []:
        if not isinstance(raw_window, dict):
            continue
        agents = raw_window.get("agents")
        if not isinstance(agents, list):
            continue
        for raw_agent in agents:
            if not isinstance(raw_agent, dict):
                continue
            agent = str(raw_agent.get("agent") or "")
            pane_id = str(raw_agent.get("pane_id") or "")
            if agent and pane_id:
                panes[agent] = pane_id
    return panes


def _observed_window_panes(result: dict[str, Any], window_name: str) -> list[dict[str, Any]]:
    raw_window = _payload_window(result, window_name)
    if raw_window is None:
        return []
    observed = raw_window.get("observed")
    panes = observed.get("panes") if isinstance(observed, dict) else None
    return [dict(pane) for pane in panes if isinstance(pane, dict)] if isinstance(panes, list) else []


def _payload_window(result: dict[str, Any], window_name: str) -> dict[str, Any] | None:
    payload = _payload(result)
    windows = payload.get("windows")
    if not isinstance(windows, list):
        return None
    for raw_window in windows:
        if not isinstance(raw_window, dict) or raw_window.get("name") != window_name:
            continue
        return raw_window
    return None


def _observed_window_agent_panes(result: dict[str, Any], window_name: str) -> list[dict[str, Any]]:
    raw_window = _payload_window(result, window_name)
    if raw_window is None:
        return []
    raw_agents = raw_window.get("agent_names")
    expected_agents = {str(agent) for agent in raw_agents} if isinstance(raw_agents, list) else set()
    return [
        pane
        for pane in _observed_window_panes(result, window_name)
        if str(pane.get("ccb_agent") or pane.get("ccb_slot") or "").strip() in expected_agents
    ]


def _observed_window_agent_pane_count(result: dict[str, Any], window_name: str) -> int:
    return len(_observed_window_agent_panes(result, window_name))


def _observed_window_agent_pane_counts(result: dict[str, Any]) -> dict[str, int]:
    payload = _payload(result)
    windows = payload.get("windows")
    if not isinstance(windows, list):
        return {}
    counts: dict[str, int] = {}
    for raw_window in windows:
        if not isinstance(raw_window, dict):
            continue
        name = str(raw_window.get("name") or "")
        if not name:
            continue
        counts[name] = _observed_window_agent_pane_count(result, name)
    return counts


def _observed_all_windows_have_geometry(result: dict[str, Any], names: tuple[str, ...]) -> bool:
    return all(_observed_panes_have_geometry(_observed_window_agent_panes(result, name)) for name in names)


def _observed_panes_have_geometry(panes: list[dict[str, Any]]) -> bool:
    if not panes:
        return False
    for pane in panes:
        width = pane.get("pane_width")
        height = pane.get("pane_height")
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            return False
    return True


def _observed_pane_indexes_contiguous(panes: list[dict[str, Any]]) -> bool:
    if not panes:
        return False
    indexes = [pane.get("pane_index") for pane in panes]
    if not all(isinstance(index, int) for index in indexes):
        return False
    ordered = sorted(indexes)
    return ordered == list(range(ordered[0], ordered[0] + len(ordered)))


def _observed_panes_min_width(panes: list[dict[str, Any]]) -> int:
    widths = [pane.get("pane_width") for pane in panes if isinstance(pane.get("pane_width"), int)]
    return min(widths) if widths else 0


def _observed_panes_match_fixed_columns(result: dict[str, Any], window_name: str, agents: tuple[str, ...]) -> bool:
    panes = _observed_window_agent_panes(result, window_name)
    by_agent = {
        str(pane.get("ccb_agent") or pane.get("ccb_slot") or ""): pane
        for pane in panes
    }
    if set(by_agent) != set(agents):
        return False
    ordered = [by_agent[agent] for agent in agents]
    if not _observed_panes_have_position(ordered):
        return False
    if len(ordered) == 1:
        return True
    left_column = ordered[0::2]
    right_column = ordered[1::2]
    if not right_column:
        return _same_left(left_column) and _strictly_increasing_top(left_column)
    left_x = left_column[0]["pane_left"]
    right_x = right_column[0]["pane_left"]
    if not isinstance(left_x, int) or not isinstance(right_x, int) or right_x <= left_x:
        return False
    if not _same_left(left_column) or not _same_left(right_column):
        return False
    if not _strictly_increasing_top(left_column) or not _strictly_increasing_top(right_column):
        return False
    if left_column[0].get("pane_top") != right_column[0].get("pane_top"):
        return False
    return True


def _observed_panes_have_position(panes: list[dict[str, Any]]) -> bool:
    if not panes:
        return False
    for pane in panes:
        left = pane.get("pane_left")
        top = pane.get("pane_top")
        if not isinstance(left, int) or not isinstance(top, int) or left < 0 or top < 0:
            return False
    return True


def _same_left(panes: list[dict[str, Any]]) -> bool:
    if not panes:
        return False
    left = panes[0].get("pane_left")
    return isinstance(left, int) and all(pane.get("pane_left") == left for pane in panes)


def _strictly_increasing_top(panes: list[dict[str, Any]]) -> bool:
    tops = [pane.get("pane_top") for pane in panes]
    if not all(isinstance(top, int) for top in tops):
        return False
    return all(current < following for current, following in zip(tops, tops[1:]))


def _accepted(result: dict[str, Any]) -> bool:
    return int(result.get("returncode") or 0) == 0 and "accepted job=" in str(result.get("stdout") or "")


def _watch_submitted_jobs(
    *,
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    asks: tuple[dict[str, Any], ...],
    timeout: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    watch_env = dict(env)
    watch_timeout = max(10, int(timeout))
    watch_env["CCB_WATCH_TIMEOUT_S"] = str(watch_timeout)
    watch_env.setdefault("CCB_WATCH_POLL_INTERVAL_S", "0.1")
    for ask in asks:
        job_id = _job_id(ask)
        if job_id is None:
            continue
        results.append(
            _run(
                f"watch_{job_id}",
                [str(ccb_test), "--project", str(project_root), "pend", "--watch", job_id],
                cwd=test_root,
                env=watch_env,
                timeout=watch_timeout + 5,
            )
        )
    return results


def _job_id(result: dict[str, Any]) -> str | None:
    match = re.search(r"\bjob=(job_[A-Za-z0-9_-]+)\b", str(result.get("stdout") or ""))
    return match.group(1) if match else None


def _watch_commands_terminal(results: list[dict[str, Any]]) -> bool:
    watch_results = [item for item in results if str(item.get("name") or "").startswith("watch_job_")]
    return bool(watch_results) and all(
        int(item.get("returncode") or 0) == 0 and "watch_status: terminal" in str(item.get("stdout") or "")
        for item in watch_results
    )


def _all_success(results: list[dict[str, Any]]) -> bool:
    return all(int(item.get("returncode") or 0) == 0 for item in results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CCB dynamic window/pane layout smoke tests.")
    parser.add_argument("--test-root", type=Path, default=DEFAULT_TEST_ROOT)
    parser.add_argument("--project-prefix", default="dynamic-layout-smoke")
    parser.add_argument("--ccb-test", type=Path, default=DEFAULT_CCB_TEST)
    parser.add_argument("--provider", action="append", dest="providers", help="Provider to run; repeat for a guarded provider matrix. Defaults to fake.")
    parser.add_argument("--flow", action="append", choices=FLOW_NAMES, help="Flow to run; repeat to run multiple flows. Defaults to all flows.")
    parser.add_argument("--provider-home-mode", choices=("source-home", "real-home"), default="source-home")
    parser.add_argument(
        "--resolve-preflight-static-provider",
        default=None,
        help="Provider used only for static resolve-preflight filler panes; defaults to the selected provider.",
    )
    parser.add_argument("--command-timeout", type=int, default=DEFAULT_COMMAND_TIMEOUT_S)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--full-output", action="store_true", help="Print complete command stdout and JSON payloads.")
    parser.add_argument("--output", type=Path, help="Write the printed JSON payload to this path.")
    args = parser.parse_args(argv)

    providers = _normalize_providers(tuple(args.providers or ()))
    if len(providers) == 1:
        payload = run_dynamic_layout_smoke(
            test_root=args.test_root,
            project_prefix=args.project_prefix,
            ccb_test=args.ccb_test,
            provider=providers[0],
            flows=tuple(args.flow or ()),
            provider_home_mode=args.provider_home_mode,
            command_timeout_s=args.command_timeout,
            resolve_preflight_static_provider=args.resolve_preflight_static_provider,
            prepare_only=args.prepare_only,
            reset=args.reset,
            keep_running=args.keep_running,
        )
    else:
        payload = run_dynamic_layout_provider_matrix(
            test_root=args.test_root,
            project_prefix=args.project_prefix,
            ccb_test=args.ccb_test,
            providers=providers,
            flows=tuple(args.flow or ()),
            provider_home_mode=args.provider_home_mode,
            command_timeout_s=args.command_timeout,
            resolve_preflight_static_provider=args.resolve_preflight_static_provider,
            prepare_only=args.prepare_only,
            reset=args.reset,
            keep_running=args.keep_running,
        )
    printable = payload if args.full_output else compact_smoke_payload(payload)
    text = json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload.get("dynamic_layout_smoke_status") in {"ok", "prepared"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
