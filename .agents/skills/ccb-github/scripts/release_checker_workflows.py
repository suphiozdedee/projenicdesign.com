from __future__ import annotations

from collections.abc import Callable
import json
import time
from pathlib import Path

from release_checker_shared import (
    BRANCH_VALIDATION_WORKFLOWS,
    DEV_ALWAYS_REQUIRED_WORKFLOWS,
    DEV_DEFAULT_BRANCH_WORKFLOWS,
    RELEASE_RUN_LIMIT,
    _stderr,
    fail,
    git_output,
    run,
    warn,
)


GithubAuthCheck = Callable[[Path, list[str]], bool]
RepoDefaultBranch = Callable[[Path, str, list[str]], str]


def read_github_runs(root: Path, repo: str, limit: int = RELEASE_RUN_LIMIT) -> list[dict[str, object]] | None:
    runs = run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--limit",
            str(limit),
            "--json",
            "name,status,conclusion,headBranch,event,databaseId,url,headSha",
        ],
        root,
    )
    if runs.returncode != 0:
        return None
    try:
        return json.loads(runs.stdout)
    except json.JSONDecodeError:
        return None


def required_dev_workflows(branch: str, default_branch: str) -> set[str]:
    required = set(DEV_ALWAYS_REQUIRED_WORKFLOWS)
    if branch in {default_branch, "main", "dev"}:
        required.update(DEV_DEFAULT_BRANCH_WORKFLOWS)
    return required


def check_dev_branch_workflows(
    *,
    root: Path,
    repo: str,
    wait_seconds: int,
    poll_interval: int,
    issues: list[str],
    warnings: list[str],
    gh_auth_is_ready_fn: GithubAuthCheck,
    repo_default_branch_fn: RepoDefaultBranch,
) -> None:
    if not gh_auth_is_ready_fn(root, issues):
        return

    branch = git_output(root, ["branch", "--show-current"]) or ""
    head = git_output(root, ["rev-parse", "HEAD"]) or ""
    upstream_head = git_output(root, ["rev-parse", "@{u}"]) or ""
    if not branch or not head:
        warn(warnings, "Could not determine current branch/head; GitHub workflow state was not checked")
        return
    if upstream_head and head != upstream_head:
        warn(warnings, "Current HEAD is not pushed to upstream; GitHub workflow state for this commit cannot be complete yet")
        return

    default_branch = repo_default_branch_fn(root, repo, warnings)
    required = required_dev_workflows(branch, default_branch)
    if "Cross-Platform Compatibility Test" not in required:
        warn(
            warnings,
            f"Cross-Platform Compatibility Test is not required for branch {branch!r}; "
            "it only runs on main/dev or manual dispatch",
        )

    deadline = time.monotonic() + max(wait_seconds, 0)
    latest_by_name: dict[str, dict[str, object]] = {}
    last_wait_status = ""
    while True:
        run_payload = read_github_runs(root, repo)
        if run_payload is None:
            fail(
                issues,
                "Could not read GitHub Actions runs for dev workflow check",
                fix="retry after GitHub/API connectivity recovers; final dev verification requires workflow status",
            )
            return
        latest_by_name = {}
        for item in run_payload:
            if item.get("headSha") != head:
                continue
            name = str(item.get("name") or "")
            if name in required and name not in latest_by_name:
                latest_by_name[name] = item

        all_done = True
        for workflow_name in required:
            item = latest_by_name.get(workflow_name)
            if not item or item.get("status") != "completed":
                all_done = False
                break
        if all_done or wait_seconds <= 0 or time.monotonic() >= deadline:
            break
        wait_status = _format_workflow_wait_status(latest_by_name, required)
        if wait_status != last_wait_status:
            _stderr(f"Waiting for dev workflows: {wait_status}")
            last_wait_status = wait_status
        time.sleep(max(poll_interval, 1))

    for workflow_name in sorted(required):
        item = latest_by_name.get(workflow_name)
        if not item:
            fail(
                issues,
                f"No GitHub Actions run found for current commit {head[:12]}: {workflow_name}",
                fix=(
                    "open or update the pull request, manually dispatch the workflow, "
                    "or push main/dev and wait for GitHub Actions"
                ),
            )
            continue
        if item.get("status") != "completed" or item.get("conclusion") != "success":
            fail(
                issues,
                f"GitHub Actions {workflow_name} for current commit is {item.get('status')}/{item.get('conclusion')}: {item.get('url')}",
                fix=f"wait for the run to complete or fix/rerun it; use --wait-seconds to let the checker wait automatically",
            )


def _format_workflow_wait_status(workflows: dict[str, dict[str, object]], required: set[str]) -> str:
    parts = []
    for workflow_name in sorted(required):
        item = workflows.get(workflow_name)
        if item is None:
            parts.append(f"{workflow_name}=missing")
        else:
            parts.append(f"{workflow_name}={item.get('status')}/{item.get('conclusion') or '-'}")
    return ", ".join(parts)


def _check_branch_validation_runs(run_payload: list[dict[str, object]], *, tag_commit: str, warnings: list[str]) -> None:
    if not tag_commit:
        return
    found: set[str] = set()
    for workflow_name in sorted(BRANCH_VALIDATION_WORKFLOWS):
        candidates = [
            item
            for item in run_payload
            if item.get("name") == workflow_name
            and item.get("headSha") == tag_commit
            and item.get("headBranch") not in {"", None}
            and not str(item.get("headBranch")).startswith("v")
        ]
        if not candidates:
            continue
        found.add(workflow_name)
        latest = candidates[0]
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            warn(
                warnings,
                f"Branch validation workflow {workflow_name} for release commit is {latest.get('status')}/{latest.get('conclusion')}: {latest.get('url')}",
            )
    missing = sorted(BRANCH_VALIDATION_WORKFLOWS - found)
    if missing:
        warn(warnings, f"No recent branch validation run found for release commit for: {', '.join(missing)}")
