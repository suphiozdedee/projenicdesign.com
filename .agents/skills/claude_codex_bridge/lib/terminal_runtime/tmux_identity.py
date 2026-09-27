from __future__ import annotations

from .tmux_theme import TmuxPaneVisual, pane_visual


def apply_ccb_pane_identity(
    backend,
    pane_id: str,
    *,
    title: str,
    agent_label: str,
    project_id: str,
    order_index: int | None = None,
    is_cmd: bool = False,
    role: str | None = None,
    slot_key: str | None = None,
    window_name: str | None = None,
    sidebar_instance: str | None = None,
    session_id: str | None = None,
    namespace_epoch: int | None = None,
    managed_by: str | None = 'ccbd',
) -> None:
    role_text = str(role or '').strip() or ('cmd' if is_cmd else 'agent')
    visual = pane_visual(
        project_id=project_id,
        slot_key=slot_key or title,
        order_index=order_index,
        is_cmd=is_cmd,
        role=role_text,
    )
    user_options = {
        '@ccb_label_style': visual.label_style,
        '@ccb_border_style': visual.border_style,
        '@ccb_active_border_style': visual.active_border_style,
        '@ccb_agent': agent_label,
        '@ccb_role': role_text,
    }
    if slot_key:
        user_options['@ccb_slot'] = slot_key
    if str(window_name or '').strip():
        user_options['@ccb_window'] = str(window_name).strip()
    if str(sidebar_instance or '').strip():
        user_options['@ccb_sidebar_instance'] = str(sidebar_instance).strip()
    if str(session_id or '').strip():
        user_options['@ccb_session_id'] = str(session_id).strip()
    if namespace_epoch is not None:
        user_options['@ccb_namespace_epoch'] = str(int(namespace_epoch))
    if str(managed_by or '').strip():
        user_options['@ccb_managed_by'] = str(managed_by).strip()
    user_options['@ccb_project_id'] = project_id
    batch_setter = getattr(backend, 'set_pane_identity', None)
    if callable(batch_setter):
        batch_setter(
            pane_id,
            title=title,
            user_options=user_options,
            border_style=visual.border_style,
            active_border_style=visual.active_border_style,
        )
        return
    backend.set_pane_title(pane_id, title)
    backend.set_pane_user_option(pane_id, '@ccb_label_style', visual.label_style)
    backend.set_pane_user_option(pane_id, '@ccb_border_style', visual.border_style)
    backend.set_pane_user_option(pane_id, '@ccb_active_border_style', visual.active_border_style)
    backend.set_pane_user_option(pane_id, '@ccb_agent', agent_label)
    backend.set_pane_user_option(pane_id, '@ccb_role', role_text)
    if slot_key:
        backend.set_pane_user_option(pane_id, '@ccb_slot', slot_key)
    if str(window_name or '').strip():
        backend.set_pane_user_option(pane_id, '@ccb_window', str(window_name).strip())
    if str(sidebar_instance or '').strip():
        backend.set_pane_user_option(pane_id, '@ccb_sidebar_instance', str(sidebar_instance).strip())
    if str(session_id or '').strip():
        backend.set_pane_user_option(pane_id, '@ccb_session_id', str(session_id).strip())
    if namespace_epoch is not None:
        backend.set_pane_user_option(pane_id, '@ccb_namespace_epoch', str(int(namespace_epoch)))
    if str(managed_by or '').strip():
        backend.set_pane_user_option(pane_id, '@ccb_managed_by', str(managed_by).strip())
    backend.set_pane_user_option(pane_id, '@ccb_project_id', project_id)
    setter = getattr(backend, 'set_pane_style', None)
    if callable(setter):
        setter(
            pane_id,
            border_style=visual.border_style,
            active_border_style=visual.active_border_style,
        )


__all__ = ['TmuxPaneVisual', 'apply_ccb_pane_identity', 'pane_visual']
