#!/usr/bin/env python3
"""Read-only release surface checker for the CCB GitHub project."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import release_checker_github as github  # noqa: E402,F401
import release_checker_local as local  # noqa: E402,F401
import release_checker_assets as assets  # noqa: E402,F401
import release_checker_workflows as workflows  # noqa: E402,F401
from release_checker_assets import (  # noqa: E402,F401
    _latest_release_workflows,
    _published_state_is_pending,
    _read_release_payload,
    _release_artifacts_run_matches,
    _release_workflow_candidates,
    check_sha256sums,
)
from release_checker_github import (  # noqa: E402,F401
    _published_wait_status,
    _read_published_release_state,
    check_default_branch_contains_release,
    check_dev_branch_workflows,
    check_github,
    check_readme_surface,
    check_remote_homepage,
    gh_api_text,
    gh_auth_is_ready,
    repo_default_branch,
)
from release_checker_local import (  # noqa: E402,F401
    TRACKED_SKILL_FILES,
    _file_sha256,
    check_active_skill_sync,
    check_dev_change_set,
    check_git_tag,
    check_local_files,
    check_local_git_state,
    classify_dev_path,
    dev_changed_paths,
)
from release_checker_markdown import (  # noqa: E402,F401
    has_substantive_release_text,
    install_section,
    markdown_section,
    readme_release_block,
    release_note_versions,
    semver_tuple,
)
from release_checker_shared import (  # noqa: E402,F401
    BRANCH_VALIDATION_WORKFLOWS,
    CHECKSUMMED_ASSETS,
    DEV_ALWAYS_REQUIRED_WORKFLOWS,
    DEV_DEFAULT_BRANCH_WORKFLOWS,
    DEV_HOMEPAGE_PATHS,
    DEV_RELEASE_TRIGGER_PATHS,
    DEV_STRICT_PHASES,
    EXPECTED_ASSETS,
    RELEASE_RUN_LIMIT,
    REQUIRED_TAG_WORKFLOWS,
    _stderr,
    fail,
    git_output,
    infer_repo,
    read,
    repo_root,
    run,
    warn,
)
from release_checker_workflows import (  # noqa: E402,F401
    _check_branch_validation_runs,
    _format_workflow_wait_status,
    read_github_runs,
    required_dev_workflows,
)


def check_dev_state(
    *,
    root: Path,
    repo: str,
    wait_seconds: int,
    poll_interval: int,
    issues: list[str],
    warnings: list[str],
) -> None:
    check_dev_change_set(root, warnings)
    check_dev_branch_workflows(
        root=root,
        repo=repo,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        issues=issues,
        warnings=warnings,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check CCB release-facing local and GitHub state.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--repo", default=None, help="GitHub repo, e.g. SeemSeam/claude_codex_bridge")
    parser.add_argument("--version", default=None, help="Release version, with or without leading v")
    parser.add_argument("--phase", choices=("dev", "prepare", "published"), default="prepare")
    parser.add_argument("--wait-seconds", type=int, default=0, help="wait this many seconds for GitHub workflows")
    parser.add_argument("--poll-interval", type=int, default=30, help="poll interval in seconds when waiting for workflows")
    args = parser.parse_args()

    root = repo_root(args.repo_root)
    repo = args.repo or infer_repo(root)
    raw_version = args.version or read(root / "VERSION").strip()
    version = raw_version if raw_version.startswith("v") else f"v{raw_version}"

    issues: list[str] = []
    warnings: list[str] = []

    check_active_skill_sync(root, warnings)
    check_local_git_state(root, args.phase, issues, warnings)
    if args.phase == "dev":
        check_dev_state(
            root=root,
            repo=repo,
            wait_seconds=args.wait_seconds,
            poll_interval=args.poll_interval,
            issues=issues,
            warnings=warnings,
        )
    else:
        check_local_files(root, version, repo, issues, warnings)
        check_git_tag(root, version, args.phase, issues, warnings)

    if args.phase == "published":
        check_github(
            root,
            version,
            repo,
            issues,
            warnings,
            wait_seconds=args.wait_seconds,
            poll_interval=args.poll_interval,
        )

    print(f"CCB release check: {version} ({args.phase})")
    print(f"repo root: {root}")
    print(f"github repo: {repo}")

    if warnings:
        print("\nWarnings:")
        for item in warnings:
            print(f"- {item}")

    if issues:
        print("\nIssues:")
        for item in issues:
            print(f"- {item}")
        return 1

    print("\nOK: no blocking release-surface drift found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
