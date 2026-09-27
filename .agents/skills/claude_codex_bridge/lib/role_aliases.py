from __future__ import annotations

LEGACY_ROLE_ALIASES = {
    'ccb.archi': 'agentroles.archi',
    'agentrole.ccb_self': 'agentroles.ccb_self',
}


def canonical_role_id(role_id: str) -> str:
    normalized = str(role_id or '').strip().lower()
    return LEGACY_ROLE_ALIASES.get(normalized, normalized)


def legacy_role_ids(canonical_id: str) -> tuple[str, ...]:
    canonical = canonical_role_id(canonical_id)
    return tuple(
        legacy
        for legacy, target in sorted(LEGACY_ROLE_ALIASES.items())
        if target == canonical
    )


def role_id_candidates(role_id: str) -> tuple[str, ...]:
    canonical = canonical_role_id(role_id)
    candidates = [canonical]
    candidates.extend(item for item in legacy_role_ids(canonical) if item not in candidates)
    return tuple(candidates)


__all__ = [
    'LEGACY_ROLE_ALIASES',
    'canonical_role_id',
    'legacy_role_ids',
    'role_id_candidates',
]
