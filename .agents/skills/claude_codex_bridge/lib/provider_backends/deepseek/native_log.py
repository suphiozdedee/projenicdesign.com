from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Iterable

from provider_backends.native_cli_support import clean_native_reply
from provider_core.source_home import current_provider_source_home


TERMINAL_SUCCESS_STATUSES = {"completed"}
TERMINAL_FAILURE_STATUSES = {"failed", "error"}
INTERRUPTED_STATUSES = {"interrupted", "cancelled", "canceled"}
WAITING_USER_STATUSES = {"ask_permission", "waiting_for_user"}
PERMISSION_DENIED_STATUSES = {"permission_denied"}


@dataclass(frozen=True)
class DeepSeekSessionObservation:
    request_seen: bool
    completed: bool
    status: str
    reply: str
    session_id: str | None
    session_path: str | None
    provider_turn_ref: str | None
    line_count: int
    fail_reason: str | None = None
    updated_at: object | None = None


def observe_deepseek_session(
    work_dir: Path,
    req_id: str,
    *,
    home_candidates: Iterable[Path] | None = None,
) -> DeepSeekSessionObservation | None:
    if not req_id:
        return None
    observations: list[DeepSeekSessionObservation] = []
    for project_root in _project_roots(work_dir, home_candidates=home_candidates):
        observed = _observe_project_root(project_root, req_id=req_id)
        if observed is not None:
            observations.append(observed)
    if not observations:
        return None
    completed = [item for item in observations if item.completed]
    if completed:
        return max(completed, key=_observation_sort_key)
    return max(observations, key=_observation_sort_key)


def deepseek_project_code(work_dir: Path) -> str:
    normalized = str(Path(work_dir).expanduser().resolve(strict=False))
    legacy = normalized.replace("\\", "-").replace("/", "-").replace(":", "")
    if len(legacy) <= 64:
        return legacy

    hash_input = normalized.lower() if sys.platform.startswith("win") else normalized
    digest = hashlib.sha256(hash_input.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    basename = _sanitize_project_name(Path(normalized).name) or "project"
    max_prefix = max(1, 64 - len(digest) - 1)
    prefix = basename[:max_prefix].rstrip("-.") or "project"
    return f"{prefix}-{digest}"


def deepseek_project_root(work_dir: Path, *, home: Path | None = None) -> Path:
    return _deepcode_home(home) / "projects" / deepseek_project_code(work_dir)


def _project_roots(work_dir: Path, *, home_candidates: Iterable[Path] | None) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for home in _candidate_homes(home_candidates):
        root = deepseek_project_root(work_dir, home=home)
        try:
            resolved = root.resolve(strict=False)
        except Exception:
            resolved = root
        if resolved in seen:
            continue
        seen.add(resolved)
        if root.is_dir():
            roots.append(root)
    return roots


def _candidate_homes(home_candidates: Iterable[Path] | None) -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("DEEPCODE_HOME") or os.environ.get("DEEPSEEK_HOME")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if home_candidates is not None:
        candidates.extend(Path(item).expanduser() for item in home_candidates)
    candidates.append(current_provider_source_home())
    candidates.append(Path.home().expanduser())

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _deepcode_home(home: Path | None) -> Path:
    if home is None:
        return current_provider_source_home() / ".deepcode"
    if home.name == ".deepcode":
        return home
    return home / ".deepcode"


def _observe_project_root(project_root: Path, *, req_id: str) -> DeepSeekSessionObservation | None:
    index = _read_json(project_root / "sessions-index.json")
    entries = _index_entries(index)
    observations: list[DeepSeekSessionObservation] = []
    for entry in entries:
        session_id = _coerce_str(entry.get("id") or entry.get("sessionId") or entry.get("session_id"))
        if not session_id:
            continue
        session_path = project_root / f"{session_id}.jsonl"
        observed = _observe_session_file(session_path, req_id=req_id, index_entry=entry)
        if observed is not None:
            observations.append(observed)
    if observations:
        return max(observations, key=_observation_sort_key)
    for session_path in project_root.glob("*.jsonl"):
        observed = _observe_session_file(session_path, req_id=req_id, index_entry=None)
        if observed is not None:
            observations.append(observed)
    if observations:
        return max(observations, key=_observation_sort_key)
    return None


def _index_entries(index: object) -> list[dict[str, object]]:
    if isinstance(index, list):
        return [item for item in index if isinstance(item, dict)]
    if not isinstance(index, dict):
        return []
    for key in ("sessions", "items", "data"):
        value = index.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [value for value in index.values() if isinstance(value, dict)]


def _observe_session_file(
    path: Path,
    *,
    req_id: str,
    index_entry: dict[str, object] | None,
) -> DeepSeekSessionObservation | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    active = False
    reply_parts: list[str] = []
    last_assistant_id: str | None = None
    request_line = 0
    last_line = 0
    for index, line in enumerate(lines, 1):
        try:
            message = json.loads(line)
        except Exception:
            continue
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or message.get("type") or "").strip().lower()
        content = _message_text(message)
        if role == "user" and req_id in content:
            active = True
            reply_parts = []
            last_assistant_id = None
            request_line = index
            last_line = index
            continue
        if not active:
            continue
        if role == "user" and req_id not in content:
            active = False
            continue
        if role == "assistant":
            cleaned = clean_native_reply(content, req_id)
            if cleaned:
                reply_parts.append(cleaned)
                last_assistant_id = _coerce_str(message.get("id") or message.get("messageId") or message.get("message_id"))
                last_line = index

    if request_line <= 0:
        return None

    entry = index_entry or {}
    status = _coerce_str(entry.get("status")) or ""
    entry_reply = clean_native_reply(_coerce_str(entry.get("assistantReply") or entry.get("assistant_reply")) or "", req_id)
    reply = entry_reply or clean_native_reply("\n\n".join(reply_parts), req_id)
    fail_reason = _coerce_str(entry.get("failReason") or entry.get("fail_reason") or entry.get("error"))
    updated_at = entry.get("updateTime") or entry.get("updatedAt") or entry.get("updated_at")
    completed = status in TERMINAL_SUCCESS_STATUSES
    session_id = _coerce_str(entry.get("id") or entry.get("sessionId") or path.stem)
    return DeepSeekSessionObservation(
        request_seen=True,
        completed=completed,
        status=status,
        reply=reply,
        session_id=session_id,
        session_path=str(path),
        provider_turn_ref=last_assistant_id or session_id,
        line_count=max(last_line, request_line),
        fail_reason=fail_reason,
        updated_at=updated_at,
    )


def _message_text(message: dict[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)
    text = message.get("text")
    return text if isinstance(text, str) else ""


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _sanitize_project_name(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-.")


def _coerce_str(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _observation_sort_key(observation: DeepSeekSessionObservation) -> tuple[float, int]:
    mtime = _path_mtime(Path(observation.session_path or ""))
    return (mtime, int(observation.line_count or 0))


def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


__all__ = [
    "DeepSeekSessionObservation",
    "INTERRUPTED_STATUSES",
    "PERMISSION_DENIED_STATUSES",
    "TERMINAL_FAILURE_STATUSES",
    "TERMINAL_SUCCESS_STATUSES",
    "WAITING_USER_STATUSES",
    "deepseek_project_code",
    "deepseek_project_root",
    "observe_deepseek_session",
]
