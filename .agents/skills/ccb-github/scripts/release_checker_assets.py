from __future__ import annotations

from collections.abc import Callable
import json
import re
import tempfile
import time
from pathlib import Path

from release_checker_shared import (
    CHECKSUMMED_ASSETS,
    EXPECTED_ASSETS,
    RELEASE_RUN_LIMIT,
    REQUIRED_TAG_WORKFLOWS,
    _stderr,
    fail,
    read,
    run,
    warn,
)


GithubRunsReader = Callable[[Path, str, int], list[dict[str, object]] | None]
WorkflowStatusFormatter = Callable[[dict[str, dict[str, object]], set[str]], str]


def check_sha256sums(root: Path, version: str, repo: str, issues: list[str], warnings: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="ccb-release-check-") as tmp:
        tmp_path = Path(tmp)
        download = run(
            [
                "gh",
                "release",
                "download",
                version,
                "--repo",
                repo,
                "--pattern",
                "SHA256SUMS",
                "--dir",
                str(tmp_path),
            ],
            root,
        )
        if download.returncode != 0:
            fail(
                issues,
                "Could not download SHA256SUMS from the GitHub release",
                fix="rerun Release Artifacts or re-upload SHA256SUMS, then rerun the published check",
            )
            return
        sums_path = tmp_path / "SHA256SUMS"
        payload = read(sums_path)
        found: dict[str, str] = {}
        for line in payload.splitlines():
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            digest, name = parts
            if re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                found[Path(name).name] = digest.lower()
        missing = sorted(CHECKSUMMED_ASSETS - set(found))
        if missing:
            fail(
                issues,
                f"SHA256SUMS is missing checksum entry/entries for: {', '.join(missing)}",
                fix="rerun Release Artifacts so SHA256SUMS is regenerated from the uploaded tarballs",
            )
        extra = sorted(set(found) - CHECKSUMMED_ASSETS)
        if extra:
            warn(warnings, f"SHA256SUMS contains unexpected extra asset checksum(s): {', '.join(extra)}")


def _read_release_payload(root: Path, version: str, repo: str) -> tuple[dict[str, object] | None, str]:
    release = run(["gh", "release", "view", version, "--repo", repo, "--json", "tagName,url,assets,isDraft"], root)
    if release.returncode != 0:
        return None, release.stderr.strip() or release.stdout.strip()
    try:
        payload = json.loads(release.stdout)
    except json.JSONDecodeError as exc:
        return None, f"Could not parse gh release JSON: {exc}"
    return payload, ""


def _release_artifacts_run_matches(item: dict[str, object], *, version: str, tag_commit: str) -> bool:
    if item.get("headBranch") == version:
        return True
    if tag_commit and item.get("headSha") == tag_commit:
        return True
    return False


def _release_workflow_candidates(
    run_payload: list[dict[str, object]],
    *,
    workflow_name: str,
    version: str,
    tag_commit: str,
) -> list[dict[str, object]]:
    return [
        item
        for item in run_payload
        if item.get("name") == workflow_name and _release_artifacts_run_matches(item, version=version, tag_commit=tag_commit)
    ]


def _latest_release_workflows(
    run_payload: list[dict[str, object]],
    *,
    version: str,
    tag_commit: str,
) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for workflow_name in sorted(REQUIRED_TAG_WORKFLOWS):
        candidates = _release_workflow_candidates(
            run_payload,
            workflow_name=workflow_name,
            version=version,
            tag_commit=tag_commit,
        )
        if candidates:
            latest[workflow_name] = candidates[0]
    return latest


def _published_state_is_pending(
    *,
    release_payload: dict[str, object] | None,
    run_payload: list[dict[str, object]] | None,
    version: str,
    tag_commit: str,
) -> bool:
    if release_payload is None or run_payload is None:
        return False
    latest = _latest_release_workflows(run_payload, version=version, tag_commit=tag_commit)
    for workflow_name in REQUIRED_TAG_WORKFLOWS:
        item = latest.get(workflow_name)
        if item and item.get("status") == "completed" and item.get("conclusion") != "success":
            return False
    asset_names = {asset.get("name") for asset in release_payload.get("assets", [])}
    if EXPECTED_ASSETS - asset_names:
        return True
    for workflow_name in REQUIRED_TAG_WORKFLOWS:
        item = latest.get(workflow_name)
        if item is None:
            return True
        if item.get("status") != "completed":
            return True
    return False


def _published_wait_status(
    *,
    release_payload: dict[str, object] | None,
    run_payload: list[dict[str, object]] | None,
    version: str,
    tag_commit: str,
    format_workflow_wait_status_fn: WorkflowStatusFormatter,
) -> str:
    if release_payload is None:
        return "release=missing"
    asset_names = {str(asset.get("name")) for asset in release_payload.get("assets", [])}
    missing_assets = sorted(EXPECTED_ASSETS - asset_names)
    latest = _latest_release_workflows(run_payload or [], version=version, tag_commit=tag_commit)
    workflows = format_workflow_wait_status_fn(latest, REQUIRED_TAG_WORKFLOWS)
    assets = "assets=ready" if not missing_assets else f"assets=missing({','.join(missing_assets)})"
    return f"{assets}; {workflows}"


def _read_published_release_state(
    *,
    root: Path,
    version: str,
    repo: str,
    tag_commit: str,
    wait_seconds: int,
    poll_interval: int,
    issues: list[str],
    read_github_runs_fn: GithubRunsReader,
    format_workflow_wait_status_fn: WorkflowStatusFormatter,
) -> tuple[dict[str, object] | None, list[dict[str, object]] | None]:
    deadline = time.monotonic() + max(wait_seconds, 0)
    last_wait_status = ""
    while True:
        release_payload, release_error = _read_release_payload(root, version, repo)
        if release_payload is None:
            fail(
                issues,
                f"GitHub release {version} not found for {repo}: {release_error}",
                fix=f"create the release page first: gh release create {version} --repo {repo} --title {version} --notes-file <notes-file>",
            )
            return None, None

        run_payload = read_github_runs_fn(root, repo, RELEASE_RUN_LIMIT)
        if run_payload is None:
            fail(
                issues,
                "Could not read GitHub Actions runs",
                fix="retry after GitHub/API connectivity recovers; final release verification requires workflow status",
            )
            return release_payload, None

        pending = _published_state_is_pending(
            release_payload=release_payload,
            run_payload=run_payload,
            version=version,
            tag_commit=tag_commit,
        )
        if not pending or wait_seconds <= 0 or time.monotonic() >= deadline:
            return release_payload, run_payload

        wait_status = _published_wait_status(
            release_payload=release_payload,
            run_payload=run_payload,
            version=version,
            tag_commit=tag_commit,
            format_workflow_wait_status_fn=format_workflow_wait_status_fn,
        )
        if wait_status != last_wait_status:
            _stderr(f"Waiting for published release state: {wait_status}")
            last_wait_status = wait_status
        time.sleep(max(poll_interval, 1))
