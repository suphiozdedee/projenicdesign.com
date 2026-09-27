from __future__ import annotations

from agents.store import AgentRuntimeStore
from ccbd.lifecycle_report_store import CcbdShutdownReportStore, CcbdStartupReportStore
from ccbd.restore_report_store import CcbdRestoreReportStore
from ccbd.services.project_namespace_state import ProjectNamespaceEventStore, ProjectNamespaceStateStore
from ccbd.services.start_policy import CcbdStartPolicyStore
from completion.snapshot_store import CompletionSnapshotStore
from mailbox_kernel import DeliveryLeaseStore, InboundEventStore, MailboxKernelService, MailboxStore
from provider_execution.state_store import ExecutionStateStore

from ..tmux_cleanup_history import TmuxCleanupHistoryStore


def doctor_stores(context) -> dict[str, object]:
    mailbox_store = MailboxStore(context.paths)
    inbox_store = InboundEventStore(context.paths)
    lease_store = DeliveryLeaseStore(context.paths)
    return {
        'runtime': AgentRuntimeStore(context.paths),
        'snapshot': CompletionSnapshotStore(context.paths),
        'mailbox': mailbox_store,
        'mailbox_kernel': MailboxKernelService(
            context.paths,
            clock=lambda: '1970-01-01T00:00:00Z',
            mailbox_store=mailbox_store,
            inbound_store=inbox_store,
            lease_store=lease_store,
        ),
        'execution_state': ExecutionStateStore(context.paths),
        'restore_report': CcbdRestoreReportStore(context.paths),
        'startup_report': CcbdStartupReportStore(context.paths),
        'shutdown_report': CcbdShutdownReportStore(context.paths),
        'namespace_state': ProjectNamespaceStateStore(context.paths),
        'namespace_event': ProjectNamespaceEventStore(context.paths),
        'start_policy': CcbdStartPolicyStore(context.paths),
        'tmux_cleanup': TmuxCleanupHistoryStore(context.paths),
    }


def report_summary_fields(report) -> dict:
    if report is None:
        return {}
    return report.summary_fields()


def safe_report_load(loader, errors: list[str], *, label: str):
    try:
        return loader()
    except Exception as exc:
        errors.append(f'{label}:{exc}')
        return None


__all__ = ['doctor_stores', 'report_summary_fields', 'safe_report_load']
