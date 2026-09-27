#!/usr/bin/env python3
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from single_lane_evidence_harness import (  # noqa: E402
    CASE_REQUIREMENTS,
    EXECUTION_MODE,
    INPUT_SCHEMA,
    REQUIRED_CASE_IDS,
    sha256_value,
)


FIXTURE_SOURCE_COMMIT = "f7c4f5bcfa5abbbf5a18055fe667fb644bd5396f"


def build_fixture_manifest() -> dict[str, Any]:
    cases = [
        _base_case(case_id, scenario, count, shape)
        for case_id, scenario, count, shape, _expected in CASE_REQUIREMENTS
    ]
    by_id = {case["case_id"]: case for case in cases}

    _reviewer_rework(by_id["reviewer-rework-pass"])
    _worker_failure(by_id["worker-failure-partial"])
    _reviewer_failure(by_id["reviewer-failure-replan"])
    _round_failure(by_id["round-failure-blocked"])
    _restart_replay(by_id["unknown-submission-restart-replay"])
    _merge_conflict(by_id["merge-conflict-replan"])
    _root_failure_rollback(by_id["root-test-failure-rollback"])
    _bounded_busy(by_id["release-busy-bounded"])
    _persistent_release_failure(by_id["release-persistent-failure"])
    del by_id["missing-node-evidence"]["nodes"][1]["jobs"]
    _fake_success(by_id["provider-fake-success"])
    _duplicate_ask(by_id["duplicate-ask"])
    _duplicate_integration(by_id["duplicate-integration"])
    _runtime_leak(by_id["process-runtime-leak"])
    by_id["artifact-digest-mismatch"]["artifacts"][0]["sha256"] = "0" * 64

    return {
        "schema": INPUT_SCHEMA,
        "campaign_id": "wave2-e1-deterministic-failure-matrix",
        "execution_mode": EXECUTION_MODE,
        "required_case_ids": list(REQUIRED_CASE_IDS),
        "cases": cases,
    }


def write_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_fixture_manifest(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _base_case(case_id: str, scenario: str, count: int, shape: str) -> dict[str, Any]:
    dependencies = _dependencies(count, shape)
    nodes = [_node(case_id, index + 1, dependencies[index]) for index in range(count)]
    node_ids = [node["node_id"] for node in nodes]
    execution_agents = [
        agent
        for node_id in node_ids
        for agent in (
            f"loop-{case_id}-{node_id}-coder",
            f"loop-{case_id}-{node_id}-code-reviewer",
        )
    ]
    dynamic_agents = [
        f"loop-{case_id}-orchestrator",
        *execution_agents,
        f"loop-{case_id}-ccb-round-reviewer",
    ]
    pre_manifest = {"README.md": "base\n"}
    integration_manifest = {"README.md": "base\n", **{f"work/{node_id}.txt": f"accepted {node_id}\n" for node_id in node_ids}}
    selection = {
        "workgroup_count": count,
        "complexity": ("atomic" if count == 1 else "bounded" if count == 2 else "complex" if count == 3 else "very_complex"),
        "cutability": "none" if count == 1 else "high",
        "execution_shape": shape,
        "rationale": f"deterministic {shape} fixture with {count} reviewed workgroup(s)",
    }
    task_semantic = {"task_id": f"task-{case_id}", "request": f"fixture scenario {scenario}"}
    bundle_semantic = {
        "task_id": task_semantic["task_id"],
        "selection": selection,
        "nodes": [{"node_id": node["node_id"], "dependencies": node["dependencies"]} for node in nodes],
    }
    desired = {"agents": dynamic_agents, "owner": case_id}
    observed = {"agents": [], "owner": case_id}
    integration_head = _commit(f"integration:{case_id}")
    artifacts = [
        _artifact("task", f"raw/{case_id}/task.json", task_semantic),
        _artifact("bundle", f"raw/{case_id}/bundle.json", bundle_semantic),
        _artifact("round", f"raw/{case_id}/round.json", {"result": "pass", "task_status": "done"}),
    ]
    asks = [{"intent_key": "1:bundle:orchestrator:1", "job_id": f"job-{case_id}-orchestrator-1"}] + [
        {"intent_key": f"1:{node['node_id']}:worker:1", "job_id": f"job-{case_id}-{node['node_id']}-worker-1"}
        for node in nodes
    ] + [
        {"intent_key": f"1:{node['node_id']}:reviewer:1", "job_id": f"job-{case_id}-{node['node_id']}-reviewer-1"}
        for node in nodes
    ] + [{"intent_key": "1:round:round_reviewer:1", "job_id": f"job-{case_id}-round-reviewer-1"}]
    placements = [
        {
            "agent_id": agent,
            "window": (
                "ccb-plan"
                if "orchestrator" in agent or "round-reviewer" in agent
                else "ccb-exec-1" if index - 1 < 6 else "ccb-exec-2"
            ),
            "pane": f"%{index + 10}",
        }
        for index, agent in enumerate(dynamic_agents)
    ]
    activation_count = len(dynamic_agents)
    return {
        "case_id": case_id,
        "scenario": scenario,
        "task": {"task_id": task_semantic["task_id"], "revision": 1, "semantic": task_semantic, "digest": sha256_value(task_semantic)},
        "bundle": {"revision": 1, "semantic": bundle_semantic, "digest": sha256_value(bundle_semantic), "selection": selection},
        "nodes": nodes,
        "dependency_readiness": {
            "ready_node_ids": node_ids,
            "blocked_node_ids": [],
            "observations": [
                {"node_id": node["node_id"], "dependencies": node["dependencies"], "ready": True}
                for node in nodes
            ],
        },
        "integration": {
            "worktree": f"/fixtures/{case_id}/integration",
            "base_commit": _commit(f"base:{case_id}"),
            "order": node_ids,
            "head_commit": integration_head,
            "tree_manifest": integration_manifest,
            "tree_digest": sha256_value(integration_manifest),
            "checks": [{"command": "python -m unittest", "status": "passed"}],
            "conflict": False,
            "status": "passed",
        },
        "root": {
            "pre_manifest": pre_manifest,
            "pre_digest": sha256_value(pre_manifest),
            "post_manifest": integration_manifest,
            "post_digest": sha256_value(integration_manifest),
            "promotion": "promoted",
            "rollback": "not_required",
            "rollback_manifest": None,
            "rollback_digest": None,
            "checks": [{"command": "python -m unittest", "status": "passed"}],
        },
        "round": {
            "result": "pass",
            "task_status": "done",
            "round_reviewer_job": f"job-{case_id}-round-reviewer-1",
            "round_review_result": "pass",
            "script_owned_import": True,
            "provider_prose": "Provider reports success; scripts independently verify authority.",
        },
        "topology_release": {
            "desired": desired,
            "desired_digest": sha256_value(desired),
            "observed": observed,
            "observed_digest": sha256_value(observed),
            "released_count": len(dynamic_agents),
            "retained_count": 0,
            "drained_agents": dynamic_agents,
            "release_incomplete": False,
            "release_status": "released",
        },
        "runtime_residue": {
            "captured_before_cleanup": True,
            "processes": [],
            "runtime_files": [],
            "bounded_retained_busy": None,
        },
        "ui_placement": {
            "socket": f"/fixtures/{case_id}/tmux.sock",
            "windows": ["ccb-plan", "ccb-exec-1"] + (["ccb-exec-2"] if len(execution_agents) > 6 else []),
            "placements": placements,
            "sidebar_agents": dynamic_agents,
        },
        "immaculate_freshness": {
            "activations": [
                {"activation_id": f"activation-{case_id}-{index + 1}", "immaculate": True}
                for index in range(activation_count)
            ],
            "provider_sessions": [
                {"session_id": f"session-{case_id}-{index + 1}", "reused": False}
                for index in range(activation_count)
            ],
        },
        "authority_log": {
            "events": [
                {"actor": "provider", "mutation": "evidence_only", "accepted": True},
                {"actor": "script", "mutation": "round_result_import", "accepted": True},
            ],
            "asks": asks,
            "integrations": [{"node_id": node["node_id"], "commit": node["reviewed_commit"]} for node in nodes],
            "legacy_files": [],
            "reported_classification": None,
        },
        "artifacts": artifacts,
    }


def _node(case_id: str, number: int, dependencies: list[str]) -> dict[str, Any]:
    node_id = f"node-{number:03d}"
    tree_manifest = {f"work/{node_id}.txt": f"accepted {node_id}\n"}
    head = _commit(f"head:{case_id}:{node_id}")
    return {
        "node_id": node_id,
        "dependencies": dependencies,
        "intent_records": [
            {"purpose": "worker", "attempt": 1, "state": "consumed"},
            {"purpose": "reviewer", "attempt": 1, "state": "consumed"},
        ],
        "jobs": [
            {"purpose": "worker", "attempt": 1, "job_id": f"job-{case_id}-{node_id}-worker-1", "status": "completed"},
            {"purpose": "reviewer", "attempt": 1, "job_id": f"job-{case_id}-{node_id}-reviewer-1", "status": "completed"},
        ],
        "workspace": f"/fixtures/{case_id}/{node_id}",
        "branch": f"ccb/{case_id}/{node_id}",
        "base_commit": _commit(f"base:{case_id}:{node_id}"),
        "head_commit": head,
        "tree_manifest": tree_manifest,
        "tree_digest": sha256_value(tree_manifest),
        "review_result": "pass",
        "reviewed_tree_digest": sha256_value(tree_manifest),
        "reviewed_commit": head,
        "integrated": True,
    }


def _dependencies(count: int, shape: str) -> list[list[str]]:
    if shape in {"single_unit", "parallel"}:
        return [[] for _ in range(count)]
    if shape == "serial":
        return [[]] + [[f"node-{index:03d}"] for index in range(1, count)]
    if shape == "mixed_dag":
        return [[], [], ["node-001"], ["node-001", "node-002"]]
    raise ValueError(f"unsupported fixture shape: {shape}")


def _reviewer_rework(case: dict[str, Any]) -> None:
    node = case["nodes"][0]
    node["intent_records"] = [
        {"purpose": "worker", "attempt": 1, "state": "consumed"},
        {"purpose": "reviewer", "attempt": 1, "state": "consumed"},
        {"purpose": "worker_rework", "attempt": 1, "state": "consumed"},
        {"purpose": "reviewer_recheck", "attempt": 2, "state": "consumed"},
    ]
    node["jobs"] = [
        node["jobs"][0],
        {**node["jobs"][1], "status": "rework"},
        {"purpose": "worker_rework", "attempt": 1, "job_id": f"job-{case['case_id']}-node-001-rework-1", "status": "completed"},
        {"purpose": "reviewer", "attempt": 2, "job_id": f"job-{case['case_id']}-node-001-reviewer-2", "status": "completed"},
    ]


def _worker_failure(case: dict[str, Any]) -> None:
    failed = case["nodes"][1]
    failed["jobs"] = [{**failed["jobs"][0], "status": "failed"}]
    failed["intent_records"] = [failed["intent_records"][0]]
    failed["review_result"] = "not_run"
    failed["reviewed_tree_digest"] = None
    failed["reviewed_commit"] = None
    failed["integrated"] = False
    _non_success(case, "partial", "partial", integrated_node_ids=["node-001"])


def _reviewer_failure(case: dict[str, Any]) -> None:
    failed = case["nodes"][1]
    failed["jobs"][1]["status"] = "failed"
    failed["review_result"] = "failed"
    failed["reviewed_tree_digest"] = None
    failed["reviewed_commit"] = None
    failed["integrated"] = False
    _non_success(case, "replan_required", "replan_required", integrated_node_ids=["node-001"])


def _round_failure(case: dict[str, Any]) -> None:
    case["round"]["result"] = "blocked"
    case["round"]["task_status"] = "blocked"
    case["round"]["round_review_result"] = "failed"
    _rollback(case)


def _restart_replay(case: dict[str, Any]) -> None:
    case["authority_log"]["events"].insert(
        1, {"actor": "script", "mutation": "restart_replay_resolved_unknown_submission", "accepted": True}
    )
    case["nodes"][0]["jobs"][0]["submission_history"] = ["unknown", "resolved_same_job"]


def _merge_conflict(case: dict[str, Any]) -> None:
    for node in case["nodes"]:
        node["integrated"] = False
    _non_success(case, "replan_required", "replan_required", integrated_node_ids=[])
    case["integration"]["status"] = "conflict"
    case["integration"]["conflict"] = True
    case["integration"]["checks"] = [{"command": "git merge", "status": "failed"}]


def _root_failure_rollback(case: dict[str, Any]) -> None:
    case["round"]["result"] = "partial"
    case["round"]["task_status"] = "partial"
    case["round"]["round_review_result"] = "not_run"
    case["root"]["checks"] = [{"command": "python -m unittest", "status": "failed"}]
    _rollback(case)


def _bounded_busy(case: dict[str, Any]) -> None:
    desired_agents = case["topology_release"]["desired"]["agents"]
    agent_id = next(agent for agent in desired_agents if "-coder" in agent)
    observed = {"agents": [agent_id], "owner": case["case_id"]}
    case["topology_release"].update(
        {
            "observed": observed,
            "observed_digest": sha256_value(observed),
            "released_count": len(desired_agents) - 1,
            "retained_count": 1,
            "drained_agents": [agent for agent in desired_agents if agent != agent_id],
            "release_incomplete": True,
            "release_status": "retained_busy",
        }
    )
    case["runtime_residue"].update(
        {
            "processes": [{"kind": "provider", "agent_id": agent_id, "state": "busy"}],
            "runtime_files": [{"kind": "agent_runtime", "agent_id": agent_id, "state": "retained"}],
            "bounded_retained_busy": {
                "agent_ids": [agent_id],
                "reason": "retained_busy",
                "bounded": True,
                "retry_attempt": 1,
                "max_retries": 3,
            },
        }
    )


def _persistent_release_failure(case: dict[str, Any]) -> None:
    agents = case["topology_release"]["desired"]["agents"]
    observed = {"agents": agents, "owner": case["case_id"]}
    case["topology_release"].update(
        {
            "observed": observed,
            "observed_digest": sha256_value(observed),
            "released_count": 0,
            "retained_count": 0,
            "drained_agents": [],
            "release_incomplete": True,
            "release_status": "failed",
        }
    )


def _fake_success(case: dict[str, Any]) -> None:
    case["round"].update({"result": "blocked", "task_status": "blocked", "round_review_result": "failed"})
    case["authority_log"]["reported_classification"] = "pass"
    case["authority_log"]["events"].append(
        {"actor": "provider", "mutation": "task_result_pass", "accepted": True}
    )
    _rollback(case)


def _duplicate_ask(case: dict[str, Any]) -> None:
    case["authority_log"]["asks"].append(deepcopy(case["authority_log"]["asks"][0]))


def _duplicate_integration(case: dict[str, Any]) -> None:
    case["authority_log"]["integrations"].append(deepcopy(case["authority_log"]["integrations"][0]))


def _runtime_leak(case: dict[str, Any]) -> None:
    agent_id = case["topology_release"]["desired"]["agents"][0]
    case["runtime_residue"].update(
        {
            "processes": [
                {"kind": "ccbd", "agent_id": agent_id, "state": "running"},
                {"kind": "tmux", "agent_id": agent_id, "state": "running"},
                {"kind": "provider", "agent_id": agent_id, "state": "running"},
            ],
            "runtime_files": [{"kind": "agent_runtime", "agent_id": agent_id, "state": "active"}],
        }
    )


def _non_success(case: dict[str, Any], result: str, task_status: str, *, integrated_node_ids: list[str]) -> None:
    case["round"].update({"result": result, "task_status": task_status, "round_review_result": "not_run"})
    case["integration"]["order"] = integrated_node_ids
    case["integration"]["status"] = "partial"
    case["authority_log"]["integrations"] = [
        item for item in case["authority_log"]["integrations"] if item["node_id"] in integrated_node_ids
    ]
    case["root"]["post_manifest"] = deepcopy(case["root"]["pre_manifest"])
    case["root"]["post_digest"] = case["root"]["pre_digest"]
    case["root"]["promotion"] = "not_promoted"
    case["root"]["checks"] = [{"command": "python -m unittest", "status": "not_run"}]


def _rollback(case: dict[str, Any]) -> None:
    case["root"]["rollback"] = "restored"
    case["root"]["rollback_manifest"] = deepcopy(case["root"]["pre_manifest"])
    case["root"]["rollback_digest"] = case["root"]["pre_digest"]
    case["root"]["post_manifest"] = deepcopy(case["root"]["pre_manifest"])
    case["root"]["post_digest"] = case["root"]["pre_digest"]


def _artifact(name: str, path: str, content: Any) -> dict[str, Any]:
    return {"name": name, "path": path, "content": content, "sha256": sha256_value(content)}


def _commit(label: str) -> str:
    return hashlib.sha1(label.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: single_lane_evidence_fixtures.py OUTPUT.json")
    write_fixture(Path(sys.argv[1]))
