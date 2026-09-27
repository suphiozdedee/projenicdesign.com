from __future__ import annotations


def build_inbox_handler(dispatcher):
    def handle(payload: dict) -> dict:
        agent_name = str(payload.get('agent_name') or '').strip()
        if not agent_name:
            raise ValueError('inbox requires agent_name')
        detail = payload.get('detail')
        if detail is not None:
            detail = bool(detail)
        return dispatcher.inbox(agent_name, detail=detail)

    return handle
