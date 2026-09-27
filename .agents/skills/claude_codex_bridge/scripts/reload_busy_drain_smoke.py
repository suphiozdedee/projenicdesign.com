#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from time import monotonic, sleep
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dynamic_layout_smoke as layout_smoke  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = REPO_ROOT / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from ccbd.socket_client import CcbdClient  # noqa: E402
from storage.paths import PathLayout  # noqa: E402


DEFAULT_TEST_ROOT = Path(os.environ.get("CCB_RELOAD_BUSY_DRAIN_SMOKE_TEST_ROOT", "/home/bfly/yunwei/test_ccb2"))
DEFAULT_CCB_TEST = REPO_ROOT / "ccb_test"
DEFAULT_COMMAND_TIMEOUT_S = int(os.environ.get("CCB_RELOAD_BUSY_DRAIN_SMOKE_COMMAND_TIMEOUT_S", "90"))
REAL_RUN_ENV = "CCB_RELOAD_BUSY_DRAIN_SMOKE_RUN_REAL"


def build_busy_remove_config(
    *,
    provider: str = "fake",
    include_agent2: bool = True,
    sidebar_width: str | None = None,
) -> str:
    agents = f"agent1:{provider}, agent2:{provider}" if include_agent2 else f"agent1:{provider}"
    lines = [
        "version = 2",
        'entry_window = "main"',
        "",
        "[windows]",
        f'main = "{agents}"',
        "",
    ]
    if sidebar_width:
        lines.extend(
            [
                "[ui.sidebar]",
                'mode = "every_window"',
                f'width = "{sidebar_width}"',
                "",
            ]
        )
    return "\n".join(lines)


def prepare_busy_remove_project(
    *,
    test_root: Path,
    project_name: str,
    provider: str = "fake",
    reset: bool = False,
    sidebar_width: str | None = None,
) -> dict[str, str]:
    project_root = layout_smoke._project_root(test_root, project_name)
    if reset and project_root.exists():
        shutil.rmtree(project_root)
    _write_project_config(
        project_root,
        build_busy_remove_config(provider=provider, include_agent2=True, sidebar_width=sidebar_width),
    )
    role_store = project_root / "roles"
    role_store.mkdir(parents=True, exist_ok=True)
    return {"project_root": str(project_root), "role_store": str(role_store)}


def run_busy_remove_drain_smoke(
    *,
    test_root: Path,
    project_name: str,
    ccb_test: Path,
    provider: str = "fake",
    provider_home_mode: str = "source-home",
    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
    reset: bool = False,
    keep_running: bool = False,
    busy_latency_ms: int = 5000,
    auto_retry: bool = False,
    check_sidebar_render: bool = False,
) -> dict[str, Any]:
    test_root = test_root.expanduser().resolve(strict=False)
    test_root.mkdir(parents=True, exist_ok=True)
    preflight_payload = layout_smoke.preflight(
        test_root=test_root,
        provider=provider,
        ccb_test=ccb_test,
        provider_home_mode=provider_home_mode,
    )
    preflight_payload["checks"]["busy_drain_real_run_opt_in"] = os.environ.get(REAL_RUN_ENV) == "1"
    if provider != "fake" and os.environ.get(REAL_RUN_ENV) != "1":
        raise RuntimeError(f"real provider busy reload drain smoke requires {REAL_RUN_ENV}=1")

    sidebar_width = "40%" if check_sidebar_render else None
    prepared = prepare_busy_remove_project(
        test_root=test_root,
        project_name=project_name,
        provider=provider,
        reset=reset,
        sidebar_width=sidebar_width,
    )
    project_root = Path(prepared["project_root"])
    provider_home = layout_smoke._provider_home(test_root=test_root, mode=provider_home_mode)
    provider_home.mkdir(parents=True, exist_ok=True)
    env = layout_smoke._env(provider_home=provider_home, role_store=Path(prepared["role_store"]))
    if auto_retry:
        env["CCB_CCBD_IDLE_FULL_HEARTBEAT_INTERVAL_S"] = "1"
    else:
        env["CCB_CCBD_RELOAD_DRAIN_AUTO_RETRY"] = "0"
    commands: list[dict[str, Any]] = []
    expected_failure_commands: list[dict[str, Any]] = []

    try:
        commands.append(
            layout_smoke._run(
                "config_validate_initial",
                [str(ccb_test), "--project", str(project_root), "config", "validate"],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
        )
        commands.append(
            layout_smoke._run(
                "start",
                [str(ccb_test), "--project", str(project_root)],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
        )
        busy_ask = layout_smoke._run(
            "ask_agent2_busy",
            [
                str(ccb_test),
                "--project",
                str(project_root),
                "ask",
                "--task-id",
                f"fake;latency_ms={max(1000, int(busy_latency_ms))}",
                "agent2",
            ],
            cwd=test_root,
            env=env,
            input_text="busy reload drain smoke holds agent2 while config removes it\n",
            timeout=command_timeout_s,
        )
        commands.append(busy_ask)

        _write_project_config(
            project_root,
            build_busy_remove_config(provider=provider, include_agent2=False, sidebar_width=sidebar_width),
        )
        blocked_reload = layout_smoke._run(
            "reload_remove_agent2_while_busy",
            [str(ccb_test), "--project", str(project_root), "reload"],
            cwd=test_root,
            env=env,
            timeout=command_timeout_s,
        )
        commands.append(blocked_reload)
        expected_failure_commands.append(blocked_reload)

        blocked_view = _project_view_result("project_view_after_blocked_reload", project_root, timeout_s=5.0)
        commands.append(blocked_view)
        blocked_view_payload = layout_smoke._payload(blocked_view)

        sidebar_render = None
        if check_sidebar_render:
            sidebar_render = _wait_for_sidebar_drain_render(
                "sidebar_render_after_blocked_reload",
                blocked_view_payload,
                cwd=test_root,
                env=env,
                timeout_s=8.0,
            )
            commands.append(sidebar_render)

        rejected_ask = layout_smoke._run(
            "ask_agent2_during_reload_drain",
            [str(ccb_test), "--project", str(project_root), "ask", "agent2"],
            cwd=test_root,
            env=env,
            input_text="this should be rejected while agent2 is draining\n",
            timeout=command_timeout_s,
        )
        commands.append(rejected_ask)
        expected_failure_commands.append(rejected_ask)

        commands.extend(
            layout_smoke._watch_submitted_jobs(
                ccb_test=ccb_test,
                project_root=project_root,
                test_root=test_root,
                env=env,
                asks=(busy_ask,),
                timeout=max(command_timeout_s, int(busy_latency_ms / 1000) + 10),
            )
        )

        auto_retry_wait = None
        if auto_retry:
            retry_reload = None
            auto_retry_wait = _wait_for_auto_retry(
                "wait_for_auto_retry_remove_agent2",
                project_root,
                timeout_s=max(20.0, float(busy_latency_ms) / 1000.0 + 15.0),
            )
            commands.append(auto_retry_wait)
            final_view = auto_retry_wait if _returncode(auto_retry_wait, default=1) == 0 else _project_view_result("project_view_after_auto_retry_timeout", project_root, timeout_s=5.0)
        else:
            retry_reload = layout_smoke._run(
                "reload_remove_agent2_after_drain",
                [str(ccb_test), "--project", str(project_root), "reload"],
                cwd=test_root,
                env=env,
                timeout=command_timeout_s,
            )
            commands.append(retry_reload)
            final_view = _project_view_result("project_view_after_retry_reload", project_root, timeout_s=5.0)
        if final_view is not auto_retry_wait:
            commands.append(final_view)

        final_view_payload = layout_smoke._payload(final_view)
        successful_commands = [item for item in commands if item not in expected_failure_commands]
        checks = {
            "initial_config_valid": _command_ok(commands, "config_validate_initial"),
            "busy_ask_accepted": layout_smoke._accepted(busy_ask),
            "blocked_reload_returned_blocked": int(blocked_reload.get("returncode") or 0) != 0
            and "reload_status: blocked" in _combined_output(blocked_reload),
            "blocked_reload_is_remove_agent": "plan_class: remove_agent" in _combined_output(blocked_reload),
            "blocked_reload_reports_active_drain": "reload_drain_active_count: 1" in _combined_output(blocked_reload)
            and "reload_drain_active: agent=agent2" in _combined_output(blocked_reload)
            and "reload_drain_retry: ccb reload" in _combined_output(blocked_reload),
            "project_view_records_active_drain": _active_drain_count(blocked_view_payload) == 1
            and _agent_dispatch_blocked_by_drain(blocked_view_payload, "agent2"),
            "sidebar_renders_active_drain": True
            if not check_sidebar_render
            else _returncode(sidebar_render or {}, default=1) == 0
            and "drain:waiting" in _combined_output(sidebar_render or {}),
            "new_ask_rejected_while_draining": int(rejected_ask.get("returncode") or 0) != 0
            and "draining" in _combined_output(rejected_ask),
            "busy_job_terminal": layout_smoke._watch_commands_terminal(commands),
            "retry_reload_published": True
            if auto_retry
            else _returncode(retry_reload or {}, default=1) == 0
            and (
                "reload_status: published" in _combined_output(retry_reload or {})
                or "reload_status: noop" in _combined_output(retry_reload or {})
            ),
            "auto_retry_published": _returncode(auto_retry_wait, default=1) == 0 if auto_retry else True,
            "project_view_drain_cleared": _active_drain_count(final_view_payload) == 0,
            "agent2_removed_from_view": not _view_has_agent(final_view_payload, "agent2")
            and not _view_windows_include_agent(final_view_payload, "agent2"),
        }
        status = "ok" if all(checks.values()) and layout_smoke._all_success(successful_commands) else "failed"
        return {
            "reload_busy_drain_smoke_status": status,
            "provider": provider,
            "provider_home_mode": provider_home_mode,
            "auto_retry": bool(auto_retry),
            "check_sidebar_render": bool(check_sidebar_render),
            "preflight": preflight_payload,
            "project_root": str(project_root),
            "checks": checks,
            "commands": commands,
        }
    finally:
        if not keep_running:
            commands.append(
                layout_smoke._run(
                    "kill",
                    [str(ccb_test), "--project", str(project_root), "kill", "-f"],
                    cwd=test_root,
                    env=env,
                    timeout=command_timeout_s,
                )
            )


def compact_busy_drain_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "reload_busy_drain_smoke_status": payload.get("reload_busy_drain_smoke_status"),
        "provider": payload.get("provider"),
        "provider_home_mode": payload.get("provider_home_mode"),
        "auto_retry": payload.get("auto_retry"),
        "check_sidebar_render": payload.get("check_sidebar_render"),
        "preflight": payload.get("preflight"),
        "project_root": payload.get("project_root"),
        "checks": payload.get("checks"),
        "commands": [
            layout_smoke._compact_command(item)
            for item in payload.get("commands", [])
            if isinstance(item, dict)
        ],
    }


def _write_project_config(project_root: Path, text: str) -> None:
    config_path = project_root / ".ccb" / "ccb.config"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")


def _project_view_result(name: str, project_root: Path, *, timeout_s: float) -> dict[str, Any]:
    try:
        payload = CcbdClient(PathLayout(project_root).ccbd_socket_path, timeout_s=timeout_s).project_view(schema_version=1)
    except Exception as exc:  # pragma: no cover - exercised by real smoke diagnostics.
        return {
            "name": name,
            "returncode": 1,
            "stdout": "",
            "stderr": str(exc),
            "timeout": False,
        }
    return {
        "name": name,
        "returncode": 0,
        "stdout": json.dumps(payload, sort_keys=True),
        "stderr": "",
        "timeout": False,
        "payload": payload,
    }


def _wait_for_auto_retry(name: str, project_root: Path, *, timeout_s: float) -> dict[str, Any]:
    deadline = monotonic() + max(1.0, float(timeout_s))
    attempts = 0
    last: dict[str, Any] | None = None
    while monotonic() < deadline:
        attempts += 1
        current = _project_view_result(f"{name}_attempt_{attempts}", project_root, timeout_s=5.0)
        last = current
        payload = layout_smoke._payload(current)
        if _active_drain_count(payload) == 0 and not _view_has_agent(payload, "agent2") and not _view_windows_include_agent(payload, "agent2"):
            enriched = dict(payload)
            enriched["auto_retry_attempts"] = attempts
            return {
                "name": name,
                "returncode": 0,
                "stdout": json.dumps(enriched, sort_keys=True),
                "stderr": "",
                "timeout": False,
                "payload": enriched,
            }
        sleep(0.5)
    return {
        "name": name,
        "returncode": 1,
        "stdout": str((last or {}).get("stdout") or ""),
        "stderr": "timed out waiting for reload drain auto retry to remove agent2",
        "timeout": True,
        "payload": layout_smoke._payload(last or {}),
    }


def _wait_for_sidebar_drain_render(
    name: str,
    project_view_payload: dict[str, Any],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    expected_text: str = "drain:waiting",
) -> dict[str, Any]:
    scope = _sidebar_capture_scope(project_view_payload, window_name="main")
    if scope is None:
        return {
            "name": name,
            "returncode": 1,
            "stdout": "",
            "stderr": "project_view did not include tmux socket/session and main sidebar pane id\n",
            "timeout": False,
            "payload": {"found": False, "expected_text": expected_text, "reason": "missing_sidebar_scope"},
        }
    socket_path, session_name, sidebar_pane_id = scope
    deadline = monotonic() + max(1.0, float(timeout_s))
    attempts = 0
    last: dict[str, Any] | None = None
    while monotonic() < deadline:
        attempts += 1
        current = layout_smoke._run(
            f"{name}_attempt_{attempts}",
            [
                "tmux",
                "-S",
                socket_path,
                "capture-pane",
                "-p",
                "-t",
                sidebar_pane_id,
                "-S",
                "-80",
            ],
            cwd=cwd,
            env=env,
            timeout=max(2, int(timeout_s)),
        )
        last = current
        if int(current.get("returncode") or 0) == 0 and expected_text in _combined_output(current):
            enriched = dict(current)
            enriched["name"] = name
            enriched["payload"] = {
                "found": True,
                "expected_text": expected_text,
                "attempts": attempts,
                "socket_path": socket_path,
                "session_name": session_name,
                "sidebar_pane_id": sidebar_pane_id,
            }
            return enriched
        sleep(0.5)
    return {
        "name": name,
        "returncode": 1,
        "stdout": str((last or {}).get("stdout") or ""),
        "stderr": f"timed out waiting for sidebar pane {sidebar_pane_id} to render {expected_text!r}",
        "timeout": True,
        "payload": {
            "found": False,
            "expected_text": expected_text,
            "attempts": attempts,
            "socket_path": socket_path,
            "session_name": session_name,
            "sidebar_pane_id": sidebar_pane_id,
        },
    }


def _sidebar_capture_scope(payload: dict[str, Any], *, window_name: str) -> tuple[str, str, str] | None:
    namespace = _view(payload).get("namespace")
    namespace = namespace if isinstance(namespace, dict) else {}
    socket_path = str(namespace.get("socket_path") or "").strip()
    session_name = str(namespace.get("session_name") or "").strip()
    sidebar_pane_id = ""
    for window in tuple(_view(payload).get("windows") or ()):
        if not isinstance(window, dict):
            continue
        if str(window.get("name") or "") == window_name:
            sidebar_pane_id = str(window.get("sidebar_pane_id") or "").strip()
            break
    if not socket_path or not session_name or not sidebar_pane_id:
        return None
    return socket_path, session_name, sidebar_pane_id


def _command_ok(commands: list[dict[str, Any]], name: str) -> bool:
    return any(str(item.get("name") or "") == name and int(item.get("returncode") or 0) == 0 for item in commands)


def _returncode(command: dict[str, Any], *, default: int) -> int:
    value = command.get("returncode")
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _combined_output(command: dict[str, Any]) -> str:
    return f"{command.get('stdout') or ''}\n{command.get('stderr') or ''}"


def _view(payload: dict[str, Any]) -> dict[str, Any]:
    view = payload.get("view")
    return view if isinstance(view, dict) else {}


def _active_drain_count(payload: dict[str, Any]) -> int:
    drains = _view(payload).get("reload_drains")
    if not isinstance(drains, dict):
        return 0
    try:
        return int(drains.get("active_count") or 0)
    except Exception:
        return 0


def _agent_dispatch_blocked_by_drain(payload: dict[str, Any], agent_name: str) -> bool:
    agent = _view_agent(payload, agent_name)
    return bool(agent.get("dispatch_blocked_by_reload_drain")) if agent else False


def _view_has_agent(payload: dict[str, Any], agent_name: str) -> bool:
    return _view_agent(payload, agent_name) is not None


def _view_agent(payload: dict[str, Any], agent_name: str) -> dict[str, Any] | None:
    for agent in tuple(_view(payload).get("agents") or ()):
        if isinstance(agent, dict) and agent.get("name") == agent_name:
            return agent
    return None


def _view_windows_include_agent(payload: dict[str, Any], agent_name: str) -> bool:
    for window in tuple(_view(payload).get("windows") or ()):
        if not isinstance(window, dict):
            continue
        if agent_name in tuple(window.get("agents") or ()):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CCB busy reload drain smoke tests.")
    parser.add_argument("--test-root", type=Path, default=DEFAULT_TEST_ROOT)
    parser.add_argument("--project-name", default="reload-busy-drain-smoke")
    parser.add_argument("--ccb-test", type=Path, default=DEFAULT_CCB_TEST)
    parser.add_argument("--provider", default="fake")
    parser.add_argument("--provider-home-mode", choices=("source-home", "real-home"), default="source-home")
    parser.add_argument("--command-timeout", type=int, default=DEFAULT_COMMAND_TIMEOUT_S)
    parser.add_argument("--busy-latency-ms", type=int, default=5000)
    parser.add_argument("--auto-retry", action="store_true", help="Wait for daemon heartbeat auto retry instead of running a manual retry reload.")
    parser.add_argument("--check-sidebar-render", action="store_true", help="Capture the live sidebar pane and require a drain status marker during the blocked reload.")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--full-output", action="store_true", help="Print complete command stdout and JSON payloads.")
    args = parser.parse_args(argv)

    payload = run_busy_remove_drain_smoke(
        test_root=args.test_root,
        project_name=args.project_name,
        ccb_test=args.ccb_test,
        provider=args.provider,
        provider_home_mode=args.provider_home_mode,
        command_timeout_s=args.command_timeout,
        reset=args.reset,
        keep_running=args.keep_running,
        busy_latency_ms=args.busy_latency_ms,
        auto_retry=bool(args.auto_retry),
        check_sidebar_render=bool(args.check_sidebar_render),
    )
    output = payload if args.full_output else compact_busy_drain_payload(payload)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if payload.get("reload_busy_drain_smoke_status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
