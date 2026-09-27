from __future__ import annotations

from .ensure_context import load_namespace_context, refresh_session_liveness
from .ensure_identity import prepare_namespace_root_pane
from .ensure_state import (
    build_created_namespace,
    force_recreate_namespace,
    persist_refreshed_namespace,
    recreate_for_layout_change,
)
from .materialize_topology import (
    existing_topology_agent_panes,
    existing_topology_cmd_pane,
    materialize_topology,
    refresh_topology_ui_for_project,
    topology_active_panes,
    topology_recreate_reason,
)
from ccbd.services.project_namespace_pane import snapshot_project_namespace_panes


def ensure_project_namespace(
    controller,
    *,
    layout_signature: str | None = None,
    topology_plan=None,
    force_recreate: bool = False,
    recreate_reason: str | None = None,
    session_probe_timeout_s: float | None = None,
    terminal_size: tuple[int, int] | None = None,
) -> object:
    controller._layout.ccbd_dir.mkdir(parents=True, exist_ok=True)
    context = load_namespace_context(
        controller,
        layout_signature=layout_signature,
        topology_plan=topology_plan,
        recreate_reason=recreate_reason,
    )
    context = refresh_session_liveness(
        controller,
        context,
        timeout_s=session_probe_timeout_s,
    )
    pane_records = (
        snapshot_project_namespace_panes(context.backend)
        if topology_plan is not None and context.session_is_alive and context.current is not None
        else None
    )

    if force_recreate:
        context = force_recreate_namespace(controller, context)
    context = recreate_for_layout_change(controller, context)
    if topology_plan is not None and context.session_is_alive and context.current is not None:
        reason = topology_recreate_reason(
            controller,
            context,
            topology_plan=topology_plan,
            pane_records=pane_records,
        )
        if reason is not None:
            context = force_recreate_namespace(
                controller,
                context.with_updates(recreate_cause=reason),
            )
            pane_records = None

    if context.session_is_alive and context.current is not None:
        if topology_plan is not None:
            agent_panes = existing_topology_agent_panes(
                controller,
                context,
                topology_plan=topology_plan,
                pane_records=pane_records,
                include_dead=True,
            )
            cmd_pane = existing_topology_cmd_pane(
                controller,
                context,
                topology_plan=topology_plan,
                pane_records=pane_records,
            )
            refresh_topology_ui_for_project(
                controller,
                context,
                topology_plan=topology_plan,
                timeout_s=session_probe_timeout_s,
                pane_records=pane_records,
            )
            setattr(controller, '_last_materialized_agent_panes', agent_panes)
            setattr(controller, '_last_materialized_cmd_pane', cmd_pane)
            setattr(
                controller,
                '_last_topology_active_panes',
                topology_active_panes(
                    controller,
                    context,
                    topology_plan=topology_plan,
                    pane_records=pane_records,
                ),
            )
            setattr(controller, '_last_topology_pane_records', pane_records)
        else:
            setattr(controller, '_last_materialized_agent_panes', {})
            setattr(controller, '_last_materialized_cmd_pane', None)
            setattr(controller, '_last_topology_active_panes', ())
            setattr(controller, '_last_topology_pane_records', {})
        return persist_refreshed_namespace(
            controller,
            context,
            timeout_s=session_probe_timeout_s,
        )

    epoch = context.current.namespace_epoch + 1 if context.current is not None else 1
    if topology_plan is not None:
        agent_panes, cmd_pane = materialize_topology(
            controller,
            context,
            topology_plan=topology_plan,
            epoch=epoch,
            terminal_size=terminal_size,
            timeout_s=session_probe_timeout_s,
        )
        pane_records = snapshot_project_namespace_panes(context.backend)
        if cmd_pane is None:
            cmd_pane = existing_topology_cmd_pane(
                controller,
                context,
                topology_plan=topology_plan,
                pane_records=pane_records,
                namespace_epoch=epoch,
            )
        setattr(controller, '_last_materialized_agent_panes', agent_panes)
        setattr(controller, '_last_materialized_cmd_pane', cmd_pane)
        setattr(
            controller,
            '_last_topology_active_panes',
            topology_active_panes(
                controller,
                context,
                topology_plan=topology_plan,
                pane_records=pane_records,
                namespace_epoch=epoch,
            ),
        )
        setattr(controller, '_last_topology_pane_records', pane_records)
        # topology materialize 成功即证明 Herdr 会话 socket 可用
        setattr(controller, '_last_namespace_socket_verified', True)
    else:
        verified = prepare_namespace_root_pane(
            controller,
            context,
            epoch=epoch,
            terminal_size=terminal_size,
            timeout_s=session_probe_timeout_s,
        )
        setattr(controller, '_last_namespace_socket_verified', bool(verified))
        setattr(controller, '_last_materialized_agent_panes', {})
        setattr(controller, '_last_materialized_cmd_pane', None)
        setattr(controller, '_last_topology_active_panes', ())
        setattr(controller, '_last_topology_pane_records', {})
    return build_created_namespace(
        controller,
        context,
        timeout_s=session_probe_timeout_s,
    )


__all__ = ['ensure_project_namespace']
