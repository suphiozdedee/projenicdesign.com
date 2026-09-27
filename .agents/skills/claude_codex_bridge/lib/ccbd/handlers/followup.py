from __future__ import annotations


def build_followup_handler(dispatcher):
    def handle(payload: dict) -> dict:
        job_id = str(payload.get('job_id') or '').strip()
        message = str(payload.get('message') or '').strip()
        if not job_id:
            raise ValueError('followup requires job_id')
        if not message:
            raise ValueError('followup requires a non-empty message')
        return dispatcher.followup(job_id, message)

    return handle


__all__ = ['build_followup_handler']
