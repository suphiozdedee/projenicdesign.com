from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypedDict, cast

from provider_runtime.session_payload import (
    redacted_namespace_ref,
    redacted_provider_runtime_backend_ref,
    redacted_restore_tokens,
)

CapabilityStatus = Literal["supported", "partial", "blocked", "unsupported"]
SupportTierProjection = Literal["unsupported", "experimental", "beta"]
SupportTierProjectionSource = Literal[
    "backend_capability",
    "validation_pending",
    "supportability_deferred",
]


class HerdrSurfaceProjection(TypedDict):
    backend_impl: Literal["herdr"]
    capability_status: CapabilityStatus
    support_tier_projection: SupportTierProjection
    support_tier_projection_source: SupportTierProjectionSource
    beta_gaps: list[str]
    blocking_gaps: list[str]
    degraded_next_action: str | None
    evidence_refs: dict[str, object]


_CAPABILITY_STATUSES = frozenset({"supported", "partial", "blocked", "unsupported"})
_SUPPORT_TIER_PROJECTIONS = frozenset({"unsupported", "experimental", "beta"})
_SUPPORT_TIER_SOURCES = frozenset(
    {"backend_capability", "validation_pending", "supportability_deferred"}
)
_BLOCKING_RECOVERY_ACTIONS = frozenset({"blocked", "circuit_open"})


def build_herdr_surface_projection(
    evidence: Mapping[str, object] | None,
) -> HerdrSurfaceProjection | None:
    if not _has_herdr_backend(evidence):
        return None
    recovery_ledger = _mapping(evidence.get("recovery_evidence_ledger") if evidence is not None else None)
    beta_gaps = _text_list(evidence.get("beta_gaps") if evidence is not None else None)
    if not beta_gaps:
        beta_gaps = ["validation_pending"]
    blocking_gaps = _blocking_gaps(evidence, recovery_ledger)
    capability_status = _capability_status(evidence, blocking_gaps, beta_gaps)
    return {
        "backend_impl": "herdr",
        "capability_status": capability_status,
        "support_tier_projection": _support_tier_projection(evidence, capability_status),
        "support_tier_projection_source": _support_tier_projection_source(evidence),
        "beta_gaps": beta_gaps,
        "blocking_gaps": blocking_gaps,
        "degraded_next_action": _degraded_next_action(evidence, recovery_ledger, blocking_gaps),
        "evidence_refs": _evidence_refs(evidence, recovery_ledger),
    }


def build_herdr_runtime_surface_projection(runtime) -> HerdrSurfaceProjection | None:
    if runtime is None:
        return None
    blocking_gaps = []
    if str(getattr(runtime, "reconcile_state", "") or "").strip().lower() == "blocked":
        reason = str(getattr(runtime, "last_failure_reason", "") or "").strip()
        if reason:
            blocking_gaps.append(reason)
    return build_herdr_surface_projection(
        {
            "backend_impl": getattr(runtime, "terminal_backend", None),
            "provider_runtime_backend_ref": getattr(runtime, "provider_runtime_backend_ref", None),
            "namespace_ref": getattr(runtime, "namespace_ref", None),
            "pane_ref": getattr(runtime, "pane_ref", None),
            "herdr_auto_restore_mode": getattr(runtime, "herdr_auto_restore_mode", None),
            "blocking_gaps": blocking_gaps,
        }
    )


def herdr_surface_projection_passes_gate(projection: Mapping[str, object] | None) -> bool:
    if not isinstance(projection, Mapping) or projection.get("backend_impl") != "herdr":
        return False
    return (
        _optional_text(projection.get("capability_status")) == "supported"
        and _optional_text(projection.get("support_tier_projection")) == "beta"
        and _optional_text(projection.get("support_tier_projection_source")) == "backend_capability"
        and projection.get("beta_gaps") == []
        and projection.get("blocking_gaps") == []
        and "degraded_next_action" in projection
        and projection.get("degraded_next_action") is None
    )


def _has_herdr_backend(evidence: Mapping[str, object] | None) -> bool:
    if evidence is None:
        return False
    candidates = [
        evidence.get("backend_impl"),
        _mapping(evidence.get("provider_runtime_backend_ref")).get("backend_impl"),
        _mapping(evidence.get("recovery_evidence_ledger")).get("backend_impl"),
    ]
    return any(str(candidate or "").strip().lower() == "herdr" for candidate in candidates)


def _blocking_gaps(
    evidence: Mapping[str, object] | None,
    recovery_ledger: Mapping[str, object],
) -> list[str]:
    gaps = _text_list(evidence.get("blocking_gaps") if evidence is not None else None)
    auto_restore_mode = _optional_text(
        evidence.get("herdr_auto_restore_mode") if evidence is not None else None
    )
    if auto_restore_mode and auto_restore_mode != "disabled":
        gap = f"herdr_auto_restore_mode:{auto_restore_mode}"
        if gap not in gaps:
            gaps.append(gap)
    action = str(recovery_ledger.get("action") or "").strip()
    if action in _BLOCKING_RECOVERY_ACTIONS:
        reason = _optional_text(recovery_ledger.get("reason")) or action
        if reason not in gaps:
            gaps.append(reason)
    return gaps


def _capability_status(
    evidence: Mapping[str, object] | None,
    blocking_gaps: list[str],
    beta_gaps: list[str],
) -> CapabilityStatus:
    explicit = _literal_value(
        evidence.get("capability_status") if evidence is not None else None,
        _CAPABILITY_STATUSES,
    )
    if explicit is not None:
        return cast(CapabilityStatus, explicit)
    if blocking_gaps:
        return "blocked"
    if beta_gaps:
        return "partial"
    return "supported"


def _support_tier_projection(
    evidence: Mapping[str, object] | None,
    capability_status: CapabilityStatus,
) -> SupportTierProjection:
    explicit = _literal_value(
        evidence.get("support_tier_projection") if evidence is not None else None,
        _SUPPORT_TIER_PROJECTIONS,
    )
    if explicit is not None:
        return cast(SupportTierProjection, explicit)
    if capability_status == "unsupported":
        return "unsupported"
    if capability_status == "supported":
        return "beta"
    return "experimental"


def _support_tier_projection_source(
    evidence: Mapping[str, object] | None,
) -> SupportTierProjectionSource:
    explicit = _literal_value(
        evidence.get("support_tier_projection_source") if evidence is not None else None,
        _SUPPORT_TIER_SOURCES,
    )
    if explicit is not None:
        return cast(SupportTierProjectionSource, explicit)
    return "validation_pending"


def _degraded_next_action(
    evidence: Mapping[str, object] | None,
    recovery_ledger: Mapping[str, object],
    blocking_gaps: list[str],
) -> str | None:
    explicit = _optional_text(evidence.get("degraded_next_action") if evidence is not None else None)
    if explicit:
        return explicit
    action = str(recovery_ledger.get("action") or "").strip()
    if action == "circuit_open":
        return "wait-probation"
    if blocking_gaps:
        return "collect-validation-transcript"
    return None


def _evidence_refs(
    evidence: Mapping[str, object] | None,
    recovery_ledger: Mapping[str, object],
) -> dict[str, object]:
    refs: dict[str, object] = {}
    backend_ref = _mapping(evidence.get("provider_runtime_backend_ref") if evidence is not None else None)
    namespace_ref = (
        _mapping(evidence.get("namespace_ref") if evidence is not None else None)
        or _mapping(backend_ref.get("namespace_ref"))
        or _mapping(recovery_ledger.get("namespace_ref"))
    )
    pane_ref = (
        _mapping(evidence.get("pane_ref") if evidence is not None else None)
        or _mapping(backend_ref.get("pane_ref"))
        or _mapping(recovery_ledger.get("pane_ref"))
    )
    redacted_namespace = redacted_namespace_ref(namespace_ref)
    if redacted_namespace is not None:
        refs["namespace_ref"] = redacted_namespace
    if pane_ref:
        refs["pane_ref"] = redacted_restore_tokens(dict(pane_ref))
    redacted_backend = redacted_provider_runtime_backend_ref(backend_ref)
    if redacted_backend is not None:
        refs["provider_runtime_backend_ref"] = redacted_restore_tokens(redacted_backend)
    if recovery_ledger:
        refs["recovery_evidence_ledger"] = redacted_restore_tokens(dict(recovery_ledger))
    return refs


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text_list(value: object) -> list[str]:
    if isinstance(value, (str, bytes)):
        values = [value]
    elif isinstance(value, list | tuple | set):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = _optional_text(item)
        if text and text not in result:
            result.append(text)
    return result


def _literal_value(value: object, allowed: frozenset[str]) -> str | None:
    text = str(value or "").strip()
    return text if text in allowed else None


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "CapabilityStatus",
    "HerdrSurfaceProjection",
    "SupportTierProjection",
    "SupportTierProjectionSource",
    "build_herdr_runtime_surface_projection",
    "build_herdr_surface_projection",
    "herdr_surface_projection_passes_gate",
]
