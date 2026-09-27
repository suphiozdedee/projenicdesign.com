from __future__ import annotations

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


RUST_PROJECT_VIEW_ENV = 'CCB_RUST_PROJECT_VIEW'
RUST_PROJECT_VIEW_RECENT_JOBS_ENV = 'CCB_RUST_PROJECT_VIEW_RECENT_JOBS'
PROJECT_VIEW_TMUX_PARSE_CAPABILITY = 'project_view.tmux.parse'
PROJECT_VIEW_RECENT_JOBS_CAPABILITY = 'project_view.recent_jobs'
JOBS_QUERY_RECENT_CAPABILITY = 'jobs.query.recent'


def parse_tmux_project_view_outputs(
    *,
    focus_stdout: str,
    windows_stdout: str,
    sidebars_stdout: str,
    session_name: str,
    project_id: str,
    env: Mapping[str, str] | None = None,
    helper_bin: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
    which=shutil.which,
    run=subprocess.run,
) -> RustHelperCallResult[dict[str, object]]:
    started = time.monotonic()
    payload = {
        'focus_stdout': str(focus_stdout or ''),
        'windows_stdout': str(windows_stdout or ''),
        'sidebars_stdout': str(sidebars_stdout or ''),
        'session_name': str(session_name or ''),
        'project_id': str(project_id or ''),
    }
    required = _project_view_helper_required(env)

    def fallback():
        if required:
            _raise_required_project_view_helper_unavailable(PROJECT_VIEW_TMUX_PARSE_CAPABILITY)
        return _python_parse_tmux_project_view_outputs(**payload)

    helper_env = _project_view_helper_env(env=env, helper_bin=helper_bin)

    result = call_rust_helper_or_fallback(
        capability=PROJECT_VIEW_TMUX_PARSE_CAPABILITY,
        payload=payload,
        fallback=fallback,
        env=helper_env,
        timeout_s=timeout_s,
        script_root=script_root,
        which=which,
        run=run,
    )
    if not result.helper_used:
        if required:
            _raise_required_project_view_helper_unavailable(PROJECT_VIEW_TMUX_PARSE_CAPABILITY)
        return RustHelperCallResult(
            value=_coerce_payload(result.value, fallback),
            helper_used=False,
            diagnostics=result.diagnostics,
            helper_path=result.helper_path,
        )

    value = _validate_payload(result.value)
    if value is None:
        if required:
            _raise_required_project_view_helper_unavailable(PROJECT_VIEW_TMUX_PARSE_CAPABILITY)
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


def read_project_view_recent_jobs_required(
    requests: Sequence[Mapping[str, object]],
    *,
    statuses: Sequence[str],
    result_limit: int,
    env: Mapping[str, str] | None = None,
    helper_bin: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
    which=shutil.which,
    run=subprocess.run,
) -> RustHelperCallResult[dict[str, object]]:
    normalized = _normalize_recent_job_requests(requests)
    if result_limit < 0:
        raise ValueError('result_limit cannot be negative')
    helper_env = dict(env if env is not None else os.environ)
    helper_env[RUST_HELPERS_ENV] = '1'
    if helper_bin is not None:
        helper_env[RUST_HELPER_BIN_ENV] = str(helper_bin)

    result = call_rust_helper_or_fallback(
        capability=PROJECT_VIEW_RECENT_JOBS_CAPABILITY,
        payload={
            'requests': [request.copy() for request in normalized],
            'statuses': [str(status) for status in statuses],
            'result_limit': int(result_limit),
        },
        fallback=lambda: _raise_required_project_view_helper_unavailable(PROJECT_VIEW_RECENT_JOBS_CAPABILITY),
        env=helper_env,
        timeout_s=timeout_s,
        script_root=script_root,
        which=which,
        run=run,
    )
    if not result.helper_used:
        _raise_required_project_view_helper_unavailable(PROJECT_VIEW_RECENT_JOBS_CAPABILITY)
    value = _validate_recent_jobs_payload(result.value)
    return RustHelperCallResult(
        value=value,
        helper_used=True,
        diagnostics=result.diagnostics,
        helper_path=result.helper_path,
    )


def read_jobs_query_recent_required(
    requests: Sequence[Mapping[str, object]],
    *,
    statuses: Sequence[str],
    result_limit: int,
    per_agent_initial: int,
    per_agent_max: int,
    body_preview_chars: int | None = None,
    env: Mapping[str, str] | None = None,
    helper_bin: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
    which=shutil.which,
    run=subprocess.run,
) -> RustHelperCallResult[dict[str, object]]:
    normalized = _normalize_recent_job_query_requests(requests)
    if result_limit < 0:
        raise ValueError('result_limit cannot be negative')
    if per_agent_initial < 0:
        raise ValueError('per_agent_initial cannot be negative')
    if per_agent_max < 0:
        raise ValueError('per_agent_max cannot be negative')
    if body_preview_chars is not None and body_preview_chars < 0:
        raise ValueError('body_preview_chars cannot be negative')
    helper_env = dict(env if env is not None else os.environ)
    helper_env[RUST_HELPERS_ENV] = '1'
    if helper_bin is not None:
        helper_env[RUST_HELPER_BIN_ENV] = str(helper_bin)

    payload: dict[str, object] = {
        'requests': [request.copy() for request in normalized],
        'statuses': [str(status) for status in statuses],
        'result_limit': int(result_limit),
        'per_agent_initial': int(per_agent_initial),
        'per_agent_max': int(per_agent_max),
    }
    if body_preview_chars is not None:
        payload['body_preview_chars'] = int(body_preview_chars)

    result = call_rust_helper_or_fallback(
        capability=JOBS_QUERY_RECENT_CAPABILITY,
        payload=payload,
        fallback=lambda: _raise_required_project_view_helper_unavailable(JOBS_QUERY_RECENT_CAPABILITY),
        env=helper_env,
        timeout_s=timeout_s,
        script_root=script_root,
        which=which,
        run=run,
    )
    if not result.helper_used:
        _raise_required_project_view_helper_unavailable(JOBS_QUERY_RECENT_CAPABILITY)
    value = _validate_jobs_query_recent_payload(result.value)
    return RustHelperCallResult(
        value=value,
        helper_used=True,
        diagnostics=result.diagnostics,
        helper_path=result.helper_path,
    )


def _project_view_helper_env(
    *,
    env: Mapping[str, str] | None,
    helper_bin: str | os.PathLike[str] | None,
) -> dict[str, str]:
    base = dict(env if env is not None else os.environ)
    mode = str(base.get(RUST_PROJECT_VIEW_ENV, '')).strip().lower()
    base[RUST_HELPERS_ENV] = '1' if mode in {'1', 'auto', 'required'} else '0'
    if helper_bin is not None:
        base[RUST_HELPER_BIN_ENV] = str(helper_bin)
    return base


def _project_view_helper_required(env: Mapping[str, str] | None) -> bool:
    base = env if env is not None else os.environ
    return str(base.get(RUST_PROJECT_VIEW_ENV, '')).strip().lower() == 'required'


def _normalize_recent_job_requests(requests: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for index, request in enumerate(requests):
        if not isinstance(request, Mapping):
            raise TypeError('ProjectView recent job request must be a mapping')
        path = request.get('path')
        if path is None:
            raise ValueError('ProjectView recent job request requires path')
        n = request.get('n', 0)
        if isinstance(n, bool) or not isinstance(n, int):
            raise TypeError('ProjectView recent job request n must be an integer')
        if n < 0:
            raise ValueError('ProjectView recent job request n must be non-negative')
        request_id = request.get('id', str(index))
        normalized.append({'id': str(request_id), 'path': str(path), 'n': n})
    return normalized


def _normalize_recent_job_query_requests(requests: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for index, request in enumerate(requests):
        if not isinstance(request, Mapping):
            raise TypeError('Jobs recent query request must be a mapping')
        path = request.get('path')
        if path is None:
            raise ValueError('Jobs recent query request requires path')
        request_id = request.get('id', str(index))
        normalized.append({'id': str(request_id), 'path': str(path)})
    return normalized


def _python_parse_tmux_project_view_outputs(
    *,
    focus_stdout: str,
    windows_stdout: str,
    sidebars_stdout: str,
    session_name: str,
    project_id: str,
) -> dict[str, object]:
    return {
        'focus': _parse_focus(focus_stdout),
        'windows': _parse_windows(windows_stdout),
        'sidebars': _parse_sidebars(sidebars_stdout, session_name=session_name, project_id=project_id),
    }


def _parse_focus(stdout: str) -> dict[str, object]:
    parts = ((str(stdout or '').splitlines() or [''])[0]).split('\t')
    if len(parts) != 4:
        return {}
    role = parts[2].strip()
    active_agent = parts[3].strip() if role == 'agent' else None
    return {
        'active_window': parts[0].strip() or None,
        'active_pane_id': parts[1].strip() or None,
        'active_agent': active_agent or None,
    }


def _parse_windows(stdout: str) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for line in str(stdout or '').splitlines():
        parts = line.split('\t')
        if len(parts) != 3:
            continue
        window_name, window_id, window_index = (_clean_text(item) for item in parts)
        if window_name is None:
            continue
        result[window_name] = {
            'tmux_window_id': window_id,
            'tmux_window_index': _coerce_int(window_index),
        }
    return result


def _parse_sidebars(stdout: str, *, session_name: str, project_id: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in str(stdout or '').splitlines():
        parts = line.split('\t')
        if len(parts) != 7:
            continue
        session, window_name, pane_id, pane_project_id, role, sidebar_instance, ccb_window = (
            _clean_text(item) for item in parts
        )
        if session != session_name or pane_project_id != project_id or role != 'sidebar':
            continue
        if pane_id is None or not pane_id.startswith('%'):
            continue
        resolved_window = sidebar_instance or ccb_window or window_name
        if resolved_window is None or resolved_window in result:
            continue
        result[resolved_window] = pane_id
    return result


def _clean_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def _coerce_int(value: str | None) -> int | None:
    text = str(value or '').strip()
    if not text.isdigit():
        return None
    return int(text)


def _coerce_payload(value: object, fallback) -> dict[str, object]:
    valid = _validate_payload(value)
    if valid is not None:
        return valid
    return fallback()


def _validate_recent_jobs_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f'{PROJECT_VIEW_RECENT_JOBS_CAPABILITY} returned an invalid payload')
    error = value.get('error')
    if error not in (None, {}):
        if not isinstance(error, Mapping):
            raise RuntimeError(f'{PROJECT_VIEW_RECENT_JOBS_CAPABILITY} returned an invalid error payload')
        kind = str(error.get('kind') or 'helper_error')
        message = str(error.get('message') or kind)
        path = str(error.get('path') or '').strip()
        detail = f'{path}: {message}' if path else message
        raise RuntimeError(f'{PROJECT_VIEW_RECENT_JOBS_CAPABILITY} {kind}: {detail}')
    jobs = value.get('jobs')
    if not isinstance(jobs, list) or not all(isinstance(job, Mapping) for job in jobs):
        raise RuntimeError(f'{PROJECT_VIEW_RECENT_JOBS_CAPABILITY} returned invalid jobs')
    return {'jobs': [dict(job) for job in jobs], 'error': None}


def _validate_jobs_query_recent_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f'{JOBS_QUERY_RECENT_CAPABILITY} returned an invalid payload')
    error = value.get('error')
    if error not in (None, {}):
        if not isinstance(error, Mapping):
            raise RuntimeError(f'{JOBS_QUERY_RECENT_CAPABILITY} returned an invalid error payload')
        kind = str(error.get('kind') or 'helper_error')
        message = str(error.get('message') or kind)
        path = str(error.get('path') or '').strip()
        detail = f'{path}: {message}' if path else message
        raise RuntimeError(f'{JOBS_QUERY_RECENT_CAPABILITY} {kind}: {detail}')
    jobs = value.get('jobs')
    if not isinstance(jobs, list) or not all(isinstance(job, Mapping) for job in jobs):
        raise RuntimeError(f'{JOBS_QUERY_RECENT_CAPABILITY} returned invalid jobs')
    scanned = value.get('scanned', 0)
    returned = value.get('returned', len(jobs))
    truncated = value.get('truncated', False)
    hint = value.get('next_budget_hint', {})
    if isinstance(scanned, bool) or not isinstance(scanned, int) or scanned < 0:
        raise RuntimeError(f'{JOBS_QUERY_RECENT_CAPABILITY} returned invalid scanned count')
    if isinstance(returned, bool) or not isinstance(returned, int) or returned < 0:
        raise RuntimeError(f'{JOBS_QUERY_RECENT_CAPABILITY} returned invalid returned count')
    if not isinstance(truncated, bool):
        raise RuntimeError(f'{JOBS_QUERY_RECENT_CAPABILITY} returned invalid truncated flag')
    if not isinstance(hint, Mapping):
        raise RuntimeError(f'{JOBS_QUERY_RECENT_CAPABILITY} returned invalid budget hint')
    return {
        'jobs': [dict(job) for job in jobs],
        'scanned': scanned,
        'returned': returned,
        'truncated': truncated,
        'next_budget_hint': dict(hint),
        'error': None,
    }


def _validate_payload(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    focus = _validate_focus(value.get('focus'))
    windows = _validate_windows(value.get('windows'))
    sidebars = _validate_sidebars(value.get('sidebars'))
    if focus is None or windows is None or sidebars is None:
        return None
    return {'focus': focus, 'windows': windows, 'sidebars': sidebars}


def _validate_focus(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, object] = {}
    for key in ('active_window', 'active_pane_id', 'active_agent'):
        item = value.get(key)
        if item is None:
            result[key] = None
        elif isinstance(item, str):
            result[key] = item
        else:
            return None
    return result


def _validate_windows(value: object) -> dict[str, dict[str, object]] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, dict[str, object]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, Mapping):
            return None
        window_id = item.get('tmux_window_id')
        window_index = item.get('tmux_window_index')
        if window_id is not None and not isinstance(window_id, str):
            return None
        if isinstance(window_index, bool) or (window_index is not None and not isinstance(window_index, int)):
            return None
        result[key] = {
            'tmux_window_id': window_id,
            'tmux_window_index': window_index,
        }
    return result


def _validate_sidebars(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            return None
        result[key] = item
    return result


def _raise_required_project_view_helper_unavailable(capability: str):
    raise RuntimeError(f'{capability} requires ccb-rs-helper; no Python fallback is available for this path')


__all__ = [
    'PROJECT_VIEW_TMUX_PARSE_CAPABILITY',
    'PROJECT_VIEW_RECENT_JOBS_CAPABILITY',
    'JOBS_QUERY_RECENT_CAPABILITY',
    'RUST_PROJECT_VIEW_ENV',
    'RUST_PROJECT_VIEW_RECENT_JOBS_ENV',
    'parse_tmux_project_view_outputs',
    'read_jobs_query_recent_required',
    'read_project_view_recent_jobs_required',
]
