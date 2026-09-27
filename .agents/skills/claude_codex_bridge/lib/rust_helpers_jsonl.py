from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Mapping, Sequence

from rust_helpers import (
    DEFAULT_TIMEOUT_S,
    RUST_HELPER_BIN_ENV,
    RUST_HELPER_BINARY,
    RUST_HELPERS_ENV,
    RustHelperCallResult,
    RustHelperDiagnostic,
    call_rust_helper_or_fallback,
)


RUST_JSONL_ENV = 'CCB_RUST_JSONL'
RUST_JSONL_STORE_ENV = 'CCB_RUST_JSONL_STORE'
JSONL_TAIL_CAPABILITY = 'jsonl.tail'
JSONL_TAIL_STRICT_CAPABILITY = 'jsonl.tail.strict'
JOBS_TAIL_SUMMARY_CAPABILITY = 'jobs.tail.summary'


def read_jsonl_tail_batch(
    requests: Sequence[Mapping[str, object]],
    *,
    env: Mapping[str, str] | None = None,
    helper_bin: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
    which=shutil.which,
    run=subprocess.run,
) -> RustHelperCallResult[dict[str, object]]:
    started = time.monotonic()
    normalized = _normalize_requests(requests)
    fallback = lambda: _python_tail_batch(normalized)
    helper_env = _jsonl_helper_env(env=env, helper_bin=helper_bin)

    result = call_rust_helper_or_fallback(
        capability=JSONL_TAIL_CAPABILITY,
        payload={'requests': [request.copy() for request in normalized]},
        fallback=fallback,
        env=helper_env,
        timeout_s=timeout_s,
        script_root=script_root,
        which=which,
        run=run,
    )
    if not result.helper_used:
        return RustHelperCallResult(
            value=_coerce_value(result.value, fallback),
            helper_used=False,
            diagnostics=result.diagnostics,
            helper_path=result.helper_path,
        )

    value = _validate_helper_payload(result.value, normalized)
    if value is None:
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

    return RustHelperCallResult(
        value=value,
        helper_used=True,
        diagnostics=result.diagnostics,
        helper_path=result.helper_path,
    )


def read_jsonl_tail(
    path: str | os.PathLike[str],
    n: int,
    *,
    request_id: str = 'default',
    env: Mapping[str, str] | None = None,
    helper_bin: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
) -> RustHelperCallResult[list[dict[str, Any]]]:
    result = read_jsonl_tail_batch(
        [{'id': request_id, 'path': str(path), 'n': n}],
        env=env,
        helper_bin=helper_bin,
        timeout_s=timeout_s,
        script_root=script_root,
    )
    rows: list[dict[str, Any]] = []
    payload_requests = result.value.get('requests') if isinstance(result.value, Mapping) else None
    if isinstance(payload_requests, list) and payload_requests:
        first = payload_requests[0]
        if isinstance(first, Mapping) and isinstance(first.get('rows'), list):
            rows = [row for row in first['rows'] if isinstance(row, dict)]
    return RustHelperCallResult(
        value=rows,
        helper_used=result.helper_used,
        diagnostics=result.diagnostics,
        helper_path=result.helper_path,
    )


def read_jsonl_tail_strict_required(
    path: str | os.PathLike[str],
    n: int,
    *,
    request_id: str = 'default',
    env: Mapping[str, str] | None = None,
    helper_bin: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
    which=shutil.which,
    run=subprocess.run,
) -> RustHelperCallResult[list[dict[str, Any]]]:
    result = read_jsonl_tail_strict_batch_required(
        [{'id': request_id, 'path': str(path), 'n': n}],
        env=env,
        helper_bin=helper_bin,
        timeout_s=timeout_s,
        script_root=script_root,
        which=which,
        run=run,
    )
    rows: list[dict[str, Any]] = []
    value_requests = result.value.get('requests')
    if isinstance(value_requests, list) and value_requests:
        first = value_requests[0]
        if isinstance(first, Mapping) and isinstance(first.get('rows'), list):
            rows = [dict(row) for row in first['rows'] if isinstance(row, dict)]
    return RustHelperCallResult(
        value=rows,
        helper_used=result.helper_used,
        diagnostics=result.diagnostics,
        helper_path=result.helper_path,
    )


def read_jsonl_tail_strict_batch_required(
    requests: Sequence[Mapping[str, object]],
    *,
    env: Mapping[str, str] | None = None,
    helper_bin: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
    which=shutil.which,
    run=subprocess.run,
) -> RustHelperCallResult[dict[str, object]]:
    normalized = _normalize_requests(requests)
    helper_env = dict(env if env is not None else os.environ)
    helper_env[RUST_HELPERS_ENV] = '1'
    if helper_bin is not None:
        helper_env[RUST_HELPER_BIN_ENV] = str(helper_bin)

    result = call_rust_helper_or_fallback(
        capability=JSONL_TAIL_STRICT_CAPABILITY,
        payload={'requests': [request.copy() for request in normalized]},
        fallback=lambda: _raise_required_helper_unavailable(JSONL_TAIL_STRICT_CAPABILITY),
        env=helper_env,
        timeout_s=timeout_s,
        script_root=script_root,
        which=which,
        run=run,
    )
    if not result.helper_used:
        _raise_required_helper_unavailable(JSONL_TAIL_STRICT_CAPABILITY)

    value = _validate_strict_batch_payload(result.value, normalized)
    return RustHelperCallResult(
        value=value,
        helper_used=True,
        diagnostics=result.diagnostics,
        helper_path=result.helper_path,
    )


def read_job_tail_summaries_required(
    requests: Sequence[Mapping[str, object]],
    *,
    env: Mapping[str, str] | None = None,
    helper_bin: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
    which=shutil.which,
    run=subprocess.run,
) -> RustHelperCallResult[dict[str, object]]:
    normalized = _normalize_requests(requests)
    helper_env = dict(env if env is not None else os.environ)
    helper_env[RUST_HELPERS_ENV] = '1'
    if helper_bin is not None:
        helper_env[RUST_HELPER_BIN_ENV] = str(helper_bin)

    result = call_rust_helper_or_fallback(
        capability=JOBS_TAIL_SUMMARY_CAPABILITY,
        payload={'requests': [request.copy() for request in normalized]},
        fallback=lambda: _raise_required_helper_unavailable(JOBS_TAIL_SUMMARY_CAPABILITY),
        env=helper_env,
        timeout_s=timeout_s,
        script_root=script_root,
        which=which,
        run=run,
    )
    if not result.helper_used:
        _raise_required_helper_unavailable(JOBS_TAIL_SUMMARY_CAPABILITY)

    value = _validate_job_tail_summary_payload(result.value, normalized)
    return RustHelperCallResult(
        value=value,
        helper_used=True,
        diagnostics=result.diagnostics,
        helper_path=result.helper_path,
    )


def _normalize_requests(requests: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for index, request in enumerate(requests):
        if not isinstance(request, Mapping):
            raise TypeError('JSONL tail request must be a mapping')
        path = request.get('path')
        if path is None:
            raise ValueError('JSONL tail request requires path')
        n = request.get('n', 0)
        if isinstance(n, bool) or not isinstance(n, int):
            raise TypeError('JSONL tail request n must be an integer')
        if n < 0:
            raise ValueError('JSONL tail request n must be non-negative')
        request_id = request.get('id', str(index))
        normalized.append({'id': str(request_id), 'path': str(path), 'n': n})
    return normalized


def _jsonl_helper_env(
    *,
    env: Mapping[str, str] | None,
    helper_bin: str | os.PathLike[str] | None,
) -> dict[str, str]:
    base = dict(env if env is not None else os.environ)
    mode = str(base.get(RUST_JSONL_ENV, '')).strip().lower()
    base[RUST_HELPERS_ENV] = mode if mode in {'1', 'auto'} else '0'
    if helper_bin is not None:
        base[RUST_HELPER_BIN_ENV] = str(helper_bin)
    return base


def _python_tail_batch(requests: Sequence[Mapping[str, object]]) -> dict[str, object]:
    return {
        'requests': [
            {
                'id': str(request['id']),
                'rows': _python_tail_file(Path(str(request['path'])), int(request['n'])),
            }
            for request in requests
        ]
    }


def _python_tail_file(path: Path, n: int) -> list[dict[str, Any]]:
    if n < 0:
        raise ValueError('JSONL tail request n must be non-negative')
    if n == 0 or not path.is_file():
        return []

    rows: deque[dict[str, Any]] = deque(maxlen=n)
    try:
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return list(rows)


def _coerce_value(value: object, fallback) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    return fallback()


def _validate_helper_payload(value: object, requests: Sequence[Mapping[str, object]]) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    result_requests = value.get('requests')
    if not isinstance(result_requests, list) or len(result_requests) != len(requests):
        return None

    expected_ids = [str(request['id']) for request in requests]
    normalized: list[dict[str, object]] = []
    for expected_id, result_request in zip(expected_ids, result_requests):
        if not isinstance(result_request, Mapping):
            return None
        if result_request.get('id') != expected_id:
            return None
        rows = result_request.get('rows')
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return None
        normalized.append({'id': expected_id, 'rows': [dict(row) for row in rows]})
    return {'requests': normalized}


def _validate_strict_batch_payload(
    value: object,
    requests: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f'{JSONL_TAIL_STRICT_CAPABILITY} returned an invalid payload')
    error = value.get('error')
    if error not in (None, {}):
        if not isinstance(error, Mapping):
            raise RuntimeError(f'{JSONL_TAIL_STRICT_CAPABILITY} returned an invalid error payload')
        first_path = Path(str(requests[0]['path'])) if requests else Path('')
        _raise_strict_jsonl_error(error, path=first_path)
    result_requests = value.get('requests')
    if not isinstance(result_requests, list) or len(result_requests) != len(requests):
        raise RuntimeError(f'{JSONL_TAIL_STRICT_CAPABILITY} returned an invalid requests payload')
    normalized: list[dict[str, object]] = []
    for request, result_request in zip(requests, result_requests):
        expected_id = str(request['id'])
        if not isinstance(result_request, Mapping) or result_request.get('id') != expected_id:
            raise RuntimeError(f'{JSONL_TAIL_STRICT_CAPABILITY} returned an unexpected request id')
        rows = result_request.get('rows')
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError(f'{JSONL_TAIL_STRICT_CAPABILITY} returned invalid rows')
        normalized.append({'id': expected_id, 'rows': [dict(row) for row in rows]})
    return {'requests': normalized}


def _validate_job_tail_summary_payload(
    value: object,
    requests: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f'{JOBS_TAIL_SUMMARY_CAPABILITY} returned an invalid payload')
    error = value.get('error')
    if error not in (None, {}):
        if not isinstance(error, Mapping):
            raise RuntimeError(f'{JOBS_TAIL_SUMMARY_CAPABILITY} returned an invalid error payload')
        first_path = Path(str(requests[0]['path'])) if requests else Path('')
        _raise_strict_jsonl_error(error, path=first_path)
    result_requests = value.get('requests')
    if not isinstance(result_requests, list) or len(result_requests) != len(requests):
        raise RuntimeError(f'{JOBS_TAIL_SUMMARY_CAPABILITY} returned an invalid requests payload')
    normalized: list[dict[str, object]] = []
    for request, result_request in zip(requests, result_requests):
        expected_id = str(request['id'])
        if not isinstance(result_request, Mapping) or result_request.get('id') != expected_id:
            raise RuntimeError(f'{JOBS_TAIL_SUMMARY_CAPABILITY} returned an unexpected request id')
        jobs = result_request.get('jobs')
        if not isinstance(jobs, list) or not all(isinstance(job, Mapping) for job in jobs):
            raise RuntimeError(f'{JOBS_TAIL_SUMMARY_CAPABILITY} returned invalid jobs')
        normalized.append({'id': expected_id, 'jobs': [dict(job) for job in jobs]})
    return {'requests': normalized, 'error': None}


def _raise_strict_jsonl_error(error: Mapping[str, object], *, path: Path):
    kind = str(error.get('kind') or '').strip()
    message = str(error.get('message') or '').strip()
    error_path = Path(str(error.get('path') or path))
    if kind == 'non_object':
        raise ValueError(f'{error_path}: expected JSON object rows')
    if kind == 'invalid_json':
        raise json.JSONDecodeError(message or 'invalid JSON', '', 0)
    if kind == 'invalid_utf8':
        raise UnicodeDecodeError('utf-8', b'', 0, 1, message or 'invalid UTF-8 in JSONL row')
    if kind == 'read_error':
        raise OSError(message or f'failed to read {error_path}')
    raise RuntimeError(message or f'{JSONL_TAIL_STRICT_CAPABILITY} failed with {kind or "unknown_error"}')


def _raise_required_helper_unavailable(capability: str):
    raise RuntimeError(f'{capability} requires ccb-rs-helper; no Python fallback is available for this path')


__all__ = [
    'JOBS_TAIL_SUMMARY_CAPABILITY',
    'JSONL_TAIL_CAPABILITY',
    'JSONL_TAIL_STRICT_CAPABILITY',
    'RUST_JSONL_ENV',
    'RUST_JSONL_STORE_ENV',
    'read_jsonl_tail',
    'read_jsonl_tail_batch',
    'read_jsonl_tail_strict_batch_required',
    'read_jsonl_tail_strict_required',
    'read_job_tail_summaries_required',
]
