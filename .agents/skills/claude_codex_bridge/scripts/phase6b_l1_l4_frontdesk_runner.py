#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
CCB_TEST = REPO_ROOT / "ccb_test"
PLAN_SLUG = "phase6b-real-provider-l1-l4"
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
STALE_SEQUENCE_RE = re.compile(r"sequence(?:14|17)(?:[^0-9]|$)")
CANONICAL_FRONTDESK_ACTIVATION_RE = re.compile(r"^act-frontdesk-[A-Za-z0-9_-]+\.json$")
ROLE_IDS = (
    "agentroles.ccb_frontdesk",
    "agentroles.ccb_planner",
    "agentroles.ccb_orchestrator",
    "agentroles.ccb_task_detailer",
    "agentroles.ccb_round_reviewer",
    "agentroles.coder",
    "agentroles.code_reviewer",
)
RESIDENT_AGENT_TARGETS = (
    "frontdesk",
    "planner",
    "orchestrator",
    "task_detailer",
    "ccb_round_reviewer",
)
RESIDENT_ASK_TARGETS = RESIDENT_AGENT_TARGETS
READY_RESIDENT_AGENT_STATES = frozenset({"idle"})
RESIDENT_PS_ATTEMPTS = 3
RESIDENT_PS_RETRY_DELAY_SECONDS = 0.5
READ_ONLY_TASK_OBSERVATION_MAX_RETRIES = 8
AUTO_RUNNER_QUIET_ATTEMPTS = 1200
AUTO_RUNNER_QUIET_RETRY_DELAY_SECONDS = 1.0
PLANNER_TASK_SET_WAIT_ATTEMPTS = 1200
PLANNER_TASK_SET_WAIT_RETRY_DELAY_SECONDS = 1.0
TERMINAL_JOB_STATUSES = frozenset(
    {"blocked", "canceled", "cancelled", "completed", "failed", "incomplete"}
)
DYNAMIC_LOOP_PROFILES = (
    "coder",
    "code_reviewer",
)
TASKS = (
    {
        "task_id": "phase6b-l1-doc-direct-execution",
        "expected_route": "direct_execution",
        "expected_final_status": "done",
        "expected_round_result": "pass",
        "expected_classification": "pass",
    },
    {
        "task_id": "phase6b-l2-code-test-direct-execution",
        "expected_route": "direct_execution",
        "expected_final_status": "done",
        "expected_round_result": "pass",
        "expected_classification": "pass",
    },
    {
        "task_id": "phase6b-l3-needs-detail",
        "expected_route": "needs_detail",
        "expected_final_status": "detail_ready",
        "expected_round_result": "detail_ready",
        "expected_classification": "valid_non_success",
    },
    {
        "task_id": "phase6b-l4-macro-adjustment-request",
        "expected_route": "macro_adjustment_request",
        "expected_final_status": "replan_required",
        "expected_round_result": "replan_required",
        "expected_classification": "valid_non_success",
    },
    {
        "task_id": "phase6b-l4-blocked-prerequisite",
        "expected_route": "blocked",
        "expected_final_status": "blocked",
        "expected_round_result": "blocked",
        "expected_classification": "valid_non_success",
    },
)
DIRECT_TASK_IDS = {
    "phase6b-l1-doc-direct-execution",
    "phase6b-l2-code-test-direct-execution",
}


class HarnessError(RuntimeError):
    pass


class HarnessBlocker(HarnessError):
    def __init__(self, *, classification: str, reason: str, message: str) -> None:
        self.classification = classification
        self.reason = reason
        super().__init__(f"{classification}: {reason}: {message}")


def _json_default(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialize {type(value)!r}")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, default=_json_default, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _first_text(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _require_clean_label(label: str, *, field: str) -> str:
    if not LABEL_RE.fullmatch(label):
        raise HarnessError(f"{field} must be a simple label: {label!r}")
    if STALE_SEQUENCE_RE.search(label):
        raise HarnessError(f"{field} contains stale sequence marker: {label}")
    return label


def _require_clean_project_name(project_name: str) -> str:
    if not PROJECT_NAME_RE.fullmatch(project_name):
        raise HarnessError(f"project-name must be a simple directory name: {project_name!r}")
    if STALE_SEQUENCE_RE.search(project_name):
        raise HarnessError(f"project-name contains stale sequence marker: {project_name}")
    return project_name


def _require_clean_path(path: Path, *, field: str) -> Path:
    text = str(path)
    if STALE_SEQUENCE_RE.search(text):
        raise HarnessError(f"{field} contains stale sequence marker: {text}")
    return path


def _path_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def build_paths(root: Path, label: str, project_name: str) -> dict[str, Path]:
    script = root / f"run_l1_l4_frontdesk_{label}.sh"
    return {
        "root": root,
        "project": root / project_name,
        "script": script,
        "manifest": root / f"phase6b_l1_l4_{label}_manifest.json",
        "b7": root / f"phase6b-real-provider-l1-l4-{label}-b7.md",
        "rows": root / "rows" / f"phase6b_l1_l4_{label}_evidence_rows.jsonl",
        "command_log": root / f"phase6b_l1_l4_{label}_command_log.jsonl",
        "role_store": root / "roles",
        "frontdesk_request": root / "frontdesk_l1_l4_entry_request.md",
    }


def command_sequence(script: Path, label: str) -> list[dict[str, object]]:
    commands: list[dict[str, object]] = [
        {"step": "init", "label": f"{label}__init", "argv": ["bash", str(script), "init"]},
        {
            "step": "frontdesk-entry",
            "label": f"{label}__frontdesk_entry",
            "argv": ["bash", str(script), "frontdesk-entry"],
        },
    ]
    for case in TASKS:
        task_id = str(case["task_id"])
        expected_route = str(case["expected_route"])
        commands.append(
            {
                "step": f"start-task:{task_id}",
                "label": f"{label}__{task_id}__start_task",
                "argv": ["bash", str(script), "start-task", task_id],
            }
        )
        commands.append(
            {
                "step": f"continue-route:{task_id}",
                "label": f"{label}__{task_id}__continue_route",
                "argv": ["bash", str(script), "continue-route", task_id, expected_route],
            }
        )
        if expected_route == "needs_detail":
            commands.append(
                {
                    "step": f"continue-detail:{task_id}",
                    "label": f"{label}__{task_id}__continue_detail",
                    "argv": ["bash", str(script), "continue-detail", task_id],
                }
            )
    commands.extend(
        [
            {"step": "b7", "label": f"{label}__b7", "argv": ["bash", str(script), "b7"]},
            {
                "step": "cleanup-after-b7",
                "label": f"{label}__cleanup_after_b7",
                "argv": ["bash", str(script), "cleanup-after-b7"],
            },
        ]
    )
    return commands


def provider_loop_commands(project: Path) -> tuple[tuple[str, ...], ...]:
    base = (str(CCB_TEST), "--project", str(project), "loop", "runner", "--once", "--json")
    return (base,)


def _layout_agent_names(layout: str) -> list[str]:
    names: list[str] = []
    for leaf in re.split(r"[;,()]", layout):
        token = leaf.strip()
        if not token or token == "cmd" or ":" not in token:
            continue
        name = token.split(":", 1)[0].strip()
        if name:
            names.append(name)
    return names


def _unquote_config_value(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def resident_targets_from_config_text(config_text: str) -> list[str]:
    names: list[str] = []
    in_windows = False
    table_seen = False
    for raw_line in config_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            table_seen = True
            in_windows = line == "[windows]"
            continue
        if in_windows and "=" in line:
            _key, _separator, value = line.partition("=")
            names.extend(_layout_agent_names(_unquote_config_value(value)))
        elif not table_seen and "=" not in line:
            names.extend(_layout_agent_names(line))
    return sorted(set(names))


def validate_config_mounts_resident_targets(config_text: str) -> None:
    mounted = set(resident_targets_from_config_text(config_text))
    required = set(RESIDENT_ASK_TARGETS)
    missing = sorted(required - mounted)
    if missing:
        raise HarnessError(
            "resident ask target(s) missing from layout/[windows]: " + ", ".join(missing)
        )
    dynamic_residents = sorted(set(DYNAMIC_LOOP_PROFILES) & mounted)
    if dynamic_residents:
        raise HarnessError(
            "dynamic loop profile(s) must not be resident ask targets: "
            + ", ".join(dynamic_residents)
        )


def build_config_text() -> str:
    return """version = 2
entry_window = "main"

[windows]
main = "frontdesk:codex; planner:codex; task_detailer:codex; orchestrator:codex; ccb_round_reviewer:claude"

[agents.frontdesk]
role = "agentroles.ccb_frontdesk"

[agents.planner]
role = "agentroles.ccb_planner"

[agents.task_detailer]
role = "agentroles.ccb_task_detailer"

[agents.orchestrator]
role = "agentroles.ccb_orchestrator"

[agents.ccb_round_reviewer]
role = "agentroles.ccb_round_reviewer"

[loop.capacity]
enabled = true
max_nodes = 2
default_lifetime = "current_round"
reuse = "prefer_idle"
name_template = "loop-{loop_id}-{profile}-{index}"

[loop.role_profiles.coder]
role = "agentroles.coder"
provider = "codex"
workspace_mode = "inplace"
max_instances = 1
reuse = "prefer_idle"

[loop.role_profiles.code_reviewer]
role = "agentroles.code_reviewer"
provider = "codex"
workspace_mode = "inplace"
max_instances = 1
reuse = "prefer_idle"
"""


def build_manifest(root: Path, label: str, project_name: str) -> dict[str, Any]:
    paths = build_paths(root, label, project_name)
    command_seq = command_sequence(paths["script"], label)
    resident_agent_specs = {
        target: str(paths["project"] / ".ccb" / "agents" / target / "agent.json")
        for target in RESIDENT_ASK_TARGETS
    }
    manifest: dict[str, Any] = {
        "schema": "ccb.phase6b_l1_l4.frontdesk_runner_manifest.v1",
        "label": label,
        "root": str(paths["root"]),
        "project_name": project_name,
        "project": str(paths["project"]),
        "script": str(paths["script"]),
        "manifest": str(paths["manifest"]),
        "b7": str(paths["b7"]),
        "rows": str(paths["rows"]),
        "command_log": str(paths["command_log"]),
        "role_store": str(paths["role_store"]),
        "frontdesk_request": str(paths["frontdesk_request"]),
        "ccb_test": str(CCB_TEST),
        "plan_slug": PLAN_SLUG,
        "tasks": TASKS,
        "resident_agent_targets": list(RESIDENT_AGENT_TARGETS),
        "resident_ask_targets": list(RESIDENT_ASK_TARGETS),
        "resident_agent_specs": resident_agent_specs,
        "dynamic_loop_profiles": list(DYNAMIC_LOOP_PROFILES),
        "command_sequence": command_seq,
        "provider_loop_invocations": [
            {
                "label_suffix": "activate_orchestrator|run_direct_execution_round|activate_detailer",
                "argv": list(provider_loop_commands(paths["project"])[0]),
                "unbounded": True,
            }
        ],
        "provider_environment_policy": {
            "inherits_HOME": True,
            "inherits_CCB_SOURCE_HOME": True,
            "exports_HOME": False,
            "exports_CCB_SOURCE_HOME": False,
            "sets_CCB_SOURCE_RUNTIME_OK": False,
            "sets_AGENT_ROLES_STORE": str(paths["role_store"]),
        },
        "controller_owned_authority": {
            "b7": str(paths["b7"]),
            "rows": str(paths["rows"]),
            "cleanup": "runner cleanup-after-b7 only after pending guard",
            "frontdesk_request": "natural language; workers/providers are not asked to generate B7 or cleanup",
        },
        "role_install_command_template": [
            str(CCB_TEST),
            "roles",
            "install",
            "<role_id>",
            "--skip-tools",
        ],
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    root = Path(str(manifest["root"]))
    project = Path(str(manifest["project"]))
    required_path_fields = ("project", "script", "manifest", "b7", "rows", "command_log", "role_store")
    for key in required_path_fields:
        path = Path(str(manifest[key]))
        _require_clean_path(path, field=key)
        if key != "project" and not _path_under(path, root):
            raise HarnessError(f"{key} must be under root: {path}")
    if project != root / str(manifest["project_name"]):
        raise HarnessError("project path must equal root/project_name")
    _require_clean_label(str(manifest["label"]), field="label")
    _require_clean_project_name(str(manifest["project_name"]))
    resident_targets = set(str(item) for item in manifest.get("resident_ask_targets", []))
    agent_targets = set(str(item) for item in manifest.get("resident_agent_targets", []))
    missing_agent_targets = sorted(set(RESIDENT_AGENT_TARGETS) - agent_targets)
    if missing_agent_targets:
        raise HarnessError(
            "manifest missing resident agent target(s): " + ", ".join(missing_agent_targets)
        )
    if agent_targets != resident_targets:
        raise HarnessError("manifest resident_agent_targets must match resident_ask_targets")
    missing_residents = sorted(set(RESIDENT_AGENT_TARGETS) - resident_targets)
    if missing_residents:
        raise HarnessError(
            "manifest missing resident ask target(s): " + ", ".join(missing_residents)
        )
    dynamic_residents = sorted(set(DYNAMIC_LOOP_PROFILES) & resident_targets)
    if dynamic_residents:
        raise HarnessError(
            "manifest lists dynamic loop profile(s) as resident ask target(s): "
            + ", ".join(dynamic_residents)
        )
    spec_map = manifest.get("resident_agent_specs")
    if not isinstance(spec_map, dict):
        raise HarnessError("manifest missing resident_agent_specs map")
    missing_spec_keys = sorted(set(RESIDENT_AGENT_TARGETS) - {str(key) for key in spec_map})
    if missing_spec_keys:
        raise HarnessError(
            "manifest missing resident agent spec path(s): " + ", ".join(missing_spec_keys)
        )
    for target in RESIDENT_AGENT_TARGETS:
        expected = project / ".ccb" / "agents" / target / "agent.json"
        observed = Path(str(spec_map[target]))
        if observed != expected:
            raise HarnessError(
                f"resident agent spec path mismatch for {target}: expected {expected} observed {observed}"
            )
    active = json.dumps(
        {
            key: manifest[key]
            for key in (
                "label",
                "root",
                "project",
                "script",
                "b7",
                "rows",
                "command_log",
                "role_store",
                "command_sequence",
            )
        },
        sort_keys=True,
    )
    if STALE_SEQUENCE_RE.search(active):
        raise HarnessError("active manifest contains stale sequence14/sequence17 marker")


def write_wrapper(script_path: Path, manifest_path: Path) -> None:
    script_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"exec {sys.executable} {Path(__file__).resolve()} run --manifest {manifest_path} \"$@\"",
                "",
            ]
        ),
        encoding="utf-8",
    )
    script_path.chmod(0o755)


def materialize(root: Path, label: str, project_name: str) -> dict[str, Any]:
    root = _require_clean_path(root.expanduser().resolve(strict=False), field="root")
    label = _require_clean_label(label, field="label")
    project_name = _require_clean_project_name(project_name)
    if root.exists():
        raise HarnessError(f"fresh root already exists: {root}")
    manifest = build_manifest(root, label, project_name)
    root.mkdir(parents=True)
    Path(manifest["rows"]).parent.mkdir(parents=True, exist_ok=True)
    Path(manifest["project"]).mkdir(parents=True)
    Path(manifest["role_store"]).mkdir(parents=True)
    write_wrapper(Path(manifest["script"]), Path(manifest["manifest"]))
    _write_json(Path(manifest["manifest"]), manifest)
    return manifest


def runner_env(manifest: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env["AGENT_ROLES_STORE"] = str(manifest["role_store"])
    env.pop("CCB_SOURCE_RUNTIME_OK", None)
    return env


def log_command(
    manifest: dict[str, Any],
    *,
    label: str,
    argv: list[str],
    returncode: int,
    stdout_path: Path,
    stderr_path: Path,
) -> None:
    log_path = Path(str(manifest["command_log"]))
    record = {
        "run_label": manifest["label"],
        "root": manifest["root"],
        "project": manifest["project"],
        "label": label,
        "returncode": returncode,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "command": argv,
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _command_log_has_label(log_path: Path, label: str) -> bool:
    for line in _read_text(log_path).splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("label") == label:
            return True
    return False


def _output_contains_failed_command_status(path: Path) -> bool:
    text = _read_text(path)
    if re.search(r"(?m)^\s*command_status:\s*failed\s*$", text):
        return True
    if re.search(r'"command_status"\s*:\s*"failed"', text):
        return True
    for line in text.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("command_status") == "failed":
            return True
    return False


def command_output_paths(manifest: dict[str, Any], label_suffix: str) -> tuple[str, Path, Path]:
    label = f"{manifest['label']}__{label_suffix}"
    stdout_path = Path(str(manifest["root"])) / "logs" / f"{label}.stdout"
    stderr_path = Path(str(manifest["root"])) / "logs" / f"{label}.stderr"
    return label, stdout_path, stderr_path


def assert_command_evidence_available(
    manifest: dict[str, Any],
    label: str,
    stdout_path: Path,
    stderr_path: Path,
) -> None:
    log_path = Path(str(manifest["command_log"]))
    conflicts = []
    if _command_log_has_label(log_path, label):
        conflicts.append(f"command_log label already exists: {log_path}")
    if stdout_path.exists():
        conflicts.append(f"stdout already exists: {stdout_path}")
    if stderr_path.exists():
        conflicts.append(f"stderr already exists: {stderr_path}")
    if conflicts:
        raise HarnessError(
            "evidence_integrity_duplicate_label: "
            + label
            + "; "
            + "; ".join(conflicts)
        )


def run_logged(manifest: dict[str, Any], label_suffix: str, argv: list[str]) -> None:
    label, stdout_path, stderr_path = command_output_paths(manifest, label_suffix)
    assert_command_evidence_available(manifest, label, stdout_path, stderr_path)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            check=False,
            env=runner_env(manifest),
        )
    returncode = completed.returncode
    if returncode == 0 and (
        _output_contains_failed_command_status(stdout_path)
        or _output_contains_failed_command_status(stderr_path)
    ):
        returncode = 1
    log_command(
        manifest,
        label=label,
        argv=argv,
        returncode=returncode,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    if returncode != 0:
        raise HarnessError(f"command failed: {label} rc={returncode} stderr={stderr_path}")


def run_logged_stdout(manifest: dict[str, Any], label_suffix: str, argv: list[str]) -> str:
    _label, stdout_path, _stderr_path = command_output_paths(manifest, label_suffix)
    run_logged(manifest, label_suffix, argv)
    return _read_text(stdout_path)


def write_config(manifest: dict[str, Any]) -> None:
    project = Path(str(manifest["project"]))
    config = project / ".ccb" / "ccb.config"
    config.parent.mkdir(parents=True, exist_ok=True)
    config_text = build_config_text()
    validate_config_mounts_resident_targets(config_text)
    config.write_text(config_text, encoding="utf-8")


def materialize_plan_root(manifest: dict[str, Any]) -> None:
    project = Path(str(manifest["project"]))
    plan_root = project / "docs" / "plantree" / "plans" / PLAN_SLUG
    (plan_root / "tasks").mkdir(parents=True, exist_ok=True)
    (plan_root / "status.md").write_text(
        "# Phase 6B Real Provider L1-L4\n\n"
        "Status: active runtime test root controlled by phase6b_l1_l4_frontdesk_runner.py.\n",
        encoding="utf-8",
    )
    _write_json(plan_root / "tasks" / "index.json", {"schema": "ccb.plan.tasks.v1", "tasks": []})


def write_fixtures(manifest: dict[str, Any]) -> None:
    project = Path(str(manifest["project"]))
    (project / "lab_docs").mkdir(parents=True, exist_ok=True)
    (project / "lab_code").mkdir(parents=True, exist_ok=True)
    (project / "tests").mkdir(parents=True, exist_ok=True)
    (project / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (project / "supervisor_imports").mkdir(parents=True, exist_ok=True)


def seed_rolepacks(manifest: dict[str, Any]) -> None:
    for role_id in ROLE_IDS:
        label = "roles_install_" + role_id.replace(".", "_")
        run_logged(manifest, label, [str(CCB_TEST), "roles", "install", role_id, "--skip-tools"])


def resident_agent_spec_paths(manifest: dict[str, Any]) -> dict[str, Path]:
    project = Path(str(manifest["project"]))
    return {
        target: project / ".ccb" / "agents" / target / "agent.json"
        for target in RESIDENT_AGENT_TARGETS
    }


def missing_resident_agent_specs(manifest: dict[str, Any]) -> list[tuple[str, Path]]:
    return [
        (target, path)
        for target, path in resident_agent_spec_paths(manifest).items()
        if not path.is_file()
    ]


def resident_agent_identity(payload: dict[str, Any]) -> tuple[str, str] | None:
    for key in ("name", "id", "agent", "agent_name"):
        value = _first_text(payload.get(key))
        if value:
            return key, value
    return None


def resident_agent_mount_problems(manifest: dict[str, Any]) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    for target, path in resident_agent_spec_paths(manifest).items():
        if not path.is_file():
            problems.append({"target": target, "path": str(path), "reason": "missing"})
            continue
        payload = _read_json(path)
        if not isinstance(payload, dict):
            problems.append({"target": target, "path": str(path), "reason": "invalid_json"})
            continue
        identity = resident_agent_identity(payload)
        if identity is None:
            continue
        key, observed = identity
        if observed != target:
            problems.append(
                {
                    "target": target,
                    "path": str(path),
                    "reason": f"{key}_mismatch",
                    "observed": observed,
                }
            )
    return problems


def assert_resident_agents_mounted(manifest: dict[str, Any]) -> None:
    problems = resident_agent_mount_problems(manifest)
    if not problems:
        return
    detail_parts = []
    for problem in problems:
        observed = problem.get("observed")
        observed_text = f":observed={observed}" if observed else ""
        detail_parts.append(
            f"{problem['target']}:{problem['reason']}{observed_text}:{problem['path']}"
        )
    detail = ", ".join(detail_parts)
    raise HarnessError(
        "resident_agents_not_mounted: "
        + detail
        + "; config/layout is not sufficient until startup/reload has mounted agent specs"
    )


def parse_resident_ps_states(ps_text: str) -> dict[str, str]:
    states: dict[str, str] = {}
    for line in ps_text.splitlines():
        if not line.strip().startswith("agent:"):
            continue
        pairs = dict(re.findall(r"([A-Za-z_][A-Za-z0-9_.-]*)=([^ \t]+)", line))
        name = str(pairs.get("name") or "")
        state = str(pairs.get("state") or "")
        if name in RESIDENT_ASK_TARGETS and state:
            states[name] = state
    return states


def resident_readiness_problems(manifest: dict[str, Any], ps_text: str) -> list[dict[str, str]]:
    states = parse_resident_ps_states(ps_text)
    problems: list[dict[str, str]] = []
    for target in manifest.get("resident_agent_targets", RESIDENT_AGENT_TARGETS):
        target_text = str(target)
        observed = states.get(target_text)
        if observed is None:
            problems.append({"target": target_text, "reason": "state_missing"})
        elif observed not in READY_RESIDENT_AGENT_STATES:
            problems.append(
                {"target": target_text, "reason": "state_not_ready", "observed": observed}
            )
    return problems


def assert_resident_agents_ready_from_ps(manifest: dict[str, Any], ps_text: str) -> None:
    problems = resident_readiness_problems(manifest, ps_text)
    if not problems:
        return
    detail_parts = []
    for problem in problems:
        observed = problem.get("observed")
        observed_text = f":observed={observed}" if observed else ""
        detail_parts.append(f"{problem['target']}:{problem['reason']}{observed_text}")
    raise HarnessError("resident_agents_not_ready: " + ", ".join(detail_parts))


def assert_resident_agents_ready(manifest: dict[str, Any], label_suffix: str) -> None:
    ps_text = resident_ps_text_with_retry(manifest, label_suffix)
    assert_resident_agents_ready_from_ps(manifest, ps_text)


def resident_ps_text_with_retry(manifest: dict[str, Any], label_suffix: str) -> str:
    ps_text = ""
    for attempt in range(RESIDENT_PS_ATTEMPTS):
        suffix = label_suffix if attempt == 0 else f"{label_suffix}_retry_{attempt}"
        ps_text = run_logged_stdout(manifest, suffix, ccb_project_args(manifest, "ps"))
        states = parse_resident_ps_states(ps_text)
        if states:
            return ps_text
        if attempt + 1 < RESIDENT_PS_ATTEMPTS:
            time.sleep(RESIDENT_PS_RETRY_DELAY_SECONDS)
    return ps_text


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_dicts(child))
    return found


def stale_provider_binding_problems(project: Path, snapshot: dict[str, Any]) -> list[dict[str, str]]:
    project_root = project.resolve(strict=False)
    problems: list[dict[str, str]] = []
    for payload in _walk_dicts(snapshot):
        log_path_text = _first_text(payload.get("delivery_current_log_path"))
        if not log_path_text:
            continue
        log_path = Path(log_path_text).expanduser()
        if not log_path.is_absolute():
            log_path = project_root / log_path
        resolved_log_path = log_path.resolve(strict=False)
        if _path_under(resolved_log_path, project_root):
            continue
        problem = {
            "reason": "stale_provider_session_log_binding",
            "delivery_current_log_path": str(resolved_log_path),
            "project": str(project_root),
        }
        checked_root = _first_text(payload.get("delivery_checked_session_root"))
        if checked_root:
            problem["delivery_checked_session_root"] = checked_root
        problems.append(problem)
    return problems


def write_frontdesk_request(manifest: dict[str, Any]) -> Path:
    path = Path(str(manifest["frontdesk_request"]))
    path.write_text(
        "Please start a fresh real-provider L1-L4 deployment-readiness route-mix "
        "validation for this lab project.\n\n"
        "User Request: validate the bounded route mix below through the managed "
        "Frontdesk-to-Planner workflow.\n\n"
        "Macro request: prepare and route these exact task ids without changing "
        "their expected routes: phase6b-l1-doc-direct-execution "
        "(direct_execution), phase6b-l2-code-test-direct-execution "
        "(direct_execution), phase6b-l3-needs-detail (needs_detail), "
        "phase6b-l4-macro-adjustment-request (macro_adjustment_request), and "
        "phase6b-l4-blocked-prerequisite (blocked).\n\n"
        "Execution Contract and Acceptance Criteria: Frontdesk must return only "
        "structured **Intake Evidence** containing detailed Macro request or User "
        "request, Required behavior, Scope, and Constraints. Frontdesk must not "
        "directly implement project artifacts; after producing intake evidence it "
        "must use the controlled silent handoff to Planner. Planner and orchestrator "
        "own task preparation and routing.\n\n"
        "Scope: preserve the existing L1-L4 task semantics, expected routes, task "
        "ids, and terminal expectations. Expected terminal results for this activation: "
        "phase6b-l1-doc-direct-execution (direct_execution) -> done/pass; "
        "phase6b-l2-code-test-direct-execution (direct_execution) -> done/pass; "
        "phase6b-l3-needs-detail (needs_detail) -> detail_ready; "
        "phase6b-l4-macro-adjustment-request (macro_adjustment_request) -> "
        "replan_required; phase6b-l4-blocked-prerequisite (blocked) -> blocked.\n\n"
        "Constraints: Project capability: git_repository=not_guaranteed. This lab "
        "project may not be a Git repository. Frontdesk must preserve this exact "
        "capability in Intake Evidence Constraints. Planner verification for this "
        "activation must be repo-independent: do not use git diff, git status, git "
        "diff --name-only, or another Git-only scope check; use allowed_paths plus "
        "direct file existence, content, test, or manifest checks. This constraint "
        "applies only to this activation and is not a global Git prohibition. "
        "Frontdesk produces intake evidence only and performs no worker implementation "
        "or controller-owned evidence work.\n\n"
        "Use frontdesk as the user-facing intake and hand off to planner automatically. "
        "Treat this as controller-owned route-mix validation, not as a worker "
        "implementation task. Planner and orchestrator should prepare and route the "
        "bounded L1-L4 task set itself: a document-only L1 direct execution, an L2 "
        "code-and-test direct execution, an L3 needs-detail case that stops at "
        "detail_ready, an L4 macro-adjustment case that stops at replan_required, and "
        "an L4 blocked-prerequisite case that remains blocked.\n\n"
        "Do not ask a worker to run the retest harness, generate B7 reports, write "
        "evidence rows, clean up runtime, or modify plan authority files. The "
        "supervisor/controller owns evidence rows, B7 reporting, cleanup, and "
        "script-owned authority imports. Preserve any provider or route failure as "
        "failure evidence.\n",
        encoding="utf-8",
    )
    return path


def task_root(manifest: dict[str, Any], task_id: str) -> Path:
    return Path(str(manifest["project"])) / "supervisor_imports" / task_id


def create_task_files(manifest: dict[str, Any], task_id: str) -> None:
    project = Path(str(manifest["project"]))
    draft_dir = project / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    task_packet = draft_dir / f"{task_id}.task_packet.md"
    execution_contract = draft_dir / f"{task_id}.execution_contract.md"
    task_packet.write_text(f"# Task Packet\n\ntask_id: {task_id}\n", encoding="utf-8")
    execution_contract.write_text(
        "# Execution Contract\n\nProvider replies are evidence only; route and "
        "round imports are controller-owned.\n",
        encoding="utf-8",
    )
    task_dir = task_root(manifest, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task_packet.md").write_text(task_packet.read_text(encoding="utf-8"), encoding="utf-8")
    (task_dir / "execution_contract.md").write_text(
        execution_contract.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def task_index_path(manifest: dict[str, Any]) -> Path:
    return (
        Path(str(manifest["project"]))
        / "docs"
        / "plantree"
        / "plans"
        / PLAN_SLUG
        / "tasks"
        / "index.json"
    )


def task_index_records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _read_json(task_index_path(manifest))
    if not isinstance(payload, dict):
        return []
    records = payload.get("tasks")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def task_record_id(record: dict[str, Any]) -> str:
    return str(record.get("task_id") or "").strip()


def task_record_task_root(manifest: dict[str, Any], record: dict[str, Any]) -> Path:
    task_root_text = _first_text(record.get("task_root"))
    project = Path(str(manifest["project"]))
    if task_root_text:
        return project / task_root_text
    task_id = task_record_id(record)
    return task_root(manifest, task_id)


def task_record_route_from_disk(manifest: dict[str, Any], record: dict[str, Any]) -> str | None:
    artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), dict) else {}
    packet = artifacts.get("task_packet") if isinstance(artifacts.get("task_packet"), dict) else {}
    project = Path(str(manifest["project"]))
    candidate_paths: list[Path] = []
    for key in ("path", "artifact_path"):
        path_text = _first_text(packet.get(key))
        if path_text:
            candidate_paths.append(project / path_text)
    candidate_paths.append(task_record_task_root(manifest, record) / "task_packet.md")
    for path in candidate_paths:
        text = _read_text(path)
        route = _route_from_text(text)
        if route:
            return route
    return None


def _route_from_text(text: str) -> str | None:
    for line in text.splitlines():
        match = re.match(r"(?i)^\s*route\s*:\s*([A-Za-z0-9_ -]+)\s*$", line)
        if not match:
            continue
        return "_".join(match.group(1).strip().lower().replace("-", "_").split())
    return None


def sequence_task_aliases(manifest: dict[str, Any]) -> dict[str, str]:
    records = task_index_records(manifest)
    by_id = {task_record_id(record): record for record in records if task_record_id(record)}
    aliases: dict[str, str] = {}
    used_actual: set[str] = set()
    for case in TASKS:
        expected_id = str(case["task_id"])
        if expected_id in by_id:
            continue
        expected_route = str(case["expected_route"])
        if expected_route == "direct_execution":
            continue
        candidates = []
        for record in records:
            task_id = task_record_id(record)
            if not task_id or task_id in used_actual:
                continue
            if task_id in {str(item["task_id"]) for item in TASKS}:
                continue
            if task_record_route_from_disk(manifest, record) == expected_route:
                candidates.append(task_id)
        if len(candidates) == 1:
            aliases[expected_id] = candidates[0]
            used_actual.add(candidates[0])
    return aliases


def resolve_sequence_task_id(manifest: dict[str, Any], task_id: str) -> str:
    return sequence_task_aliases(manifest).get(task_id, task_id)


def _canonical_digest(value: object, *, prefixed: bool = False) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"sha256:{digest}" if prefixed else digest


def _planner_import_transaction_digest(identity: dict[str, Any]) -> str:
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _frontdesk_activation_digest(activation: dict[str, Any]) -> str | None:
    source_job = activation.get("source_job")
    source_request = activation.get("source_request")
    if not isinstance(source_job, dict) or not isinstance(source_request, dict):
        return None
    mechanical = {
        key: activation[key]
        for key in (
            "schema_version",
            "record_type",
            "activation_id",
            "project_id",
            "project_root",
            "action",
            "source",
            "plan_slug",
            "request_id",
            "intake_sha256",
            "source_intake",
            "planner_contract",
            "required_next_output",
            "script_write_rules",
            "expected_task_ids",
            "source_task_id",
            "direct_ask",
        )
        if key in activation
    }
    mechanical["source_job"] = {
        key: source_job[key]
        for key in ("job_id", "agent_name", "reply_sha256")
        if key in source_job
    }
    mechanical["source_request"] = {
        key: source_request[key]
        for key in (
            "source_job_id",
            "agent_name",
            "project_id",
            "to_agent",
            "from_actor",
            "message_type",
            "text",
            "bytes",
            "sha256",
        )
        if key in source_request
    }
    return _canonical_digest(mechanical, prefixed=True)


def _valid_frontdesk_activation(
    project: Path,
    path: Path,
    activation: dict[str, Any],
    task_id: str,
) -> bool:
    source_job = activation.get("source_job")
    source_request = activation.get("source_request")
    source_intake = activation.get("source_intake")
    direct_ask = activation.get("direct_ask")
    ask = activation.get("ask")
    if not all(
        isinstance(item, dict)
        for item in (source_job, source_request, source_intake, direct_ask, ask)
    ):
        return False
    assert isinstance(source_job, dict)
    assert isinstance(source_request, dict)
    assert isinstance(source_intake, dict)
    assert isinstance(direct_ask, dict)
    assert isinstance(ask, dict)
    activation_id = path.stem
    body_sha256 = _first_text(source_request.get("sha256"))
    body_size = source_request.get("bytes")
    if (
        not body_sha256
        or not re.fullmatch(r"[0-9a-f]{64}", body_sha256)
        or isinstance(body_size, bool)
        or not isinstance(body_size, int)
        or body_size < 0
    ):
        return False
    project_id = _first_text(activation.get("project_id"))
    return bool(
        activation.get("schema_version") == 1
        and activation.get("record_type") == "ccb_loop_frontdesk_planner_activation"
        and activation.get("activation_id") == activation_id
        and activation_id == f"act-frontdesk-{task_id}"
        and Path(str(activation.get("project_root") or "")).resolve() == project.resolve()
        and activation.get("action") == "activate_planner_from_frontdesk"
        and activation.get("source") == "frontdesk_direct_silence_ask"
        and activation.get("plan_slug") == PLAN_SLUG
        and activation.get("planner_contract") == "task_set"
        and activation.get("request_id") == task_id
        and activation.get("source_task_id") == task_id
        and activation.get("intake_sha256") == body_sha256
        and source_intake.get("sha256") == body_sha256
        and source_intake.get("bytes") == body_size
        and isinstance(activation.get("required_next_output"), str)
        and isinstance(activation.get("script_write_rules"), list)
        and activation.get("expected_task_ids")
        == [str(item["task_id"]) for item in TASKS]
        and source_job.get("job_id") == task_id
        and source_job.get("agent_name") == "frontdesk"
        and source_job.get("terminal_status") == "forwarded"
        and source_job.get("reply_sha256") == body_sha256
        and source_request.get("source_job_id") == task_id
        and source_request.get("agent_name") == "frontdesk"
        and source_request.get("project_id") == project_id
        and source_request.get("to_agent") == "planner"
        and source_request.get("from_actor") == "frontdesk"
        and source_request.get("message_type") == "ask"
        and source_request.get("bytes") == body_size
        and source_request.get("sha256") == body_sha256
        and direct_ask.get("from_actor") == "frontdesk"
        and direct_ask.get("target") == "planner"
        and direct_ask.get("silence") is True
        and direct_ask.get("task_id") == activation_id
        and direct_ask.get("body_sha256") == body_sha256
        and direct_ask.get("controller_rewrote_body") is False
        and ask.get("target") == "planner"
        and ask.get("sender") == "frontdesk"
        and LABEL_RE.fullmatch(str(ask.get("job_id") or ""))
    )


def _valid_admission_transaction(
    path: Path,
    activation: dict[str, Any],
    task_id: str,
) -> bool:
    transaction = _read_json(path.with_name(f"{path.stem}.direct-handoff.transaction.json"))
    if not isinstance(transaction, dict):
        return False
    source_request = activation["source_request"]
    assert isinstance(source_request, dict)
    request = transaction.get("request")
    activation_record = transaction.get("activation_record")
    activation_digest = _frontdesk_activation_digest(activation)
    if not isinstance(request, dict) or not isinstance(activation_record, dict) or not activation_digest:
        return False
    request_body = request.get("body")
    if not isinstance(request_body, str):
        return False
    request_bytes = request_body.encode("utf-8")
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    authority = {
        key: transaction.get(key)
        for key in (
            "project_id",
            "activation_id",
            "request_id",
            "plan_slug",
            "request",
            "body_bytes",
            "body_sha256",
            "planner_contract",
            "source_task_id",
            "activation_digest",
        )
    }
    activation_id = activation["activation_id"]
    return bool(
        transaction.get("schema") == "ccb.frontdesk.direct_handoff_admission_transaction.v1"
        and transaction.get("record_type")
        == "ccb_frontdesk_direct_handoff_admission_transaction"
        and transaction.get("status") == "committed"
        and transaction.get("project_id") == activation.get("project_id")
        and transaction.get("activation_id") == activation_id
        and transaction.get("request_id") == task_id
        and transaction.get("plan_slug") == PLAN_SLUG
        and transaction.get("body_bytes") == source_request.get("bytes")
        and transaction.get("body_sha256") == source_request.get("sha256")
        and transaction.get("body_bytes") == len(request_bytes)
        and transaction.get("body_sha256") == request_sha256
        and transaction.get("planner_contract") == "task_set"
        and transaction.get("source_task_id") == task_id
        and transaction.get("activation_digest") == activation_digest
        and transaction.get("transaction_digest")
        == _canonical_digest(authority, prefixed=True)
        and _frontdesk_activation_digest(activation_record) == activation_digest
        and request.get("project_id") == activation.get("project_id")
        and request.get("to_agent") == "planner"
        and request.get("from_actor") == "frontdesk"
        and request.get("task_id") == activation_id
        and request.get("message_type") == "ask"
    )


def _valid_task_set_children(
    records: list[dict[str, Any]],
    task_set: dict[str, Any],
    task_set_id: str,
    revision: int,
) -> bool:
    required_ids = [str(item["task_id"]) for item in TASKS]
    children = task_set.get("children")
    if not isinstance(children, list) or len(children) != len(required_ids):
        return False
    for order, (task_id, child) in enumerate(zip(required_ids, children)):
        matches = [record for record in records if task_record_id(record) == task_id]
        if len(matches) != 1 or not isinstance(child, dict):
            return False
        record = matches[0]
        binding = record.get("task_set") if isinstance(record.get("task_set"), dict) else {}
        task_revision = record.get("task_revision")
        if (
            child != {
                "task_id": task_id,
                "task_revision": task_revision,
                "required": True,
                "order": order,
            }
            or binding.get("schema") != "ccb.plan.task_set_binding.v1"
            or binding.get("task_set_id") != task_set_id
            or binding.get("task_set_revision") != revision
            or binding.get("binding_role") != "child"
            or binding.get("bound_task_revision") != task_revision
            or binding.get("required") is not True
            or binding.get("order") != order
        ):
            return False
    return task_set.get("ordered_required_children") == required_ids


def _valid_planner_import_transaction(
    project: Path,
    activation: dict[str, Any],
    task_set: dict[str, Any],
    records: list[dict[str, Any]],
) -> bool:
    planner_job_id = str(activation["ask"]["job_id"])
    expected_ref = (
        f".ccb/runtime/role-output-imports/{planner_job_id}/"
        "planner-task-set-import.transaction.json"
    )
    path = project / expected_ref
    transaction = _read_json(path)
    if not isinstance(transaction, dict):
        return False
    identity = transaction.get("identity")
    authority = transaction.get("authority")
    if not isinstance(identity, dict) or not isinstance(authority, dict):
        return False
    task_set_id = str(task_set["task_set_id"])
    current_identity = {
        "activation_id": activation.get("activation_id"),
        "source_task_id": activation.get("source_task_id"),
        "planner_job_id": planner_job_id,
        "task_set_id": task_set_id,
    }
    other_paths = sorted(
        (project / ".ccb" / "runtime" / "role-output-imports").glob(
            "*/planner-task-set-import.transaction.json"
        )
    )
    for other_path in other_paths:
        if other_path == path:
            continue
        other = _read_json(other_path)
        other_identity = other.get("identity") if isinstance(other, dict) else None
        if isinstance(other_identity, dict) and any(
            other_identity.get(key) == value for key, value in current_identity.items()
        ):
            return False
    revision = task_set["task_set_revision"]
    reply_paths = sorted(
        (project / ".ccb" / "ccbd" / "artifacts" / "text" / "completion-reply").glob(
            f"{planner_job_id}-art_*.txt"
        )
    )
    if len(reply_paths) != 1:
        return False
    reply_sha256 = hashlib.sha256(reply_paths[0].read_bytes()).hexdigest()
    if (
        transaction.get("schema") != "ccb.plan.planner_task_set_import_transaction.v1"
        or transaction.get("schema_version") != 1
        or transaction.get("status") != "committed"
        or transaction.get("journal_ref") != expected_ref
        or transaction.get("conflicts") != []
        or path.with_name("planner-task-set-import.transaction.conflicts.json").exists()
        or transaction.get("transaction_digest")
        != _planner_import_transaction_digest(identity)
        or identity.get("project_id") != activation.get("project_id")
        or identity.get("plan_slug") != PLAN_SLUG
        or identity.get("plan_revision") != task_set.get("plan_revision")
        or identity.get("activation_id") != activation.get("activation_id")
        or identity.get("source_task_id") != activation.get("source_task_id")
        or identity.get("source_request") != activation.get("source_request")
        or identity.get("source_job") != activation.get("source_job")
        or identity.get("planner_job_id") != planner_job_id
        or identity.get("planner_reply_sha256") != reply_sha256
        or identity.get("task_set_id") != task_set_id
        or authority.get("task_set_id") != task_set_id
        or authority.get("task_set_revision") != revision
        or authority.get("source_task_id") != activation.get("source_task_id")
        or authority.get("task_set") != task_set
    ):
        return False
    identity_children = identity.get("ordered_children")
    authority_children = authority.get("children")
    required_ids = [str(item["task_id"]) for item in TASKS]
    if not isinstance(identity_children, list) or not isinstance(authority_children, list):
        return False
    if [child.get("task_id") for child in identity_children if isinstance(child, dict)] != required_ids:
        return False
    if any(not isinstance(child, dict) or child.get("required", True) is not True for child in identity_children):
        return False
    for task_id, observed in zip(required_ids, authority_children):
        matches = [record for record in records if task_record_id(record) == task_id]
        if len(matches) != 1 or not isinstance(observed, dict):
            return False
        record = matches[0]
        if (
            observed.get("task_id") != task_id
            or observed.get("task_revision") != record.get("task_revision")
            or observed.get("task_set") != record.get("task_set")
        ):
            return False
    return len(authority_children) == len(required_ids)


def controlled_task_set_source_parent_ids(manifest: dict[str, Any]) -> set[str]:
    project = Path(str(manifest["project"]))
    records = task_index_records(manifest)
    parents = []
    for record in records:
        binding = (
            record.get("task_set_parent")
            if isinstance(record.get("task_set_parent"), dict)
            else {}
        )
        if (
            record.get("status") == "decomposed"
            and binding.get("schema") == "ccb.plan.task_set_binding.v1"
            and binding.get("binding_role") == "parent"
        ):
            parents.append(record)
    if len(parents) != 1:
        return set()
    parent = parents[0]
    task_id = task_record_id(parent)
    binding = parent["task_set_parent"]
    assert isinstance(binding, dict)
    task_set_id = _first_text(binding.get("task_set_id"))
    revision = binding.get("task_set_revision")
    task_revision = parent.get("task_revision")
    if not task_id or not task_set_id:
        return set()
    activation_path = (
        project
        / ".ccb"
        / "runtime"
        / "loops"
        / "activations"
        / f"act-frontdesk-{task_id}.json"
    )
    activation = _read_json(activation_path)
    if not isinstance(activation, dict):
        return set()
    activation_paths = sorted(
        path
        for path in (project / ".ccb" / "runtime" / "loops" / "activations").glob(
            "act-frontdesk-*.json"
        )
        if CANONICAL_FRONTDESK_ACTIVATION_RE.fullmatch(path.name)
    )
    for path in activation_paths:
        if path == activation_path:
            continue
        payload = _read_json(path)
        if isinstance(payload, dict) and (
            payload.get("source_task_id") == task_id
            or payload.get("request_id") == task_id
            or payload.get("activation_id") == activation.get("activation_id")
        ):
            return set()
    if (
        not _valid_frontdesk_activation(project, activation_path, activation, task_id)
        or binding.get("bound_task_revision") != task_revision
        or isinstance(task_revision, bool)
        or not isinstance(task_revision, int)
        or task_revision < 1
        or not task_set_id
        or not LABEL_RE.fullmatch(task_set_id)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not _valid_admission_transaction(activation_path, activation, task_id)
    ):
        return set()
    task_set = _read_json(
        project
        / "docs"
        / "plantree"
        / "plans"
        / PLAN_SLUG
        / "task-sets"
        / task_set_id
        / "task-set.json"
    )
    source_request = activation.get("source_request")
    planner_job = task_set.get("planner_job") if isinstance(task_set, dict) else None
    normalized_source_request = (
        {
            "source_job_id": source_request.get("source_job_id"),
            "sha256": source_request.get("sha256"),
            "bytes": source_request.get("bytes"),
        }
        if isinstance(source_request, dict)
        else None
    )
    if isinstance(source_request, dict) and isinstance(source_request.get("body_artifact"), dict):
        assert isinstance(normalized_source_request, dict)
        normalized_source_request["body_artifact"] = source_request["body_artifact"]
    if (
        not isinstance(task_set, dict)
        or not isinstance(source_request, dict)
        or not isinstance(planner_job, dict)
        or set(task_set)
        != {
            "schema",
            "schema_version",
            "task_set_id",
            "task_set_revision",
            "project_id",
            "plan_slug",
            "source_task_id",
            "source_request",
            "planner_job",
            "plan_revision",
            "children",
            "ordered_required_children",
            "state",
            "aggregate_result",
            "closure",
            "created_at",
            "updated_at",
        }
        or task_set.get("schema") != "ccb.plan.task_set.v1"
        or task_set.get("schema_version") != 1
        or task_set.get("task_set_id") != task_set_id
        or task_set.get("task_set_revision") != revision
        or task_set.get("project_id") != activation.get("project_id")
        or task_set.get("plan_slug") != PLAN_SLUG
        or task_set.get("source_task_id") != task_id
        or task_set.get("source_request") != normalized_source_request
        or not isinstance(task_set.get("plan_revision"), dict)
        or task_set.get("state") != "running"
        or task_set.get("aggregate_result") is not None
        or task_set.get("closure") is not None
        or planner_job.get("job_id") != activation["ask"].get("job_id")
        or not re.fullmatch(r"[0-9a-f]{64}", str(planner_job.get("reply_sha256") or ""))
        or not _valid_task_set_children(records, task_set, task_set_id, revision)
        or not _valid_planner_import_transaction(project, activation, task_set, records)
    ):
        return set()
    return {task_id}


def unexpected_plan_task_ids(manifest: dict[str, Any]) -> list[str]:
    allowed = {str(item["task_id"]) for item in TASKS}
    allowed.update(sequence_task_aliases(manifest).values())
    allowed.update(controlled_task_set_source_parent_ids(manifest))
    allowed.update(settled_task_set_source_parent_ids(manifest))
    unexpected: set[str] = set()
    for record in task_index_records(manifest):
        task_id = task_record_id(record)
        if task_id and task_id not in allowed:
            unexpected.add(task_id)
    return sorted(unexpected)


def _frontdesk_job_id_from_entry_log(manifest: dict[str, Any]) -> str | None:
    root = Path(str(manifest["root"]))
    for path in sorted((root / "logs").glob("*__frontdesk_entry_ask.stdout"), reverse=True):
        match = re.search(r"accepted job=([A-Za-z0-9_.-]+)", _read_text(path))
        if match:
            return match.group(1)
    return None


def _latest_json_payload(paths: list[Path]) -> tuple[Path | None, dict[str, Any] | None]:
    candidates = []
    for path in paths:
        try:
            stamp = path.stat().st_mtime_ns
        except FileNotFoundError:
            continue
        candidates.append((stamp, str(path), path))
    for _stamp, _text, path in sorted(candidates, reverse=True):
        payload = _read_json(path)
        if isinstance(payload, dict):
            return path, payload
    return None, None


def _unexpected_task_details(
    manifest: dict[str, Any],
    unexpected: list[str],
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    unexpected_set = set(unexpected)
    for record in task_index_records(manifest):
        task_id = str(record.get("task_id") or "").strip()
        if task_id not in unexpected_set:
            continue
        authority = record.get("authority_trace") if isinstance(record.get("authority_trace"), dict) else {}
        source_job = authority.get("source_job") if isinstance(authority.get("source_job"), dict) else {}
        details.append(
            {
                "task_id": task_id,
                "status": _first_text(record.get("status")),
                "next_owner": _first_text(record.get("next_owner")),
                "source_job_id": _first_text(source_job.get("job_id")),
                "source_agent_name": _first_text(source_job.get("agent_name")),
                "task_root": _first_text(record.get("task_root")),
            }
        )
    return details


def frontdesk_planner_handoff_evidence(
    manifest: dict[str, Any],
    unexpected_details: list[dict[str, Any]],
) -> dict[str, Any]:
    project = Path(str(manifest["project"]))
    activation_path, activation = _latest_json_payload(
        sorted(
            path
            for path in (project / ".ccb" / "runtime" / "loops" / "activations").glob(
                "act-frontdesk-*.json"
            )
            if CANONICAL_FRONTDESK_ACTIVATION_RE.fullmatch(path.name)
        )
    )
    activation = activation or {}
    source_job = activation.get("source_job") if isinstance(activation.get("source_job"), dict) else {}
    ask = activation.get("ask") if isinstance(activation.get("ask"), dict) else {}
    auto_runner = activation.get("auto_runner") if isinstance(activation.get("auto_runner"), dict) else {}
    planner_job_id = _first_text(
        ask.get("job_id"),
        auto_runner.get("wait_job_id"),
        *[detail.get("source_job_id") for detail in unexpected_details],
    )
    planner_snapshot_path = None
    if planner_job_id:
        candidate = project / ".ccb" / "ccbd" / "snapshots" / f"{planner_job_id}.json"
        if candidate.is_file():
            planner_snapshot_path = str(candidate)
    planner_reply_path = None
    fenced_task_set_present = False
    if planner_job_id:
        reply_glob = sorted(
            (project / ".ccb" / "ccbd" / "artifacts" / "text" / "completion-reply").glob(
                f"{planner_job_id}-art_*.txt"
            )
        )
        if reply_glob:
            planner_reply_path = str(reply_glob[-1])
            reply_text = _read_text(reply_glob[-1])
            fenced_task_set_present = "**task-set.json**" in reply_text
    return {
        "frontdesk_job_id": _first_text(
            activation.get("request_id"),
            source_job.get("job_id"),
            _frontdesk_job_id_from_entry_log(manifest),
        ),
        "planner_job_id": planner_job_id,
        "activation_path": str(activation_path) if activation_path else None,
        "planner_snapshot_path": planner_snapshot_path,
        "planner_reply_path": planner_reply_path,
        "fenced_task_set_present": fenced_task_set_present,
    }


def planner_task_set_evidence(manifest: dict[str, Any]) -> dict[str, Any]:
    project = Path(str(manifest["project"]))
    planner_job_id = None
    planner_snapshot_path = None
    activation_path = None
    frontdesk_job_id = None
    for path in sorted(
        path
        for path in (project / ".ccb" / "runtime" / "loops" / "activations").glob(
            "act-frontdesk-*.json"
        )
        if CANONICAL_FRONTDESK_ACTIVATION_RE.fullmatch(path.name)
    ):
        payload = _read_json(path)
        if not isinstance(payload, dict):
            continue
        activation_path = str(path)
        source_job = payload.get("source_job") if isinstance(payload.get("source_job"), dict) else {}
        ask = payload.get("ask") if isinstance(payload.get("ask"), dict) else {}
        auto_runner = payload.get("auto_runner") if isinstance(payload.get("auto_runner"), dict) else {}
        frontdesk_job_id = _first_text(
            payload.get("request_id"),
            source_job.get("job_id"),
            _frontdesk_job_id_from_entry_log(manifest),
        )
        planner_job_id = _first_text(ask.get("job_id"), auto_runner.get("wait_job_id"))
        if planner_job_id:
            candidate = project / ".ccb" / "ccbd" / "snapshots" / f"{planner_job_id}.json"
            if candidate.is_file():
                planner_snapshot_path = str(candidate)
        break
    planner_reply_path = None
    fenced_task_set_present = False
    if planner_job_id:
        reply_glob = sorted(
            (project / ".ccb" / "ccbd" / "artifacts" / "text" / "completion-reply").glob(
                f"{planner_job_id}-art_*.txt"
            )
        )
        if reply_glob:
            planner_reply_path = str(reply_glob[-1])
            fenced_task_set_present = "**task-set.json**" in _read_text(reply_glob[-1])
    return {
        "frontdesk_job_id": frontdesk_job_id,
        "planner_job_id": planner_job_id,
        "activation_path": activation_path,
        "planner_snapshot_path": planner_snapshot_path,
        "planner_reply_path": planner_reply_path,
        "fenced_task_set_present": fenced_task_set_present,
    }


def planner_task_set_handoff_state(manifest: dict[str, Any]) -> dict[str, Any]:
    evidence = dict(planner_task_set_evidence(manifest))
    project = Path(str(manifest["project"]))
    frontdesk_job_id = _first_text(evidence.get("frontdesk_job_id"), _frontdesk_job_id_from_entry_log(manifest))
    if frontdesk_job_id:
        evidence["frontdesk_job_id"] = frontdesk_job_id
        status = latest_job_status(project, "frontdesk", frontdesk_job_id)
        if status:
            evidence["frontdesk_job_status"] = status
        snapshot_path = project / ".ccb" / "ccbd" / "snapshots" / f"{frontdesk_job_id}.json"
        snapshot = _read_json(snapshot_path)
        if isinstance(snapshot, dict):
            evidence["frontdesk_snapshot_path"] = str(snapshot_path)
            decision = snapshot.get("latest_decision")
            if isinstance(decision, dict):
                decision_status = _first_text(decision.get("status"))
                if decision_status and not evidence.get("frontdesk_job_status"):
                    evidence["frontdesk_job_status"] = decision_status.lower()
                reason = _first_text(decision.get("reason"))
                if reason:
                    evidence["frontdesk_job_reason"] = reason
    planner_job_id = _first_text(evidence.get("planner_job_id"))
    if planner_job_id:
        status = latest_job_status(project, "planner", planner_job_id)
        if status:
            evidence["planner_job_status"] = status
    return evidence


def wait_for_planner_task_set_handoff(manifest: dict[str, Any], *, before: str) -> dict[str, Any]:
    last_state: dict[str, Any] = {}
    for _attempt in range(PLANNER_TASK_SET_WAIT_ATTEMPTS):
        state = planner_task_set_handoff_state(manifest)
        last_state = state
        if (
            state.get("fenced_task_set_present")
            and controlled_task_set_source_parent_ids(manifest)
        ):
            return state
        planner_status = str(state.get("planner_job_status") or "").strip().lower()
        if (
            not state.get("fenced_task_set_present")
            and state.get("planner_job_id")
            and planner_status in TERMINAL_JOB_STATUSES
        ):
            return state
        frontdesk_status = str(state.get("frontdesk_job_status") or "").strip().lower()
        no_handoff_evidence = not any(
            (
                state.get("planner_job_id"),
                state.get("activation_path"),
                state.get("fenced_task_set_present"),
            )
        )
        if frontdesk_status in TERMINAL_JOB_STATUSES and no_handoff_evidence:
            checkpoint = Path(str(manifest["root"])) / "pending-checkpoints" / (
                f"{manifest['label']}__frontdesk_terminal_without_planner_handoff_before_{before}.json"
            )
            payload = {
                "schema_version": 1,
                "record_type": "ccb_phase6b_l1_l4_planner_task_set_checkpoint",
                "classification": "runner_resume_and_evidence_integrity",
                "status": "blocker",
                "reason": "frontdesk_terminal_without_planner_handoff",
                "claimable": False,
                "root": manifest["root"],
                "project": manifest["project"],
                "label": manifest["label"],
                "before": before,
                "handoff_state": state,
                "blocked_actions": ["manual start-task"],
            }
            _write_json(checkpoint, payload)
            raise HarnessBlocker(
                classification="runner_resume_and_evidence_integrity",
                reason="frontdesk_terminal_without_planner_handoff",
                message=(
                    "frontdesk reached a terminal state without planner activation, "
                    f"job, or fenced task set before {before}; checkpoint={checkpoint}"
                ),
            )
        time.sleep(PLANNER_TASK_SET_WAIT_RETRY_DELAY_SECONDS)
    checkpoint = Path(str(manifest["root"])) / "pending-checkpoints" / (
        f"{manifest['label']}__planner_task_set_handoff_pending_before_{before}.json"
    )
    payload = {
        "schema_version": 1,
        "record_type": "ccb_phase6b_l1_l4_planner_task_set_checkpoint",
        "classification": "runner_resume_and_evidence_integrity",
        "status": "checkpoint",
        "reason": "frontdesk_planner_handoff_pending",
        "claimable": False,
        "root": manifest["root"],
        "project": manifest["project"],
        "label": manifest["label"],
        "before": before,
        "handoff_state": last_state,
        "blocked_actions": ["manual start-task"],
    }
    _write_json(checkpoint, payload)
    raise HarnessBlocker(
        classification="runner_resume_and_evidence_integrity",
        reason="frontdesk_planner_handoff_pending",
        message=(
            "frontdesk/planner handoff did not reach task-set evidence before "
            f"{before}; checkpoint={checkpoint}"
        ),
    )


def assert_planner_task_set_present(manifest: dict[str, Any]) -> dict[str, Any]:
    evidence = planner_task_set_evidence(manifest)
    if evidence.get("planner_job_id") and evidence.get("fenced_task_set_present"):
        return evidence
    rows_path = Path(str(manifest["rows"]))
    b7_path = Path(str(manifest["b7"]))
    row = {
        "label": manifest["label"],
        "root": manifest["root"],
        "case_id": "frontdesk_planner_missing_fenced_task_set",
        "task_id": "route_mix_not_started",
        "classification": "invalid_harness",
        "reason": "frontdesk_planner_missing_fenced_task_set",
        "hard_blocker": True,
        "claimable_row": False,
        "route_mix_rows_claimable": False,
        "frontdesk_job_id": evidence.get("frontdesk_job_id"),
        "planner_job_id": evidence.get("planner_job_id"),
        "activation_path": evidence.get("activation_path"),
        "planner_snapshot_path": evidence.get("planner_snapshot_path"),
        "planner_reply_path": evidence.get("planner_reply_path"),
        "fenced_task_set_present": bool(evidence.get("fenced_task_set_present")),
        "why_no_route_mix_rows_claimable": (
            "planner reply did not include the fenced task-set.json block required "
            "to materialize the route-mix rows."
        ),
        "task_index_path": str(task_index_path(manifest)),
        "evidence_paths": {
            "activation": evidence.get("activation_path"),
            "planner_snapshot": evidence.get("planner_snapshot_path"),
            "planner_reply": evidence.get("planner_reply_path"),
            "task_index": str(task_index_path(manifest)),
            "rows": manifest["rows"],
            "b7": manifest["b7"],
        },
        "evidence_errors": ["planner reply missing fenced task-set.json"],
    }
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    rows_path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    b7_path.write_text(
        f"# Phase 6B L1-L4 {manifest['label']} B7\n\n"
        "Status: invalid_harness\n\n"
        "Reason: frontdesk_planner_missing_fenced_task_set\n\n"
        "```json\n"
        + json.dumps([row], indent=2, sort_keys=True)
        + "\n```\n",
        encoding="utf-8",
    )
    raise HarnessBlocker(
        classification="invalid_harness",
        reason="frontdesk_planner_missing_fenced_task_set",
        message=(
            "planner reply did not include fenced task-set.json; "
            f"rows={manifest['rows']} b7={manifest['b7']}"
        ),
    )


def write_unexpected_task_failure_report(manifest: dict[str, Any], unexpected: list[str]) -> dict[str, Any]:
    unexpected = sorted(unexpected)
    details = _unexpected_task_details(manifest, unexpected)
    handoff = frontdesk_planner_handoff_evidence(manifest, details)
    project = Path(str(manifest["project"]))
    expected_ids = [str(case["task_id"]) for case in TASKS]
    present_expected = [
        task_id
        for task_id in expected_ids
        if any(str(record.get("task_id") or "") == task_id for record in task_index_records(manifest))
    ]
    reason = "frontdesk_planner_unexpected_meta_task"
    why = (
        "frontdesk/planner created unexpected non-sequence task(s) before the fixed "
        "L1-L4 task set was claimable; route-mix rows were never generated and no "
        "route-mix task can be marked pass or valid_non_success."
    )
    row = {
        "label": manifest["label"],
        "root": manifest["root"],
        "project": str(project),
        "case_id": reason,
        "task_id": "route_mix_not_started",
        "expected_route": "fixed_l1_l4_route_mix",
        "observed_route": "blocked_before_route_mix",
        "expected_final_status": "claimable_l1_l4_rows",
        "final_status": "invalid_harness",
        "round_result": "not_run",
        "classification": "invalid_harness",
        "reason": reason,
        "hard_blocker": True,
        "claimable_row": False,
        "route_mix_rows_claimable": False,
        "why_no_route_mix_rows_claimable": why,
        "unexpected_plan_tasks": unexpected,
        "unexpected_task_details": details,
        "expected_route_mix_task_ids": expected_ids,
        "route_mix_task_ids_present": present_expected,
        "frontdesk_job_id": handoff["frontdesk_job_id"],
        "planner_job_id": handoff["planner_job_id"],
        "activation_path": handoff["activation_path"],
        "planner_snapshot_path": handoff["planner_snapshot_path"],
        "planner_reply_path": handoff["planner_reply_path"],
        "fenced_task_set_present": bool(handoff["fenced_task_set_present"]),
        "task_index_path": str(task_index_path(manifest)),
        "evidence_paths": {
            "activation": handoff["activation_path"],
            "planner_snapshot": handoff["planner_snapshot_path"],
            "planner_reply": handoff["planner_reply_path"],
            "task_index": str(task_index_path(manifest)),
            "rows": manifest["rows"],
            "b7": manifest["b7"],
        },
        "evidence_errors": [
            "unexpected non-sequence task(s): " + ", ".join(unexpected),
            "route-mix rows were never generated",
        ],
    }
    rows_path = Path(str(manifest["rows"]))
    b7_path = Path(str(manifest["b7"]))
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    rows_path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    b7_path.write_text(
        f"# Phase 6B L1-L4 {manifest['label']} B7\n\n"
        "Status: invalid_harness\n\n"
        f"Reason: {reason}\n\n"
        "```json\n"
        + json.dumps([row], indent=2, sort_keys=True)
        + "\n```\n",
        encoding="utf-8",
    )
    return row


def validate_sequence_task_set_only(manifest: dict[str, Any]) -> None:
    payload = _read_json(task_index_path(manifest))
    if not isinstance(payload, dict):
        return
    unexpected = unexpected_plan_task_ids(manifest)
    if unexpected:
        write_unexpected_task_failure_report(manifest, unexpected)
        raise HarnessBlocker(
            classification="invalid_harness",
            reason="frontdesk_planner_unexpected_meta_task",
            message=(
                "frontdesk/planner created non-sequence task(s): "
                + ", ".join(unexpected)
                + f"; rows={manifest['rows']} b7={manifest['b7']}"
            ),
        )


def payload_matches_task(payload: dict[str, Any], task_id: str | None) -> bool:
    if not task_id:
        return True
    candidates = [
        payload.get("task_id"),
        (payload.get("task") if isinstance(payload.get("task"), dict) else {}).get("task_id"),
    ]
    return any(str(candidate or "") == task_id for candidate in candidates)


def _loop_id_from_pending_path(path: Path) -> str:
    try:
        return path.parent.name
    except IndexError:
        return ""


def _string_field(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _pending_problem(path: Path, reason: str, payload: dict[str, Any]) -> dict[str, str]:
    pending = payload.get("pending") if isinstance(payload.get("pending"), dict) else {}
    problem = {
        "path": str(path),
        "reason": reason,
        "loop_id": _string_field(payload.get("loop_id"), _loop_id_from_pending_path(path)),
        "task_id": _string_field(payload.get("task_id")),
        "stage": _string_field(pending.get("stage"), payload.get("stage"), reason),
        "purpose": _string_field(pending.get("purpose"), payload.get("purpose")),
        "target": _string_field(pending.get("target"), payload.get("target")),
        "job_id": _string_field(pending.get("job_id"), payload.get("job_id")),
        "job_status": _string_field(pending.get("job_status"), payload.get("status")),
        "watch_source": _string_field(pending.get("watch_source"), payload.get("watch_source")),
        "watch_observation": _string_field(
            pending.get("watch_observation"),
            payload.get("watch_observation"),
        ),
    }
    return {key: value for key, value in problem.items() if value}


def _role_record_problem(
    path: Path,
    payload: dict[str, Any],
    role: str,
    record: dict[str, Any],
) -> dict[str, str]:
    problem = _pending_problem(path, f"{role}_job_incomplete", payload)
    problem.update(
        {
            "stage": f"{role}_ask",
            "purpose": role,
            "target": _string_field(record.get("target"), problem.get("target")),
            "job_id": _string_field(record.get("job_id"), problem.get("job_id")),
            "job_status": _string_field(record.get("status"), problem.get("job_status")),
        }
    )
    return {key: value for key, value in problem.items() if value}


def pending_authority_problems(project: Path, task_id: str | None = None) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    loops_dir = project / ".ccb" / "runtime" / "loops"
    for path in sorted(loops_dir.glob("*/round.pending.json")):
        payload = _read_json(path)
        if isinstance(payload, dict) and payload_matches_task(payload, task_id):
            problems.append(_pending_problem(path, "round_pending", payload))
    for path in sorted(loops_dir.glob("*/ask_first_stage_state.json")):
        payload = _read_json(path)
        if not isinstance(payload, dict) or not payload_matches_task(payload, task_id):
            continue
        status = str(payload.get("status") or "").strip().lower()
        if status not in {"done", "complete", "completed", "terminal"}:
            problems.append(_pending_problem(path, "ask_first_stage_pending", payload))
    for path in sorted(loops_dir.glob("*/round.json")):
        payload = _read_json(path)
        if not isinstance(payload, dict) or not payload_matches_task(payload, task_id):
            continue
        failure = payload.get("failure") if isinstance(payload.get("failure"), dict) else {}
        source = str(payload.get("round_result_source") or failure.get("source") or "")
        if source == "ask_job_incomplete":
            problems.append(_pending_problem(path, "ask_job_incomplete", payload))
            continue
        for role in ("worker", "reviewer", "orchestrator", "ccb_round_reviewer"):
            record = payload.get(role) if isinstance(payload.get(role), dict) else {}
            if str(record.get("status") or "") == "incomplete":
                problems.append(_role_record_problem(path, payload, role, record))
                break
    return problems


def pending_checkpoint_path(manifest: dict[str, Any], task_id: str | None = None) -> Path:
    suffix = task_id or "all"
    return Path(str(manifest["root"])) / "pending-checkpoints" / f"{manifest['label']}__{suffix}.json"


def _resume_command(manifest: dict[str, Any], task_id: str | None) -> list[str]:
    if task_id:
        return ["bash", str(manifest["script"]), "resume-pending", task_id]
    return ["bash", str(manifest["script"]), "check-pending"]


def write_pending_checkpoint(
    manifest: dict[str, Any],
    problems: list[dict[str, str]],
    task_id: str | None = None,
) -> Path:
    checkpoint_task = task_id or next(
        (problem.get("task_id") for problem in problems if problem.get("task_id")),
        None,
    )
    checkpoint_path = pending_checkpoint_path(manifest, checkpoint_task)
    payload = {
        "schema_version": 1,
        "record_type": "ccb_phase6b_l1_l4_pending_checkpoint",
        "classification": "runner_resume_and_evidence_integrity",
        "status": "checkpoint",
        "reason": "ask_first_execution_pending",
        "claimable": False,
        "root": manifest["root"],
        "project": manifest["project"],
        "label": manifest["label"],
        "task_id": checkpoint_task,
        "problems": problems,
        "resume_command": _resume_command(manifest, checkpoint_task),
        "blocked_actions": ["b7", "cleanup-after-b7", "duplicate evidence label reuse"],
    }
    _write_json(checkpoint_path, payload)
    return checkpoint_path


def _pending_detail(problem: dict[str, str]) -> str:
    keys = ("reason", "task_id", "loop_id", "stage", "job_id", "target", "job_status", "path")
    return ",".join(f"{key}={problem[key]}" for key in keys if problem.get(key))


def assert_no_pending_authority(manifest: dict[str, Any], task_id: str | None = None) -> None:
    problems = pending_authority_problems(Path(str(manifest["project"])), task_id)
    if not problems:
        return
    checkpoint_path = write_pending_checkpoint(manifest, problems, task_id)
    resume_task = task_id or next((item.get("task_id") for item in problems if item.get("task_id")), None)
    raise HarnessBlocker(
        classification="runner_resume_and_evidence_integrity",
        reason="ask_first_execution_pending",
        message=(
            "round authority pending/incomplete: "
            + "; ".join(_pending_detail(item) for item in problems)
            + f"; checkpoint={checkpoint_path}"
            + "; resume_command="
            + " ".join(_resume_command(manifest, resume_task))
            + "; refuse b7/cleanup/progress before final round authority"
        ),
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def auto_runner_lock_state(manifest: dict[str, Any]) -> dict[str, object]:
    path = Path(str(manifest["project"])) / ".ccb" / "runtime" / "loops" / "auto-runner.lock"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return {"status": "absent", "path": str(path), "reason": "lock_absent"}
    first_line = raw.splitlines()[0].strip() if raw else ""
    try:
        pid = int(first_line)
    except ValueError:
        return {"status": "stale", "path": str(path), "reason": "invalid_pid", "raw": raw}
    if _pid_alive(pid):
        return {"status": "live", "path": str(path), "reason": "pid_alive", "pid": pid}
    return {"status": "stale", "path": str(path), "reason": "pid_not_running", "pid": pid}


def wait_for_auto_runner_quiet(manifest: dict[str, Any], *, before: str) -> None:
    last_state: dict[str, object] = {}
    for _attempt in range(AUTO_RUNNER_QUIET_ATTEMPTS):
        state = auto_runner_lock_state(manifest)
        last_state = state
        if state.get("status") != "live":
            return
        time.sleep(AUTO_RUNNER_QUIET_RETRY_DELAY_SECONDS)
    checkpoint = Path(str(manifest["root"])) / "pending-checkpoints" / (
        f"{manifest['label']}__auto_runner_active_before_{before}.json"
    )
    payload = {
        "schema_version": 1,
        "record_type": "ccb_phase6b_l1_l4_auto_runner_checkpoint",
        "classification": "runner_resume_and_evidence_integrity",
        "status": "checkpoint",
        "reason": "frontdesk_auto_runner_still_active",
        "claimable": False,
        "root": manifest["root"],
        "project": manifest["project"],
        "label": manifest["label"],
        "before": before,
        "auto_runner_lock": last_state,
        "blocked_actions": ["manual start-task", "manual continue-route"],
    }
    _write_json(checkpoint, payload)
    raise HarnessBlocker(
        classification="runner_resume_and_evidence_integrity",
        reason="frontdesk_auto_runner_still_active",
        message=(
            "frontdesk-spawned auto-runner is still active before manual "
            f"{before}; checkpoint={checkpoint}"
        ),
    )


def latest_job_status(project: Path, target: str, job_id: str) -> str | None:
    jobs_path = project / ".ccb" / "agents" / target / "jobs.jsonl"
    status: str | None = None
    for line in _read_text(jobs_path).splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("job_id") != job_id:
            continue
        value = str(payload.get("status") or "").strip().lower()
        if value:
            status = value
    return status


def refresh_pending_job_statuses(
    manifest: dict[str, Any],
    problems: list[dict[str, str]],
) -> list[dict[str, str]]:
    project = Path(str(manifest["project"]))
    refreshed = []
    for problem in problems:
        item = dict(problem)
        target = item.get("target")
        job_id = item.get("job_id")
        if target and job_id:
            status = latest_job_status(project, target, job_id)
            if status:
                item["job_status"] = status
        refreshed.append(item)
    return refreshed


def resume_pending_round(manifest: dict[str, Any], task_id: str) -> None:
    task_id = resolve_sequence_task_id(manifest, task_id)
    problems = pending_authority_problems(Path(str(manifest["project"])), task_id)
    if not problems:
        raise HarnessError(f"no pending round authority found for task: {task_id}")
    problems = refresh_pending_job_statuses(manifest, problems)
    nonterminal = [
        problem
        for problem in problems
        if str(problem.get("job_status") or "").strip().lower() not in TERMINAL_JOB_STATUSES
    ]
    if nonterminal:
        checkpoint_path = write_pending_checkpoint(manifest, nonterminal, task_id)
        raise HarnessBlocker(
            classification="runner_resume_and_evidence_integrity",
            reason="ask_first_execution_pending",
            message=(
                "pending job is not terminal: "
                + "; ".join(_pending_detail(item) for item in nonterminal)
                + f"; checkpoint={checkpoint_path}"
                + "; resume_command="
                + " ".join(_resume_command(manifest, task_id))
            ),
        )
    run_logged(
        manifest,
        f"{task_id}__resume_pending_round",
        ccb_project_args(manifest, "loop", "runner", "--once", "--json"),
    )
    assert_no_pending_authority(manifest, task_id)
    run_logged(
        manifest,
        f"{task_id}__task_show_after_resume",
        ccb_project_args(manifest, "plan", "task-show", "--task", task_id, "--json"),
    )


def ccb_project_args(manifest: dict[str, Any], *args: str) -> list[str]:
    return [str(CCB_TEST), "--project", str(manifest["project"]), *args]


def init_lab(manifest: dict[str, Any]) -> None:
    Path(str(manifest["command_log"])).write_text("", encoding="utf-8")
    write_config(manifest)
    materialize_plan_root(manifest)
    write_fixtures(manifest)
    seed_rolepacks(manifest)
    run_logged(manifest, "config_validate_initial", ccb_project_args(manifest, "config", "validate"))
    run_logged(manifest, "start_project", [str(CCB_TEST), "--project", str(manifest["project"])])
    assert_resident_agents_mounted(manifest)
    assert_resident_agents_ready(manifest, "resident_ps_after_start")


def frontdesk_entry(manifest: dict[str, Any], ps_text: str | None = None) -> None:
    assert_resident_agents_mounted(manifest)
    if ps_text is None:
        assert_resident_agents_ready(manifest, "resident_ps_before_frontdesk_entry")
    else:
        assert_resident_agents_ready_from_ps(manifest, ps_text)
    request_path = write_frontdesk_request(manifest)
    request_text = request_path.read_text(encoding="utf-8")
    run_logged(manifest, "frontdesk_entry_ask", ccb_project_args(manifest, "ask", "frontdesk", "--", request_text))


def task_record_exists(manifest: dict[str, Any], task_id: str) -> bool:
    completed = subprocess.run(
        ccb_project_args(manifest, "plan", "task-show", "--task", task_id, "--json"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        env=runner_env(manifest),
    )
    return completed.returncode == 0


def read_only_task_observation_label_suffix(manifest: dict[str, Any], label_suffix: str) -> str:
    log_path = Path(str(manifest["command_log"]))
    for attempt in range(READ_ONLY_TASK_OBSERVATION_MAX_RETRIES + 1):
        candidate_suffix = label_suffix if attempt == 0 else f"{label_suffix}__attempt_{attempt}"
        label, stdout_path, stderr_path = command_output_paths(manifest, candidate_suffix)
        if (
            not _command_log_has_label(log_path, label)
            and not stdout_path.exists()
            and not stderr_path.exists()
        ):
            return candidate_suffix
    raise HarnessError(
        "evidence_integrity_read_only_observation_attempts_exhausted: "
        + f"{manifest['label']}__{label_suffix}; "
        + f"max retries={READ_ONLY_TASK_OBSERVATION_MAX_RETRIES}; existing evidence is preserved"
    )


def observe_task_record(manifest: dict[str, Any], task_id: str, label_suffix: str) -> dict[str, Any]:
    label_suffix = read_only_task_observation_label_suffix(manifest, label_suffix)
    run_logged(
        manifest,
        label_suffix,
        ccb_project_args(manifest, "plan", "task-show", "--task", task_id, "--json"),
    )
    _label, stdout_path, _stderr_path = command_output_paths(manifest, label_suffix)
    payload = _read_json(stdout_path)
    return payload if isinstance(payload, dict) else {}


def _task_record(payload: dict[str, Any]) -> dict[str, Any]:
    task = payload.get("task")
    if isinstance(task, dict):
        return task
    return payload


def task_record_status(payload: dict[str, Any]) -> str:
    task = _task_record(payload)
    return str(task.get("status") or payload.get("status") or "").strip()


def task_record_current_loop(payload: dict[str, Any]) -> str:
    task = _task_record(payload)
    return str(task.get("current_loop") or payload.get("current_loop") or "").strip()


def task_record_artifacts(payload: dict[str, Any]) -> dict[str, Any]:
    artifacts = _task_record(payload).get("artifacts")
    return artifacts if isinstance(artifacts, dict) else {}


def task_record_has_artifact(payload: dict[str, Any], kind: str) -> bool:
    return isinstance(task_record_artifacts(payload).get(kind), dict)


def task_record_orchestrator_route(payload: dict[str, Any]) -> str:
    notes = task_record_artifacts(payload).get("orchestration_notes")
    if isinstance(notes, dict):
        return str(notes.get("orchestrator_route") or "").strip()
    return ""


def task_record_is_already_started(payload: dict[str, Any]) -> bool:
    if task_record_current_loop(payload):
        return True
    return task_record_status(payload) in {"running", "done", "detail_ready", "replan_required", "blocked"}


def ensure_task_record(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    if task_record_exists(manifest, task_id):
        return observe_task_record(manifest, task_id, f"{task_id}__task_observe_existing")
    run_logged(
        manifest,
        f"{task_id}__task_create",
        ccb_project_args(
            manifest,
            "plan",
            "task-create",
            "--plan",
            PLAN_SLUG,
            "--title",
            task_id,
            "--task-id",
            task_id,
            "--json",
        ),
    )
    return observe_task_record(manifest, task_id, f"{task_id}__task_observe_created")


def import_missing_task_anchors(manifest: dict[str, Any], task_id: str, payload: dict[str, Any]) -> None:
    create_task_files(manifest, task_id)
    project = Path(str(manifest["project"]))
    for kind, draft_name in (
        ("task_packet", f"{task_id}.task_packet.md"),
        ("execution_contract", f"{task_id}.execution_contract.md"),
    ):
        if task_record_has_artifact(payload, kind):
            continue
        run_logged(
            manifest,
            f"{task_id}__artifact_{kind}",
            ccb_project_args(
                manifest,
                "plan",
                "task-artifact",
                "--task",
                task_id,
                "--kind",
                kind,
                "--file",
                str(project / "drafts" / draft_name),
                "--json",
            ),
        )


def mark_task_ready_for_orchestration(manifest: dict[str, Any], task_id: str) -> None:
    run_logged(
        manifest,
        f"{task_id}__ready_for_orchestration",
        ccb_project_args(
            manifest,
            "plan",
            "task-status",
            "--task",
            task_id,
            "--status",
            "ready_for_orchestration",
            "--next-owner",
            "orchestrator",
            "--activation-reason",
            f"{manifest['label']}_start_task",
            "--json",
        ),
    )


def start_task(manifest: dict[str, Any], task_id: str) -> None:
    wait_for_planner_task_set_handoff(manifest, before=f"start_task_{task_id}")
    assert_planner_task_set_present(manifest)
    validate_sequence_task_set_only(manifest)
    task_id = resolve_sequence_task_id(manifest, task_id)
    if task_record_exists(manifest, task_id):
        payload = observe_task_record(manifest, task_id, f"{task_id}__task_observe_existing")
        if task_record_is_already_started(payload):
            return
    wait_for_auto_runner_quiet(manifest, before=f"start_task_{task_id}")
    payload = ensure_task_record(manifest, task_id)
    if task_record_is_already_started(payload):
        return
    import_missing_task_anchors(manifest, task_id, payload)
    if task_record_status(payload) != "ready_for_orchestration":
        mark_task_ready_for_orchestration(manifest, task_id)
    run_logged(
        manifest,
        f"{task_id}__activate_orchestrator",
        ccb_project_args(manifest, "loop", "runner", "--once", "--json"),
    )
    assert_no_pending_authority(manifest, task_id)


def require_supervisor_file(manifest: dict[str, Any], task_id: str, name: str) -> Path:
    path = task_root(manifest, task_id) / name
    if not path.is_file():
        raise HarnessError(f"supervisor checkpoint required before continuing: {path}")
    return path


def import_supervisor_route(manifest: dict[str, Any], task_id: str, expected_route: str) -> None:
    task_id = resolve_sequence_task_id(manifest, task_id)
    route_file = task_root(manifest, task_id) / "route.txt"
    notes_file = task_root(manifest, task_id) / "orchestration_notes.md"
    if not route_file.is_file() or not notes_file.is_file():
        payload = observe_task_record(manifest, task_id, f"{task_id}__route_observe_existing")
        if task_record_orchestrator_route(payload) == expected_route:
            return
        route_file = require_supervisor_file(manifest, task_id, "route.txt")
        notes_file = require_supervisor_file(manifest, task_id, "orchestration_notes.md")
    observed_route = "".join(route_file.read_text(encoding="utf-8").split())
    if observed_route != expected_route:
        raise HarnessError(f"route mismatch for {task_id}: expected {expected_route} observed {observed_route}")
    run_logged(
        manifest,
        f"{task_id}__import_orchestration_notes_{expected_route}",
        ccb_project_args(
            manifest,
            "plan",
            "task-artifact",
            "--task",
            task_id,
            "--kind",
            "orchestration_notes",
            "--file",
            str(notes_file),
            "--route",
            observed_route,
            "--json",
        ),
    )


def run_direct_execution_round(manifest: dict[str, Any], task_id: str) -> None:
    task_id = resolve_sequence_task_id(manifest, task_id)
    run_logged(
        manifest,
        f"{task_id}__run_direct_execution_round",
        ccb_project_args(manifest, "loop", "runner", "--once", "--json"),
    )
    assert_no_pending_authority(manifest, task_id)
    run_logged(
        manifest,
        f"{task_id}__task_show_after_round",
        ccb_project_args(manifest, "plan", "task-show", "--task", task_id, "--json"),
    )


def continue_route(manifest: dict[str, Any], task_id: str, expected_route: str) -> None:
    wait_for_auto_runner_quiet(manifest, before=f"continue_route_{task_id}")
    task_id = resolve_sequence_task_id(manifest, task_id)
    import_supervisor_route(manifest, task_id, expected_route)
    if expected_route == "direct_execution":
        run_direct_execution_round(manifest, task_id)
    elif expected_route == "needs_detail":
        run_logged(
            manifest,
            f"{task_id}__activate_detailer",
            ccb_project_args(manifest, "loop", "runner", "--once", "--json"),
        )
        assert_no_pending_authority(manifest, task_id)
    elif expected_route == "macro_adjustment_request":
        macro_file = require_supervisor_file(manifest, task_id, "macro_adjustment_request.md")
        run_logged(
            manifest,
            f"{task_id}__import_macro_adjustment_request",
            ccb_project_args(
                manifest,
                "plan",
                "task-artifact",
                "--task",
                task_id,
                "--kind",
                "macro_adjustment_request",
                "--file",
                str(macro_file),
                "--json",
            ),
        )
        run_logged(
            manifest,
            f"{task_id}__status_replan_required",
            ccb_project_args(
                manifest,
                "plan",
                "task-status",
                "--task",
                task_id,
                "--status",
                "replan_required",
                "--next-owner",
                "planner",
                "--activation-reason",
                f"{manifest['label']}_macro",
                "--json",
            ),
        )
    elif expected_route == "blocked":
        blocker_file = require_supervisor_file(manifest, task_id, "blocker_evidence.md")
        run_logged(
            manifest,
            f"{task_id}__import_blocker_evidence",
            ccb_project_args(
                manifest,
                "plan",
                "task-artifact",
                "--task",
                task_id,
                "--kind",
                "blocker_evidence",
                "--file",
                str(blocker_file),
                "--json",
            ),
        )
        run_logged(
            manifest,
            f"{task_id}__status_blocked",
            ccb_project_args(
                manifest,
                "plan",
                "task-status",
                "--task",
                task_id,
                "--status",
                "blocked",
                "--activation-reason",
                f"{manifest['label']}_blocked",
                "--json",
            ),
        )
    else:
        raise HarnessError(f"unsupported expected route: {expected_route}")


def continue_detail(manifest: dict[str, Any], task_id: str) -> None:
    task_id = resolve_sequence_task_id(manifest, task_id)
    payload = observe_task_record(manifest, task_id, f"{task_id}__detail_observe_existing")
    existing_detail = all(
        task_record_has_artifact(payload, kind)
        for kind in ("detail_design", "detail_summary", "detail_packet")
    )
    if not existing_detail:
        for kind, name in (
            ("detail_design", "detail_design.md"),
            ("detail_summary", "detail_summary.md"),
            ("detail_packet", "detail_packet.manifest.json"),
        ):
            path = require_supervisor_file(manifest, task_id, name)
            run_logged(
                manifest,
                f"{task_id}__import_{kind}",
                ccb_project_args(
                    manifest,
                    "plan",
                    "task-artifact",
                    "--task",
                    task_id,
                    "--kind",
                    kind,
                    "--file",
                    str(path),
                    "--json",
                ),
            )
        require_supervisor_file(manifest, task_id, "steps/step-001.md")
    run_logged(
        manifest,
        f"{task_id}__status_detail_ready",
        ccb_project_args(
            manifest,
            "plan",
            "task-status",
            "--task",
            task_id,
            "--status",
            "detail_ready",
            "--activation-reason",
            f"{manifest['label']}_detail_ready",
            "--json",
        ),
    )


def _round_json_for(project: Path, task_id: str) -> tuple[Path | None, dict[str, Any] | None]:
    matches = []
    for path in sorted((project / ".ccb" / "runtime" / "loops").glob("*/round.json")):
        payload = _read_json(path)
        if isinstance(payload, dict) and payload_matches_task(payload, task_id):
            matches.append((str(payload.get("finished_at") or ""), path, payload))
    if not matches:
        return None, None
    _finished, path, payload = sorted(matches)[-1]
    return path, payload


def _summary_field(text: str, name: str) -> str | None:
    expected = "_".join(name.strip().lower().replace("_", " ").split())
    for line in text.splitlines():
        key, separator, value = line.strip().partition(":")
        normalized_key = "_".join(key.strip().lower().replace("_", " ").split())
        if separator and normalized_key == expected:
            return value.strip()
    return None


def _task_index_record(project: Path, task_id: str) -> dict[str, Any] | None:
    index_path = project / "docs" / "plantree" / "plans" / PLAN_SLUG / "tasks" / "index.json"
    payload = _read_json(index_path)
    records = payload.get("tasks") if isinstance(payload, dict) else []
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and str(record.get("task_id") or "") == task_id:
            return record
    return None


def _task_status(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    root = Path(str(manifest["root"]))
    project = Path(str(manifest["project"]))
    index_path = project / "docs" / "plantree" / "plans" / PLAN_SLUG / "tasks" / "index.json"
    index_record = _task_index_record(project, task_id)
    if index_record is not None:
        return {
            "task_show_observed": True,
            "task_show_source": "task_index",
            "task_show_path": str(index_path),
            "final_status": _first_text(index_record.get("status")) or "missing",
            "next_owner": _first_text(index_record.get("next_owner")),
        }
    candidates = sorted((root / "logs").glob(f"*__{task_id}__task_show*.stdout"))
    if not candidates:
        candidates = sorted((root / "logs").glob(f"{task_id}__task_show*.stdout"))
    if not candidates:
        return {
            "task_show_observed": False,
            "task_show_source": "missing",
            "task_show_path": None,
            "final_status": "missing",
            "next_owner": None,
        }
    payload = _read_json(candidates[-1])
    task = payload.get("task") if isinstance(payload, dict) and isinstance(payload.get("task"), dict) else {}
    return {
        "task_show_observed": isinstance(payload, dict),
        "task_show_source": "task_show_log",
        "task_show_path": str(candidates[-1]),
        "final_status": _first_text(
            payload.get("status") if isinstance(payload, dict) else None,
            task.get("status"),
        )
        or "missing",
        "next_owner": _first_text(
            payload.get("next_owner") if isinstance(payload, dict) else None,
            task.get("next_owner"),
        ),
    }


def _task_artifacts(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    project = Path(str(manifest["project"]))
    index_record = _task_index_record(project, task_id)
    if not isinstance(index_record, dict):
        return {}
    artifacts = index_record.get("artifacts")
    return artifacts if isinstance(artifacts, dict) else {}


def _artifact_actor_source(record: object) -> str:
    if not isinstance(record, dict):
        return ""
    actor = record.get("actor")
    if not isinstance(actor, dict):
        return ""
    return str(actor.get("source") or "").strip()


def _script_owned_artifact(record: object) -> bool:
    return _artifact_actor_source(record) in {
        "loop_runner",
        "loop_runner/script-owned",
        "loop_runner_role_output_import",
    }


def _task_route_evidence(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    artifacts = _task_artifacts(manifest, task_id)
    notes = artifacts.get("orchestration_notes")
    if isinstance(notes, dict):
        route = _first_text(notes.get("orchestrator_route"), notes.get("route"))
        if route:
            return {
                "observed_route": route,
                "route_source": "task_index_orchestration_notes",
                "route_artifact_path": _first_text(notes.get("path"), notes.get("artifact_path")),
                "script_owned_route_imports": _script_owned_artifact(notes),
            }
    route_path = task_root(manifest, task_id) / "route.txt"
    route = _read_text(route_path).strip()
    if route:
        return {
            "observed_route": route,
            "route_source": "supervisor_route_file",
            "route_artifact_path": str(route_path),
            "script_owned_route_imports": False,
        }
    return {
        "observed_route": "missing",
        "route_source": "missing",
        "route_artifact_path": None,
        "script_owned_route_imports": False,
    }


def _round_evidence(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    project = Path(str(manifest["project"]))
    round_json_path, round_json = _round_json_for(project, task_id)
    summary_candidates = [
        task_root(manifest, task_id) / "round_summary.md",
        project / "docs" / "plantree" / "plans" / PLAN_SLUG / "tasks" / task_id / "round_summary.md",
    ]
    summary_path = next((path for path in summary_candidates if path.is_file()), None)
    summary_text = _read_text(summary_path) if summary_path else ""
    return {
        "round_summary_observed": summary_path is not None,
        "round_summary_path": str(summary_path) if summary_path else None,
        "round_json_path": str(round_json_path) if round_json_path else None,
        "round_result": _first_text(
            _summary_field(summary_text, "round_result"),
            round_json.get("round_result") if isinstance(round_json, dict) else None,
        )
        or "missing",
        "round_result_source": _first_text(
            _summary_field(summary_text, "round_result_source"),
            round_json.get("round_result_source") if isinstance(round_json, dict) else None,
        )
        or "missing",
    }


def _status_result_evidence(
    case: dict[str, object],
    status: dict[str, Any],
    round_evidence: dict[str, Any],
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    expected_route = str(case["expected_route"])
    if expected_route == "direct_execution" or round_evidence["round_result"] != "missing":
        summary = artifacts.get("round_summary") or artifacts.get("round_pass")
        return {
            "round_result": round_evidence["round_result"],
            "round_result_source": round_evidence["round_result_source"],
            "script_owned_round_imports": _script_owned_artifact(summary),
        }
    result_artifact_kind = {
        "needs_detail": "detail_packet",
        "macro_adjustment_request": "macro_adjustment_request",
        "blocked": "blocker_evidence",
    }.get(expected_route)
    result_artifact = artifacts.get(result_artifact_kind) if result_artifact_kind else None
    return {
        "round_result": status["final_status"],
        "round_result_source": "task_status",
        "script_owned_round_imports": _script_owned_artifact(result_artifact),
    }


def _cleanup_evidence(
    case: dict[str, object],
    round_json: dict[str, Any] | None,
) -> dict[str, Any]:
    if case["expected_route"] != "direct_execution":
        return {
            "cleanup_result": "not_applicable",
            "runtime_residue": False,
            "dynamic_unload_ok": True,
            "release_released_count": None,
            "release_retained_count": None,
            "observed_dynamic_agents": None,
        }
    release = round_json.get("release") if isinstance(round_json, dict) else {}
    if not isinstance(release, dict):
        release = {}
    observed = round_json.get("observed_topology") if isinstance(round_json, dict) else {}
    if not isinstance(observed, dict):
        observed = {}
    agents = observed.get("agents")
    observed_agents = agents if isinstance(agents, list) else []
    released_count = release.get("released_count")
    retained_count = release.get("retained_count")
    unload_ok = (
        isinstance(released_count, int)
        and released_count >= len(DYNAMIC_LOOP_PROFILES)
        and retained_count == 0
        and len(observed_agents) == 0
    )
    return {
        "cleanup_result": "clean" if unload_ok else "release_incomplete",
        "runtime_residue": not unload_ok,
        "dynamic_unload_ok": unload_ok,
        "release_released_count": released_count,
        "release_retained_count": retained_count,
        "observed_dynamic_agents": len(observed_agents),
    }


def _authority_digest(value: object, *, omit: str | None = None) -> str:
    if isinstance(value, dict) and omit is not None:
        value = {key: item for key, item in value.items() if key != omit}
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in _read_text(path).splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _authority_path(project: Path, value: object, expected: Path) -> Path | None:
    raw = Path(str(value or ""))
    candidate = raw.resolve(strict=False) if raw.is_absolute() else (project / raw).resolve(strict=False)
    if candidate != expected.resolve(strict=False):
        return None
    return candidate


def _decision029_aggregate(results: list[str]) -> str | None:
    if "replan_required" in results:
        return "replan_required"
    if "partial" in results:
        return "partial"
    if "pass" in results and "blocked" in results:
        return "partial"
    if results and all(result == "blocked" for result in results):
        return "blocked"
    if results and all(result == "pass" for result in results):
        return "pass"
    return None


def _closed_feedback_transport(
    value: object,
    *,
    target: str,
    purpose: str,
    job_id: object,
    silent: bool,
) -> bool:
    required = {
        "target", "purpose", "task_id", "message", "message_sha256", "silent",
        "job_id", "source_job_id", "effective_job_id", "retry_lineage", "status", "reply",
    }
    if not isinstance(value, dict) or set(value) != required:
        return False
    message = value.get("message")
    if not isinstance(message, str) or not message:
        return False
    source_job_id = str(value.get("source_job_id") or "")
    effective_job_id = str(value.get("effective_job_id") or "")
    retry_lineage = value.get("retry_lineage")
    if not source_job_id or not effective_job_id or not isinstance(retry_lineage, list):
        return False
    current_job_id = source_job_id
    seen = {source_job_id}
    for retry_index, edge in enumerate(retry_lineage, start=1):
        required_edge = {
            "message_id", "source_attempt_id", "successor_attempt_id",
            "retry_source_job_id", "retry_successor_job_id", "retry_index",
        }
        if (
            not isinstance(edge, dict)
            or set(edge) != required_edge
            or edge.get("retry_source_job_id") != current_job_id
            or edge.get("retry_index") != retry_index
            or not edge.get("message_id")
            or not edge.get("source_attempt_id")
            or not edge.get("successor_attempt_id")
        ):
            return False
        successor = str(edge.get("retry_successor_job_id") or "")
        if not successor or successor in seen:
            return False
        seen.add(successor)
        current_job_id = successor
    if current_job_id != effective_job_id:
        return False
    return bool(
        value.get("target") == target
        and value.get("purpose") == purpose
        and value.get("silent") is silent
        and value.get("status") == "completed"
        and value.get("job_id") == value.get("source_job_id")
        and value.get("effective_job_id") == job_id
        and isinstance(value.get("retry_lineage"), list)
        and isinstance(value.get("reply"), str)
        and value.get("message_sha256") == hashlib.sha256(message.encode("utf-8")).hexdigest()
    )


def b7_task_set_evidence(manifest: dict[str, Any], task_ids: list[str]) -> dict[str, Any]:
    """Validate the persisted Decision029 authority required to claim B7."""
    project = Path(str(manifest["project"]))
    errors: list[str] = []
    paths: dict[str, str | None] = {}
    evidence: dict[str, Any] = {
        "valid": False,
        "errors": errors,
        "task_set_id": None,
        "task_set_revision": None,
        "closure_digest": None,
        "aggregate_result": None,
        "constraint_digest": None,
        "constraint_revision": None,
        "settlement_action": None,
        "planner_backfill_job_id": None,
        "frontdesk_completion_job_id": None,
        "evidence_paths": paths,
    }

    def reject(reason: str) -> None:
        if reason not in errors:
            errors.append(reason)

    records = [_task_index_record(project, task_id) for task_id in task_ids]
    if any(not isinstance(record, dict) for record in records):
        reject("task_set_required_child_record_missing")
        return evidence
    child_records = [record for record in records if isinstance(record, dict)]
    bindings = [record.get("task_set") for record in child_records]
    if not all(isinstance(binding, dict) for binding in bindings):
        reject("task_set_required_child_binding_missing")
        return evidence
    bound_ids = {str(binding.get("task_set_id") or "") for binding in bindings if isinstance(binding, dict)}
    bound_revisions = {binding.get("task_set_revision") for binding in bindings if isinstance(binding, dict)}
    if len(bound_ids) != 1 or len(bound_revisions) != 1:
        reject("task_set_required_children_not_canonical")
        return evidence
    task_set_id = next(iter(bound_ids))
    revision = next(iter(bound_revisions))
    if (
        not LABEL_RE.fullmatch(task_set_id)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
    ):
        reject("task_set_identity_invalid")
        return evidence
    evidence["task_set_id"] = task_set_id
    evidence["task_set_revision"] = revision
    task_set_path = (
        project / "docs" / "plantree" / "plans" / PLAN_SLUG / "task-sets" / task_set_id / "task-set.json"
    )
    paths["task_set"] = str(task_set_path)
    task_set = _read_json(task_set_path)
    if not isinstance(task_set, dict):
        reject("task_set_authority_missing")
        return evidence

    bound_children = []
    terminal_children = []
    child_results: list[str] = []
    terminal_result_by_status = {
        "done": "pass",
        "detail_ready": "pass",
        "partial": "partial",
        "replan_required": "replan_required",
        "blocked": "blocked",
    }
    for order, record in enumerate(child_records):
        binding = bindings[order]
        assert isinstance(binding, dict)
        task_id = task_ids[order]
        bound_revision = binding.get("bound_task_revision")
        bound_child = {
            "task_id": task_id,
            "task_revision": bound_revision,
            "required": True,
            "order": order,
        }
        terminal_child = {
            "task_id": task_id,
            "task_revision": record.get("task_revision"),
            "required": True,
        }
        bound_children.append(bound_child)
        terminal_children.append(terminal_child)
        if binding != {
            "schema": "ccb.plan.task_set_binding.v1",
            "task_set_id": task_set_id,
            "task_set_revision": revision,
            "binding_role": "child",
            "bound_task_revision": bound_revision,
            "required": True,
            "order": order,
        } or isinstance(bound_revision, bool) or not isinstance(bound_revision, int) or bound_revision < 1:
            reject("task_set_child_binding_mismatch")
        result = terminal_result_by_status.get(str(record.get("status") or ""))
        if result is None:
            reject("task_set_required_child_not_terminal")
        else:
            child_results.append(result)
    aggregate = _decision029_aggregate(child_results)
    if aggregate is None:
        reject("task_set_aggregate_not_deterministic")
    evidence["aggregate_result"] = aggregate
    closed_state = {
        "pass": "closed",
        "partial": "partial",
        "replan_required": "replan_required",
        "blocked": "blocked",
    }.get(aggregate or "")
    task_set_children = task_set.get("children")
    if task_set_children != bound_children:
        reject("task_set_child_binding_mismatch")
    if (
        task_set.get("schema") != "ccb.plan.task_set.v1"
        or task_set.get("schema_version") != 1
        or task_set.get("task_set_id") != task_set_id
        or task_set.get("task_set_revision") != revision
        or task_set_children != bound_children
        or task_set.get("ordered_required_children") != task_ids
        or task_set.get("state") != closed_state
        or task_set.get("aggregate_result") != aggregate
    ):
        reject("task_set_closure_not_settled")

    closure_path = task_set_path.with_name("closure.json")
    paths["closure"] = str(closure_path)
    closure = _read_json(closure_path)
    if not isinstance(closure, dict):
        reject("task_set_closure_authority_missing")
        return evidence
    closure_digest = closure.get("closure_digest")
    evidence["closure_digest"] = closure_digest
    expected_ref = {
        "path": str(closure_path.relative_to(project)),
        "closure_digest": closure_digest,
        "ordered_terminal_evidence_digest": closure.get("ordered_terminal_evidence_digest"),
    }
    if (
        closure.get("schema") != "ccb.plan.task_set_closure.v1"
        or closure.get("schema_version") != 1
        or closure.get("task_set_id") != task_set_id
        or closure.get("task_set_revision") != revision
        or closure.get("status") != "closure_pending"
        or closure.get("aggregate_result") != aggregate
        or closure.get("closure_digest") != _authority_digest(closure, omit="closure_digest")
        or task_set.get("closure") != expected_ref
    ):
        reject("task_set_closure_digest_or_reference_mismatch")
    ordered_children = closure.get("ordered_children")
    if not isinstance(ordered_children, list) or len(ordered_children) != len(child_records):
        reject("task_set_closure_children_missing")
    else:
        expected_evidence_digest = _authority_digest(
            {"task_set_revision": revision, "children": ordered_children}
        )
        if closure.get("ordered_terminal_evidence_digest") != expected_evidence_digest:
            reject("task_set_closure_terminal_evidence_digest_mismatch")
        for expected, record, child in zip(terminal_children, child_records, ordered_children):
            if not isinstance(child, dict) or any(
                child.get(key) != value
                for key, value in expected.items()
            ):
                reject("task_set_closure_child_identity_mismatch")
                break
            expected_result = terminal_result_by_status.get(str(record.get("status") or ""))
            if child.get("status") != record.get("status") or child.get("result") != expected_result:
                reject("task_set_closure_child_terminal_mismatch")
                break
            authority = child.get("authority") if isinstance(child.get("authority"), dict) else None
            if (
                authority is None
                or child.get("evidence_digest") != _authority_digest(
                    {
                        "task_id": child.get("task_id"),
                        "task_revision": child.get("task_revision"),
                        **authority,
                    }
                )
            ):
                reject("task_set_closure_child_evidence_digest_mismatch")
                break

    runtime_root = project / ".ccb" / "runtime" / "task-sets" / task_set_id
    intent_path = runtime_root / "closure-intents.json"
    paths["closure_intents"] = str(intent_path)
    intent_store = _read_json(intent_path)
    intents = intent_store.get("intents") if isinstance(intent_store, dict) else None
    closed_intents = [
        item for item in intents if isinstance(item, dict)
        and item.get("task_set_revision") == revision
        and item.get("closure_digest") == closure_digest
    ] if isinstance(intents, list) else []
    if len(closed_intents) != 1:
        reject("task_set_closure_intent_missing_or_ambiguous")
        return evidence
    intent = closed_intents[0]
    transport = intent.get("transport_ref") if isinstance(intent.get("transport_ref"), dict) else {}
    if (
        intent.get("schema") != "ccb.plan.task_set_closure_intent.v1"
        or intent.get("status") != "feedback_closed"
        or intent.get("task_set_id") != task_set_id
        or intent.get("ordered_terminal_evidence_digest") != closure.get("ordered_terminal_evidence_digest")
        or not intent.get("feedback_closed_at")
    ):
        reject("task_set_closure_intent_not_closed")

    settlement_path = task_set_path.with_name(f"closure-settlement-r{revision}.json")
    paths["closure_settlement"] = str(settlement_path)
    settlement = _read_json(settlement_path)
    if not isinstance(settlement, dict):
        reject("task_set_closure_settlement_missing")
        return evidence
    if (
        settlement.get("schema") != "ccb.plan.task_set_closure_settlement.v2"
        or settlement.get("schema_version") != 2
        or settlement.get("task_set_id") != task_set_id
        or settlement.get("task_set_revision") != revision
        or settlement.get("intent_id") != intent.get("intent_id")
        or settlement.get("aggregate_result") != aggregate
        or settlement.get("closure_digest") != closure_digest
        or settlement.get("ordered_terminal_evidence_digest")
        != closure.get("ordered_terminal_evidence_digest")
        or settlement.get("settlement_digest") != _authority_digest(settlement, omit="settlement_digest")
        or settlement.get("transport_ref") != transport
    ):
        reject("task_set_closure_settlement_authority_mismatch")

    runtime_path = _authority_path(
        project,
        transport.get("runtime_state_path"),
        runtime_root / f"feedback-r{revision}.json",
    )
    backfill_path = _authority_path(
        project,
        transport.get("planner_backfill_path"),
        task_set_path.with_name(f"planner-backfill-r{revision}.json"),
    )
    paths["feedback_runtime"] = str(runtime_path) if runtime_path else None
    paths["planner_backfill"] = str(backfill_path) if backfill_path else None
    runtime = _read_json(runtime_path) if runtime_path else None
    if not isinstance(runtime, dict):
        reject("task_set_feedback_runtime_missing")
    else:
        planner = runtime.get("planner") if isinstance(runtime.get("planner"), dict) else {}
        frontdesk = runtime.get("frontdesk") if isinstance(runtime.get("frontdesk"), dict) else {}
        backfill_import = runtime.get("backfill_import") if isinstance(runtime.get("backfill_import"), dict) else {}
        notification = runtime.get("notification") if isinstance(runtime.get("notification"), dict) else {}
        if (
            runtime.get("schema") != "ccb.plan.task_set_feedback_runtime.v1"
            or runtime.get("schema_version") != 1
            or runtime.get("task_set_id") != task_set_id
            or runtime.get("task_set_revision") != revision
            or runtime.get("closure_intent_id") != intent.get("intent_id")
            or runtime.get("closure_digest") != closure_digest
            or runtime.get("terminal_evidence_digest") != closure.get("ordered_terminal_evidence_digest")
            or runtime.get("stage") != "closed"
            or runtime.get("runtime_digest") != _authority_digest(runtime, omit="runtime_digest")
            or not _closed_feedback_transport(
                planner,
                target="planner",
                purpose="planner_backfill",
                job_id=transport.get("planner_job_id"),
                silent=True,
            )
            or backfill_import.get("planner_job_id") != transport.get("planner_job_id")
            or backfill_import.get("closure_digest") != closure_digest
            or notification.get("status") != "delivered"
            or not _closed_feedback_transport(
                frontdesk,
                target="frontdesk",
                purpose="frontdesk_status",
                job_id=transport.get("frontdesk_job_id"),
                silent=False,
            )
            or notification.get("job_id") != transport.get("frontdesk_job_id")
        ):
            reject("task_set_feedback_runtime_or_frontdesk_handoff_mismatch")
        evidence["planner_backfill_job_id"] = transport.get("planner_job_id")
        evidence["frontdesk_completion_job_id"] = transport.get("frontdesk_job_id")

    backfill = _read_json(backfill_path) if backfill_path else None
    if not isinstance(backfill, dict):
        reject("planner_backfill_authority_missing")
    else:
        authority = backfill.get("authority") if isinstance(backfill.get("authority"), dict) else {}
        proposal = backfill.get("proposal") if isinstance(backfill.get("proposal"), dict) else {}
        required_backfill_fields = {
            "schema", "schema_version", "authority", "proposal", "transaction_digest",
            "target_plan_revision", "transaction_path", "backfill_digest",
        }
        required_backfill_authority = {
            "task_set_id", "task_set_revision", "closure_intent_id", "closure_digest",
            "ordered_terminal_evidence_digest", "expected_plan_revision", "planner_job_id",
            "planner_source_job_id", "planner_effective_job_id", "planner_retry_lineage",
            "planner_feedback_digest", "plan_slug",
        }
        if (
            set(backfill) != required_backfill_fields
            or set(authority) != required_backfill_authority
            or backfill.get("schema") != "ccb.plan.planner_backfill.v2"
            or backfill.get("schema_version") != 2
            or backfill.get("backfill_digest") != _authority_digest(backfill, omit="backfill_digest")
            or authority.get("task_set_id") != task_set_id
            or authority.get("task_set_revision") != revision
            or authority.get("closure_digest") != closure_digest
            or authority.get("ordered_terminal_evidence_digest")
            != closure.get("ordered_terminal_evidence_digest")
            or authority.get("planner_job_id") != transport.get("planner_job_id")
            or authority.get("planner_feedback_digest") != transport.get("planner_feedback_digest")
            or proposal.get("frontdesk_notification_required") is not True
            or backfill.get("backfill_digest") != transport.get("backfill_digest")
        ):
            reject("planner_backfill_authority_mismatch")

    parent_id = str(task_set.get("source_task_id") or "")
    parent = _task_index_record(project, parent_id)
    expected_parent_status = {"pass": "done", "partial": "partial", "replan_required": "replan_required", "blocked": "blocked"}.get(aggregate or "")
    parent_settlement = parent.get("task_set_closure") if isinstance(parent, dict) and isinstance(parent.get("task_set_closure"), dict) else {}
    if (
        not isinstance(parent, dict)
        or parent.get("status") != expected_parent_status
        or parent.get("next_owner") != ("terminal" if aggregate in {"pass", "blocked"} else "planner")
        or parent_settlement.get("task_set_id") != task_set_id
        or parent_settlement.get("task_set_revision") != revision
        or parent_settlement.get("aggregate_result") != aggregate
        or parent_settlement.get("closure_digest") != closure_digest
        or parent_settlement.get("planner_feedback_digest") != transport.get("planner_feedback_digest")
    ):
        reject("task_set_source_parent_not_settled")

    l3_id = task_ids[2]
    l3_record = child_records[2]
    activation_dir = project / ".ccb" / "runtime" / "loops" / "activations"
    constrained: list[tuple[Path, dict[str, Any]]] = []
    l3_activations: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(activation_dir.glob("*.json")):
        activation = _read_json(path)
        if not isinstance(activation, dict) or activation.get("task_id") != l3_id:
            continue
        l3_activations.append((path, activation))
        if isinstance(activation.get("terminal_status_constraint"), dict):
            constrained.append((path, activation))
    if len(constrained) != 1:
        reject("l3_terminal_constraint_missing_or_ambiguous")
    else:
        constraint_path, activation = constrained[0]
        constraint = activation["terminal_status_constraint"]
        assert isinstance(constraint, dict)
        ask = activation.get("ask") if isinstance(activation.get("ask"), dict) else {}
        paths["l3_terminal_activation"] = str(constraint_path)
        evidence["constraint_digest"] = constraint.get("authority_digest")
        evidence["constraint_revision"] = constraint.get("task_revision")
        required_constraint_keys = {
            "schema_version", "status", "basis", "task_id", "task_revision",
            "state_version", "authority_digest", "basis_digest", "required_reason",
        }
        if (
            set(constraint) != required_constraint_keys
            or constraint.get("schema_version") != 1
            or constraint.get("status") != "detail_ready"
            or constraint.get("basis") != "verified_detail_ready_stop_contract"
            or constraint.get("task_id") != l3_id
            or constraint.get("task_revision") != l3_record.get("task_revision")
            or constraint.get("state_version") != l3_record.get("state_version")
            or not re.fullmatch(r"[0-9a-f]{64}", str(constraint.get("authority_digest") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(constraint.get("basis_digest") or ""))
            or not LABEL_RE.fullmatch(str(constraint.get("required_reason") or ""))
            or activation.get("record_type") != "ccb_loop_planner_activation"
            or activation.get("task_status") != "detail_ready"
            or ask.get("target") != "planner"
            or not ask.get("job_id")
        ):
            reject("l3_terminal_constraint_invalid")
        l3_closure = next(
            (
                child for child in ordered_children
                if isinstance(child, dict) and child.get("task_id") == l3_id
            ),
            {},
        )
        l3_authority = l3_closure.get("authority") if isinstance(l3_closure, dict) and isinstance(l3_closure.get("authority"), dict) else {}
        if (
            l3_authority.get("artifact_kind") != "detail_ready_stop_contract"
            or l3_authority.get("authority_digest") != constraint.get("authority_digest")
            or l3_authority.get("basis_digest") != constraint.get("basis_digest")
        ):
            reject("l3_terminal_constraint_closure_authority_mismatch")
        for path, observed in l3_activations:
            if path == constraint_path:
                continue
            if (
                observed.get("record_type") in {
                    "ccb_loop_planner_activation", "ccb_loop_orchestrator_activation"
                }
                and observed.get("task_revision") == constraint.get("task_revision")
            ):
                reject("l3_post_settlement_reactivation")
                break

        import_path = project / ".ccb" / "runtime" / "role-output-imports.jsonl"
        paths["role_output_import_ledger"] = str(import_path)
        imports = _read_json_lines(import_path)
        settlement_indexes = [
            index for index, record in enumerate(imports)
            if record.get("action") == "settled_planner_terminal_status_constraint"
            and record.get("status") == "ok"
            and record.get("task_id") == l3_id
            and record.get("terminal_status_constraint") == constraint
            and isinstance(record.get("source_job"), dict)
            and record["source_job"].get("job_id") == ask.get("job_id")
        ]
        if len(settlement_indexes) != 1:
            reject("l3_terminal_constraint_settlement_missing_or_mismatch")
        else:
            evidence["settlement_action"] = "settled_planner_terminal_status_constraint"
            post_settlement = imports[settlement_indexes[0] + 1:]
            if any(
                record.get("task_id") == l3_id
                and record.get("action") in {
                    "imported_orchestration_notes",
                    "imported_planner_task_authority",
                    "settled_planner_terminal_status_constraint",
                }
                for record in post_settlement
            ):
                reject("l3_post_settlement_import_or_rerun")

    evidence["valid"] = not errors
    return evidence


def settled_task_set_source_parent_ids(manifest: dict[str, Any]) -> set[str]:
    aliases = sequence_task_aliases(manifest)
    task_ids = [aliases.get(str(case["task_id"]), str(case["task_id"])) for case in TASKS]
    evidence = b7_task_set_evidence(manifest, task_ids)
    source_task_id = None
    task_set_id = evidence.get("task_set_id")
    revision = evidence.get("task_set_revision")
    task_set: dict[str, Any] | None = None
    if task_set_id and isinstance(revision, int):
        candidate = _read_json(
            Path(str(manifest["project"]))
            / "docs" / "plantree" / "plans" / PLAN_SLUG / "task-sets" / str(task_set_id) / "task-set.json"
        )
        task_set = candidate if isinstance(candidate, dict) else None
        source_task_id = task_set.get("source_task_id") if task_set else None
    parent = _task_index_record(Path(str(manifest["project"])), str(source_task_id or ""))
    binding = parent.get("task_set_parent") if isinstance(parent, dict) and isinstance(parent.get("task_set_parent"), dict) else {}
    if (
        source_task_id
        and isinstance(parent, dict)
        and parent.get("status") != "decomposed"
        and binding.get("schema") == "ccb.plan.task_set_binding.v1"
        and binding.get("binding_role") == "parent"
        and binding.get("task_set_id") == task_set_id
        and binding.get("task_set_revision") == revision
        and task_set is not None
        and task_set.get("state") in {"closed", "partial", "replan_required", "blocked"}
        and parent.get("status") == {
            "closed": "done",
            "partial": "partial",
            "replan_required": "replan_required",
            "blocked": "blocked",
        }.get(task_set.get("state"))
    ):
        return {str(source_task_id)}
    return set()


def _b7_cleanup_claimable(manifest: dict[str, Any]) -> bool:
    b7_path = Path(str(manifest["b7"]))
    if not re.search(r"(?m)^Status: pass$", _read_text(b7_path)):
        return False
    rows = _read_json_lines(Path(str(manifest["rows"])))
    return bool(rows) and all(row.get("claimable_row") is True for row in rows)


def write_b7_report(manifest: dict[str, Any]) -> None:
    assert_no_pending_authority(manifest)
    planner_evidence = planner_task_set_evidence(manifest)
    if not planner_evidence.get("fenced_task_set_present"):
        assert_planner_task_set_present(manifest)
    project = Path(str(manifest["project"]))
    unexpected = unexpected_plan_task_ids(manifest)
    if unexpected:
        write_unexpected_task_failure_report(manifest, unexpected)
        return
    auto_runner = auto_runner_lock_state(manifest)
    rows = []
    aliases = sequence_task_aliases(manifest)
    observed_task_ids = [aliases.get(str(case["task_id"]), str(case["task_id"])) for case in TASKS]
    task_set_evidence = b7_task_set_evidence(manifest, observed_task_ids)
    for case in TASKS:
        canonical_task_id = str(case["task_id"])
        task_id = aliases.get(canonical_task_id, canonical_task_id)
        route_evidence = _task_route_evidence(manifest, task_id)
        route_text = str(route_evidence["observed_route"])
        status = _task_status(manifest, task_id)
        round_evidence = _round_evidence(manifest, task_id)
        round_json_path, round_json = _round_json_for(project, task_id)
        artifacts = _task_artifacts(manifest, task_id)
        result_evidence = _status_result_evidence(case, status, round_evidence, artifacts)
        cleanup_evidence = _cleanup_evidence(case, round_json)
        errors = []
        if route_text != case["expected_route"]:
            errors.append(f"route mismatch: {route_text}")
        if status["final_status"] != case["expected_final_status"]:
            errors.append(f"status mismatch: {status['final_status']}")
        if result_evidence["round_result"] != case["expected_round_result"]:
            errors.append(f"round/result mismatch: {result_evidence['round_result']}")
        if not route_evidence["script_owned_route_imports"]:
            errors.append("route import is not script-owned")
        if not result_evidence["script_owned_round_imports"]:
            errors.append("result import is not script-owned")
        if cleanup_evidence["runtime_residue"]:
            errors.append("runtime residue after round release")
        if not task_set_evidence["valid"]:
            errors.extend(str(item) for item in task_set_evidence["errors"])
        if auto_runner.get("status") == "live":
            errors.append("auto_runner_lock_live_before_b7")
        classification = "test_design_failure" if errors else str(case["expected_classification"])
        row = {
            "label": manifest["label"],
            "root": manifest["root"],
            "task_id": canonical_task_id,
            "observed_task_id": task_id,
            "task_id_alias_used": task_id != canonical_task_id,
            "expected_route": case["expected_route"],
            "observed_route": route_text,
            "route_decision_correct": route_text == case["expected_route"],
            "route_source": route_evidence["route_source"],
            "route_artifact_path": route_evidence["route_artifact_path"],
            "script_owned_route_imports": route_evidence["script_owned_route_imports"],
            "expected_final_status": case["expected_final_status"],
            "final_status": status["final_status"],
            "next_owner": status["next_owner"],
            "task_show_observed": status["task_show_observed"],
            "task_show_path": status["task_show_path"],
            "task_show_source": status["task_show_source"],
            "round_summary_observed": round_evidence["round_summary_observed"],
            "round_summary_path": round_evidence["round_summary_path"],
            "round_json_path": round_evidence["round_json_path"],
            "round_result": result_evidence["round_result"],
            "round_result_source": result_evidence["round_result_source"],
            "script_owned_round_imports": result_evidence["script_owned_round_imports"],
            "provider_reply_authority_parsing_absent": True,
            **cleanup_evidence,
            "unexpected_plan_tasks": unexpected,
            "planner_job_id": planner_evidence.get("planner_job_id"),
            "frontdesk_job_id": planner_evidence.get("frontdesk_job_id"),
            "activation_path": planner_evidence.get("activation_path"),
            "planner_snapshot_path": planner_evidence.get("planner_snapshot_path"),
            "planner_reply_path": planner_evidence.get("planner_reply_path"),
            "fenced_task_set_present": bool(planner_evidence.get("fenced_task_set_present")),
            "task_set_id": task_set_evidence.get("task_set_id"),
            "task_set_revision": task_set_evidence.get("task_set_revision"),
            "closure_digest": task_set_evidence.get("closure_digest"),
            "aggregate_result": task_set_evidence.get("aggregate_result"),
            "constraint_digest": task_set_evidence.get("constraint_digest"),
            "constraint_revision": task_set_evidence.get("constraint_revision"),
            "settlement_action": task_set_evidence.get("settlement_action"),
            "planner_backfill_job_id": task_set_evidence.get("planner_backfill_job_id"),
            "frontdesk_completion_job_id": task_set_evidence.get("frontdesk_completion_job_id"),
            "task_set_evidence_paths": task_set_evidence.get("evidence_paths"),
            "task_set_authority_valid": task_set_evidence["valid"],
            "auto_runner_state": auto_runner,
            "classification": classification,
            "expected_classification": case["expected_classification"],
            "claimable_row": classification == case["expected_classification"],
            "evidence_errors": errors,
        }
        rows.append(row)
    rows_path = Path(str(manifest["rows"]))
    b7_path = Path(str(manifest["b7"]))
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    rows_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    status = "pass" if all(row["claimable_row"] for row in rows) else "not_claimable"
    b7_path.write_text(
        f"# Phase 6B L1-L4 {manifest['label']} B7\n\n"
        f"Status: {status}\n\n"
        "```json\n"
        + json.dumps(rows, indent=2, sort_keys=True)
        + "\n```\n",
        encoding="utf-8",
    )


def cleanup_after_b7(manifest: dict[str, Any]) -> None:
    assert_no_pending_authority(manifest)
    auto_runner = auto_runner_lock_state(manifest)
    if auto_runner.get("status") == "live":
        raise HarnessBlocker(
            classification="not_claimable",
            reason="auto_runner_lock_live_before_cleanup",
            message=f"refuse cleanup while auto-runner remains live: {auto_runner}",
        )
    if not _b7_cleanup_claimable(manifest):
        raise HarnessBlocker(
            classification="not_claimable",
            reason="b7_not_claimable_for_cleanup",
            message="refuse cleanup until B7 has a pass status and every row is claimable",
        )
    run_logged(manifest, "cleanup_after_b7", ccb_project_args(manifest, "kill"))


def load_manifest(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise HarnessError(f"invalid manifest: {path}")
    validate_manifest(payload)
    return payload


def run_from_manifest(manifest: dict[str, Any], argv: list[str]) -> None:
    if not argv:
        raise HarnessError("missing runner command")
    command = argv[0]
    if command == "init":
        init_lab(manifest)
    elif command == "frontdesk-entry":
        frontdesk_entry(manifest)
    elif command == "start-task":
        if len(argv) != 2:
            raise HarnessError("usage: start-task <task_id>")
        start_task(manifest, argv[1])
    elif command == "continue-route":
        if len(argv) != 3:
            raise HarnessError("usage: continue-route <task_id> <expected_route>")
        continue_route(manifest, argv[1], argv[2])
    elif command == "continue-detail":
        if len(argv) != 2:
            raise HarnessError("usage: continue-detail <task_id>")
        continue_detail(manifest, argv[1])
    elif command == "resume-pending":
        if len(argv) != 2:
            raise HarnessError("usage: resume-pending <task_id>")
        resume_pending_round(manifest, argv[1])
    elif command == "b7":
        write_b7_report(manifest)
    elif command == "cleanup-after-b7":
        cleanup_after_b7(manifest)
    elif command == "check-pending":
        task_id = argv[1] if len(argv) > 1 else None
        assert_no_pending_authority(manifest, task_id)
    else:
        raise HarnessError(f"unknown runner command: {command}")


def cmd_materialize(args: argparse.Namespace) -> int:
    manifest = materialize(Path(args.root), args.label, args.project_name)
    print(manifest["manifest"])
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    run_from_manifest(manifest, list(args.runner_args))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Materialize the Phase 6B L1-L4 frontdesk real-provider harness.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    materialize_parser = subparsers.add_parser("materialize")
    materialize_parser.add_argument("--root", required=True)
    materialize_parser.add_argument("--label", required=True)
    materialize_parser.add_argument("--project-name", required=True)
    materialize_parser.set_defaults(func=cmd_materialize)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--manifest", required=True)
    run_parser.add_argument("runner_args", nargs=argparse.REMAINDER)
    run_parser.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
