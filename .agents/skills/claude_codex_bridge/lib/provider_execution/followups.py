from __future__ import annotations

from dataclasses import dataclass, field

from .base import ProviderSubmission


@dataclass(frozen=True)
class ActiveFollowupCapability:
    supported: bool
    mechanism: str
    provider_turn_ref: str | None = None
    reason: str = ''
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ActiveFollowupRequest:
    followup_id: str
    job_id: str
    message: str
    expected_provider_turn_ref: str


@dataclass(frozen=True)
class ActiveFollowupResult:
    submission: ProviderSubmission | None
    status: str
    reason: str
    mechanism: str
    provider_turn_ref: str | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {'accepted', 'injected', 'rejected', 'terminal'}:
            raise ValueError(f'unsupported active follow-up result status: {self.status}')


def unsupported_active_followup_capability(
    reason: str,
    *,
    mechanism: str = 'unsupported',
    diagnostics: dict[str, object] | None = None,
) -> ActiveFollowupCapability:
    return ActiveFollowupCapability(
        supported=False,
        mechanism=mechanism,
        reason=str(reason or '').strip() or 'provider_active_followup_unsupported',
        diagnostics=dict(diagnostics or {}),
    )


__all__ = [
    'ActiveFollowupCapability',
    'ActiveFollowupRequest',
    'ActiveFollowupResult',
    'unsupported_active_followup_capability',
]
