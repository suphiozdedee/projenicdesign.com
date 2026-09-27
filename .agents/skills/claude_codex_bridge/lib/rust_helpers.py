from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Callable, Generic, Mapping, Sequence, TypeVar


RUST_HELPERS_ENV = 'CCB_RUST_HELPERS'
RUST_HELPER_BIN_ENV = 'CCB_RUST_HELPER_BIN'
RUST_HELPER_BINARY = 'ccb-rs-helper'
RUST_HELPER_SCHEMA_VERSION = 1
CONTRACT_ECHO_CAPABILITY = 'contract.echo'
DEFAULT_TIMEOUT_S = 0.5
STDERR_TAIL_LIMIT = 512

_T = TypeVar('_T')
_CAPABILITY_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}


@dataclass(frozen=True)
class RustHelperDiagnostic:
    helper: str
    failure_kind: str
    elapsed_ms: float
    stderr_tail: str = ''

    def to_dict(self) -> dict[str, object]:
        return {
            'helper': self.helper,
            'failure_kind': self.failure_kind,
            'elapsed_ms': self.elapsed_ms,
            'stderr_tail': self.stderr_tail,
        }


@dataclass(frozen=True)
class RustHelperCallResult(Generic[_T]):
    value: _T
    helper_used: bool
    diagnostics: tuple[RustHelperDiagnostic, ...] = ()
    helper_path: str | None = None

    @property
    def fallback_used(self) -> bool:
        return not self.helper_used


@dataclass(frozen=True)
class RustHelperResolution:
    path: str | None
    source: str
    reason: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.path)


def call_rust_helper_or_fallback(
    *,
    capability: str,
    payload: Mapping[str, object] | None,
    fallback: Callable[[], _T],
    helper_name: str = RUST_HELPER_BINARY,
    env: Mapping[str, str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    script_root: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> RustHelperCallResult[_T | Any]:
    """Call an optional Rust helper, returning fallback output on any contract miss.

    This wrapper is intentionally not wired to runtime hot paths yet. It defines
    the failure and diagnostic contract that future helpers must preserve.
    """

    started = time.monotonic()
    env_map = env if env is not None else os.environ
    mode = _helper_mode(env_map.get(RUST_HELPERS_ENV))
    if mode == 'disabled':
        return _fallback_result(
            fallback,
            helper_name=helper_name,
            failure_kind='disabled',
            started=started,
        )

    resolution = resolve_rust_helper(helper_name=helper_name, env=env_map, script_root=script_root, which=which)
    if not resolution.available or resolution.path is None:
        return _fallback_result(
            fallback,
            helper_name=helper_name,
            failure_kind='missing',
            started=started,
        )

    metadata = _cached_capabilities(resolution.path)
    if metadata is None:
        capability_probe = _run_helper(
            [resolution.path, '--capabilities'],
            input_text=None,
            timeout_s=timeout_s,
            run=run,
        )
        if capability_probe.diagnostic is not None:
            return _fallback_result(
                fallback,
                helper_name=helper_name,
                failure_kind=capability_probe.diagnostic.failure_kind,
                started=started,
                stderr_tail=capability_probe.diagnostic.stderr_tail,
            )
        metadata = capability_probe.payload
        validation_failure = _validate_capability_envelope(metadata)
        if validation_failure is not None:
            return _fallback_result(
                fallback,
                helper_name=helper_name,
                failure_kind=validation_failure,
                started=started,
            )
        _store_capabilities(resolution.path, metadata)

    validation_failure = _validate_capabilities(metadata, capability=capability)
    if validation_failure is not None:
        return _fallback_result(
            fallback,
            helper_name=helper_name,
            failure_kind=validation_failure,
            started=started,
        )

    request = {
        'schema_version': RUST_HELPER_SCHEMA_VERSION,
        'capability': capability,
        'payload': dict(payload or {}),
    }
    helper_call = _run_helper(
        [resolution.path],
        input_text=json.dumps(request, separators=(',', ':')),
        timeout_s=timeout_s,
        run=run,
    )
    if helper_call.diagnostic is not None:
        return _fallback_result(
            fallback,
            helper_name=helper_name,
            failure_kind=helper_call.diagnostic.failure_kind,
            started=started,
            stderr_tail=helper_call.diagnostic.stderr_tail,
        )

    response = helper_call.payload
    response_failure = _validate_response(response, capability=capability)
    if response_failure is not None:
        return _fallback_result(
            fallback,
            helper_name=helper_name,
            failure_kind=response_failure,
            started=started,
        )

    return RustHelperCallResult(
        value=response.get('payload'),
        helper_used=True,
        diagnostics=(),
        helper_path=resolution.path,
    )


def resolve_rust_helper(
    *,
    helper_name: str = RUST_HELPER_BINARY,
    env: Mapping[str, str] | None = None,
    script_root: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> RustHelperResolution:
    env_map = env if env is not None else os.environ
    override = _clean_text(env_map.get(RUST_HELPER_BIN_ENV))
    if override is not None:
        return _resolve_explicit(Path(override).expanduser(), source=RUST_HELPER_BIN_ENV)

    root = script_root or _default_script_root()
    root_candidate = root / 'bin' / helper_name
    if _is_executable_file(root_candidate):
        return RustHelperResolution(path=str(root_candidate), source='script_root_bin')

    prefix = _clean_text(env_map.get('CODEX_INSTALL_PREFIX'))
    if prefix is not None:
        prefix_candidate = Path(prefix).expanduser() / 'bin' / helper_name
        if _is_executable_file(prefix_candidate):
            return RustHelperResolution(path=str(prefix_candidate), source='CODEX_INSTALL_PREFIX')

    path_candidate = _clean_text(which(helper_name) if callable(which) else None)
    if path_candidate is not None:
        return RustHelperResolution(path=path_candidate, source='PATH')

    return RustHelperResolution(path=None, source='missing', reason=f'{helper_name} not found')


@dataclass(frozen=True)
class _HelperRunResult:
    payload: dict[str, Any] | None = None
    diagnostic: RustHelperDiagnostic | None = None


def _run_helper(
    command: Sequence[str],
    *,
    input_text: str | None,
    timeout_s: float,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> _HelperRunResult:
    helper_name = Path(command[0]).name if command else RUST_HELPER_BINARY
    started = time.monotonic()
    try:
        completed = run(
            list(command),
            input=input_text,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return _HelperRunResult(
            diagnostic=_diagnostic(
                helper_name=helper_name,
                failure_kind='timeout',
                started=started,
                stderr_tail=_safe_stderr_tail(getattr(exc, 'stderr', '') or ''),
            )
        )
    except OSError:
        return _HelperRunResult(diagnostic=_diagnostic(helper_name=helper_name, failure_kind='missing', started=started))

    if completed.returncode != 0:
        return _HelperRunResult(
            diagnostic=_diagnostic(
                helper_name=helper_name,
                failure_kind='nonzero_exit',
                started=started,
                stderr_tail=_safe_stderr_tail(completed.stderr),
            )
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _HelperRunResult(
            diagnostic=_diagnostic(
                helper_name=helper_name,
                failure_kind='invalid_json',
                started=started,
                stderr_tail=_safe_stderr_tail(completed.stderr),
            )
        )
    if not isinstance(payload, dict):
        return _HelperRunResult(diagnostic=_diagnostic(helper_name=helper_name, failure_kind='invalid_json', started=started))
    return _HelperRunResult(payload=payload)


def _cached_capabilities(path: str) -> dict[str, Any] | None:
    key = _capability_cache_key(path)
    if key is None:
        return None
    cached = _CAPABILITY_CACHE.get(key)
    return dict(cached) if cached is not None else None


def _store_capabilities(path: str, payload: Mapping[str, Any] | None) -> None:
    key = _capability_cache_key(path)
    if key is None or not isinstance(payload, Mapping):
        return
    _CAPABILITY_CACHE[key] = dict(payload)


def _capability_cache_key(path: str) -> tuple[str, int, int] | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (str(Path(path)), int(stat.st_mtime_ns), int(stat.st_size))


def _validate_capability_envelope(payload: Mapping[str, Any] | None) -> str | None:
    if not isinstance(payload, Mapping):
        return 'invalid_json'
    if payload.get('schema_version') != RUST_HELPER_SCHEMA_VERSION:
        return 'unknown_schema'
    capabilities = payload.get('capabilities')
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        return 'unknown_schema'
    return None


def _validate_capabilities(payload: Mapping[str, Any] | None, *, capability: str) -> str | None:
    validation_failure = _validate_capability_envelope(payload)
    if validation_failure is not None:
        return validation_failure
    capabilities = payload.get('capabilities') if isinstance(payload, Mapping) else None
    if capability not in capabilities:
        return 'unsupported_capability'
    return None


def _validate_response(payload: Mapping[str, Any] | None, *, capability: str) -> str | None:
    if not isinstance(payload, Mapping):
        return 'invalid_json'
    if payload.get('schema_version') != RUST_HELPER_SCHEMA_VERSION:
        return 'unknown_schema'
    if payload.get('ok') is not True:
        return 'nonzero_exit'
    if payload.get('capability') != capability:
        return 'unsupported_capability'
    return None


def _fallback_result(
    fallback: Callable[[], _T],
    *,
    helper_name: str,
    failure_kind: str,
    started: float,
    stderr_tail: str = '',
) -> RustHelperCallResult[_T]:
    value = fallback()
    return RustHelperCallResult(
        value=value,
        helper_used=False,
        diagnostics=(
            _diagnostic(
                helper_name=helper_name,
                failure_kind=failure_kind,
                started=started,
                stderr_tail=stderr_tail,
            ),
        ),
    )


def _diagnostic(
    *,
    helper_name: str,
    failure_kind: str,
    started: float,
    stderr_tail: str = '',
) -> RustHelperDiagnostic:
    return RustHelperDiagnostic(
        helper=helper_name,
        failure_kind=failure_kind,
        elapsed_ms=round((time.monotonic() - started) * 1000, 3),
        stderr_tail=_safe_stderr_tail(stderr_tail),
    )


def _helper_mode(value: str | None) -> str:
    text = str(value or '').strip().lower()
    if text in {'1', 'true', 'yes', 'on', 'enabled', 'auto'}:
        return 'enabled'
    return 'disabled'


def _safe_stderr_tail(value: object) -> str:
    text = str(value or '').replace('\x00', '').strip()
    if not text:
        return ''
    if text.startswith('[redacted stderr: ') and text.endswith(' chars captured]'):
        return text
    return f'[redacted stderr: {min(len(text), STDERR_TAIL_LIMIT)} chars captured]'


def _resolve_explicit(path: Path, *, source: str) -> RustHelperResolution:
    if _is_executable_file(path):
        return RustHelperResolution(path=str(path), source=source)
    return RustHelperResolution(path=None, source=source, reason=f'{source} points to a missing or non-executable file')


def _default_script_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _clean_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


__all__ = [
    'CONTRACT_ECHO_CAPABILITY',
    'RUST_HELPER_BIN_ENV',
    'RUST_HELPERS_ENV',
    'RUST_HELPER_BINARY',
    'RUST_HELPER_SCHEMA_VERSION',
    'RustHelperCallResult',
    'RustHelperDiagnostic',
    'RustHelperResolution',
    'call_rust_helper_or_fallback',
    'resolve_rust_helper',
]
