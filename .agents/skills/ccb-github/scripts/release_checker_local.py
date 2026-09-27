from __future__ import annotations

import hashlib
import re
from pathlib import Path

from release_checker_markdown import (
    has_substantive_release_text,
    install_section,
    markdown_section,
    readme_release_block,
    release_note_versions,
    semver_tuple,
)
from release_checker_shared import (
    DEV_HOMEPAGE_PATHS,
    DEV_RELEASE_TRIGGER_PATHS,
    DEV_STRICT_PHASES,
    fail,
    git_output,
    read,
    run,
    warn,
)


TRACKED_SKILL_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "scripts/check_release_state.py",
    "scripts/release_checker_shared.py",
    "scripts/release_checker_markdown.py",
    "scripts/release_checker_local.py",
    "scripts/release_checker_github.py",
    "scripts/release_checker_workflows.py",
    "scripts/release_checker_assets.py",
)


def check_local_git_state(root: Path, phase: str, issues: list[str], warnings: list[str]) -> None:
    status = git_output(root, ["status", "--porcelain"])
    if status:
        message = "Worktree has uncommitted changes"
        fix = "commit or intentionally discard local changes before reporting a final dev or release result"
        if phase in DEV_STRICT_PHASES:
            fail(issues, message, fix=fix)
        else:
            warn(warnings, f"{message}; {fix}")

    branch = git_output(root, ["branch", "--show-current"])
    if not branch:
        warn(warnings, "Detached HEAD; branch push/merge state cannot be checked")
        return

    upstream = git_output(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not upstream:
        message = f"Current branch {branch} has no upstream"
        fix = f"push it with upstream tracking: git push -u origin {branch}"
        if phase in DEV_STRICT_PHASES:
            fail(issues, message, fix=fix)
        else:
            warn(warnings, f"{message}; {fix}")
        return

    local = git_output(root, ["rev-parse", "HEAD"])
    remote = git_output(root, ["rev-parse", "@{u}"])
    merge_base = git_output(root, ["merge-base", "HEAD", "@{u}"])
    if not local or not remote or not merge_base or local == remote:
        return

    if merge_base == remote:
        message = f"Current branch {branch} has unpushed commits relative to {upstream}"
        fix = f"push the branch before continuing: git push"
        if phase in DEV_STRICT_PHASES:
            fail(issues, message, fix=fix)
        else:
            warn(warnings, f"{message}; {fix}")
    elif merge_base == local:
        warn(warnings, f"Current branch {branch} is behind {upstream}; pull/rebase before release work if this is unexpected")
    else:
        message = f"Current branch {branch} has diverged from {upstream}"
        fix = "reconcile the branch with its upstream before publishing"
        if phase in DEV_STRICT_PHASES:
            fail(issues, message, fix=fix)
        else:
            warn(warnings, f"{message}; {fix}")


def dev_changed_paths(root: Path) -> list[str]:
    paths: set[str] = set()
    for cmd in (
        ["diff", "--name-only"],
        ["diff", "--cached", "--name-only"],
    ):
        output = git_output(root, cmd)
        if output:
            paths.update(item.strip() for item in output.splitlines() if item.strip())

    upstream = git_output(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if upstream:
        output = git_output(root, ["diff", "--name-only", f"{upstream}...HEAD"])
        if output:
            paths.update(item.strip() for item in output.splitlines() if item.strip())
    return sorted(paths)


def classify_dev_path(path: str) -> str:
    if path.startswith("dev_tools/"):
        return "dev_tools"
    if path.startswith("test/") or path.startswith(".github/workflows/"):
        return "verification"
    if path.startswith("docs/"):
        return "docs"
    if path in DEV_HOMEPAGE_PATHS:
        return "homepage"
    if path == "CHANGELOG.md":
        return "release_notes"
    if path in DEV_RELEASE_TRIGGER_PATHS or path.startswith("lib/") or path.startswith("bin/") or path.startswith("scripts/"):
        return "runtime_package"
    return "other"


def check_dev_change_set(root: Path, warnings: list[str]) -> None:
    paths = dev_changed_paths(root)
    if not paths:
        warn(warnings, "No local/branch delta found relative to upstream; dev check is validating git/GitHub state only")
        return
    categories: dict[str, int] = {}
    for path in paths:
        categories[classify_dev_path(path)] = categories.get(classify_dev_path(path), 0) + 1
    summary = ", ".join(f"{name}={count}" for name, count in sorted(categories.items()))
    warn(warnings, f"Development change classification: {summary}")
    if "runtime_package" in categories:
        warn(warnings, "Runtime/package files changed; decide whether this should become a versioned release before calling the work complete")
    if "homepage" in categories:
        warn(warnings, "Homepage README files changed; push/merge to the default branch before expecting GitHub's homepage to update")
    if "release_notes" in categories:
        warn(warnings, "CHANGELOG changed; if this is a public package change, use prepare/published release phases")
    if set(categories).issubset({"dev_tools", "verification", "docs"}):
        warn(warnings, "Change set appears development-only; a release tag is usually not needed")


def check_git_tag(root: Path, version: str, phase: str, issues: list[str], warnings: list[str]) -> None:
    local_commit = git_output(root, ["rev-list", "-n", "1", version])
    if phase == "prepare":
        if local_commit:
            warn(warnings, f"Local tag {version} already exists at {local_commit}; confirm this is intentional before publishing")
        return

    if not local_commit:
        fail(
            issues,
            f"Local git tag {version} does not exist",
            fix=f"create the tag on the intended release commit: git tag {version} && git push origin {version}",
        )
        return

    remote = run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{version}^{{}}"], root)
    remote_sha = remote.stdout.split()[0] if remote.returncode == 0 and remote.stdout.strip() else ""
    if not remote_sha:
        remote = run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{version}"], root)
        remote_sha = remote.stdout.split()[0] if remote.returncode == 0 and remote.stdout.strip() else ""

    if not remote_sha:
        fail(
            issues,
            f"Remote git tag {version} is missing on origin",
            fix=f"push the tag: git push origin {version}",
        )
        return

    if remote_sha != local_commit:
        fail(
            issues,
            f"Remote tag {version} points to {remote_sha}, but local tag resolves to {local_commit}",
            fix="stop and inspect the tag mismatch; do not force-push release tags without maintainer approval",
        )


def check_local_files(root: Path, version: str, repo: str, issues: list[str], warnings: list[str]) -> None:
    bare_version = version.removeprefix("v")
    files = {
        "VERSION": read(root / "VERSION"),
        "ccb": read(root / "ccb"),
        "ccb.py": read(root / "ccb.py"),
        "CHANGELOG.md": read(root / "CHANGELOG.md"),
        "README.md": read(root / "README.md"),
        "README/zh.md": read(root / "README" / "zh.md"),
    }

    if files["VERSION"].strip() != bare_version:
        fail(issues, f"VERSION is {files['VERSION'].strip()!r}, expected {bare_version!r}", fix=f"write exactly {bare_version} to VERSION")
    if f'VERSION = "{bare_version}"' not in files["ccb"] and f'VERSION = "{bare_version}"' not in files["ccb.py"]:
        fail(issues, f"ccb.py does not contain VERSION = {bare_version!r}", fix=f'update ccb.py to VERSION = "{bare_version}"')

    changelog_section = markdown_section(files["CHANGELOG.md"], version)
    if changelog_section is None:
        fail(issues, f"CHANGELOG.md has no {version} section", fix=f"add a non-empty ## {version} (...) section near the top of CHANGELOG.md")
    elif not has_substantive_release_text(changelog_section):
        fail(issues, f"CHANGELOG.md {version} section is empty", fix="add concrete user-facing release bullets before publishing")

    for readme_name in ("README.md", "README/zh.md"):
        body = files[readme_name]
        versions = release_note_versions(body)
        if versions:
            if versions[0] != version:
                fail(
                    issues,
                    f"{readme_name} first release notes entry is {versions[0]}, expected {version}",
                    fix=f"move the {version} release notes entry above older versions",
                )
            sorted_versions = sorted(versions, key=semver_tuple, reverse=True)
            if versions != sorted_versions:
                warn(warnings, f"{readme_name} release notes are not in descending semver order")
        if f"version-{bare_version}-orange.svg" not in body:
            fail(issues, f"{readme_name} version badge does not show {bare_version}", fix=f"update the top badge to version-{bare_version}-orange.svg")
        if f"<summary><b>{version}</b>" not in body:
            fail(issues, f"{readme_name} release notes do not include {version}", fix=f"add a non-empty {version} entry to Release Notes / 新版本记录")
        elif not has_substantive_release_text(readme_release_block(body, version)):
            fail(issues, f"{readme_name} release notes entry for {version} is empty", fix="add concrete release bullets under the details block")
        if ".ccb/ccb_memory.md" not in body:
            fail(issues, f"{readme_name} does not mention .ccb/ccb_memory.md", fix="state that .ccb/ccb_memory.md is the project-wide shared memory document")

        badge_versions = sorted(set(re.findall(r"version-([0-9]+\.[0-9]+\.[0-9]+)-orange\.svg", body)))
        stale_badges = [item for item in badge_versions if item != bare_version]
        if stale_badges:
            fail(issues, f"{readme_name} has stale version badges: {', '.join(stale_badges)}", fix=f"replace stale current badges with {bare_version}")

    owner, name = repo.split("/", 1)
    expected_clone = f"https://github.com/{owner}/{name}.git"
    readme_install_headings = {
        "README.md": "How to Install",
        "README/zh.md": "如何安装",
    }
    for readme_name, heading in readme_install_headings.items():
        body = files[readme_name]
        install_body = install_section(body, heading)
        clone_urls = sorted(set(re.findall(r"git\s+clone\s+(https://github\.com/[^\s`]+\.git)", install_body)))
        wrong_urls = [url for url in clone_urls if url != expected_clone]
        if wrong_urls:
            fail(issues, f"{readme_name} has clone URL(s) not matching {expected_clone}: {', '.join(wrong_urls)}", fix=f"replace README clone URLs with {expected_clone}")

    if "CCB.md" in files["README.md"] or "CCB.md" in files["README/zh.md"]:
        fail(issues, "README mentions current CCB.md support; current design must only use .ccb/ccb_memory.md", fix="remove current-feature references to CCB.md; keep only .ccb/ccb_memory.md")

    warn(warnings, "Manually inspect README What's New / 最新亮点 for stale prose; this cannot be proven by version regex alone")


def _file_sha256(path: Path) -> str | None:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(payload).hexdigest()


def check_active_skill_sync(root: Path, warnings: list[str]) -> None:
    source_dir = root / "dev_tools" / "skills" / "ccb-github"
    if not source_dir.is_dir():
        return
    ccb_dir = root / ".ccb"
    if not ccb_dir.is_dir():
        return
    active_dirs = sorted(ccb_dir.glob("agents/*/provider-state/codex/home/skills/ccb-github"))
    for active_dir in active_dirs:
        if active_dir.resolve() == source_dir.resolve():
            continue
        mismatched = [
            relative
            for relative in TRACKED_SKILL_FILES
            if _file_sha256(source_dir / relative) != _file_sha256(active_dir / relative)
        ]
        if mismatched:
            warn(
                warnings,
                f"Active ccb-github skill copy differs from dev_tools at {active_dir}: {', '.join(mismatched)}",
            )
