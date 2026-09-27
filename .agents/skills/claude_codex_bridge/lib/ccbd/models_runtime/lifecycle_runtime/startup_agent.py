from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from ccbd.models_runtime.common import CcbdModelError

from .common import clean_text, coerce_int


@dataclass(frozen=True)
class CcbdStartupAgentResult:
    agent_name: str
    provider: str | None
    action: str
    health: str
    workspace_path: str | None
    runtime_ref: str | None = None
    session_ref: str | None = None
    lifecycle_state: str | None = None
    desired_state: str | None = None
    reconcile_state: str | None = None
    binding_source: str | None = None
    terminal_backend: str | None = None
    tmux_socket_name: str | None = None
    tmux_socket_path: str | None = None
    tmux_window_name: str | None = None
    tmux_window_id: str | None = None
    pane_id: str | None = None
    active_pane_id: str | None = None
    pane_state: str | None = None
    runtime_pid: int | None = None
    runtime_root: str | None = None
    failure_reason: str | None = None
    binding_reject_reason: str | None = None
    duration_ms: float | None = None
    provider_prepare_ms: float | None = None
    provider_prepare_count: int = 0
    timings_ms: dict[str, float] | None = None

    def __post_init__(self) -> None:
        if self.agent_name == '':
            raise CcbdModelError('agent_name cannot be empty')
        if self.action == '':
            raise CcbdModelError('action cannot be empty')
        if self.health == '':
            raise CcbdModelError('health cannot be empty')

    def to_record(self) -> dict[str, Any]:
        return {
            'agent_name': self.agent_name,
            'provider': self.provider,
            'action': self.action,
            'health': self.health,
            'workspace_path': self.workspace_path,
            'runtime_ref': self.runtime_ref,
            'session_ref': self.session_ref,
            'lifecycle_state': self.lifecycle_state,
            'desired_state': self.desired_state,
            'reconcile_state': self.reconcile_state,
            'binding_source': self.binding_source,
            'terminal_backend': self.terminal_backend,
            'tmux_socket_name': self.tmux_socket_name,
            'tmux_socket_path': self.tmux_socket_path,
            'tmux_window_name': self.tmux_window_name,
            'tmux_window_id': self.tmux_window_id,
            'pane_id': self.pane_id,
            'active_pane_id': self.active_pane_id,
            'pane_state': self.pane_state,
            'runtime_pid': self.runtime_pid,
            'runtime_root': self.runtime_root,
            'failure_reason': self.failure_reason,
            'binding_reject_reason': self.binding_reject_reason,
            'duration_ms': self.duration_ms,
            'provider_prepare_ms': self.provider_prepare_ms,
            'provider_prepare_count': self.provider_prepare_count,
            'timings_ms': dict(self.timings_ms or {}),
        }

    def summary_token(self) -> str:
        return f'{self.agent_name}:{self.action}/{self.health}'

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> 'CcbdStartupAgentResult':
        return cls(
            agent_name=clean_text(record.get('agent_name')) or '',
            provider=clean_text(record.get('provider')),
            action=clean_text(record.get('action')) or 'unknown',
            health=clean_text(record.get('health')) or 'unknown',
            workspace_path=clean_text(record.get('workspace_path')),
            runtime_ref=clean_text(record.get('runtime_ref')),
            session_ref=clean_text(record.get('session_ref')),
            lifecycle_state=clean_text(record.get('lifecycle_state')),
            desired_state=clean_text(record.get('desired_state')),
            reconcile_state=clean_text(record.get('reconcile_state')),
            binding_source=clean_text(record.get('binding_source')),
            terminal_backend=clean_text(record.get('terminal_backend')),
            tmux_socket_name=clean_text(record.get('tmux_socket_name')),
            tmux_socket_path=clean_text(record.get('tmux_socket_path')),
            tmux_window_name=clean_text(record.get('tmux_window_name')),
            tmux_window_id=clean_text(record.get('tmux_window_id')),
            pane_id=clean_text(record.get('pane_id')),
            active_pane_id=clean_text(record.get('active_pane_id')),
            pane_state=clean_text(record.get('pane_state')),
            runtime_pid=coerce_int(record.get('runtime_pid')),
            runtime_root=clean_text(record.get('runtime_root')),
            failure_reason=clean_text(record.get('failure_reason')),
            binding_reject_reason=clean_text(record.get('binding_reject_reason')),
            duration_ms=_coerce_float(record.get('duration_ms')),
            provider_prepare_ms=_coerce_float(record.get('provider_prepare_ms')),
            provider_prepare_count=max(0, coerce_int(record.get('provider_prepare_count')) or 0),
            timings_ms=_clean_timings(record.get('timings_ms')),
        )


def _coerce_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return max(0.0, parsed)


def _clean_timings(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    timings: dict[str, float] = {}
    for key, raw_value in value.items():
        parsed = _coerce_float(raw_value)
        if parsed is not None:
            timings[str(key)] = parsed
    return timings


__all__ = ['CcbdStartupAgentResult']
