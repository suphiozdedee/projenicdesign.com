from __future__ import annotations

import json
from pathlib import Path
import re
import time

from provider_backends.codex.comm_runtime.binding import (
    extract_cwd_from_log_file,
    extract_session_id,
    is_codex_subagent_log,
)
from provider_backends.codex.comm_runtime.pathing import normalize_work_dir
from provider_backends.codex.session_runtime.follow_policy import codex_session_root_path

from .active_jobs import running_request_anchors
from .models import (
    STATE_AUTO_REBINDABLE,
    STATE_BOUND,
    STATE_MISMATCH,
    STATE_SWITCHED_UNBOUND,
    SwitchCandidate,
    SwitchDecision,
    SwitchEvidence,
)


def resolve_switch_decision(
    session_data: dict[str, object],
    *,
    session_file: Path,
    runtime_dir: Path | None,
) -> SwitchDecision:
    evidence = _base_evidence(session_data, runtime_dir=runtime_dir)
    if not evidence.managed_root:
        return _decision(STATE_MISMATCH, "missing_managed_session_root", None, evidence)
    if not evidence.runtime_match:
        return _decision(STATE_MISMATCH, "runtime_dir_mismatch", None, evidence)

    root = codex_session_root_path(session_data)
    work_dir = _normalized_work_dir(session_data)
    if root is None or work_dir is None:
        return _decision(STATE_MISMATCH, "missing_work_dir_or_session_root", None, evidence)

    running_anchors = running_request_anchors(session_file=session_file, session_data=session_data)
    runtime_started_at = _runtime_started_at(session_data)
    candidates = _candidate_sessions(
        session_data,
        session_file=session_file,
        root=root,
        work_dir=work_dir,
        request_anchors=running_anchors,
        runtime_started_at=runtime_started_at,
    )
    evidence = _evidence_with_candidates(evidence, candidates=candidates, running_anchors=running_anchors)

    if not candidates:
        if running_anchors and _candidate_sessions(
            session_data,
            session_file=session_file,
            root=root,
            work_dir=work_dir,
            runtime_started_at=runtime_started_at,
        ):
            return _decision(STATE_SWITCHED_UNBOUND, "running_job_anchor_not_seen", None, evidence)
        return _decision(STATE_BOUND, "no_new_managed_session", None, evidence)
    if len(candidates) != 1:
        return _decision(STATE_SWITCHED_UNBOUND, "ambiguous_session_candidates", None, evidence)

    candidate = candidates[0]
    evidence = SwitchEvidence(
        managed_root=evidence.managed_root,
        runtime_match=evidence.runtime_match,
        work_dir_match=evidence.work_dir_match,
        candidate_unique=evidence.candidate_unique,
        newer_than_bound=evidence.newer_than_bound,
        running_job_count=evidence.running_job_count,
        request_anchor_seen=(not running_anchors or _path_contains_any_exact_anchor(candidate.path, running_anchors)),
    )
    return _decision(STATE_AUTO_REBINDABLE, "single_managed_session_switch", candidate, evidence)


def _candidate_sessions(
    session_data: dict[str, object],
    *,
    session_file: Path,
    root: Path,
    work_dir: str,
    request_anchors: tuple[str, ...] = (),
    runtime_started_at: float | None = None,
) -> tuple[SwitchCandidate, ...]:
    current_path = _normalize_path(session_data.get("codex_session_path"))
    current_id = str(session_data.get("codex_session_id") or "").strip()
    current_mtime = _mtime(current_path)
    candidates: list[SwitchCandidate] = []
    try:
        paths = sorted(root.glob("**/*.jsonl"))
    except OSError:
        return ()
    for path in paths:
        if not path.is_file():
            continue
        if is_codex_subagent_log(path):
            continue
        normalized = _normalize_path(path)
        if normalized is not None and current_path is not None and normalized == current_path:
            continue
        session_id = str(extract_session_id(path) or "").strip()
        if session_id and current_id and session_id == current_id:
            continue
        if _bound_to_other_session(session_file=session_file, path=path, session_id=session_id):
            continue
        mtime = _mtime(path)
        if mtime is None:
            continue
        if current_mtime is not None and mtime <= current_mtime:
            continue
        # An idle switch is valid only when the candidate became active during
        # the current managed runtime generation.  Merely being newer than the
        # persisted binding is insufficient after a pane restart: a different
        # conversation may have been active shortly before this generation.
        if not request_anchors and runtime_started_at is not None and mtime < runtime_started_at:
            continue
        if request_anchors:
            if not _path_contains_any_exact_anchor(path, request_anchors):
                continue
        else:
            cwd = _normalized_log_cwd(path)
            if cwd != work_dir:
                continue
        candidates.append(SwitchCandidate(path=path, session_id=session_id, mtime=mtime))
    return tuple(candidates)


def select_exact_anchor_candidate(
    session_data: dict[str, object],
    *,
    session_file: Path | None,
    request_anchor: str,
) -> SwitchCandidate | None:
    """Return one safe newer managed-root log for an active request anchor.

    This deliberately ignores stale ``cwd`` metadata only after the exact
    request anchor has established ownership of a newer top-level session.
    Idle discovery continues to use the strict cwd path above.
    """
    root = codex_session_root_path(session_data)
    work_dir = _normalized_work_dir(session_data)
    anchor = str(request_anchor or "").strip()
    if root is None or not root.is_dir() or work_dir is None or not anchor:
        return None
    candidates = _candidate_sessions(
        session_data,
        session_file=session_file,
        root=root,
        work_dir=work_dir,
        request_anchors=(anchor,),
    )
    return candidates[0] if len(candidates) == 1 else None


def _base_evidence(session_data: dict[str, object], *, runtime_dir: Path | None) -> SwitchEvidence:
    root = codex_session_root_path(session_data)
    return SwitchEvidence(
        managed_root=root is not None and root.is_dir(),
        runtime_match=_runtime_matches(session_data, runtime_dir=runtime_dir),
        work_dir_match=False,
        candidate_unique=False,
        newer_than_bound=False,
        running_job_count=0,
        request_anchor_seen=False,
    )


def _evidence_with_candidates(
    evidence: SwitchEvidence,
    *,
    candidates: tuple[SwitchCandidate, ...],
    running_anchors: tuple[str, ...],
) -> SwitchEvidence:
    return SwitchEvidence(
        managed_root=evidence.managed_root,
        runtime_match=evidence.runtime_match,
        work_dir_match=bool(candidates),
        candidate_unique=len(candidates) == 1,
        newer_than_bound=bool(candidates),
        running_job_count=len(running_anchors),
        request_anchor_seen=False if running_anchors else True,
    )


def _decision(state: str, reason: str, candidate: SwitchCandidate | None, evidence: SwitchEvidence) -> SwitchDecision:
    return SwitchDecision(state=state, reason=reason, candidate=candidate, evidence=evidence)


def _normalized_work_dir(session_data: dict[str, object]) -> str | None:
    raw = session_data.get("work_dir") or session_data.get("workspace_path") or session_data.get("start_dir")
    if not raw:
        return None
    try:
        return normalize_work_dir(Path(str(raw)).expanduser())
    except Exception:
        return None


def _normalized_log_cwd(path: Path) -> str | None:
    raw = extract_cwd_from_log_file(path)
    if not raw:
        return None
    try:
        return normalize_work_dir(Path(raw).expanduser())
    except Exception:
        return None


def _runtime_matches(session_data: dict[str, object], *, runtime_dir: Path | None) -> bool:
    if runtime_dir is None:
        return True
    recorded = _normalize_path(session_data.get("runtime_dir"))
    actual = _normalize_path(runtime_dir)
    if recorded is None or actual is None:
        return False
    return recorded == actual


def _bound_to_other_session(*, session_file: Path | None, path: Path, session_id: str) -> bool:
    if session_file is None:
        return False
    session_dir = session_file.expanduser().parent
    try:
        candidates = sorted(session_dir.glob(".codex*-session"))
    except OSError:
        return False
    normalized_path = _normalize_path(path)
    for candidate in candidates:
        if _normalize_path(candidate) == _normalize_path(session_file):
            continue
        data = _read_json(candidate)
        if not data:
            continue
        candidate_path = _normalize_path(data.get("codex_session_path"))
        if normalized_path is not None and candidate_path == normalized_path:
            return True
        candidate_id = str(data.get("codex_session_id") or "").strip()
        if session_id and candidate_id and candidate_id == session_id:
            return True
    return False


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_path(value: object) -> Path | None:
    if value is None:
        return None
    try:
        return Path(value).expanduser().resolve()
    except Exception:
        try:
            return Path(value).expanduser()
        except Exception:
            return None


def _mtime(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _path_contains_any_exact_anchor(path: Path, anchors: tuple[str, ...]) -> bool:
    if not anchors:
        return True
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 1024 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
    except Exception:
        return False
    return any(_path_contains_exact_anchor_text(text, anchor) for anchor in anchors)


def _runtime_started_at(session_data: dict[str, object]) -> float | None:
    raw = str(session_data.get("started_at") or "").strip()
    if not raw:
        return None
    try:
        return time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
    except (OverflowError, ValueError):
        return None


def _path_contains_exact_anchor_text(text: str, anchor: str) -> bool:
    normalized = str(anchor or "").strip()
    if not normalized:
        return False
    return bool(re.search(rf"CCB_REQ_ID:\s*{re.escape(normalized)}(?![A-Za-z0-9_-])", text))


__all__ = ["resolve_switch_decision", "select_exact_anchor_candidate"]
