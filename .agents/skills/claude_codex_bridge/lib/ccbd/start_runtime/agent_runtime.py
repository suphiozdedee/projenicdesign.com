from __future__ import annotations

import math
from time import monotonic_ns

from ccbd.models import CcbdStartupAgentResult
from terminal_runtime.tmux_identity import pane_visual

from .agent_runtime_binding import resolve_runtime_binding_state
from .agent_runtime_models import StartAgentExecution


_AGENT_TIMING_FIELDS = (
    'prepare_launch_context',
    'build_start_cmd',
    'tmux_respawn',
    'pane_identity',
    'pane_agent_report',
    'session_write',
    'provider_post_launch',
    'binding_resolve',
    'pane_and_runtime_facts',
    'authority_commit',
    'restore_bookkeeping',
)


def _namespace_assigned_pane_id(
    *,
    assigned_pane_id: str | None,
    stale_binding: bool,
    tmux_socket_path: str | None,
) -> str | None:
    if stale_binding:
        return None
    pane_id = str(assigned_pane_id or '').strip()
    if not pane_id or not pane_id.startswith('%'):
        return None
    if not str(tmux_socket_path or '').strip():
        return None
    return pane_id


def _binding_attr(binding, field_name: str):
    return getattr(binding, field_name, None) if binding is not None else None


def _best_effort_resolve_pane_pid(*, pane_id, namespace_backend_impl, resolver) -> int | None:
    """方向 A：herdr pane-backed agent 回填 runtime_pid。

    binding.runtime_pid 来自 provider session（herdr pane respawn 不经
    provider session，故为 None）。用注入的 resolver 从 herdr ``pane
    process-info`` 查询 pane 前台进程 pid。best-effort：任何失败返回 None。

    ``pane_id`` 优先用 binding_state.runtime_pane_id（herdr 实际 pane）；
    binding.pane_id 在 herdr 下可能为空（binding 不经 provider session）。
    """
    if resolver is None:
        return None
    if str(namespace_backend_impl or '').strip().lower() != 'herdr':
        return None
    if not pane_id:
        return None
    try:
        pid = resolver(pane_id)
    except Exception:
        return None
    if pid is None:
        return None
    try:
        return int(pid)
    except (TypeError, ValueError):
        return None


def _runtime_ref_prefix(
    namespace_backend_impl: str | None,
    assigned_pane_ref: dict[str, object] | None,
) -> str:
    """按后端选择 runtime_ref 前缀（design I-2）。

    tmux/rmux → ``tmux:``，herdr → ``mux:``。
    优先取 namespace_backend_impl，其次 assigned_pane_ref['backend_impl']。
    """
    backend = str(namespace_backend_impl or '').strip().lower()
    if not backend and isinstance(assigned_pane_ref, dict):
        backend = str(assigned_pane_ref.get('backend_impl') or '').strip().lower()
    if backend == 'herdr':
        return 'mux'
    return 'tmux'


def start_agent_runtime(
    *,
    context,
    command,
    runtime_service,
    agent_name: str,
    spec,
    plan,
    binding,
    raw_binding,
    stale_binding: bool,
    assigned_pane_id: str | None,
    assigned_pane_ref: dict[str, object] | None = None,
    namespace_ref: dict[str, object] | None = None,
    style_index: int,
    project_id: str,
    tmux_socket_path: str | None,
    namespace_backend_impl: str | None = None,
    namespace_epoch: int | None,
    ensure_agent_runtime_fn,
    launch_binding_hint_fn,
    relabel_project_namespace_pane_fn,
    same_tmux_socket_path_fn,
    workspace_window_id: str | None = None,
    workspace_epoch: int | None = None,
    window_name: str | None = None,
    namespace_pane_records: dict[str, object] | None = None,
    provider_prepared: bool = False,
    effective_command=None,
    provider_prepare_ms: float = 0.0,
    binding_reject_reason: str | None = None,
    pane_runtime_pid_resolver=None,
) -> StartAgentExecution:
    started_ns = monotonic_ns()
    timings_ms: dict[str, float] = {}
    facts_started_ns = monotonic_ns()
    try:
        original_pane_id = _binding_attr(binding, 'pane_id') or _binding_attr(binding, 'active_pane_id')
        original_namespace_record = (namespace_pane_records or {}).get(str(original_pane_id or ''))
        reused_pane_identity_current = _pane_identity_is_current(
            original_namespace_record,
            binding=binding,
            agent_name=agent_name,
            project_id=project_id,
            style_index=style_index,
            window_name=window_name,
            namespace_epoch=namespace_epoch,
        )
        _record_elapsed_ms(timings_ms, 'pane_and_runtime_facts', facts_started_ns)
        binding_state = resolve_runtime_binding_state(
            context=context,
            command=command,
            agent_name=agent_name,
            spec=spec,
            plan=plan,
            binding=binding,
            raw_binding=raw_binding,
            stale_binding=stale_binding,
            assigned_pane_id=assigned_pane_id,
            assigned_pane_ref=assigned_pane_ref,
            namespace_ref=namespace_ref,
            style_index=style_index,
            project_id=project_id,
            tmux_socket_path=tmux_socket_path,
            namespace_backend_impl=namespace_backend_impl,
            namespace_epoch=namespace_epoch,
            window_name=window_name,
            ensure_agent_runtime_fn=ensure_agent_runtime_fn,
            launch_binding_hint_fn=launch_binding_hint_fn,
            relabel_project_namespace_pane_fn=relabel_project_namespace_pane_fn,
            same_tmux_socket_path_fn=same_tmux_socket_path_fn,
            provider_prepared=provider_prepared,
            effective_command=effective_command,
            reused_pane_identity_current=reused_pane_identity_current,
            timings_ms=timings_ms,
        )
    except Exception as exc:
        _record_failed_agent_result(
            exc,
            started_ns=started_ns,
            timings_ms=timings_ms,
            agent_name=agent_name,
            spec=spec,
            plan=plan,
            binding_reject_reason=binding_reject_reason,
            provider_prepare_ms=provider_prepare_ms,
            provider_prepared=provider_prepared,
        )
        raise
    facts_started_ns = monotonic_ns()
    try:
        registry = getattr(runtime_service, '_registry', None)
        existing = registry.get(agent_name) if registry is not None else None
        reused_existing_binding = binding is not None and binding_state.agent_action == 'attached'
        preserve_existing_success_health = bool(
            reused_existing_binding
            and existing is not None
            and getattr(existing, 'health', None) in {'healthy', 'restored'}
        )
        namespace_pane_id = _namespace_assigned_pane_id(
            assigned_pane_id=assigned_pane_id,
            stale_binding=stale_binding,
            tmux_socket_path=tmux_socket_path,
        )
        namespace_runtime_ref = (
            f'{_runtime_ref_prefix(namespace_backend_impl, assigned_pane_ref)}:{namespace_pane_id}'
            if namespace_pane_id else None
        )
        namespace_socket_path = str(tmux_socket_path or '').strip() or None
        binding_pane_id = _binding_attr(binding_state.binding, 'pane_id') or namespace_pane_id
        namespace_record = (namespace_pane_records or {}).get(str(binding_pane_id or ''))
        tmux_window_id = (
            getattr(namespace_record, 'window_id', None)
            or _binding_attr(binding_state.binding, 'tmux_window_id')
        )
        tmux_window_name = (
            window_name
            or getattr(namespace_record, 'ccb_window', None)
            or getattr(namespace_record, 'window_name', None)
            or _binding_attr(binding_state.binding, 'tmux_window_name')
        )
        attach_kwargs = dict(
            agent_name=agent_name,
            workspace_path=str(plan.workspace_path),
            backend_type=spec.runtime_mode.value,
            runtime_ref=binding_state.runtime_ref or namespace_runtime_ref,
            session_ref=binding_state.session_ref,
            health=None if preserve_existing_success_health else binding_state.health,
            provider=spec.provider,
            runtime_root=_binding_attr(binding_state.binding, 'runtime_root'),
            runtime_pid=(
                _binding_attr(binding_state.binding, 'runtime_pid')
                or _best_effort_resolve_pane_pid(
                    pane_id=(
                        binding_state.runtime_pane_id
                        or _binding_attr(binding_state.binding, 'pane_id')
                        or _binding_attr(binding_state.binding, 'active_pane_id')
                    ),
                    namespace_backend_impl=namespace_backend_impl,
                    resolver=pane_runtime_pid_resolver,
                )
            ),
            terminal_backend=(
                _binding_attr(binding_state.binding, 'terminal')
                or ('tmux' if namespace_pane_id else None)
            ),
            pane_id=_binding_attr(binding_state.binding, 'pane_id') or namespace_pane_id,
            active_pane_id=(
                _binding_attr(binding_state.binding, 'active_pane_id') or namespace_pane_id
            ),
            pane_title_marker=_binding_attr(binding_state.binding, 'pane_title_marker'),
            pane_state=_binding_attr(binding_state.binding, 'pane_state'),
            tmux_socket_name=_binding_attr(binding_state.binding, 'tmux_socket_name'),
            tmux_socket_path=_binding_attr(binding_state.binding, 'tmux_socket_path') or (
                namespace_socket_path if namespace_pane_id else None
            ),
            tmux_window_name=tmux_window_name,
            tmux_window_id=tmux_window_id,
            session_file=_binding_attr(binding_state.binding, 'session_file'),
            session_id=_binding_attr(binding_state.binding, 'session_id'),
            slot_key=agent_name,
            window_id=tmux_window_id or (workspace_window_id if window_name is None else None),
            workspace_epoch=workspace_epoch,
            lifecycle_state=binding_state.lifecycle_state,
            managed_by='ccbd',
            binding_source='provider-session',
        )
        attempt_id = str(getattr(existing, 'mount_attempt_id', '') or '').strip() or None
    except Exception as exc:
        _record_elapsed_ms(timings_ms, 'pane_and_runtime_facts', facts_started_ns)
        _record_failed_agent_result(
            exc,
            started_ns=started_ns,
            timings_ms=timings_ms,
            agent_name=agent_name,
            spec=spec,
            plan=plan,
            binding_reject_reason=binding_reject_reason,
            provider_prepare_ms=provider_prepare_ms,
            provider_prepared=provider_prepared,
        )
        raise
    _record_elapsed_ms(timings_ms, 'pane_and_runtime_facts', facts_started_ns)

    attach_started_ns = monotonic_ns()
    try:
        warm_reuse_without_store_write = (
            reused_existing_binding
            and preserve_existing_success_health
            and existing is not None
            and _existing_runtime_matches_attach(existing, attach_kwargs)
        )
        if warm_reuse_without_store_write:
            runtime = existing
        elif attempt_id and getattr(existing, 'reconcile_state', None) == 'starting':
            runtime, applied = runtime_service.attach_mount_attempt_authority(
                attempt_id=attempt_id,
                **attach_kwargs,
            )
            if not applied:
                raise RuntimeError(
                    f'startup mount attempt authority was superseded for {agent_name}'
                )
        else:
            runtime = runtime_service.attach(**attach_kwargs)
    except Exception as exc:
        _record_elapsed_ms(timings_ms, 'authority_commit', attach_started_ns)
        _record_failed_agent_result(
            exc,
            started_ns=started_ns,
            timings_ms=timings_ms,
            agent_name=agent_name,
            spec=spec,
            plan=plan,
            binding_reject_reason=binding_reject_reason,
            provider_prepare_ms=provider_prepare_ms,
            provider_prepared=provider_prepared,
        )
        raise
    _record_elapsed_ms(timings_ms, 'authority_commit', attach_started_ns)

    restore_started_ns = monotonic_ns()
    actions_taken = list(binding_state.actions_taken)
    try:
        if command.restore and binding_state.agent_action != 'degraded' and not reused_existing_binding:
            runtime_service.restore(agent_name)
            actions_taken.append(f'restore_runtime:{agent_name}')
            refreshed_runtime = registry.get(agent_name) if registry is not None else None
            if (
                refreshed_runtime is not None
                and getattr(refreshed_runtime, 'health', None) == 'restored'
                and getattr(refreshed_runtime, 'runtime_ref', None) == runtime.runtime_ref
                and getattr(refreshed_runtime, 'session_ref', None) == runtime.session_ref
            ):
                runtime = refreshed_runtime
    except Exception as exc:
        _record_elapsed_ms(timings_ms, 'restore_bookkeeping', restore_started_ns)
        _record_failed_agent_result(
            exc,
            started_ns=started_ns,
            timings_ms=timings_ms,
            agent_name=agent_name,
            spec=spec,
            plan=plan,
            binding_reject_reason=binding_reject_reason,
            provider_prepare_ms=provider_prepare_ms,
            provider_prepared=provider_prepared,
        )
        raise
    _record_elapsed_ms(timings_ms, 'restore_bookkeeping', restore_started_ns)

    try:
        result_fields = dict(
            agent_name=agent_name,
            provider=spec.provider,
            action=binding_state.agent_action,
            health=getattr(runtime, 'health', None) or binding_state.health,
            workspace_path=str(plan.workspace_path),
            runtime_ref=runtime.runtime_ref,
            session_ref=runtime.session_ref,
            lifecycle_state=runtime.lifecycle_state,
            desired_state=runtime.desired_state,
            reconcile_state=runtime.reconcile_state,
            binding_source=runtime.binding_source.value,
            terminal_backend=runtime.terminal_backend,
            tmux_socket_name=runtime.tmux_socket_name,
            tmux_socket_path=runtime.tmux_socket_path,
            tmux_window_name=runtime.tmux_window_name,
            tmux_window_id=runtime.tmux_window_id,
            pane_id=runtime.pane_id,
            active_pane_id=runtime.active_pane_id,
            pane_state=runtime.pane_state,
            runtime_pid=runtime.runtime_pid,
            runtime_root=runtime.runtime_root,
            failure_reason='stale_binding_unresolved' if binding_state.agent_action == 'degraded' else None,
            binding_reject_reason=(
                binding_reject_reason
                if binding_state.agent_action in {'launched', 'relaunched', 'degraded'}
                else None
            ),
        )
        duration_ms = (monotonic_ns() - started_ns) / 1_000_000
        _finish_agent_timings(timings_ms, duration_ms=duration_ms)
        return StartAgentExecution(
            agent_result=CcbdStartupAgentResult(
                **result_fields,
                duration_ms=duration_ms,
                provider_prepare_ms=provider_prepare_ms,
                provider_prepare_count=int(bool(provider_prepared)),
                timings_ms=timings_ms,
            ),
            actions_taken=tuple(actions_taken),
            socket_name=binding_state.socket_name or (namespace_socket_path if namespace_pane_id else None),
            runtime_pane_id=binding_state.runtime_pane_id or namespace_pane_id,
            project_socket_active_pane_id=(
                binding_state.project_socket_active_pane_id or namespace_pane_id
            ),
        )
    except Exception as exc:
        _record_failed_agent_result(
            exc,
            started_ns=started_ns,
            timings_ms=timings_ms,
            agent_name=agent_name,
            spec=spec,
            plan=plan,
            binding_reject_reason=binding_reject_reason,
            provider_prepare_ms=provider_prepare_ms,
            provider_prepared=provider_prepared,
        )
        raise


def _pane_identity_is_current(
    record,
    *,
    binding,
    agent_name: str,
    project_id: str,
    style_index: int,
    window_name: str | None,
    namespace_epoch: int | None,
) -> bool:
    if record is None or binding is None:
        return False
    visual = pane_visual(
        project_id=project_id,
        slot_key=agent_name,
        order_index=style_index,
        is_cmd=False,
        role='agent',
    )
    expected = {
        'pane_title': agent_name,
        'agent_label': agent_name,
        'role': 'agent',
        'slot_key': agent_name,
        'project_id': project_id,
        'managed_by': 'ccbd',
        'label_style': visual.label_style,
        'border_style': visual.border_style,
        'active_border_style': visual.active_border_style,
    }
    if any(str(getattr(record, field, '') or '').strip() != value for field, value in expected.items()):
        return False
    if window_name is not None and str(getattr(record, 'ccb_window', '') or '').strip() != window_name:
        return False
    if namespace_epoch is not None and getattr(record, 'namespace_epoch', None) != int(namespace_epoch):
        return False
    ccb_session_id = str(getattr(binding, 'ccb_session_id', '') or '').strip()
    if ccb_session_id and str(getattr(record, 'ccb_session_id', '') or '').strip() != ccb_session_id:
        return False
    return True


def _record_elapsed_ms(timings_ms: dict[str, float], field_name: str, started_ns: int) -> None:
    elapsed_ms = max(0.0, (monotonic_ns() - started_ns) / 1_000_000)
    timings_ms[field_name] = timings_ms.get(field_name, 0.0) + elapsed_ms


def _existing_runtime_matches_attach(existing, attach_kwargs: dict[str, object]) -> bool:
    comparable_fields = (
        'agent_name',
        'workspace_path',
        'backend_type',
        'runtime_ref',
        'session_ref',
        'provider',
        'runtime_root',
        'runtime_pid',
        'terminal_backend',
        'pane_id',
        'active_pane_id',
        'pane_title_marker',
        'pane_state',
        'tmux_socket_name',
        'tmux_socket_path',
        'tmux_window_name',
        'tmux_window_id',
        'session_file',
        'session_id',
        'slot_key',
        'window_id',
        'workspace_epoch',
        'lifecycle_state',
        'managed_by',
    )
    for field_name in comparable_fields:
        if _attach_compare_text(getattr(existing, field_name, None)) != _attach_compare_text(attach_kwargs.get(field_name)):
            return False
    return True


def _attach_compare_text(value: object) -> str:
    return str(value or '').replace('\\', '/')


def _finish_agent_timings(timings_ms: dict[str, float], *, duration_ms: float) -> None:
    # Keep the persisted schema explicit.  A missing stage is ambiguous to
    # downstream benchmark tooling, while a zero means that the stage did not
    # execute for this reuse/launch/failure path.
    for field_name in _AGENT_TIMING_FIELDS:
        timings_ms.setdefault(field_name, 0.0)
    attributed_ms = sum(
        value for field_name, value in timings_ms.items()
        if field_name != 'unattributed'
    )
    timings_ms['unattributed'] = max(0.0, duration_ms - attributed_ms)
    total_ms = sum(timings_ms.values())
    if total_ms > duration_ms and timings_ms['unattributed'] > 0:
        timings_ms['unattributed'] = max(
            0.0,
            timings_ms['unattributed'] - (total_ms - duration_ms) - math.ulp(duration_ms),
        )


def _attach_failed_agent_result(exc: Exception, result: CcbdStartupAgentResult) -> None:
    try:
        setattr(exc, 'ccb_startup_agent_result', result)
        setattr(exc, 'ccb_startup_timings_ms', dict(result.timings_ms or {}))
    except Exception:
        return


def _record_failed_agent_result(
    exc: Exception,
    *,
    started_ns: int,
    timings_ms: dict[str, float],
    agent_name: str,
    spec,
    plan,
    binding_reject_reason: str | None,
    provider_prepare_ms: float,
    provider_prepared: bool,
) -> None:
    duration_ms = max(0.0, (monotonic_ns() - started_ns) / 1_000_000)
    _finish_agent_timings(timings_ms, duration_ms=duration_ms)
    _attach_failed_agent_result(
        exc,
        CcbdStartupAgentResult(
            agent_name=agent_name,
            provider=spec.provider,
            action='failed',
            health='failed',
            workspace_path=str(plan.workspace_path),
            failure_reason=str(exc) or type(exc).__name__,
            binding_reject_reason=binding_reject_reason,
            duration_ms=duration_ms,
            provider_prepare_ms=provider_prepare_ms,
            provider_prepare_count=int(bool(provider_prepared)),
            timings_ms=dict(timings_ms),
        ),
    )


__all__ = ['start_agent_runtime']
