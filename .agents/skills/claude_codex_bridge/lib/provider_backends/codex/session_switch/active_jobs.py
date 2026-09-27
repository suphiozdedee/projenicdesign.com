from __future__ import annotations

import json
from pathlib import Path

from agents.models import normalize_agent_name
from provider_core.protocol import request_anchor_for_job
from storage.path_helpers import runtime_state_root_from_anchor


def running_request_anchors(*, session_file: Path, session_data: dict[str, object]) -> tuple[str, ...]:
    jobs_path = _jobs_path(session_file=session_file, session_data=session_data)
    if jobs_path is None or not jobs_path.is_file():
        return ()
    statuses: dict[str, str] = {}
    try:
        lines = jobs_path.read_text(encoding="utf-8-sig").splitlines()
    except Exception:
        return ()
    for line in lines:
        record = _load_record(line)
        if not record:
            continue
        job_id = str(record.get("job_id") or "").strip()
        status = str(record.get("status") or "").strip().lower()
        if job_id and status:
            statuses[job_id] = status
    return tuple(request_anchor_for_job(job_id) for job_id, status in statuses.items() if status == "running")


def _jobs_path(*, session_file: Path, session_data: dict[str, object]) -> Path | None:
    agent_name = _agent_name(session_file=session_file, session_data=session_data)
    if not agent_name:
        return None
    ccb_dir = _ccb_dir(session_file)
    if ccb_dir is None:
        return None
    raw_state_root = str(session_data.get("runtime_state_root") or "").strip()
    if raw_state_root:
        state_root = Path(raw_state_root).expanduser()
    else:
        project_id = str(session_data.get("ccb_project_id") or "").strip() or None
        state_root = runtime_state_root_from_anchor(ccb_dir, project_id=project_id)
    return state_root / "agents" / agent_name / "jobs.jsonl"


def _agent_name(*, session_file: Path, session_data: dict[str, object]) -> str:
    raw = str(session_data.get("agent_name") or "").strip()
    if not raw:
        raw = _agent_name_from_session_filename(session_file.name) or ""
    if not raw:
        return ""
    try:
        return normalize_agent_name(raw)
    except Exception:
        return raw.strip()


def _agent_name_from_session_filename(filename: str) -> str | None:
    if not filename.startswith(".codex-") or not filename.endswith("-session"):
        return None
    value = filename[len(".codex-") : -len("-session")].strip()
    return value or None


def _ccb_dir(session_file: Path) -> Path | None:
    parent = session_file.expanduser().parent
    if parent.name == ".ccb" or (parent / "agents").exists():
        return parent
    for candidate in parent.parents:
        if candidate.name == ".ccb" or (candidate / "agents").exists():
            return candidate
    return None


def _load_record(line: str) -> dict[str, object] | None:
    try:
        value = json.loads(line)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


__all__ = ["running_request_anchors"]
