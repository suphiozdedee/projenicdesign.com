from __future__ import annotations

from provider_core.protocol import REQ_ID_PREFIX


def wrap_mimo_prompt(message: str, req_id: str) -> str:
    message = (message or "").rstrip()
    return f"{REQ_ID_PREFIX} {req_id}\n\n{message}\n"


__all__ = ["wrap_mimo_prompt"]
