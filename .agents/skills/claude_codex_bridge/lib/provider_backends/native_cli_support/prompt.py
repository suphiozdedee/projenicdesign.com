from __future__ import annotations

import re

from provider_core.protocol import ANY_DONE_LINE_RE, REQ_ID_PREFIX, strip_done_text


_ASSISTANT_UI_PREFIX_RE = re.compile(r"^•\s+")


def wrap_native_prompt(message: str, req_id: str) -> str:
    rendered = (message or "").rstrip()
    return f"{REQ_ID_PREFIX} {req_id}\n\n{rendered}\n"


def clean_native_reply(text: str, req_id: str) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if req_id:
        try:
            cleaned = strip_done_text(cleaned, req_id)
        except Exception:
            pass
    cleaned = ANY_DONE_LINE_RE.sub("", cleaned)
    lines = [line.rstrip() for line in cleaned.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    for index, line in enumerate(lines):
        if line.strip():
            lines[index] = _ASSISTANT_UI_PREFIX_RE.sub("", line, count=1)
            break
    return "\n".join(lines).strip()
