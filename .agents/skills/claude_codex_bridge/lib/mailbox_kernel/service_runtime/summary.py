from __future__ import annotations


def next_summary_version(prior) -> int:
    if prior is None:
        return 1
    return int(getattr(prior, 'summary_version', 0)) + 1


def _summary_version_for_record(
    prior,
    *,
    queue_depth: int,
    pending_reply_count: int,
    active_inbound_event_id,
    head_inbound_event_id,
    head_event_type,
    head_status,
    head_message_id,
    head_attempt_id,
    head_payload_ref,
    last_started_at: str | None,
    last_finished_at: str | None,
    mailbox_state,
    lease_version: int,
) -> int:
    if prior is None:
        return 1
    unchanged = (
        prior.active_inbound_event_id == active_inbound_event_id
        and prior.queue_depth == queue_depth
        and prior.pending_reply_count == pending_reply_count
        and prior.head_inbound_event_id == head_inbound_event_id
        and prior.head_event_type == head_event_type
        and prior.head_status == head_status
        and prior.head_message_id == head_message_id
        and prior.head_attempt_id == head_attempt_id
        and prior.head_payload_ref == head_payload_ref
        and prior.last_inbound_started_at == last_started_at
        and prior.last_inbound_finished_at == last_finished_at
        and prior.mailbox_state == mailbox_state
        and prior.lease_version == lease_version
    )
    if unchanged:
        return int(getattr(prior, 'summary_version', 0))
    return next_summary_version(prior)


def summary_head_from_event(record) -> dict[str, object]:
    if record is None:
        return _empty_head()
    return {
        'head_inbound_event_id': record.inbound_event_id,
        'head_event_type': record.event_type.value,
        'head_status': record.status.value,
        'head_message_id': record.message_id,
        'head_attempt_id': record.attempt_id,
        'head_payload_ref': record.payload_ref,
    }


def build_mailbox_summary_record(
    service,
    agent_name: str,
    *,
    prior,
    lease,
    queue_depth: int,
    pending_reply_count: int,
    active_inbound_event_id,
    last_started_at: str | None,
    last_finished_at: str | None,
    updated_at: str,
    summary_source: str,
    summary_head=Ellipsis,
):
    if summary_head is Ellipsis:
        summary_head = _resolve_summary_head(service, agent_name, prior=prior)
    mailbox_id = prior.mailbox_id if prior is not None else f'mbx_{agent_name}'
    lease_version = lease.lease_version if lease is not None else (prior.lease_version if prior is not None else 0)
    has_active = (
        lease is not None
        and lease.lease_state is service._lease_state_acquired
        and bool(lease.inbound_event_id)
    ) or bool(active_inbound_event_id)
    mailbox_state = _derive_mailbox_state(service, has_active=has_active, queue_depth=queue_depth)
    summary_version = _summary_version_for_record(
        prior,
        queue_depth=queue_depth,
        pending_reply_count=pending_reply_count,
        active_inbound_event_id=active_inbound_event_id,
        head_inbound_event_id=summary_head.get('head_inbound_event_id'),
        head_event_type=summary_head.get('head_event_type'),
        head_status=summary_head.get('head_status'),
        head_message_id=summary_head.get('head_message_id'),
        head_attempt_id=summary_head.get('head_attempt_id'),
        head_payload_ref=summary_head.get('head_payload_ref'),
        last_started_at=last_started_at,
        last_finished_at=last_finished_at,
        mailbox_state=mailbox_state,
        lease_version=lease_version,
    )
    return service._mailbox_record_cls(
        mailbox_id=mailbox_id,
        agent_name=agent_name,
        summary_version=summary_version,
        summary_source=summary_source,
        summary_refreshed_at=updated_at,
        active_inbound_event_id=active_inbound_event_id,
        queue_depth=queue_depth,
        pending_reply_count=pending_reply_count,
        head_inbound_event_id=summary_head.get('head_inbound_event_id'),
        head_event_type=summary_head.get('head_event_type'),
        head_status=summary_head.get('head_status'),
        head_message_id=summary_head.get('head_message_id'),
        head_attempt_id=summary_head.get('head_attempt_id'),
        head_payload_ref=summary_head.get('head_payload_ref'),
        last_inbound_started_at=last_started_at,
        last_inbound_finished_at=last_finished_at,
        mailbox_state=mailbox_state,
        lease_version=lease_version,
        updated_at=updated_at,
    )


def apply_incremental_summary_update(
    service,
    agent_name: str,
    *,
    queue_delta: int = 0,
    pending_reply_delta: int = 0,
    active_inbound_event_id=Ellipsis,
    last_started_at: str | None = None,
    last_finished_at: str | None = None,
    updated_at: str | None = None,
):
    normalized = service._normalize_agent_name(agent_name)
    timestamp = updated_at or service._clock()
    prior = service._mailbox_store.load(normalized)
    expected_summary_version = _summary_version(prior)
    lease = service._lease_store.load(normalized)
    queue_depth = max(0, (prior.queue_depth if prior is not None else 0) + int(queue_delta))
    pending_reply_count = max(
        0,
        (prior.pending_reply_count if prior is not None else 0) + int(pending_reply_delta),
    )
    current_active = prior.active_inbound_event_id if prior is not None else None
    if active_inbound_event_id is not Ellipsis:
        current_active = active_inbound_event_id
    record = build_mailbox_summary_record(
        service,
        normalized,
        prior=prior,
        lease=lease,
        queue_depth=queue_depth,
        pending_reply_count=pending_reply_count,
        active_inbound_event_id=current_active,
        last_started_at=_latest_timestamp(
            prior.last_inbound_started_at if prior is not None else None,
            last_started_at,
        ),
        last_finished_at=_latest_timestamp(
            prior.last_inbound_finished_at if prior is not None else None,
            last_finished_at,
        ),
        updated_at=timestamp,
        summary_source='incremental-upsert',
    )
    return _save_summary_record(service, record, expected_summary_version=expected_summary_version)


def apply_transition_summary_update(
    service,
    agent_name: str,
    *,
    queue_depth: int,
    pending_reply_count: int,
    active_inbound_event_id,
    last_started_at: str | None,
    last_finished_at: str | None,
    updated_at: str,
    summary_source: str,
    summary_head=Ellipsis,
):
    normalized = service._normalize_agent_name(agent_name)
    timestamp = updated_at or service._clock()
    prior = service._mailbox_store.load(normalized)
    expected_summary_version = _summary_version(prior)
    lease = service._lease_store.load(normalized)
    record = build_mailbox_summary_record(
        service,
        normalized,
        prior=prior,
        lease=lease,
        queue_depth=max(0, int(queue_depth)),
        pending_reply_count=max(0, int(pending_reply_count)),
        active_inbound_event_id=active_inbound_event_id,
        last_started_at=last_started_at,
        last_finished_at=last_finished_at,
        updated_at=timestamp,
        summary_source=summary_source,
        summary_head=summary_head,
    )
    return _save_summary_record(service, record, expected_summary_version=expected_summary_version)


def mailbox_head_payload(record) -> dict | None:
    if record is None or not record.head_inbound_event_id:
        return None
    head = {
        'inbound_event_id': record.head_inbound_event_id,
        'event_type': record.head_event_type,
        'status': record.head_status,
        'message_id': record.head_message_id,
        'attempt_id': record.head_attempt_id,
        'payload_ref': record.head_payload_ref,
    }
    return head


def _derive_mailbox_state(service, *, has_active: bool, queue_depth: int):
    if has_active:
        return service._mailbox_state_delivering
    if queue_depth > 0:
        return service._mailbox_state_blocked
    return service._mailbox_state_idle


def _latest_timestamp(current: str | None, candidate: str | None) -> str | None:
    if candidate and (current is None or candidate > current):
        return candidate
    return current


def _save_summary_record(service, record, *, expected_summary_version: int | None):
    compare_and_save = getattr(service._mailbox_store, 'compare_and_save', None)
    if callable(compare_and_save):
        if compare_and_save(record, expected_summary_version=expected_summary_version):
            return record
        current = service._mailbox_store.load(record.agent_name)
        if current is not None:
            return current
    service._mailbox_store.save(record)
    return record


def save_summary_record(service, record, *, expected_summary_version: int | None):
    return _save_summary_record(
        service,
        record,
        expected_summary_version=expected_summary_version,
    )


def _summary_version(record) -> int | None:
    if record is None:
        return None
    return int(getattr(record, 'summary_version', 0))


def _resolve_summary_head(service, agent_name: str, *, prior) -> dict[str, object]:
    head = service.head_pending_event(agent_name)
    if head is None:
        return _empty_head()
    payload = {
        'head_inbound_event_id': head.inbound_event_id,
        'head_event_type': head.event_type.value,
        'head_status': head.status.value,
        'head_message_id': head.message_id,
        'head_attempt_id': head.attempt_id,
        'head_payload_ref': head.payload_ref,
    }
    return payload


def _empty_head() -> dict[str, object]:
    return {
        'head_inbound_event_id': None,
        'head_event_type': None,
        'head_status': None,
        'head_message_id': None,
        'head_attempt_id': None,
        'head_payload_ref': None,
    }


__all__ = [
    'apply_incremental_summary_update',
    'apply_transition_summary_update',
    'build_mailbox_summary_record',
    'mailbox_head_payload',
    'next_summary_version',
    'save_summary_record',
    'summary_head_from_event',
]
