from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
from typing import Iterable

from provider_backends.native_cli_support import clean_native_reply
from provider_core.protocol import REQ_ID_PREFIX
from provider_core.source_home import current_provider_source_home


_REQ_ID_RE = re.compile(rf"{re.escape(REQ_ID_PREFIX)}\s*([^\s`]+)")


@dataclass(frozen=True)
class AgyTranscriptObservation:
    request_seen: bool
    completed: bool
    reply: str
    conversation_id: str | None
    transcript_path: str | None
    provider_turn_ref: str | None
    line_count: int
    native_started_at: object | None = None
    native_completed_at: object | None = None
    latest_status: str | None = None
    coalesced_request_ids: tuple[str, ...] = ()
    request_is_latest: bool = True


def observe_agy_transcript(
    work_dir: Path,
    req_id: str,
    *,
    home_candidates: Iterable[Path] | None = None,
) -> AgyTranscriptObservation | None:
    del work_dir
    if not req_id:
        return None
    observations: list[AgyTranscriptObservation] = []
    for transcript in _transcript_paths(home_candidates=home_candidates):
        observed = _observe_transcript(transcript, req_id=req_id)
        if observed is not None:
            observations.append(observed)
    if not observations:
        return None
    completed = [item for item in observations if item.completed]
    if completed:
        return max(completed, key=_observation_sort_key)
    return max(observations, key=_observation_sort_key)


def agy_home_from_start_cmd(start_cmd: str) -> Path | None:
    if not start_cmd or "HOME=" not in start_cmd:
        return None
    prefix = start_cmd.split(";", 1)[0].strip()
    if prefix.startswith("export "):
        prefix = prefix[len("export ") :]
    try:
        parts = shlex.split(prefix, posix=(os.name != "nt"))
    except ValueError:
        return None
    for part in parts:
        if not part.startswith("HOME="):
            continue
        value = part.split("=", 1)[1].strip()
        if value:
            return Path(value).expanduser()
    return None


def _transcript_paths(*, home_candidates: Iterable[Path] | None) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for home in _candidate_homes(home_candidates):
        for root in _brain_roots(home):
            if not root.is_dir():
                continue
            for path in root.glob("*/.system_generated/logs/transcript*.jsonl"):
                try:
                    resolved = path.resolve(strict=False)
                except Exception:
                    resolved = path
                if resolved in seen:
                    continue
                seen.add(resolved)
                paths.append(path)
    return sorted(paths, key=_path_mtime)


def _candidate_homes(home_candidates: Iterable[Path] | None) -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("AGY_HOME") or os.environ.get("CCB_AGY_SOURCE_HOME")
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


def _brain_roots(home: Path) -> list[Path]:
    if home.name == "brain":
        return [home]
    if home.name == "antigravity-cli":
        return [home / "brain"]
    if home.name == ".gemini":
        return [home / "antigravity-cli" / "brain"]
    return [
        home / ".gemini" / "antigravity-cli" / "brain",
        home / ".antigravity" / "brain",
    ]


def _observe_transcript(path: Path, *, req_id: str) -> AgyTranscriptObservation | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    active = False
    request_line = 0
    latest_reply = ""
    latest_status: str | None = None
    started_at: object | None = None
    completed_at: object | None = None
    provider_turn_ref: str | None = None
    latest_line = 0
    coalesced_request_ids: tuple[str, ...] = ()
    request_is_latest = True

    for index, line in enumerate(lines, 1):
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue

        if _is_user_input(event):
            content = _event_content(event)
            request_ids = _request_ids(content)
            if req_id in request_ids:
                request_is_latest = request_ids[-1] == req_id
                active = request_is_latest
                request_line = index
                latest_reply = ""
                latest_status = 'COALESCED_SUPERSEDED' if not request_is_latest else _event_status(event)
                started_at = event.get("created_at") or event.get("timestamp")
                completed_at = None
                provider_turn_ref = _event_ref(event)
                latest_line = index
                coalesced_request_ids = request_ids if len(request_ids) > 1 else ()
            elif active:
                active = False
            continue

        if not active:
            continue

        status = _event_status(event)
        if status:
            latest_status = status

        if not _is_model_reply_event(event):
            continue
        reply = clean_native_reply(_event_content(event), req_id)
        if not reply:
            continue
        latest_reply = reply
        completed_at = event.get("created_at") or event.get("timestamp")
        provider_turn_ref = _event_ref(event) or provider_turn_ref
        latest_line = index

    if request_line <= 0:
        return None

    return AgyTranscriptObservation(
        request_seen=True,
        completed=bool(latest_reply),
        reply=latest_reply,
        conversation_id=_conversation_id(path),
        transcript_path=str(path),
        provider_turn_ref=provider_turn_ref or _conversation_id(path),
        line_count=max(latest_line, request_line),
        native_started_at=started_at,
        native_completed_at=completed_at,
        latest_status=latest_status,
        coalesced_request_ids=coalesced_request_ids,
        request_is_latest=request_is_latest,
    )


def _is_user_input(event: dict[str, object]) -> bool:
    source = str(event.get("source") or "").upper()
    event_type = str(event.get("type") or "").upper()
    return source.startswith("USER") and "USER_INPUT" in event_type


def _is_model_reply_event(event: dict[str, object]) -> bool:
    source = str(event.get("source") or "").upper()
    event_type = str(event.get("type") or "").upper()
    status = _event_status(event)
    if not source.startswith("MODEL"):
        return False
    if status and status != "DONE":
        return False
    return event_type in {
        "PLANNER_RESPONSE",
        "MODEL_RESPONSE",
        "ASSISTANT_RESPONSE",
        "FINAL_RESPONSE",
        "RESPONSE",
    } or event_type.endswith("_RESPONSE")


def _event_content(event: dict[str, object]) -> str:
    content = event.get("content")
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
    if isinstance(content, dict):
        value = content.get("text") or content.get("content")
        return value if isinstance(value, str) else json.dumps(content, ensure_ascii=False)
    text = event.get("text")
    return text if isinstance(text, str) else ""


def _request_ids(content: str) -> tuple[str, ...]:
    ids: list[str] = []
    for match in _REQ_ID_RE.finditer(content or ""):
        candidate = match.group(1).strip().strip(".,;:)")
        if candidate:
            ids.append(candidate)
    return tuple(ids)


def _event_status(event: dict[str, object]) -> str | None:
    text = str(event.get("status") or "").strip().upper()
    return text or None


def _event_ref(event: dict[str, object]) -> str | None:
    for key in ("id", "message_id", "step_id", "step_index"):
        value = event.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return None


def _conversation_id(path: Path) -> str | None:
    parts = path.parts
    try:
        index = parts.index("brain")
    except ValueError:
        return path.parent.parent.parent.name if len(path.parents) >= 3 else None
    if index + 1 < len(parts):
        return parts[index + 1]
    return None


def _observation_sort_key(observation: AgyTranscriptObservation) -> tuple[float, int]:
    mtime = _path_mtime(Path(observation.transcript_path or ""))
    return (mtime, int(observation.line_count or 0))


def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


__all__ = [
    "AgyTranscriptObservation",
    "agy_home_from_start_cmd",
    "observe_agy_transcript",
]
