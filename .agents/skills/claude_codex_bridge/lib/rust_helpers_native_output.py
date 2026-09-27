from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Mapping

from rust_helpers import (
    DEFAULT_TIMEOUT_S,
    RUST_HELPER_BIN_ENV,
    RUST_HELPER_BINARY,
    RUST_HELPERS_ENV,
    RustHelperCallResult,
    RustHelperDiagnostic,
    call_rust_helper_or_fallback,
)


RUST_NATIVE_OUTPUT_ENV = 'CCB_RUST_NATIVE_OUTPUT'
NATIVE_OUTPUT_OBSERVE_CAPABILITY = 'native.output.observe'
_OBSERVATION_KEYS = ('text', 'finished', 'finish_reason', 'turn_ref', 'completed_at', 'error', 'intermediate')


def observe_native_jsonl_output(
    path: str | os.PathLike[str],
    *,
    env: Mapping[str, str] | None = None,
    helper_bin: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
    which=shutil.which,
    run=subprocess.run,
) -> RustHelperCallResult[dict[str, object]]:
    started = time.monotonic()
    target = Path(path)
    required = _native_output_helper_required(env)

    def fallback():
        if required:
            _raise_required_native_output_helper_unavailable(NATIVE_OUTPUT_OBSERVE_CAPABILITY)
        return _python_observe_jsonl_output(target)

    helper_env = _native_output_helper_env(env=env, helper_bin=helper_bin)

    result = call_rust_helper_or_fallback(
        capability=NATIVE_OUTPUT_OBSERVE_CAPABILITY,
        payload={'path': str(target)},
        fallback=fallback,
        env=helper_env,
        timeout_s=timeout_s,
        script_root=script_root,
        which=which,
        run=run,
    )
    if not result.helper_used:
        if required:
            _raise_required_native_output_helper_unavailable(NATIVE_OUTPUT_OBSERVE_CAPABILITY)
        return RustHelperCallResult(
            value=_coerce_observation(result.value, fallback),
            helper_used=False,
            diagnostics=result.diagnostics,
            helper_path=result.helper_path,
        )

    value = _validate_observation(result.value)
    if value is None:
        if required:
            _raise_required_native_output_helper_unavailable(NATIVE_OUTPUT_OBSERVE_CAPABILITY)
        return RustHelperCallResult(
            value=fallback(),
            helper_used=False,
            diagnostics=(
                RustHelperDiagnostic(
                    helper=Path(result.helper_path or RUST_HELPER_BINARY).name,
                    failure_kind='unknown_schema',
                    elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                ),
            ),
            helper_path=result.helper_path,
        )
    return RustHelperCallResult(value=value, helper_used=True, diagnostics=result.diagnostics, helper_path=result.helper_path)


def _native_output_helper_env(
    *,
    env: Mapping[str, str] | None,
    helper_bin: str | os.PathLike[str] | None,
) -> dict[str, str]:
    base = dict(env if env is not None else os.environ)
    mode = str(base.get(RUST_NATIVE_OUTPUT_ENV, '')).strip().lower()
    global_mode = str(base.get(RUST_HELPERS_ENV, '')).strip().lower()
    if mode in {'0', 'false', 'no', 'off', 'disabled'}:
        base[RUST_HELPERS_ENV] = '0'
    elif mode in {'1', 'true', 'yes', 'on', 'auto', 'required'}:
        base[RUST_HELPERS_ENV] = '1'
    elif global_mode in {'0', 'false', 'no', 'off', 'disabled'}:
        base[RUST_HELPERS_ENV] = '0'
    else:
        base[RUST_HELPERS_ENV] = '1'
    if helper_bin is not None:
        base[RUST_HELPER_BIN_ENV] = str(helper_bin)
    return base


def _native_output_helper_required(env: Mapping[str, str] | None) -> bool:
    base = env if env is not None else os.environ
    return str(base.get(RUST_NATIVE_OUTPUT_ENV, '')).strip().lower() == 'required'


def _python_observe_jsonl_output(path: Path) -> dict[str, object]:
    if not path or not path.is_file():
        return _empty_observation()
    chunks: list[str] = []
    finished = False
    finish_reason = ''
    turn_ref: str | None = None
    completed_at: object | None = None
    error = ''
    intermediate = False
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError as exc:
        data = _empty_observation()
        data['error'] = f'read_stdout_failed:{exc}'
        return data
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if _is_error_event(event):
            error = _event_text(event) or _event_reason(event) or 'native_cli_error'
            continue
        if _is_tool_event(event):
            intermediate = True
            reason = _event_reason(event)
            if reason:
                finish_reason = reason
            continue
        text = _assistant_text(event)
        if text:
            chunks.append(text)
            turn_ref = turn_ref or _event_ref(event)
            completed_at = completed_at or _event_time(event)
        if _is_final_event(event):
            finished = True
            finish_reason = _event_reason(event) or finish_reason or 'completed'
            turn_ref = turn_ref or _event_ref(event)
            completed_at = completed_at or _event_time(event)
    return {
        'text': ''.join(chunks),
        'finished': finished,
        'finish_reason': finish_reason,
        'turn_ref': turn_ref,
        'completed_at': completed_at,
        'error': error,
        'intermediate': intermediate,
    }


def _empty_observation() -> dict[str, object]:
    return {
        'text': '',
        'finished': False,
        'finish_reason': '',
        'turn_ref': None,
        'completed_at': None,
        'error': '',
        'intermediate': False,
    }


def _assistant_text(event: dict[str, object]) -> str:
    if _is_user_event(event):
        return ''
    if not (_is_assistant_event(event) or _is_final_event(event)):
        return ''
    return _event_text(event)


def _is_user_event(event: dict[str, object]) -> bool:
    return _nested_text_value(event, ('role', 'sender', 'author')).strip().lower() == 'user'


def _is_assistant_event(event: dict[str, object]) -> bool:
    role = _nested_text_value(event, ('role', 'sender', 'author')).strip().lower()
    if role in {'assistant', 'agent', 'model'}:
        return True
    event_type = _event_type(event)
    return any(token in event_type for token in ('assistant', 'agent_message', 'message_delta', 'content_delta', 'text'))


def _is_final_event(event: dict[str, object]) -> bool:
    if _is_tool_event(event):
        return False
    haystack = ' '.join(
        item
        for item in (
            _event_type(event),
            _event_reason(event).strip().lower().replace('-', '_'),
            _nested_text_value(event, ('status', 'state')).strip().lower().replace('-', '_'),
        )
        if item
    )
    if not haystack:
        return False
    return any(token in haystack for token in ('final', 'result', 'completion', 'completed', 'done', 'finished', 'turn_end', 'end_turn'))


def _is_tool_event(event: dict[str, object]) -> bool:
    haystack = ' '.join(
        item
        for item in (
            _event_type(event),
            _event_reason(event).strip().lower().replace('-', '_'),
            _nested_text_value(event, ('role', 'status', 'state', 'name')).strip().lower().replace('-', '_'),
        )
        if item
    )
    return 'tool' in haystack or 'permission' in haystack or 'function_call' in haystack


def _is_error_event(event: dict[str, object]) -> bool:
    haystack = ' '.join(
        item
        for item in (
            _event_type(event),
            _event_reason(event).strip().lower().replace('-', '_'),
            _nested_text_value(event, ('status', 'state')).strip().lower().replace('-', '_'),
        )
        if item
    )
    return any(token in haystack for token in ('error', 'failed', 'failure', 'permission_denied', 'unauthorized', 'auth_failed'))


def _event_type(event: dict[str, object]) -> str:
    return _nested_text_value(event, ('type', 'event', 'kind', 'name')).strip().lower().replace('-', '_')


def _event_text(event: object) -> str:
    if isinstance(event, str):
        return event
    if isinstance(event, list):
        return ''.join(_event_text(item) for item in event)
    if not isinstance(event, dict):
        return ''
    for key in ('merged_text', 'final_answer', 'answer', 'reply', 'text', 'output', 'response'):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (dict, list)):
            text = _event_text(value)
            if text:
                return text
    value = event.get('content')
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (dict, list)):
        text = _event_text(value)
        if text:
            return text
    for key in ('payload', 'message', 'delta', 'part', 'result', 'data'):
        value = event.get(key)
        if isinstance(value, (dict, list, str)):
            text = _event_text(value)
            if text:
                return text
    return ''


def _event_reason(event: dict[str, object]) -> str:
    for key in ('reason', 'finish_reason', 'stop_reason', 'status', 'state'):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value.strip()
    for key in ('payload', 'properties', 'part', 'message', 'result', 'data'):
        nested = event.get(key)
        if isinstance(nested, dict):
            reason = _event_reason(nested)
            if reason:
                return reason
    return ''


def _event_ref(event: dict[str, object]) -> str | None:
    for key in ('id', 'message_id', 'messageID', 'session_id', 'sessionID', 'turn_id', 'request_id'):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ('payload', 'message', 'part', 'result', 'data'):
        nested = event.get(key)
        if isinstance(nested, dict):
            ref = _event_ref(nested)
            if ref:
                return ref
    return None


def _event_time(event: dict[str, object]) -> object | None:
    for key in ('completed_at', 'time', 'timestamp', 'created_at', 'updated_at'):
        value = event.get(key)
        if value:
            return value
    for key in ('payload', 'message', 'part', 'result', 'data'):
        nested = event.get(key)
        if isinstance(nested, dict):
            value = _event_time(nested)
            if value:
                return value
    return None


def _nested_text_value(event: object, keys: tuple[str, ...]) -> str:
    if isinstance(event, list):
        for item in event:
            value = _nested_text_value(item, keys)
            if value:
                return value
        return ''
    if not isinstance(event, dict):
        return ''
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ('payload', 'message', 'part', 'result', 'data'):
        value = event.get(key)
        if isinstance(value, (dict, list)):
            nested = _nested_text_value(value, keys)
            if nested:
                return nested
    return ''


def _coerce_observation(value: object, fallback) -> dict[str, object]:
    valid = _validate_observation(value)
    if valid is not None:
        return valid
    return fallback()


def _validate_observation(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    if not all(key in value for key in _OBSERVATION_KEYS):
        return None
    return {
        'text': str(value.get('text') or ''),
        'finished': bool(value.get('finished')),
        'finish_reason': str(value.get('finish_reason') or ''),
        'turn_ref': value.get('turn_ref') if isinstance(value.get('turn_ref'), str) else None,
        'completed_at': value.get('completed_at'),
        'error': str(value.get('error') or ''),
        'intermediate': bool(value.get('intermediate')),
    }


def _raise_required_native_output_helper_unavailable(capability: str):
    raise RuntimeError(f'{capability} requires ccb-rs-helper; no Python fallback is available for this path')


__all__ = [
    'NATIVE_OUTPUT_OBSERVE_CAPABILITY',
    'RUST_NATIVE_OUTPUT_ENV',
    'observe_native_jsonl_output',
]
