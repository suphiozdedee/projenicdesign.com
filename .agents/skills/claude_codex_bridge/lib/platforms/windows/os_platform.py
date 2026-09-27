"""Unified OS platform detection for CCB install and startup.

Detects four platform categories:
- ``linux``     — native Linux (not WSL)
- ``macos``     — macOS
- ``wsl``       — Windows Subsystem for Linux
- ``native_windows`` — native Windows (not WSL)
- ``unknown``   — cannot determine

Also provides Herdr availability and version checking for NativeWindows.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal


class OsPlatform(Enum):
    LINUX = "linux"
    MACOS = "macos"
    WSL = "wsl"
    NATIVE_WINDOWS = "native_windows"
    UNKNOWN = "unknown"


# ── WSL detection ──────────────────────────────────────────────────────────

def _is_wsl_from_proc_version() -> bool:
    """Check /proc/version for Microsoft/WSL markers."""
    try:
        content = Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
    except (OSError, FileNotFoundError):
        return False
    return "microsoft" in content or "wsl" in content


def _is_wsl_from_env() -> bool:
    """Check WSL-specific environment variables."""
    return bool(os.environ.get("WSL_DISTRO_NAME", "").strip())


def _is_wsl_interop_from_env() -> bool:
    """Check for WSL interop socket (WSL2)."""
    return bool(os.environ.get("WSL_INTEROP", "").strip())


def is_wsl() -> bool:
    """Detect whether running inside WSL (Windows Subsystem for Linux).

    Uses three signals in priority order:
    1. WSL_INTEROP env (WSL2 interop socket)
    2. WSL_DISTRO_NAME env (WSL distro name)
    3. /proc/version content ("microsoft" or "wsl" marker)
    """
    if _is_wsl_interop_from_env():
        return True
    if _is_wsl_from_env():
        return True
    if _is_wsl_from_proc_version():
        return True
    return False


# ── Unified platform detection ─────────────────────────────────────────────

def detect_os_platform() -> OsPlatform:
    """Detect the current OS platform.

    Returns one of: LINUX, MACOS, WSL, NATIVE_WINDOWS, UNKNOWN.

    Detection strategy:
    - macOS:   ``sys.platform == "darwin"``
    - Windows: ``sys.platform == "win32"``
      - If WSL environment variables are set → WSL (rare: running a Windows
        binary from WSL that reports win32 but has WSL env)
      - Otherwise → NATIVE_WINDOWS
    - Linux:   ``sys.platform == "linux"``
      - Check WSL markers → WSL
      - Otherwise → LINUX
    """
    if sys.platform == "darwin":
        return OsPlatform.MACOS

    if sys.platform == "win32":
        # On Windows, also check for WSL env (e.g., when called from WSL interop)
        if _is_wsl_interop_from_env() or _is_wsl_from_env():
            return OsPlatform.WSL
        return OsPlatform.NATIVE_WINDOWS

    if sys.platform == "linux":
        if is_wsl():
            return OsPlatform.WSL
        return OsPlatform.LINUX

    return OsPlatform.UNKNOWN


def detect_os_platform_string() -> str:
    """Return the OS platform as a lowercase string."""
    return detect_os_platform().value


# ── Convenience functions ──────────────────────────────────────────────────

def is_native_windows() -> bool:
    """True if running on native Windows (not WSL)."""
    return detect_os_platform() == OsPlatform.NATIVE_WINDOWS


def is_linux() -> bool:
    """True if running on native Linux (not WSL)."""
    return detect_os_platform() == OsPlatform.LINUX


def is_macos() -> bool:
    """True if running on macOS."""
    return detect_os_platform() == OsPlatform.MACOS


# ── Herdr version and availability ─────────────────────────────────────────

# Minimum Herdr version requirements for NativeWindows
HERDR_MIN_STABLE_VERSION = (0, 8, 0)
HERDR_MIN_PREVIEW_DATE = "2026-08-04"
HERDR_MIN_PREVIEW_COMMIT_PREFIX = "d78e3d3b"

# Known Herdr install locations on Windows (in priority order)
_HERDR_KNOWN_PATHS = [
    # PATH lookup first (handled by resolve)
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Herdr", "herdr.exe"),
    os.path.join(os.environ.get("ProgramFiles", ""), "Herdr", "herdr.exe"),
    os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Herdr", "herdr.exe"),
    os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "Programs", "Herdr", "herdr.exe"),
]


@dataclass
class HerdrVersionInfo:
    """Parsed Herdr version information."""

    raw: str
    """Raw version string from ``herdr --version``."""

    is_stable: bool
    """True if this is a stable release (e.g. ``v0.8.0``)."""

    is_preview: bool
    """True if this is a preview/nightly build."""

    major: int = 0
    minor: int = 0
    patch: int = 0

    preview_date: str = ""
    """Preview build date in YYYY-MM-DD format."""

    preview_commit: str = ""
    """Preview build commit hash prefix."""

    def meets_minimum(self) -> bool:
        """Check if this version meets the minimum requirements."""
        if self.is_stable:
            return (self.major, self.minor, self.patch) >= HERDR_MIN_STABLE_VERSION
        if self.is_preview:
            # Preview builds: compare by date
            if self.preview_date and self.preview_date >= HERDR_MIN_PREVIEW_DATE:
                return True
            # Also accept by commit prefix
            if self.preview_commit and self.preview_commit.startswith(HERDR_MIN_PREVIEW_COMMIT_PREFIX):
                return True
            return False
        return False

    def meets_minimum_summary(self) -> str:
        """Human-readable minimum version requirement summary."""
        return (
            f"Herder >= v{HERDR_MIN_STABLE_VERSION[0]}.{HERDR_MIN_STABLE_VERSION[1]}.{HERDR_MIN_STABLE_VERSION[2]} "
            f"(stable) or preview build >= {HERDR_MIN_PREVIEW_DATE} "
            f"(commit {HERDR_MIN_PREVIEW_COMMIT_PREFIX}...)"
        )


def resolve_herdr_exe(explicit: str | None = None) -> str | None:
    """Resolve the herdr executable path.

    Priority:
    1. Explicit path argument
    2. ``CCB_HERDR_EXE`` environment variable
    3. ``herdr`` on PATH (via ``shutil.which``)
    4. Known Windows install locations
    """
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env_exe = os.environ.get("CCB_HERDR_EXE", "").strip()
    if env_exe:
        candidates.append(env_exe)
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    path_exe = shutil.which("herdr")
    if path_exe:
        return path_exe

    for known_path in _HERDR_KNOWN_PATHS:
        if known_path and os.path.isfile(known_path):
            return known_path

    return None


def _parse_herdr_version(version_output: str) -> HerdrVersionInfo:
    """Parse the output of ``herdr --version`` into a ``HerdrVersionInfo``.

    Expected formats:
    - Stable:     ``herdr v0.8.0`` or ``herdr 0.8.0``
    - Preview:    ``herdr 0.8.0-preview.2026-08-04-d78e3d3b5126``
    - Preview v2: ``herdr preview 2026-08-04-d78e3d3b5126``
    """
    text = version_output.strip()

    info = HerdrVersionInfo(raw=text, is_stable=False, is_preview=False)

    # ── Preview patterns (check BEFORE stable — preview strings also contain
    #     version numbers that would false-match the stable regex) ────────

    # Pattern A: "herdr 0.8.0-preview.YYYY-MM-DD-<commit>"
    preview_dotted = re.compile(
        r"v?(\d+)\.(\d+)\.(\d+)-preview\.(\d{4}-\d{2}-\d{2})-([a-f0-9]{7,})",
        re.IGNORECASE,
    )
    preview_match = preview_dotted.search(text)
    if preview_match:
        info.is_preview = True
        try:
            info.major = int(preview_match.group(1))
            info.minor = int(preview_match.group(2))
            info.patch = int(preview_match.group(3))
        except (ValueError, IndexError):
            pass
        info.preview_date = preview_match.group(4)
        info.preview_commit = preview_match.group(5)
        return info

    # Pattern B: "herdr preview YYYY-MM-DD-<commit>"
    preview_space = re.compile(
        r"(?:herdr\s+)?preview\s+(\d{4}-\d{2}-\d{2})-([a-f0-9]{7,})",
        re.IGNORECASE,
    )
    preview_match = preview_space.search(text)
    if preview_match:
        info.is_preview = True
        info.preview_date = preview_match.group(1)
        info.preview_commit = preview_match.group(2)
        return info

    # Pattern C: "herdr nightly YYYY-MM-DD-<commit>" (same as preview)
    nightly = re.compile(
        r"(?:herdr\s+)?nightly\s+(\d{4}-\d{2}-\d{2})-([a-f0-9]{7,})",
        re.IGNORECASE,
    )
    nightly_match = nightly.search(text)
    if nightly_match:
        info.is_preview = True
        info.preview_date = nightly_match.group(1)
        info.preview_commit = nightly_match.group(2)
        return info

    # ── Stable version pattern: "v0.8.0" or "0.8.0" (no preview markers) ─
    stable_pattern = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?![\-\.]?preview)")
    stable_match = stable_pattern.search(text)
    if stable_match:
        info.is_stable = True
        try:
            info.major = int(stable_match.group(1))
            info.minor = int(stable_match.group(2))
            info.patch = int(stable_match.group(3))
        except (ValueError, IndexError):
            pass
        return info

    # Fallback: bare version without negative lookahead
    bare_pattern = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
    bare_match = bare_pattern.search(text)
    if bare_match:
        info.is_stable = True
        try:
            info.major = int(bare_match.group(1))
            info.minor = int(bare_match.group(2))
            info.patch = int(bare_match.group(3))
        except (ValueError, IndexError):
            pass

    return info


def get_herdr_version(exe: str | None = None) -> HerdrVersionInfo | None:
    """Get the version of the installed Herdr.

    Args:
        exe: Path to herdr executable. If None, auto-resolves.

    Returns:
        ``HerdrVersionInfo`` on success, ``None`` if Herdr cannot be found or
        the version cannot be determined.
    """
    resolved = resolve_herdr_exe(explicit=exe)
    if not resolved:
        return None
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        result = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or "").strip()
    if not output and result.stderr:
        output = result.stderr.strip()
    if not output:
        return None
    return _parse_herdr_version(output)


def check_herdr_ready(exe: str | None = None) -> tuple[bool, str, HerdrVersionInfo | None]:
    """Check if Herdr is installed and meets the minimum version requirement.

    Returns:
        ``(ready, message, version_info)``:
        - ``ready``: True if Herdr is found and meets the minimum version.
        - ``message``: Human-readable status message.
        - ``version_info``: Parsed version info, or None.
    """
    resolved = resolve_herdr_exe(explicit=exe)
    if not resolved:
        return (
            False,
            "Herdr 未找到。Native Windows 上使用 CCB 需要安装 Herdr。\n"
            "  下载: https://herdr.dev/ 或 https://github.com/herdrdev/herdr\n"
            "  要求: >= v0.8.0 (stable) 或 preview build >= 2026-08-04-d78e3d3b5126\n"
            "  安装后请确保 herdr 在 PATH 中。",
            None,
        )

    version_info = get_herdr_version(resolved)
    if version_info is None:
        return (
            False,
            f"Herdr 已找到 ({resolved})，但无法获取版本信息。\n"
            f"  请手动运行 `herdr --version` 确认版本 >= v0.8.0。",
            None,
        )

    if version_info.meets_minimum():
        version_str = version_info.raw
        return (
            True,
            f"Herdr 已就绪: {version_str} ({resolved})",
            version_info,
        )

    return (
        False,
        f"Herdr 版本过旧: {version_info.raw} ({resolved})\n"
        f"  最低要求: {version_info.meets_minimum_summary()}\n"
        f"  请升级 Herdr: https://herdr.dev/",
        version_info,
    )


# ── User interaction helper ────────────────────────────────────────────────

def interactive_confirm_platform(
    detected: OsPlatform,
    *,
    prompt: str | None = None,
    input_fn=None,
) -> OsPlatform:
    """When the platform cannot be determined automatically, ask the user.

    Args:
        detected: The automatically detected platform (may be UNKNOWN).
        prompt: Override prompt text.
        input_fn: Callable for getting user input (for testing).

    Returns:
        The confirmed or corrected platform.
    """
    if detected != OsPlatform.UNKNOWN:
        return detected

    if input_fn is None:
        input_fn = input

    print()
    print("=" * 64)
    print("无法自动检测当前操作系统平台。")
    print("请选择你的运行环境:")
    print("  1. Linux (原生)")
    print("  2. macOS")
    print("  3. WSL (Windows Subsystem for Linux)")
    print("  4. Windows (原生/Native)")
    print("=" * 64)

    while True:
        try:
            choice = input_fn("请输入数字 (1-4): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n安装已取消。")
            raise SystemExit(1)

        mapping = {
            "1": OsPlatform.LINUX,
            "2": OsPlatform.MACOS,
            "3": OsPlatform.WSL,
            "4": OsPlatform.NATIVE_WINDOWS,
        }
        if choice in mapping:
            return mapping[choice]
        print(f"无效选项: {choice}，请重新输入 (1-4)。")


def platform_needs_herdr(platform: OsPlatform) -> bool:
    """Check if the given platform requires Herdr to run CCB.

    Currently only ``NATIVE_WINDOWS`` requires Herdr.
    """
    return platform == OsPlatform.NATIVE_WINDOWS


def platform_backend_hint(platform: OsPlatform) -> str:
    """Return the recommended backend for the given platform.

    - Linux/macOS: ``tmux`` (default)
    - WSL: ``tmux`` (WSL runs Linux tooling)
    - NativeWindows: ``herdr`` (tmux not natively available)
    """
    if platform in (OsPlatform.LINUX, OsPlatform.MACOS, OsPlatform.WSL):
        return "tmux"
    if platform == OsPlatform.NATIVE_WINDOWS:
        return "herdr"
    return "tmux"


__all__ = [
    "HERDR_MIN_PREVIEW_COMMIT_PREFIX",
    "HERDR_MIN_PREVIEW_DATE",
    "HERDR_MIN_STABLE_VERSION",
    "HerdrVersionInfo",
    "OsPlatform",
    "check_herdr_ready",
    "detect_os_platform",
    "detect_os_platform_string",
    "get_herdr_version",
    "interactive_confirm_platform",
    "is_linux",
    "is_macos",
    "is_native_windows",
    "is_wsl",
    "platform_backend_hint",
    "platform_needs_herdr",
    "resolve_herdr_exe",
]
