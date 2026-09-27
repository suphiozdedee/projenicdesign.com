from __future__ import annotations

from agents.models import AgentValidationError, normalize_agent_name


CMD_ACTOR = 'cmd'
USER_ACTOR = 'user'
MAINTENANCE_HEARTBEAT_ACTOR = 'maintenance-heartbeat'
NON_MAILBOX_ACTORS = frozenset({USER_ACTOR, 'system', 'manual', 'email', MAINTENANCE_HEARTBEAT_ACTOR})
NON_AGENT_ACTORS = frozenset({CMD_ACTOR, *NON_MAILBOX_ACTORS})


def normalize_actor_name(actor: str | None) -> str:
    normalized = str(actor or '').strip().lower()
    if not normalized:
        raise AgentValidationError('actor cannot be empty')
    if normalized == CMD_ACTOR or normalized in NON_MAILBOX_ACTORS:
        return normalized
    return normalize_agent_name(normalized)


def normalize_mailbox_owner_name(actor: str | None) -> str:
    normalized = normalize_actor_name(actor)
    if normalized in NON_AGENT_ACTORS:
        raise AgentValidationError(f'actor {normalized!r} does not own a mailbox')
    return normalized


def known_mailbox_targets(config) -> frozenset[str]:
    agents = getattr(config, 'agents', {}) or {}
    return frozenset(normalize_agent_name(name) for name in agents)


def normalize_mailbox_target(actor: str | None, *, known_targets: frozenset[str]) -> str | None:
    normalized = str(actor or '').strip().lower()
    if not normalized:
        return None
    try:
        mailbox_name = normalize_mailbox_owner_name(normalized)
    except AgentValidationError:
        return None
    if mailbox_name in known_targets:
        return mailbox_name
    return None


__all__ = [
    'NON_AGENT_ACTORS',
    'NON_MAILBOX_ACTORS',
    'MAINTENANCE_HEARTBEAT_ACTOR',
    'USER_ACTOR',
    'known_mailbox_targets',
    'normalize_actor_name',
    'normalize_mailbox_owner_name',
    'normalize_mailbox_target',
]
