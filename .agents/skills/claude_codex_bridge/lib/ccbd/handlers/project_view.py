from __future__ import annotations


def build_project_view_handler(project_view_service):
    def handle(payload: dict) -> dict:
        schema_version = int(payload.get('schema_version', 1))
        return project_view_service.build_response(schema_version=schema_version)

    return handle


def build_project_view_dismiss_comms_handler(project_view_state_store):
    def handle(payload: dict) -> dict:
        comms_id = str(payload.get('id') or payload.get('comms_id') or '').strip()
        state = project_view_state_store.dismiss_comms(comms_id)
        return {
            'status': 'dismissed',
            'id': comms_id,
            'dismissed_count': len(state.dismissed_comms),
        }

    return handle


__all__ = ['build_project_view_dismiss_comms_handler', 'build_project_view_handler']
