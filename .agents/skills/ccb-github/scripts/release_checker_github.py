from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from release_checker_assets import (
    _latest_release_workflows,
    _published_state_is_pending,
    _published_wait_status as _assets_published_wait_status,
    _read_published_release_state as _assets_read_published_release_state,
    _read_release_payload,
    _release_artifacts_run_matches,
    _release_workflow_candidates,
    check_sha256sums,
)
from release_checker_markdown import (
    has_substantive_release_text,
    install_section,
    readme_release_block,
    release_note_versions,
    semver_tuple,
)
from release_checker_shared import (
    EXPECTED_ASSETS,
    REQUIRED_TAG_WORKFLOWS,
    fail,
    git_output,
    run,
    warn,
)
from release_checker_workflows import (
    _check_branch_validation_runs as _workflows_check_branch_validation_runs,
    _format_workflow_wait_status,
    check_dev_branch_workflows as _check_dev_branch_workflows,
    read_github_runs,
    required_dev_workflows,
)


def check_readme_surface(
    *,
    body: str,
    readme_name: str,
    version: str,
    repo: str,
    source: str,
    issues: list[str],
    warnings: list[str],
) -> None:
    bare_version = version.removeprefix("v")
    versions = release_note_versions(body)
    if versions:
        if versions[0] != version:
            fail(
                issues,
                f"{source} {readme_name} first release notes entry is {versions[0]}, expected {version}",
                fix="merge/push the release documentation changes to the default branch",
            )
        sorted_versions = sorted(versions, key=semver_tuple, reverse=True)
        if versions != sorted_versions:
            warn(warnings, f"{source} {readme_name} release notes are not in descending semver order")
    else:
        fail(
            issues,
            f"{source} {readme_name} has no release notes version entries",
            fix="merge/push a README with current release notes to the default branch",
        )

    if f"version-{bare_version}-orange.svg" not in body:
        fail(
            issues,
            f"{source} {readme_name} version badge does not show {bare_version}",
            fix="merge/push the release README badge update to the default branch",
        )
    if f"<summary><b>{version}</b>" not in body:
        fail(
            issues,
            f"{source} {readme_name} release notes do not include {version}",
            fix="merge/push release notes for the current version to the default branch",
        )
    elif not has_substantive_release_text(readme_release_block(body, version)):
        fail(
            issues,
            f"{source} {readme_name} release notes entry for {version} is empty",
            fix="add concrete release bullets before calling the homepage updated",
        )
    if ".ccb/ccb_memory.md" not in body:
        fail(
            issues,
            f"{source} {readme_name} does not mention .ccb/ccb_memory.md",
            fix="keep the shared memory wording in the default-branch README",
        )

    owner, name = repo.split("/", 1)
    expected_clone = f"https://github.com/{owner}/{name}.git"
    install_heading = "如何安装" if readme_name == "README/zh.md" else "How to Install"
    install_body = install_section(body, install_heading)
    clone_urls = sorted(set(re.findall(r"git\s+clone\s+(https://github\.com/[^\s`]+\.git)", install_body)))
    wrong_urls = [url for url in clone_urls if url != expected_clone]
    if wrong_urls:
        fail(
            issues,
            f"{source} {readme_name} has clone URL(s) not matching {expected_clone}: {', '.join(wrong_urls)}",
            fix=f"replace default-branch README install clone URLs with {expected_clone}",
        )

    if "CCB.md" in body:
        fail(
            issues,
            f"{source} {readme_name} mentions current CCB.md support",
            fix="default-branch README should describe only .ccb/ccb_memory.md as current shared memory",
        )


def gh_api_text(root: Path, path: str) -> str | None:
    proc = run(["gh", "api", path, "--jq", ".content"], root)
    if proc.returncode != 0:
        return None
    try:
        return base64.b64decode(proc.stdout.encode("utf-8"), validate=False).decode("utf-8", errors="replace")
    except Exception:
        return None


def check_remote_homepage(
    *,
    root: Path,
    version: str,
    repo: str,
    default_branch: str,
    issues: list[str],
    warnings: list[str],
) -> None:
    if not default_branch:
        warn(warnings, "Could not determine GitHub default branch; homepage README was not checked")
        return
    for readme_name in ("README.md", "README/zh.md"):
        body = gh_api_text(root, f"repos/{repo}/contents/{readme_name}?ref={default_branch}")
        if body is None:
            fail(
                issues,
                f"Could not read {readme_name} from GitHub default branch {default_branch}",
                fix="confirm gh auth/repo access and that the default branch contains the README",
            )
            continue
        check_readme_surface(
            body=body,
            readme_name=readme_name,
            version=version,
            repo=repo,
            source=f"GitHub default branch {default_branch}",
            issues=issues,
            warnings=warnings,
        )


def check_default_branch_contains_release(
    *,
    root: Path,
    version: str,
    repo: str,
    default_branch: str,
    issues: list[str],
    warnings: list[str],
) -> None:
    if not default_branch:
        warn(warnings, "Could not determine GitHub default branch; default-branch containment was not checked")
        return
    compare = run(
        [
            "gh",
            "api",
            f"repos/{repo}/compare/{version}...{default_branch}",
            "--jq",
            ".status",
        ],
        root,
    )
    if compare.returncode != 0:
        fail(
            issues,
            f"Could not compare release tag {version} with default branch {default_branch}",
            fix="confirm the tag exists on GitHub, then merge the release commit into the default branch if needed",
        )
        return
    status = compare.stdout.strip()
    if status not in {"identical", "ahead"}:
        fail(
            issues,
            f"GitHub default branch {default_branch} does not contain release tag {version} (compare status: {status or 'unknown'})",
            fix=f"merge the release commit/tag into {default_branch} and push; GitHub homepage README only renders from the default branch",
        )


def gh_auth_is_ready(root: Path, issues: list[str]) -> bool:
    auth = run(["gh", "auth", "status", "--hostname", "github.com"], root)
    if auth.returncode != 0:
        fail(
            issues,
            "GitHub CLI is not authenticated",
            fix="run gh auth login, then rerun the GitHub state check",
        )
        return False
    return True


def repo_default_branch(root: Path, repo: str, warnings: list[str]) -> str:
    repo_view = run(["gh", "repo", "view", repo, "--json", "defaultBranchRef"], root)
    if repo_view.returncode != 0:
        warn(warnings, f"Could not read GitHub default branch: {repo_view.stderr.strip()}")
        return ""
    try:
        payload = json.loads(repo_view.stdout)
    except json.JSONDecodeError as exc:
        warn(warnings, f"Could not parse gh repo JSON: {exc}")
        return ""
    return (payload.get("defaultBranchRef") or {}).get("name") or ""


def check_dev_branch_workflows(
    *,
    root: Path,
    repo: str,
    wait_seconds: int,
    poll_interval: int,
    issues: list[str],
    warnings: list[str],
) -> None:
    _check_dev_branch_workflows(
        root=root,
        repo=repo,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        issues=issues,
        warnings=warnings,
        gh_auth_is_ready_fn=gh_auth_is_ready,
        repo_default_branch_fn=repo_default_branch,
    )


def _published_wait_status(
    *,
    release_payload: dict[str, object] | None,
    run_payload: list[dict[str, object]] | None,
    version: str,
    tag_commit: str,
) -> str:
    return _assets_published_wait_status(
        release_payload=release_payload,
        run_payload=run_payload,
        version=version,
        tag_commit=tag_commit,
        format_workflow_wait_status_fn=_format_workflow_wait_status,
    )


def _read_published_release_state(
    *,
    root: Path,
    version: str,
    repo: str,
    tag_commit: str,
    wait_seconds: int,
    poll_interval: int,
    issues: list[str],
) -> tuple[dict[str, object] | None, list[dict[str, object]] | None]:
    return _assets_read_published_release_state(
        root=root,
        version=version,
        repo=repo,
        tag_commit=tag_commit,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        issues=issues,
        read_github_runs_fn=read_github_runs,
        format_workflow_wait_status_fn=_format_workflow_wait_status,
    )


def check_github(
    root: Path,
    version: str,
    repo: str,
    issues: list[str],
    warnings: list[str],
    *,
    wait_seconds: int = 0,
    poll_interval: int = 30,
) -> None:
    if not gh_auth_is_ready(root, issues):
        return

    tag_commit = git_output(root, ["rev-list", "-n", "1", version]) or ""
    payload, run_payload = _read_published_release_state(
        root=root,
        version=version,
        repo=repo,
        tag_commit=tag_commit,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        issues=issues,
    )
    if payload is None:
        return

    if payload.get("tagName") != version:
        fail(issues, f"GitHub release tag is {payload.get('tagName')!r}, expected {version!r}")
    if payload.get("isDraft"):
        fail(issues, f"GitHub release {version} is still a draft", fix="publish the draft after assets and notes are ready")

    asset_names = {asset.get("name") for asset in payload.get("assets", [])}
    missing = sorted(EXPECTED_ASSETS - asset_names)
    if missing:
        fail(
            issues,
            f"GitHub release missing asset(s): {', '.join(missing)}",
            fix=f"rerun Release Artifacts for {version}, then verify assets again",
        )
    elif "SHA256SUMS" in asset_names:
        check_sha256sums(root, version, repo, issues, warnings)

    default_branch = ""
    repo_view = run(["gh", "repo", "view", repo, "--json", "description,repositoryTopics,latestRelease,url,defaultBranchRef"], root)
    if repo_view.returncode != 0:
        warn(warnings, f"Could not read GitHub repo metadata: {repo_view.stderr.strip()}")
    else:
        try:
            repo_payload = json.loads(repo_view.stdout)
        except json.JSONDecodeError as exc:
            warn(warnings, f"Could not parse gh repo JSON: {exc}")
        else:
            latest = (repo_payload.get("latestRelease") or {}).get("tagName")
            default_branch = (repo_payload.get("defaultBranchRef") or {}).get("name") or ""
            if latest != version:
                fail(issues, f"GitHub latest release is {latest!r}, expected {version!r}", fix="publish the GitHub release and ensure it is not draft/prerelease unless intended")
            description = repo_payload.get("description") or ""
            if "Claude, Codex & Gemini" in description and "OpenCode" not in description:
                warn(warnings, "GitHub description may be stale: it mentions Claude/Codex/Gemini but not newer supported providers")

    check_default_branch_contains_release(
        root=root,
        version=version,
        repo=repo,
        default_branch=default_branch,
        issues=issues,
        warnings=warnings,
    )

    check_remote_homepage(
        root=root,
        version=version,
        repo=repo,
        default_branch=default_branch,
        issues=issues,
        warnings=warnings,
    )

    if run_payload is None:
        return

    for workflow_name in sorted(REQUIRED_TAG_WORKFLOWS):
        candidates = _release_workflow_candidates(
            run_payload,
            workflow_name=workflow_name,
            version=version,
            tag_commit=tag_commit,
        )
        successes = [
            item
            for item in candidates
            if item.get("status") == "completed" and item.get("conclusion") == "success"
        ]
        if successes:
            accepted = successes[0]
            if accepted.get("event") == "workflow_dispatch" and tag_commit and accepted.get("headSha") != tag_commit:
                warn(
                    warnings,
                    f"{workflow_name} was accepted from workflow_dispatch but its headSha {accepted.get('headSha')} does not match tag {tag_commit}; confirm it used input tag={version}",
                )
            continue
        if candidates:
            latest = candidates[0]
            fail(
                issues,
                f"GitHub Actions {workflow_name} for {version} is {latest.get('status')}/{latest.get('conclusion')}: {latest.get('url')}",
                fix="open the run, fix the root cause, rerun the failed workflow, and do not call the release complete while red",
            )
            continue
        fail(
            issues,
            f"Missing release workflow run for {version}: {workflow_name}",
            fix=f"push tag {version} or manually dispatch {workflow_name} with input tag={version}",
        )

    _check_branch_validation_runs(run_payload, tag_commit=tag_commit, warnings=warnings)


def _check_branch_validation_runs(run_payload: list[dict[str, object]], *, tag_commit: str, warnings: list[str]) -> None:
    _workflows_check_branch_validation_runs(run_payload, tag_commit=tag_commit, warnings=warnings)
