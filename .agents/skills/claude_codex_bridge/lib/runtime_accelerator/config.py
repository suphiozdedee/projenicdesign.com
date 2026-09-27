from __future__ import annotations

import os
import shutil
from pathlib import Path

from .client import default_socket_path

_ENABLED_VALUES = {"1", "true", "yes", "on", "auto"}
_DISABLED_VALUES = {"0", "false", "no", "off", "disabled"}


def codex_accelerator_enabled() -> bool:
    raw = str(os.environ.get("CCB_RUNTIME_ACCELERATOR_CODEX") or "").strip().lower()
    if raw in _DISABLED_VALUES:
        return False
    return True if not raw else raw in _ENABLED_VALUES


def accelerator_socket_path(project_root: str | Path | None) -> Path | None:
    override = str(os.environ.get("CCB_RUNTIME_ACCELERATOR_SOCKET") or "").strip()
    if override:
        return Path(override).expanduser()
    if project_root is None:
        return None
    raw_root = str(project_root or "").strip()
    if not raw_root:
        return None
    return default_socket_path(raw_root)


def accelerator_timeout_s(default: float = 0.2) -> float:
    return float_env("CCB_RUNTIME_ACCELERATOR_TIMEOUT_S", default)


def accelerator_startup_timeout_s(default: float = 0.5) -> float:
    return float_env("CCB_RUNTIME_ACCELERATOR_STARTUP_TIMEOUT_S", default)


def accelerator_binary() -> str | None:
    raw = str(os.environ.get("CCB_RUNTIME_ACCELERATOR_BIN") or "ccb-runtime-accelerator").strip()
    if not raw:
        return None
    if "/" in raw:
        return raw if Path(raw).expanduser().exists() else None
    found = shutil.which(raw)
    if found:
        return found
    for candidate in repo_binary_candidates(raw):
        if candidate.exists():
            return str(candidate)
    return None


def repo_binary_candidates(name: str) -> tuple[Path, ...]:
    repo_root = Path(__file__).resolve().parents[2]
    return (
        repo_root / "bin" / name,
        repo_root / "rust" / "target" / "release" / name,
        repo_root / "rust" / "target" / "debug" / name,
    )


def float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except Exception:
        return max(0.0, default)


__all__ = [
    "accelerator_binary",
    "accelerator_socket_path",
    "accelerator_startup_timeout_s",
    "accelerator_timeout_s",
    "codex_accelerator_enabled",
    "repo_binary_candidates",
]
