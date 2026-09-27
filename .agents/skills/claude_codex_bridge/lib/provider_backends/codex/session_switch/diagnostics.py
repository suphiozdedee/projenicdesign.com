from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from provider_sessions.files import safe_write_session

from .models import STATE_AUTO_REBOUND, SwitchCandidate, SwitchDecision


FILENAME = "session-switch.json"


def diagnostics_path(runtime_dir: Path | None) -> Path | None:
    if runtime_dir is None:
        return None
    try:
        return Path(runtime_dir).expanduser() / FILENAME
    except Exception:
        return None


def write_decision(runtime_dir: Path | None, decision: SwitchDecision, *, committed: bool = False) -> None:
    path = diagnostics_path(runtime_dir)
    if path is None:
        return
    record = decision.to_record()
    record["committed"] = bool(committed)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write(path, record)


def write_rebound(
    runtime_dir: Path | None,
    *,
    candidate: SwitchCandidate,
    old_session_id: str,
    old_session_path: str,
    reason: str,
) -> None:
    path = diagnostics_path(runtime_dir)
    if path is None:
        return
    _write(
        path,
        {
            "state": STATE_AUTO_REBOUND,
            "reason": reason,
            "committed": True,
            "old_session_id": old_session_id,
            "old_session_path": old_session_path,
            "candidate": candidate.to_record(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def read_diagnostics(runtime_dir: Path | None) -> dict[str, object]:
    path = diagnostics_path(runtime_dir)
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, record: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_write_session(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        return


__all__ = ["diagnostics_path", "read_diagnostics", "write_decision", "write_rebound"]
