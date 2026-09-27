from __future__ import annotations

import re

from provider_core.protocol import (
    ANY_DONE_LINE_RE,
    DONE_PREFIX,
    REQ_ID_PREFIX,
    strip_done_text,
)


def wrap_pane_quiet_prompt(message: str, req_id: str) -> str:
    rendered = (message or "").rstrip()
    return (
        f"{REQ_ID_PREFIX} {req_id}\n\n"
        f"{rendered}\n\n"
        "IMPORTANT: when you finish answering, write this exact line on its "
        "own line as the final line of your reply (no quoting, no code fence):\n"
        f"{DONE_PREFIX} {req_id}\n"
    )


_LINE_PREFIX_RE = re.compile(r"^[\s>$#]+")
_ASSISTANT_UI_PREFIX_RE = re.compile(r"^•\s+")
_BANNER_KEYWORDS = ("CCB_REQ_ID:", "CCB_DONE:")
_BANNER_INSTRUCTIONS = (
    "IMPORTANT: when you finish",
    "IMPORTANT:",
    "on its own line as the final line",
    "no quoting, no code fence",
)


def _req_anchor_re(req_id: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(REQ_ID_PREFIX)}\s*{re.escape(req_id)}")


def pane_contains_req_anchor(text: str, req_id: str) -> bool:
    if not text or not req_id:
        return False
    return _req_anchor_re(req_id).search(text) is not None


def _done_anywhere_re(req_id: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(DONE_PREFIX)}\s*{re.escape(req_id)}")


def extract_reply_for_req(text: str, req_id: str) -> tuple[str, bool]:
    if not text or not req_id:
        return "", False

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    anchor_matches = list(_req_anchor_re(req_id).finditer(text))
    if not anchor_matches:
        return "", False

    after_anchor = text[anchor_matches[-1].end():]
    done_matches = list(_done_anywhere_re(req_id).finditer(after_anchor))
    if not done_matches:
        return "", False

    if len(done_matches) == 1:
        body = after_anchor[:done_matches[0].start()]
        if _contains_banner_fragment(body):
            return "", False
        cleaned = _clean_body(body, req_id)
        if cleaned and not _contains_banner_fragment(cleaned):
            return cleaned, True
        return "", False

    _, echo_line_end = _line_bounds(after_anchor, done_matches[-2].start())
    model_line_start, _ = _line_bounds(after_anchor, done_matches[-1].start())
    reply_start = echo_line_end + 1 if echo_line_end < len(after_anchor) else echo_line_end
    body = after_anchor[reply_start:model_line_start]

    cleaned = _clean_body(body, req_id)
    if _contains_banner_fragment(cleaned):
        return "", False
    return cleaned, True


def _line_bounds(text: str, pos: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return start, end


def _contains_banner_fragment(text: str) -> bool:
    blob = text or ""
    for marker in _BANNER_INSTRUCTIONS:
        if marker in blob:
            return True
    for marker in _BANNER_KEYWORDS:
        if marker in blob:
            return True
    return False


def _clean_body(body: str, req_id: str) -> str:
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    try:
        text = strip_done_text(text, req_id)
    except Exception:
        pass
    text = ANY_DONE_LINE_RE.sub("", text)

    cleaned_lines: list[str] = []
    for raw in text.split("\n"):
        stripped = _LINE_PREFIX_RE.sub("", raw).rstrip()
        if _is_banner_line(stripped):
            continue
        cleaned_lines.append(stripped)

    while cleaned_lines and not cleaned_lines[0].strip():
        cleaned_lines.pop(0)
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()

    for index, line in enumerate(cleaned_lines):
        if line.strip():
            cleaned_lines[index] = _ASSISTANT_UI_PREFIX_RE.sub("", line, count=1)
            break

    return "\n".join(cleaned_lines).strip()


def _is_banner_line(line: str) -> bool:
    text = (line or "").strip()
    if not text:
        return False
    for marker in _BANNER_KEYWORDS:
        if marker in text:
            return True
    for marker in _BANNER_INSTRUCTIONS:
        if marker in text:
            return True
    return False


__all__ = ["extract_reply_for_req", "pane_contains_req_anchor", "wrap_pane_quiet_prompt"]
