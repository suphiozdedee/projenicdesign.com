from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ..model_enums import InboundEventStatus, LeaseState
from .mailbox import rebuild_mailbox_summary


def parse_timestamp(value: str | None) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _lease_ttl_seconds(service) -> float | None:
    ttl = getattr(service, '_lease_ttl_seconds', None)
    if ttl is None:
        return None
    try:
        ttl = float(ttl)
    except (TypeError, ValueError):
        return None
    return ttl if ttl > 0 else None


def lease_expires_at_value(service, acquired_at: str) -> str | None:
    ttl = _lease_ttl_seconds(service)
    if ttl is None:
        return None
    base = parse_timestamp(acquired_at)
    if base is None:
        return None
    return format_timestamp(base + timedelta(seconds=ttl))


def lease_deadline(service, lease) -> datetime | None:
    explicit = parse_timestamp(lease.expires_at)
    if explicit is not None:
        return explicit
    ttl = _lease_ttl_seconds(service)
    if ttl is None:
        return None
    base = parse_timestamp(lease.last_progress_at) or parse_timestamp(lease.acquired_at)
    if base is None:
        return None
    return base + timedelta(seconds=ttl)


def expired_delivery_leases(service, *, now: str | None = None):
    timestamp = now or service._clock()
    reference = parse_timestamp(timestamp)
    if reference is None:
        return ()
    expired = []
    for lease in service._lease_store.list_all():
        if lease.lease_state is not service._lease_state_acquired:
            continue
        deadline = lease_deadline(service, lease)
        if deadline is None or reference < deadline:
            continue
        expired.append(lease)
    return tuple(expired)


def expire_lease(service, agent_name: str, *, finished_at: str | None = None):
    normalized = service._normalize_agent_name(agent_name)
    timestamp = finished_at or service._clock()
    lease = service._lease_store.load(normalized)
    if lease is None or lease.lease_state is not service._lease_state_acquired:
        return None
    current = service._inbound_store.get_latest(normalized, lease.inbound_event_id)
    if current is None or current.status in service._terminal_event_states:
        service._lease_store.remove(normalized)
        rebuild_mailbox_summary(service, normalized, updated_at=timestamp)
        return None
    # Mark the lease EXPIRED first so the terminal transition's lease release
    # (which only removes ACQUIRED leases) leaves this record intact for trace.
    service._lease_store.save(replace(lease, lease_state=LeaseState.EXPIRED))
    from .transitions import mark_terminal

    return mark_terminal(
        service,
        normalized,
        lease.inbound_event_id,
        status=InboundEventStatus.ABANDONED,
        finished_at=timestamp,
    )


__all__ = [
    'expire_lease',
    'expired_delivery_leases',
    'format_timestamp',
    'lease_deadline',
    'lease_expires_at_value',
    'parse_timestamp',
]
