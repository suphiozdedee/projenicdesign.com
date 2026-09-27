from __future__ import annotations

import os
from pathlib import Path


def default_state_dir() -> Path:
    if os.environ.get("CCB_SESSION_FILE"):
        runtime_dir = (
            os.environ.get("CODEX_RUNTIME_DIR")
            or os.environ.get("CCB_CALLER_RUNTIME_DIR")
            or ""
        ).strip()
        if runtime_dir:
            candidate = Path(runtime_dir).expanduser()
            if candidate.is_absolute():
                return candidate / "reconnect"
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser() / "codex-reconnect"
    return Path.home() / ".local" / "state" / "codex-reconnect"
