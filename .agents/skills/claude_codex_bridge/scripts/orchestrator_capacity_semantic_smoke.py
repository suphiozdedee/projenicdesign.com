#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
ROLEPACK_SOURCE = (
    REPO_ROOT
    / "docs"
    / "plantree"
    / "plans"
    / "agentic-loop-workflow"
    / "drafts"
    / "agentroles.ccb_orchestrator"
)
DEFAULT_TEST_ROOT = Path(os.environ.get("CCB_ORCH_SMOKE_TEST_ROOT", "/home/bfly/yunwei/test_ccb2"))
REAL_RUN_ENV = "CCB_ORCH_SMOKE_RUN_REAL"
AGENT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
PROVIDER_EXECUTABLES = {
    "fake": "fake",
    "codex": "codex",
    "claude": "claude",
    "gemini": "gemini",
}


def build_config(*, provider: str, model: str | None = None) -> str:
    model_line = f'model = "{model}"' if model else ""
    profile_model_lines = [model_line] if model_line else []
    lines = [
        "version = 2",
        'entry_window = "main"',
        "",
        "[windows]",
        f'main = "orchestrator:{provider}"',
        "",
        "[agents.orchestrator]",
        'role = "agentroles.ccb_orchestrator"',
        *profile_model_lines,
        "",
        "[loop.capacity]",
        "enabled = true",
        "max_nodes = 2",
        'default_lifetime = "current_round"',
        'name_template = "l{loop_id}-{profile}-{index}"',
        'reuse = "prefer_idle"',
        "",
        "[loop.role_profiles.worker]",
        'role = "agentroles.coder"',
        f'provider = "{provider}"',
        *profile_model_lines,
        'workspace_mode = "inplace"',
        "max_instances = 1",
        'reuse = "prefer_idle"',
        "",
        "[loop.role_profiles.code_reviewer]",
        'role = "agentroles.code_reviewer"',
        f'provider = "{provider}"',
        *profile_model_lines,
        'workspace_mode = "inplace"',
        "max_instances = 1",
        'reuse = "prefer_idle"',
        "",
    ]
    return "\n".join(lines)


def prepare_project(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    model: str | None = None,
    ccb_test: Path | None = None,
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
    config_path.write_text(build_config(provider=provider, model=model), encoding="utf-8")

    role_store = project_root / "roles"
    _install_orchestrator_role(role_store)
    _write_minimal_role(role_store, "agentroles.coder", default_agent_name="worker")
    _write_minimal_role(role_store, "agentroles.code_reviewer", default_agent_name="code_reviewer")
    shim_payload = _install_cli_shims(project_root=project_root, ccb_test=ccb_test) if ccb_test is not None else {}

    payload = {
        "project_root": str(project_root),
        "config_path": str(config_path),
        "role_store": str(role_store),
        "source_home": str(source_home),
    }
    payload.update(shim_payload)
    return payload


def preflight(*, test_root: Path, project_name: str, provider: str, ccb_test: Path) -> dict[str, Any]:
    root = test_root.expanduser().resolve(strict=False)
    project_root = (root / project_name).resolve(strict=False)
    executable = PROVIDER_EXECUTABLES.get(provider, provider)
    provider_path = shutil.which(executable)
    source_home = root / "source_home"
    real_home = Path.home()
    checks = {
        "ccb_test_exists": ccb_test.exists(),
        "rolepack_source_exists": ROLEPACK_SOURCE.is_dir(),
        "test_root_exists": root.is_dir(),
        "project_under_test_root": root in project_root.parents or project_root == root,
        "provider_executable": executable,
        "provider_executable_path": provider_path,
        "provider_executable_found": provider == "fake" or provider_path is not None,
        "source_home": str(source_home),
        "source_home_exists": source_home.is_dir(),
        "source_home_provider_auth_exists": _provider_auth_exists(provider=provider, home=source_home),
        "real_home": str(real_home),
        "real_home_provider_auth_exists": _provider_auth_exists(provider=provider, home=real_home),
        "default_loop_id": "rp1",
        "default_worker_name": _generated_agent_name(loop_id="rp1", profile="worker"),
        "default_reviewer_name": _generated_agent_name(loop_id="rp1", profile="code_reviewer"),
        "default_generated_names_valid": _default_generated_names_valid(),
        "real_run_opt_in": os.environ.get(REAL_RUN_ENV) == "1",
    }
    return {
        "preflight_status": "ok" if all(bool(checks[key]) for key in _required_preflight_keys()) else "blocked",
        "provider": provider,
        "project_root": str(project_root),
        "ccb_test": str(ccb_test),
        "checks": checks,
    }


def run_smoke(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    loop_id: str,
    task: str,
    provider_home_mode: str,
    timeout_s: int,
) -> dict[str, Any]:
    if os.environ.get(REAL_RUN_ENV) != "1":
        raise RuntimeError(f"real provider smoke requires {REAL_RUN_ENV}=1")
    project_root = (test_root.expanduser().resolve(strict=False) / project_name).resolve(strict=False)
    role_store = project_root / "roles"
    env = _smoke_env(
        test_root=test_root,
        project_root=project_root,
        role_store=role_store,
        provider_home_mode=provider_home_mode,
    )
    commands = [
        ("diagnose", [str(ccb_test), "--diagnose"]),
        ("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"]),
        ("start", [str(ccb_test), "--project", str(project_root)]),
        (
            "run_once",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "loop",
                "run-once",
                "--loop-id",
                loop_id,
                "--task",
                task,
                "--timeout",
                str(timeout_s),
                "--json",
            ],
        ),
    ]
    results: list[dict[str, Any]] = []
    try:
        for name, command in commands:
            completed = subprocess.run(
                command,
                cwd=str(test_root),
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout_s + 30,
            )
            results.append(_completed_payload(name, command, completed))
            if completed.returncode != 0:
                if name == "run_once":
                    results.extend(
                        _collect_post_failure_snapshots(
                            ccb_test=ccb_test,
                            project_root=project_root,
                            test_root=test_root,
                            env=env,
                            loop_id=loop_id,
                        )
                    )
                break
    finally:
        kill = subprocess.run(
            [str(ccb_test), "--project", str(project_root), "kill", "-f"],
            cwd=str(test_root),
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        results.append(_completed_payload("kill", [str(ccb_test), "--project", str(project_root), "kill", "-f"], kill))
    run_once = next((item for item in results if item["name"] == "run_once"), None)
    run_once_payload = _json_payload(run_once["stdout"]) if run_once else None
    return {
        "smoke_status": "ok" if run_once_payload and run_once_payload.get("loop_run_status") == "ok" else "failed",
        "provider": provider,
        "project_root": str(project_root),
        "loop_id": loop_id,
        "provider_home_mode": provider_home_mode,
        "run_once_payload": run_once_payload,
        "results": results,
    }


def run_autonomous_smoke(
    *,
    test_root: Path,
    project_name: str,
    provider: str,
    ccb_test: Path,
    loop_id: str,
    task: str,
    provider_home_mode: str,
    timeout_s: int,
    repeat_count: int = 1,
) -> dict[str, Any]:
    if os.environ.get(REAL_RUN_ENV) != "1":
        raise RuntimeError(f"real provider autonomous smoke requires {REAL_RUN_ENV}=1")
    if repeat_count < 1:
        raise ValueError("repeat_count must be >= 1")
    project_root = (test_root.expanduser().resolve(strict=False) / project_name).resolve(strict=False)
    role_store = project_root / "roles"
    _install_cli_shims(project_root=project_root, ccb_test=ccb_test)
    env = _smoke_env(
        test_root=test_root,
        project_root=project_root,
        role_store=role_store,
        provider_home_mode=provider_home_mode,
    )
    env["CCB_WATCH_TIMEOUT_S"] = str(timeout_s)
    results: list[dict[str, Any]] = []
    rounds: list[dict[str, Any]] = []
    try:
        for name, command in (
            ("diagnose", [str(ccb_test), "--diagnose"]),
            ("config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"]),
            ("start", [str(ccb_test), "--project", str(project_root)]),
        ):
            completed = _run_command(name, command, cwd=test_root, env=env, timeout=timeout_s + 30)
            results.append(completed)
            if completed["returncode"] != 0:
                break
        else:
            for index in range(1, repeat_count + 1):
                round_loop_id = _round_loop_id(loop_id=loop_id, index=index, repeat_count=repeat_count)
                round_payload = _run_autonomous_round(
                    ccb_test=ccb_test,
                    project_root=project_root,
                    test_root=test_root,
                    env=env,
                    loop_id=round_loop_id,
                    task=task,
                    timeout_s=timeout_s,
                    round_index=index,
                )
                rounds.append(round_payload)
                results.extend(round_payload["results"])
                if round_payload["round_status"] != "ok":
                    break
    finally:
        kill = _run_command(
            "kill",
            [str(ccb_test), "--project", str(project_root), "kill", "-f"],
            cwd=test_root,
            env=env,
            timeout=60,
        )
        results.append(kill)
    last_round = rounds[-1] if rounds else {}
    return {
        "autonomous_status": (
            "ok" if len(rounds) == repeat_count and all(item["round_status"] == "ok" for item in rounds) else "failed"
        ),
        "provider": provider,
        "project_root": str(project_root),
        "loop_id": loop_id,
        "repeat_count": repeat_count,
        "provider_home_mode": provider_home_mode,
        "parent_job_id": last_round.get("parent_job_id"),
        "watch_status": last_round.get("watch_status"),
        "watch_reply": last_round.get("watch_reply", ""),
        "capacity_payload": last_round.get("capacity_payload"),
        "layout_payload": last_round.get("layout_payload"),
        "rounds": rounds,
        "results": results,
    }


def _run_autonomous_round(
    *,
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    loop_id: str,
    task: str,
    timeout_s: int,
    round_index: int,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    ask_completed = _run_command(
        f"round_{round_index}_ask_orchestrator",
        [str(ccb_test), "--project", str(project_root), "ask", "orchestrator"],
        cwd=test_root,
        env=env,
        timeout=60,
        input_text=_autonomous_orchestrator_message(loop_id=loop_id, task=task),
    )
    results.append(ask_completed)
    parent_job_id = _extract_job_id(str(ask_completed.get("stdout") or ""))
    if ask_completed["returncode"] == 0 and parent_job_id:
        results.extend(
            _watch_autonomous_callback_chain(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                parent_job_id=parent_job_id,
                timeout_s=timeout_s,
            )
        )
    capacity_payload: dict[str, Any] | None = None
    layout_payload: dict[str, Any] | None = None
    if parent_job_id:
        capacity_completed = _run_command(
            f"round_{round_index}_capacity_status",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "loop",
                "capacity",
                "status",
                "--loop-id",
                loop_id,
                "--json",
            ],
            cwd=test_root,
            env=env,
            timeout=60,
        )
        results.append(capacity_completed)
        capacity_payload = _json_payload(str(capacity_completed.get("stdout") or ""))
        layout_completed = _run_command(
            f"round_{round_index}_layout_status",
            [str(ccb_test), "--project", str(project_root), "layout", "status", "--json"],
            cwd=test_root,
            env=env,
            timeout=60,
        )
        results.append(layout_completed)
        layout_payload = _json_payload(str(layout_completed.get("stdout") or ""))
    for name, command in (
        (f"round_{round_index}_post_autonomous_ps", [str(ccb_test), "--project", str(project_root), "ps"]),
        (
            f"round_{round_index}_post_autonomous_config_validate",
            [str(ccb_test), "--project", str(project_root), "config", "validate"],
        ),
    ):
        results.append(_run_command(name, command, cwd=test_root, env=env, timeout=60))
    watch = _last_watch_result(results)
    watch_status = _line_value(str(watch.get("stdout") or ""), "status") if watch else None
    reply = _line_value(str(watch.get("stdout") or ""), "reply") if watch else ""
    return {
        "round_status": (
            "ok"
            if _autonomous_success(
                watch_status=watch_status,
                reply=reply,
                capacity=capacity_payload,
                layout=layout_payload,
            )
            else "failed"
        ),
        "round_index": round_index,
        "loop_id": loop_id,
        "parent_job_id": parent_job_id,
        "watch_status": watch_status,
        "watch_reply": reply,
        "capacity_payload": capacity_payload,
        "layout_payload": layout_payload,
        "results": results,
    }


def _round_loop_id(*, loop_id: str, index: int, repeat_count: int) -> str:
    return loop_id


def _watch_autonomous_callback_chain(
    *,
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    parent_job_id: str,
    timeout_s: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    deadline = time.monotonic() + max(1, timeout_s)
    current_job_id = parent_job_id
    watched: set[str] = set()
    for index in range(8):
        if current_job_id in watched:
            break
        watched.add(current_job_id)
        remaining = max(1, int(deadline - time.monotonic()))
        completed = _run_command(
            f"watch_{index}_{current_job_id}",
            [str(ccb_test), "--project", str(project_root), "watch", current_job_id],
            cwd=test_root,
            env=env,
            timeout=remaining + 30,
        )
        results.append(completed)
        if completed.get("returncode") not in (0, None):
            break
        reply = _line_value(str(completed.get("stdout") or ""), "reply")
        if "AUTONOMOUS_LOOP_STATUS:" in reply:
            break
        child_job_id = _wait_for_callback_child_job_id(project_root=project_root, parent_job_id=current_job_id, deadline=deadline)
        if not child_job_id:
            break
        if child_job_id in watched:
            break
        watched.add(child_job_id)
        remaining = max(1, int(deadline - time.monotonic()))
        child = _run_command(
            f"watch_{index}_callback_child_{child_job_id}",
            [str(ccb_test), "--project", str(project_root), "watch", child_job_id],
            cwd=test_root,
            env=env,
            timeout=remaining + 30,
        )
        results.append(child)
        if child.get("returncode") not in (0, None):
            break
        continuation_job_id = _wait_for_callback_continuation_job_id(
            project_root=project_root,
            child_job_id=child_job_id,
            deadline=deadline,
        )
        if not continuation_job_id:
            break
        current_job_id = continuation_job_id
    return results


def _wait_for_callback_child_job_id(*, project_root: Path, parent_job_id: str, deadline: float) -> str | None:
    while time.monotonic() <= deadline:
        child = _callback_child_job_id(project_root=project_root, parent_job_id=parent_job_id)
        if child:
            return child
        time.sleep(0.2)
    return None


def _wait_for_callback_continuation_job_id(*, project_root: Path, child_job_id: str, deadline: float) -> str | None:
    while time.monotonic() <= deadline:
        continuation = _callback_continuation_job_id(project_root=project_root, child_job_id=child_job_id)
        if continuation:
            return continuation
        time.sleep(0.2)
    return None


def _callback_child_job_id(*, project_root: Path, parent_job_id: str) -> str | None:
    for event in _agent_events(project_root):
        if str(event.get("job_id") or "") != parent_job_id:
            continue
        if str(event.get("type") or "") != "job_delegated_callback":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            child = str(payload.get("callback_child_job_id") or "").strip()
            if child:
                return child
    return None


def _callback_continuation_job_id(*, project_root: Path, child_job_id: str) -> str | None:
    for event in _agent_events(project_root):
        if str(event.get("job_id") or "") != child_job_id:
            continue
        if str(event.get("type") or "") != "callback_continuation_submitted":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            continuation = str(payload.get("continuation_job_id") or "").strip()
            if continuation:
                return continuation
    return None


def _agent_events(project_root: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted((project_root / ".ccb" / "agents").glob("*/events.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
    return events


def _last_watch_result(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in reversed(results):
        if str(item.get("name") or "").startswith("watch_"):
            return item
    return None


def _collect_post_failure_snapshots(
    *,
    ccb_test: Path,
    project_root: Path,
    test_root: Path,
    env: dict[str, str],
    loop_id: str,
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    commands = [
        ("post_failure_ps", [str(ccb_test), "--project", str(project_root), "ps"]),
        ("post_failure_config_validate", [str(ccb_test), "--project", str(project_root), "config", "validate"]),
    ]
    for target in _post_failure_pend_targets(project_root=project_root, loop_id=loop_id):
        commands.append((f"post_failure_pend_{target}", [str(ccb_test), "--project", str(project_root), "pend", target]))
    for name, command in commands:
        try:
            completed = subprocess.run(
                command,
                cwd=str(test_root),
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            snapshots.append(_completed_payload(name, command, completed))
        except subprocess.TimeoutExpired as exc:
            snapshots.append(
                {
                    "name": name,
                    "command": command,
                    "returncode": None,
                    "stdout": exc.stdout or "",
                    "stderr": exc.stderr or "",
                    "timeout": True,
                }
            )
    return snapshots


def _post_failure_pend_targets(*, project_root: Path, loop_id: str) -> list[str]:
    targets: list[str] = []
    round_path = project_root / ".ccb" / "runtime" / "loops" / loop_id / "round.json"
    payload = _read_json_object(round_path)
    agents = payload.get("agents") if isinstance(payload, dict) else None
    if isinstance(agents, dict):
        for name in ("worker", "reviewer", "orchestrator"):
            value = str(agents.get(name) or "").strip()
            if value and value not in targets:
                targets.append(value)
    return targets


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _install_cli_shims(*, project_root: Path, ccb_test: Path | None) -> dict[str, str]:
    if ccb_test is None:
        return {}
    bin_dir = project_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    ccb_test_path = ccb_test.expanduser().resolve(strict=False)
    payload: dict[str, str] = {"bin_dir": str(bin_dir)}
    for name, args in {
        "ccb": "",
        "ask": " ask",
    }.items():
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


def _install_orchestrator_role(role_store: Path) -> None:
    if not ROLEPACK_SOURCE.is_dir():
        raise FileNotFoundError(f"orchestrator RolePack source missing: {ROLEPACK_SOURCE}")
    target = role_store / "installed" / "agentroles.ccb_orchestrator" / "current"
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROLEPACK_SOURCE, target)


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


def _required_preflight_keys() -> tuple[str, ...]:
    return (
        "ccb_test_exists",
        "rolepack_source_exists",
        "test_root_exists",
        "project_under_test_root",
        "provider_executable_found",
        "source_home_exists",
        "default_generated_names_valid",
    )


def _generated_agent_name(*, loop_id: str, profile: str) -> str:
    return f"l{loop_id}-{profile}-1"


def _default_generated_names_valid() -> bool:
    return all(
        bool(AGENT_NAME_RE.fullmatch(name))
        for name in (
            _generated_agent_name(loop_id="rp1", profile="worker"),
            _generated_agent_name(loop_id="rp1", profile="code_reviewer"),
        )
    )


def _smoke_env(*, test_root: Path, project_root: Path, role_store: Path, provider_home_mode: str) -> dict[str, str]:
    env = dict(os.environ)
    source_home = test_root.expanduser().resolve(strict=False) / "source_home"
    if provider_home_mode == "source-home":
        provider_home = source_home
    elif provider_home_mode == "real-home":
        provider_home = Path.home()
    else:
        raise ValueError(f"unsupported provider home mode: {provider_home_mode}")
    env["HOME"] = str(provider_home)
    env["CCB_SOURCE_HOME"] = str(provider_home)
    env["AGENT_ROLES_STORE"] = str(role_store)
    env["CCB_NO_ATTACH"] = "1"
    bin_dir = project_root / "bin"
    if bin_dir.is_dir():
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return env


def _run_command(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    input_text: str | None = None,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            input=input_text,
            capture_output=True,
            timeout=timeout,
        )
        return _completed_payload(name, command, completed)
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "command": command,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timeout": True,
        }


def _completed_payload(name: str, command: list[str], completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "name": name,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _json_payload(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_job_id(text: str) -> str | None:
    match = re.search(r"\bjob=(job_[A-Za-z0-9_-]+)\b", text)
    return match.group(1) if match else None


def _line_value(text: str, key: str) -> str:
    prefix = f"{key}: "
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""


def _autonomous_success(
    *,
    watch_status: str | None,
    reply: str,
    capacity: dict[str, Any] | None,
    layout: dict[str, Any] | None,
) -> bool:
    if str(watch_status or "").strip() != "completed":
        return False
    if "AUTONOMOUS_LOOP_STATUS: pass" not in reply:
        return False
    if not isinstance(capacity, dict):
        return False
    if str(capacity.get("loop_capacity_status") or "") != "released":
        return False
    if int(capacity.get("retained_count") or 0) != 0:
        return False
    if not isinstance(layout, dict):
        return False
    if str(layout.get("layout_status") or "") != "ok":
        return False
    if int(layout.get("loop_agent_count") or 0) != 0:
        return False
    return True


def autonomous_cleanup_contract() -> dict[str, Any]:
    canonical_reply = "AUTONOMOUS_LOOP_STATUS: pass release_status: released released_count: 2 retained_count: 0"
    canonical_capacity = {"loop_capacity_status": "released", "retained_count": 0}
    canonical_layout = {"layout_status": "ok", "loop_agent_count": 0}
    rejections = {
        "watch_not_completed": not _autonomous_success(
            watch_status="running",
            reply=canonical_reply,
            capacity=canonical_capacity,
            layout=canonical_layout,
        ),
        "missing_pass_marker": not _autonomous_success(
            watch_status="completed",
            reply="AUTONOMOUS_LOOP_STATUS: blocked",
            capacity=canonical_capacity,
            layout=canonical_layout,
        ),
        "capacity_not_released": not _autonomous_success(
            watch_status="completed",
            reply=canonical_reply,
            capacity={"loop_capacity_status": "ensured", "retained_count": 0},
            layout=canonical_layout,
        ),
        "capacity_retained_agents": not _autonomous_success(
            watch_status="completed",
            reply=canonical_reply,
            capacity={"loop_capacity_status": "released", "retained_count": 1},
            layout=canonical_layout,
        ),
        "missing_layout_payload": not _autonomous_success(
            watch_status="completed",
            reply=canonical_reply,
            capacity=canonical_capacity,
            layout=None,
        ),
        "layout_not_ok": not _autonomous_success(
            watch_status="completed",
            reply=canonical_reply,
            capacity=canonical_capacity,
            layout={"layout_status": "failed", "loop_agent_count": 0},
        ),
        "layout_retains_loop_agents": not _autonomous_success(
            watch_status="completed",
            reply=canonical_reply,
            capacity=canonical_capacity,
            layout={"layout_status": "ok", "loop_agent_count": 1},
        ),
    }
    canonical_pass = _autonomous_success(
        watch_status="completed",
        reply=canonical_reply,
        capacity=canonical_capacity,
        layout=canonical_layout,
    )
    return {
        "autonomous_cleanup_contract_status": "ok" if canonical_pass and all(rejections.values()) else "failed",
        "canonical_pass": canonical_pass,
        "required_final_checks": [
            "watch_status=completed",
            "AUTONOMOUS_LOOP_STATUS: pass",
            "capacity.loop_capacity_status=released",
            "capacity.retained_count=0",
            "layout.layout_status=ok",
            "layout.loop_agent_count=0",
        ],
        "rejections": rejections,
    }


def _autonomous_orchestrator_message(*, loop_id: str, task: str) -> str:
    return f"""Use the `orchestrator-capacity` skill and complete this as an autonomous CCB loop round.

Loop id: {loop_id}
Task packet: {task}

Hard requirements:
- Do not use `ccb loop run-once`.
- Do not edit files, tmux, provider state, `.ccb/ccb.config`, or `.ccb/runtime` directly.
- Use `ccb loop capacity ensure --loop-id {loop_id} --profile worker=1 --profile code_reviewer=1 --json`.
- Treat `loop_capacity_status = ensured` and `apply.apply_status = applied` as live capacity success.
- Parse the returned agent names; do not invent names from the template.
- Ask the returned worker with `command ask --chain "$WORKER_AGENT"` and then stop until resumed.
- After the worker callback resumes you, ask the returned reviewer with `command ask --chain "$REVIEWER_AGENT"` and then stop until resumed.
- After the reviewer callback resumes you, run `ccb loop capacity status --loop-id {loop_id} --json`, then `ccb loop capacity release --loop-id {loop_id} --policy auto --json`, then final status if useful.

Worker request:
- Reply with exactly one small result: `status: done` and one evidence line.
- No file edits.

Reviewer request:
- Review the worker reply against this task packet.
- Reply with `status: pass` if the worker reply contains `status: done`, otherwise `status: rework_required`.
- Include one fallback/degradation audit line.

Final reply schema:
AUTONOMOUS_LOOP_STATUS: pass|blocked
worker_agent: <name>
reviewer_agent: <name>
worker_job: <job id or unknown>
reviewer_job: <job id or unknown>
release_status: <released|retained|failed>
released_count: <number or unknown>
retained_count: <number or unknown>
evidence: <one concise line>
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare and optionally run the orchestrator capacity real-provider smoke.")
    parser.add_argument("--test-root", default=str(DEFAULT_TEST_ROOT))
    parser.add_argument("--project-name", default="orchestrator-capacity-real-provider-smoke")
    parser.add_argument("--provider", default="codex", choices=sorted(PROVIDER_EXECUTABLES))
    parser.add_argument("--model", default=None)
    parser.add_argument("--ccb-test", default=str(REPO_ROOT / "ccb_test"))
    parser.add_argument("--loop-id", default="rp1")
    parser.add_argument("--task", default="Reply with a concise pass/fail summary for this CCB orchestrator capacity smoke.")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--provider-home-mode", choices=("source-home", "real-home"), default="source-home")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-autonomous", action="store_true")
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
        model=args.model,
        ccb_test=ccb_test,
        reset=bool(args.reset),
    )
    payload: dict[str, Any] = {
        "prepare": prepared,
        "preflight": preflight(test_root=test_root, project_name=args.project_name, provider=args.provider, ccb_test=ccb_test),
        "autonomous_cleanup_contract": autonomous_cleanup_contract(),
    }
    if args.run:
        payload["run"] = run_smoke(
            test_root=test_root,
            project_name=args.project_name,
            provider=args.provider,
            ccb_test=ccb_test,
            loop_id=args.loop_id,
            task=args.task,
            provider_home_mode=args.provider_home_mode,
            timeout_s=int(args.timeout),
        )
    if args.run_autonomous:
        payload["autonomous"] = run_autonomous_smoke(
            test_root=test_root,
            project_name=args.project_name,
            provider=args.provider,
            ccb_test=ccb_test,
            loop_id=args.loop_id,
            task=args.task,
            provider_home_mode=args.provider_home_mode,
            timeout_s=int(args.timeout),
            repeat_count=int(args.repeat),
        )
    if args.json or args.prepare_only or args.run or args.run_autonomous:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"project_root: {prepared['project_root']}")
        print(f"role_store: {prepared['role_store']}")
        print(f"preflight_status: {payload['preflight']['preflight_status']}")
        print(f"run_requires: {REAL_RUN_ENV}=1 --run or --run-autonomous")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
