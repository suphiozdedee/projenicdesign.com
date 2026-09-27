from __future__ import annotations


def build_queue_handler(dispatcher):
    def handle(payload: dict) -> dict:
        target = str(payload.get('target') or 'all').strip()
        if not target:
            raise ValueError('queue requires target')
        detail = payload.get('detail')
        if detail is not None:
            detail = bool(detail)
        return dispatcher.queue(target, detail=detail)

    return handle
