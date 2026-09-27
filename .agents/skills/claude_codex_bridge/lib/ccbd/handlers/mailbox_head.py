from __future__ import annotations


def build_mailbox_head_handler(dispatcher):
    def handle(payload: dict) -> dict:
        agent_name = str(payload.get('agent_name') or '').strip()
        if not agent_name:
            raise ValueError('mailbox_head requires agent_name')
        return dispatcher.mailbox_head(agent_name)

    return handle
