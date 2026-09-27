from __future__ import annotations

from pathlib import Path

from ccbd.socket_client import CcbdClient, CcbdClientError

from .frontdesk_intake import frontdesk_intake


def frontdesk_intake_command(context, command, services=None) -> dict[str, object]:
    socket_path = Path(context.paths.ccbd_socket_path)
    if not socket_path.exists():
        return frontdesk_intake(context, command, services)
    try:
        return CcbdClient(socket_path).frontdesk_forward_planner(
            plan_slug=getattr(command, 'plan_slug', None),
            request_id=getattr(command, 'request_id', None),
            file_path=getattr(command, 'file_path', None),
            intake_base64=getattr(command, 'intake_base64', None),
            intake_text=getattr(command, 'intake_text', ''),
            json_output=bool(getattr(command, 'json_output', False)),
        )
    except CcbdClientError as exc:
        return {
            'schema_version': 1,
            'record_type': 'ccb_frontdesk_intake',
            'frontdesk_intake_status': 'blocked',
            'project_id': context.project.project_id,
            'project_root': str(context.project.project_root),
            'action': 'rejected',
            'reason': 'frontdesk_daemon_forward_failed',
            'evidence': {
                'socket_path': str(socket_path),
                'error': str(exc),
            },
            'next_activation': 'inspect',
        }


__all__ = ['frontdesk_intake_command']
