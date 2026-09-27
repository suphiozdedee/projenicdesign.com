from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


STATE_BOUND = "bound"
STATE_AUTO_REBINDABLE = "auto_rebindable"
STATE_AUTO_REBOUND = "auto_rebound"
STATE_SWITCHED_UNBOUND = "switched_unbound"
STATE_MISMATCH = "mismatch"


@dataclass(frozen=True)
class SwitchCandidate:
    path: Path
    session_id: str
    mtime: float

    def to_record(self) -> dict[str, object]:
        return {
            "session_path": str(self.path),
            "session_id": self.session_id,
            "mtime": self.mtime,
        }


@dataclass(frozen=True)
class SwitchEvidence:
    managed_root: bool
    runtime_match: bool
    work_dir_match: bool
    candidate_unique: bool
    newer_than_bound: bool
    running_job_count: int
    request_anchor_seen: bool

    def to_record(self) -> dict[str, object]:
        return {
            "managed_root": self.managed_root,
            "runtime_match": self.runtime_match,
            "work_dir_match": self.work_dir_match,
            "candidate_unique": self.candidate_unique,
            "newer_than_bound": self.newer_than_bound,
            "running_job_count": self.running_job_count,
            "request_anchor_seen": self.request_anchor_seen,
        }


@dataclass(frozen=True)
class SwitchDecision:
    state: str
    reason: str
    candidate: SwitchCandidate | None
    evidence: SwitchEvidence

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "state": self.state,
            "reason": self.reason,
            "evidence": self.evidence.to_record(),
        }
        if self.candidate is not None:
            record["candidate"] = self.candidate.to_record()
        return record


__all__ = [
    "STATE_AUTO_REBINDABLE",
    "STATE_AUTO_REBOUND",
    "STATE_BOUND",
    "STATE_MISMATCH",
    "STATE_SWITCHED_UNBOUND",
    "SwitchCandidate",
    "SwitchDecision",
    "SwitchEvidence",
]
