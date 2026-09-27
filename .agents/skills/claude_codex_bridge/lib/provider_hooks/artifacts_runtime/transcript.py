from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from provider_core.protocol import ANY_REQ_ID_PATTERN, REQ_ID_BOUNDARY_PATTERN

REQ_ID_RE = re.compile(rf'CCB_REQ_ID:\s*({ANY_REQ_ID_PATTERN}){REQ_ID_BOUNDARY_PATTERN}', re.IGNORECASE)
OUTER_REQ_ID_RE = re.compile(rf'^\s*CCB_REQ_ID:\s*({ANY_REQ_ID_PATTERN}){REQ_ID_BOUNDARY_PATTERN}', re.IGNORECASE)


def extract_req_id(text: str) -> str | None:
    match = REQ_ID_RE.search(str(text or ''))
    if not match:
        return None
    return str(match.group(1) or '').strip() or None


def extract_outer_req_id(text: str) -> str | None:
    match = OUTER_REQ_ID_RE.search(str(text or ''))
    if not match:
        return None
    return str(match.group(1) or '').strip() or None


def latest_req_id_from_transcript(transcript_path: str | Path | None) -> str | None:
    raw = str(transcript_path or '').strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return None
    user_req_id = latest_user_req_id_from_transcript_text(content)
    if user_req_id:
        return user_req_id
    prompt_req_id = latest_last_prompt_req_id_from_transcript_text(content)
    if prompt_req_id:
        return prompt_req_id
    return extract_outer_req_id(content)


def current_turn_req_id_from_transcript(
    transcript_path: str | Path | None,
    *,
    assistant_reply: str | None = None,
) -> str | None:
    raw = str(transcript_path or '').strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return None
    return current_turn_req_id_from_transcript_text(content, assistant_reply=assistant_reply)


def current_turn_req_id_from_transcript_text(content: str, *, assistant_reply: str | None = None) -> str | None:
    records = _parse_jsonl_records(content)
    if not records:
        return None
    if not str(assistant_reply or '').strip():
        return _empty_reply_turn_req_id(records, content)
    assistant_index = _current_assistant_index(records, assistant_reply=assistant_reply)
    if assistant_index is None:
        has_assistant = any(_is_assistant_record(record) for record in records)
        if has_assistant:
            return None
        return latest_req_id_from_transcript_text(content)
    return _req_id_for_assistant_turn(records[assistant_index], _uuid_index(records))


def latest_req_id_from_transcript_text(content: str) -> str | None:
    prompt_record_req_id = _latest_prompt_record_req_id_from_transcript_text(content)
    if prompt_record_req_id:
        return prompt_record_req_id
    prompt_req_id = latest_last_prompt_req_id_from_transcript_text(content)
    if prompt_req_id:
        return prompt_req_id
    return extract_outer_req_id(content)


def latest_user_req_id_from_transcript_text(content: str) -> str | None:
    latest: str | None = None
    for record in _parse_jsonl_records(content):
        if _is_tool_result_user_record(record):
            continue
        text = _user_message_text(record)
        if text is None:
            continue
        req_id = extract_outer_req_id(text)
        if req_id:
            latest = req_id
    return latest


def _latest_prompt_record_req_id_from_transcript_text(content: str) -> str | None:
    latest: str | None = None
    for record in _parse_jsonl_records(content):
        text = _prompt_record_text(record)
        if text is None:
            continue
        req_id = extract_outer_req_id(text)
        if req_id:
            latest = req_id
    return latest


def latest_last_prompt_req_id_from_transcript_text(content: str) -> str | None:
    latest: str | None = None
    for line in str(content or '').splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get('type') or '').strip().lower() != 'last-prompt':
            continue
        req_id = extract_outer_req_id(str(record.get('lastPrompt') or ''))
        if req_id:
            latest = req_id
    return latest


def _parse_jsonl_records(content: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in str(content or '').splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _uuid_index(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        uuid = str(record.get('uuid') or '').strip()
        if uuid:
            indexed[uuid] = record
    return indexed


def _current_assistant_index(records: list[dict[str, Any]], *, assistant_reply: str | None) -> int | None:
    expected = str(assistant_reply or '').strip()
    if expected:
        for index in range(len(records) - 1, -1, -1):
            record = records[index]
            if not _is_assistant_record(record):
                continue
            if _assistant_message_text(record).strip() == expected:
                return index
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if _is_assistant_record(record) and _assistant_message_text(record).strip():
            return index
    return None


def _empty_reply_turn_req_id(records: list[dict[str, Any]], content: str) -> str | None:
    latest_ccb_prompt: tuple[int, str] | None = None
    latest_assistant_index: int | None = None
    for index, record in enumerate(records):
        if _is_assistant_record(record):
            latest_assistant_index = index
        text = _prompt_record_text(record)
        if text is None:
            continue
        req_id = extract_outer_req_id(text)
        if req_id:
            latest_ccb_prompt = (index, req_id)
    if latest_ccb_prompt is not None and (
        latest_assistant_index is None or latest_ccb_prompt[0] > latest_assistant_index
    ):
        return latest_ccb_prompt[1]
    if latest_assistant_index is None:
        return latest_req_id_from_transcript_text(content)
    return None


def _req_id_for_assistant_turn(record: dict[str, Any], indexed: dict[str, dict[str, Any]]) -> str | None:
    parent_uuid = str(record.get('parentUuid') or '').strip()
    seen: set[str] = set()
    while parent_uuid and parent_uuid not in seen:
        seen.add(parent_uuid)
        parent = indexed.get(parent_uuid)
        if not parent:
            return None
        if _is_tool_result_user_record(parent):
            parent_uuid = str(parent.get('parentUuid') or '').strip()
            continue
        text = _prompt_record_text(parent)
        if text is not None:
            return extract_outer_req_id(text)
        parent_uuid = str(parent.get('parentUuid') or '').strip()
    return None


def _prompt_record_text(record: dict[str, Any]) -> str | None:
    if _is_tool_result_user_record(record):
        return None
    user_text = _user_message_text(record)
    if user_text is not None:
        return user_text
    return _queue_operation_text(record)


def _queue_operation_text(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    if str(record.get('type') or '').strip().lower() != 'queue-operation':
        return None
    text = str(record.get('content') or '')
    if not text.strip():
        return None
    return text


def _is_user_record(record: dict[str, Any]) -> bool:
    message = record.get('message')
    if not isinstance(message, dict):
        return False
    if str(record.get('type') or '').strip().lower() != 'user':
        return False
    role = str(message.get('role') or '').strip().lower()
    return not role or role == 'user'


def _is_tool_result_user_record(record: dict[str, Any]) -> bool:
    if not _is_user_record(record):
        return False
    if isinstance(record.get('toolUseResult'), dict):
        return True
    content = record.get('message', {}).get('content')
    if not isinstance(content, list) or not content:
        return False
    return all(isinstance(item, dict) and str(item.get('type') or '').strip().lower() == 'tool_result' for item in content)


def _is_assistant_record(record: dict[str, Any]) -> bool:
    if str(record.get('type') or '').strip().lower() != 'assistant':
        return False
    message = record.get('message')
    if not isinstance(message, dict):
        return False
    role = str(message.get('role') or '').strip().lower()
    return not role or role == 'assistant'


def _assistant_message_text(record: dict[str, Any]) -> str:
    message = record.get('message')
    if not isinstance(message, dict):
        return ''
    return _content_text(message.get('content'))


def _user_message_text(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    message = record.get('message')
    if not isinstance(message, dict):
        return None
    if str(record.get('type') or '').strip().lower() != 'user':
        return None
    role = str(message.get('role') or '').strip().lower()
    if role and role != 'user':
        return None
    return _content_text(message.get('content'))


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or '')
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get('text')
            if text is not None:
                parts.append(str(text))
    return '\n'.join(parts)


__all__ = [
    'current_turn_req_id_from_transcript',
    'current_turn_req_id_from_transcript_text',
    'extract_outer_req_id',
    'extract_req_id',
    'latest_last_prompt_req_id_from_transcript_text',
    'latest_req_id_from_transcript',
    'latest_req_id_from_transcript_text',
    'latest_user_req_id_from_transcript_text',
]
