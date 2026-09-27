from __future__ import annotations

from sidebar_click_targets import resolve_sidebar_click_target


def build_project_focus_window_handler(project_focus_service):
    def handle(payload: dict) -> dict:
        return project_focus_service.focus_window(
            window=str(payload.get('window') or ''),
            namespace_epoch=_optional_int(payload.get('namespace_epoch')),
        )

    return handle


def build_project_focus_agent_handler(project_focus_service):
    def handle(payload: dict) -> dict:
        return project_focus_service.focus_agent(
            agent=str(payload.get('agent') or ''),
            namespace_epoch=_optional_int(payload.get('namespace_epoch')),
        )

    return handle


def build_project_sidebar_click_handler(project_view_service, project_focus_service):
    def handle(payload: dict) -> dict:
        view_payload = project_view_service.build_response(
            schema_version=_optional_int(payload.get('schema_version')) or 1,
        )
        view = view_payload.get('view') if isinstance(view_payload, dict) else None
        if not isinstance(view, dict):
            return {'focused': False, 'target': None}
        target = resolve_sidebar_click_target(
            view,
            mouse_y=int(payload.get('mouse_y') or 0),
            pane_top=int(payload.get('pane_top') or 0),
            pane_height=int(payload.get('pane_height') or 0),
        )
        if target is None:
            return {'focused': False, 'target': None}
        kind, name = target
        namespace = view.get('namespace') if isinstance(view.get('namespace'), dict) else {}
        namespace_epoch = namespace.get('epoch') if isinstance(namespace, dict) else None
        if kind == 'window':
            result = project_focus_service.focus_window(
                window=name,
                namespace_epoch=_optional_int(namespace_epoch),
            )
        else:
            result = project_focus_service.focus_agent(
                agent=name,
                namespace_epoch=_optional_int(namespace_epoch),
            )
        response = dict(result)
        response['target'] = f'{kind}:{name}'
        return response

    return handle


def _optional_int(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    return int(text) if text else None


__all__ = [
    'build_project_focus_agent_handler',
    'build_project_focus_window_handler',
    'build_project_sidebar_click_handler',
]
