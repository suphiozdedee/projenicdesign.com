from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SOURCE_STATUS_OK: Literal["ok"] = "ok"
SOURCE_STATUS_ERROR: Literal["error"] = "error"
SourceStatus = Literal["ok", "error"]


@dataclass(frozen=True)
class PaneCompletionEvidence:
    """Pane completion evidence is observation only, not job lifecycle authority.

    Passing it to dispatcher.complete() or using it to build a CompletionDecision
    directly is a bug.
    """

    outcome: str
    source: str
    reason: str

    def __not_a_job_terminator__(self) -> None:
        return None

    def to_record(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "source": self.source,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ProviderPaneStatusSignal:
    provider: str
    source_status: SourceStatus
    parsed_state: str
    reason: str
    matched_patterns: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    completion_evidence: PaneCompletionEvidence | None = None

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "provider": self.provider,
            "source_status": self.source_status,
            "parsed_state": self.parsed_state,
            "reason": self.reason,
            "matched_patterns": list(self.matched_patterns),
            "notes": list(self.notes),
        }
        if self.completion_evidence is not None:
            record["completion_evidence"] = self.completion_evidence.to_record()
        return record
