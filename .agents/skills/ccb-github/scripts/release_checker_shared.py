from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


EXPECTED_ASSETS = {
    "ccb-linux-x86_64.tar.gz",
    "ccb-macos-universal.tar.gz",
    "SHA256SUMS",
}
CHECKSUMMED_ASSETS = EXPECTED_ASSETS - {"SHA256SUMS"}
REQUIRED_TAG_WORKFLOWS = {"Release Artifacts"}
RELEASE_RUN_LIMIT = 50
BRANCH_VALIDATION_WORKFLOWS = {
    "Tests",
    "CCBD Real Platform Smoke",
    "Cross-Platform Compatibility Test",
}
DEV_STRICT_PHASES = {"dev", "published"}
DEV_ALWAYS_REQUIRED_WORKFLOWS = {
    "Tests",
    "CCBD Real Platform Smoke",
}
DEV_DEFAULT_BRANCH_WORKFLOWS = {
    "Cross-Platform Compatibility Test",
}
DEV_RELEASE_TRIGGER_PATHS = {
    "VERSION",
    "ccb",
}
DEV_HOMEPAGE_PATHS = {
    "README.md",
    "README/zh.md",
}


def run(cmd: list[str], cwd: Path, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        detail = stderr or f"command timed out after {timeout}s"
        return subprocess.CompletedProcess(cmd, 124, stdout=stdout, stderr=detail)


def repo_root(start: Path) -> Path:
    proc = run(["git", "rev-parse", "--show-toplevel"], start)
    if proc.returncode == 0:
        return Path(proc.stdout.strip())
    return start.resolve()


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def fail(issues: list[str], message: str, *, fix: str | None = None) -> None:
    if fix:
        issues.append(f"FAIL: {message}\n      fix: {fix}")
    else:
        issues.append(f"FAIL: {message}")


def warn(warnings: list[str], message: str) -> None:
    warnings.append(f"WARN: {message}")


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def infer_repo(root: Path) -> str:
    proc = run(["git", "remote", "get-url", "origin"], root)
    if proc.returncode != 0:
        return "SeemSeam/claude_codex_bridge"
    url = proc.stdout.strip()
    match = re.search(r"github.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", url)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return "SeemSeam/claude_codex_bridge"


def git_output(root: Path, cmd: list[str]) -> str | None:
    proc = run(["git", *cmd], root)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()
