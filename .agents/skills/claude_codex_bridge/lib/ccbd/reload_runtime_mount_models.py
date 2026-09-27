from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AdditiveRuntimeMountResult:
    status: str
    requested_agents: tuple[str, ...] = ()
    mounted_agents: tuple[str, ...] = ()
    runtime_authority_written_agents: tuple[str, ...] = ()
    moved_agents: tuple[str, ...] = ()
    runtime_authority_moved_agents: tuple[str, ...] = ()
    unloaded_agents: tuple[str, ...] = ()
    replaced_agents: tuple[str, ...] = ()
    runtime_authority_stopped_agents: tuple[str, ...] = ()
    helper_terminated_agents: tuple[str, ...] = ()
    preserved_runtime_unchanged_agents: tuple[str, ...] = ()
    partial: bool = False
    summary: dict[str, object] | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_record(self) -> dict[str, object]:
        return {
            'status': self.status,
            'requested_agents': list(self.requested_agents),
            'mounted_agents': list(self.mounted_agents),
            'runtime_authority_written_agents': list(
                self.runtime_authority_written_agents
            ),
            'moved_agents': list(self.moved_agents),
            'runtime_authority_moved_agents': list(
                self.runtime_authority_moved_agents
            ),
            'unloaded_agents': list(self.unloaded_agents),
            'replaced_agents': list(self.replaced_agents),
            'runtime_authority_stopped_agents': list(
                self.runtime_authority_stopped_agents
            ),
            'helper_terminated_agents': list(self.helper_terminated_agents),
            'preserved_runtime_unchanged_agents': list(
                self.preserved_runtime_unchanged_agents
            ),
            'partial': bool(self.partial),
            'summary': dict(self.summary) if self.summary is not None else None,
            'diagnostics': dict(self.diagnostics),
        }


def blocked_mount_result(
    reason: str,
    message: str,
    extra_diagnostics: dict[str, object] | None = None,
    *,
    requested_agents: tuple[str, ...] = (),
) -> AdditiveRuntimeMountResult:
    return AdditiveRuntimeMountResult(
        status='blocked',
        requested_agents=requested_agents,
        diagnostics={
            'reason': reason,
            'message': message,
            **dict(extra_diagnostics or {}),
            **_no_publish_diagnostics(),
        },
    )


def noop_mount_result(preserved_agents: tuple[str, ...]) -> AdditiveRuntimeMountResult:
    return AdditiveRuntimeMountResult(
        status='noop',
        preserved_runtime_unchanged_agents=preserved_agents,
        diagnostics={
            'reason': 'no_new_agent_panes',
            **_no_publish_diagnostics(),
        },
    )


def deferred_mount_result(
    requested_agents: tuple[str, ...],
    preserved_agents: tuple[str, ...] = (),
) -> AdditiveRuntimeMountResult:
    return AdditiveRuntimeMountResult(
        status='noop',
        requested_agents=requested_agents,
        preserved_runtime_unchanged_agents=preserved_agents,
        diagnostics={
            'reason': 'provider_runtime_deferred_on_herdr',
            'runtime_mount_deferred': True,
            'runtime_authority_scope': 'provider_runtime_on_herdr',
            **_no_publish_diagnostics(),
        },
    )


def failed_mount_result(
    *,
    reason: str,
    error: Exception,
    requested_agents: tuple[str, ...],
    mounted_agents: tuple[str, ...],
    written_agents: tuple[str, ...],
    preserved_unchanged_agents: tuple[str, ...],
    preserved_changed_agents: tuple[str, ...],
    summary: dict[str, object] | None,
) -> AdditiveRuntimeMountResult:
    return AdditiveRuntimeMountResult(
        status='failed',
        requested_agents=requested_agents,
        mounted_agents=mounted_agents,
        runtime_authority_written_agents=written_agents,
        preserved_runtime_unchanged_agents=preserved_unchanged_agents,
        partial=bool(written_agents),
        summary=summary,
        diagnostics={
            'reason': reason,
            'error_type': type(error).__name__,
            'error': str(error),
            'runtime_authority_scope': (
                'new_agents_only'
                if not preserved_changed_agents
                else 'preserved_agent_changed'
            ),
            'preserved_runtime_changed_agents': list(preserved_changed_agents),
            **_no_publish_diagnostics(),
        },
    )


def mounted_result(
    *,
    requested_agents: tuple[str, ...],
    mounted_agents: tuple[str, ...],
    written_agents: tuple[str, ...],
    preserved_agents: tuple[str, ...],
    summary: dict[str, object] | None,
) -> AdditiveRuntimeMountResult:
    return AdditiveRuntimeMountResult(
        status='mounted',
        requested_agents=requested_agents,
        mounted_agents=mounted_agents,
        runtime_authority_written_agents=written_agents,
        preserved_runtime_unchanged_agents=preserved_agents,
        partial=False,
        summary=summary,
        diagnostics={
            'reason': None,
            'runtime_authority_scope': 'new_agents_only',
            **_no_publish_diagnostics(),
        },
    )


def unloaded_result(
    *,
    requested_agents: tuple[str, ...],
    unloaded_agents: tuple[str, ...],
    stopped_agents: tuple[str, ...],
    helper_terminated_agents: tuple[str, ...],
    preserved_agents: tuple[str, ...],
) -> AdditiveRuntimeMountResult:
    return AdditiveRuntimeMountResult(
        status='unloaded',
        requested_agents=requested_agents,
        unloaded_agents=unloaded_agents,
        runtime_authority_stopped_agents=stopped_agents,
        helper_terminated_agents=helper_terminated_agents,
        preserved_runtime_unchanged_agents=preserved_agents,
        partial=False,
        diagnostics={
            'reason': None,
            'runtime_authority_scope': 'removed_agents_only',
            **_no_publish_diagnostics(),
            'unload_or_replace_executed': bool(unloaded_agents or stopped_agents),
        },
    )


def replaced_result(
    *,
    requested_agents: tuple[str, ...],
    replaced_agents: tuple[str, ...],
    mounted_agents: tuple[str, ...],
    written_agents: tuple[str, ...],
    stopped_agents: tuple[str, ...],
    helper_terminated_agents: tuple[str, ...],
    preserved_agents: tuple[str, ...],
    summary: dict[str, object] | None,
) -> AdditiveRuntimeMountResult:
    return AdditiveRuntimeMountResult(
        status='replaced',
        requested_agents=requested_agents,
        replaced_agents=replaced_agents,
        mounted_agents=mounted_agents,
        runtime_authority_written_agents=written_agents,
        runtime_authority_stopped_agents=stopped_agents,
        helper_terminated_agents=helper_terminated_agents,
        preserved_runtime_unchanged_agents=preserved_agents,
        partial=False,
        summary=summary,
        diagnostics={
            'reason': None,
            'runtime_authority_scope': 'replaced_agents_only',
            **_no_publish_diagnostics(),
            'unload_or_replace_executed': bool(replaced_agents or stopped_agents),
        },
    )


def moved_result(
    *,
    requested_agents: tuple[str, ...],
    moved_agents: tuple[str, ...],
    written_agents: tuple[str, ...],
    preserved_agents: tuple[str, ...],
) -> AdditiveRuntimeMountResult:
    return AdditiveRuntimeMountResult(
        status='moved',
        requested_agents=requested_agents,
        moved_agents=moved_agents,
        runtime_authority_moved_agents=written_agents,
        preserved_runtime_unchanged_agents=preserved_agents,
        partial=False,
        diagnostics={
            'reason': None,
            'runtime_authority_scope': 'moved_agents_only',
            **_no_publish_diagnostics(),
        },
    )


def _no_publish_diagnostics() -> dict[str, object]:
    return {
        'graph_published': False,
        'lease_or_lifecycle_written': False,
        'cleanup_tmux_orphans': False,
        'config_watch_started': False,
    }


__all__ = [
    'AdditiveRuntimeMountResult',
    'blocked_mount_result',
    'deferred_mount_result',
    'failed_mount_result',
    'mounted_result',
    'moved_result',
    'noop_mount_result',
    'replaced_result',
    'unloaded_result',
]
