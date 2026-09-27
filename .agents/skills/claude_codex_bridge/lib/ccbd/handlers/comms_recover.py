from __future__ import annotations


def build_comms_recover_handler(dispatcher):
    def handle(payload: dict) -> dict:
        return dispatcher.comms_recover(payload)

    return handle


__all__ = ['build_comms_recover_handler']
