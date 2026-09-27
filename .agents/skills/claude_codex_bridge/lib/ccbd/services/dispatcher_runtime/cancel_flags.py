"""Filesystem cancel flags retained as a cooperative compatibility signal.

Active execution is cancelled out of band by the provider execution service.
Managed project memory documents the optional flag protocol once, so normal
job prompts do not repeat cancellation instructions or require per-step polls.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def cancel_flag_path(layout, agent_name: str, job_id: str) -> Path:
    return layout.agent_dir(agent_name) / "cancel_flags" / f"{job_id}.cancel"


def write_cancel_flag(layout, agent_name: str, job_id: str) -> Path | None:
    """Atomically drop the flag; best-effort, never raises."""
    try:
        target = cancel_flag_path(layout, agent_name, job_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".cancel.tmp")
        tmp.write_text(str(time.time()), encoding="utf-8")
        os.replace(tmp, target)
        return target
    except OSError:
        return None


def cleanup_cancel_flags(layout, agent_name: str, *, max_age_seconds: float = 86_400.0) -> None:
    """Drop stale flags so the directory does not grow; never raises."""
    try:
        flag_dir = layout.agent_dir(agent_name) / "cancel_flags"
        cutoff = time.time() - max_age_seconds
        for entry in flag_dir.glob("*.cancel"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue
    except OSError:
        pass


def clear_cancel_flag(layout, agent_name: str, job_id: str) -> None:
    """Remove a losing-race flag after another terminal result wins."""
    try:
        cancel_flag_path(layout, agent_name, job_id).unlink(missing_ok=True)
    except OSError:
        pass


__all__ = ["cancel_flag_path", "cleanup_cancel_flags", "clear_cancel_flag", "write_cancel_flag"]
