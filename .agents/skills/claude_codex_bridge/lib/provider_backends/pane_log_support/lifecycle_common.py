from __future__ import annotations

import json
import time
from typing import Callable

from provider_core.tmux_ownership import (
    apply_session_tmux_identity,
    inspect_tmux_pane_ownership,
)
from provider_runtime.session_payload import (
    pane_ref_from_session,
    session_uses_tmux_compatible_pane,
)
from terminal_runtime.mux_backend_contract import MuxCommandErrorV2

# Substrings that mark a pane crash as an *unrecoverable* provider auth failure:
# restarting the pane will not fix it, the provider must be re-authenticated.
# Matched case-insensitively against the captured crash log. Kept deliberately
# specific to avoid classifying transient network 401s as auth revocation.
_AUTH_REVOKED_SIGNATURES = (
    'refresh token was revoked',
    'log out and sign in again',
    'please sign in again',
    'run `codex login`',
    'run codex login',
    'you are not signed in',
)
_SESSION_MISSING_SIGNATURES = ('no conversation found to continue',)
_HELPER_UNAVAILABLE_SIGNATURES = ('failed to connect to remote app server',)
_CRASH_SIGNATURES = {
    'provider_auth_revoked': _AUTH_REVOKED_SIGNATURES,
    'provider_session_missing': _SESSION_MISSING_SIGNATURES,
    'provider_helper_unavailable': _HELPER_UNAVAILABLE_SIGNATURES,
}
MAX_PANE_CRASH_LOGS = 50

_CRASH_REASON_DETAIL = {
    'provider_auth_revoked': (
        'Provider authentication was revoked or expired; the captured crash log '
        'matched a re-authentication signature. CCB may refresh changed inherited '
        'Codex auth once; otherwise run `codex login` in the source profile or '
        'repair agent-local auth, then remount.'
    ),
    'provider_session_missing': (
        'The provider resume target no longer exists. CCB may remove its own '
        'stale Claude --continue flag and start a fresh managed conversation.'
    ),
    'provider_helper_unavailable': (
        'The managed provider helper/app server is unavailable. Automatic pane '
        'respawn is blocked; restart the affected agent or remount the project.'
    ),
}


def classify_crash_reason(text: str) -> str | None:
    """Classify a captured pane crash log against known unrecoverable conditions.

    Returns a short reason code for known provider/session failures that need
    guarded preparation or fail-closed handling before a pane restart. Pure and
    side-effect free so it can be unit tested without a live pane.
    """
    if not text:
        return None
    haystack = text.lower()
    for reason, signatures in _CRASH_SIGNATURES.items():
        for signature in signatures:
            if signature in haystack:
                return reason
    return None


def attach_pane_log(session, backend: object, pane_id: object) -> None:
    pane_target = pane_lifecycle_target(session, pane_id)
    ensure = getattr(backend, 'ensure_pane_log', None)
    if callable(ensure):
        try:
            ensure(pane_target)
            return
        except Exception:
            pass
    capture = getattr(backend, 'capture_pane', None)
    if not callable(capture) or session_uses_tmux_compatible_pane(_session_data(session)):
        return
    try:
        capture(pane_target, lines=1)
    except Exception:
        pass


def live_owned_pane(session, backend: object, pane_id: str) -> str | None:
    if not pane_id or not backend_is_alive(backend, pane_lifecycle_target(session, pane_id)):
        return None
    if not session_uses_tmux_compatible_pane(_session_data(session)):
        return str(pane_id)
    ownership = inspect_tmux_pane_ownership(session, backend, str(pane_id))
    if not ownership.is_owned:
        return None
    return str(pane_id)


def backend_is_alive(backend: object, pane_target: object) -> bool:
    checker = getattr(backend, 'is_alive', None)
    if not callable(checker):
        return False
    return bool(checker(pane_target))


def pane_lifecycle_target(session, pane_id: object) -> object:
    if not session_uses_tmux_compatible_pane(_session_data(session)):
        pane_ref = pane_ref_from_session(_session_data(session))
        if pane_ref is not None:
            return pane_ref
    return str(pane_id or '').strip()


def lifecycle_backend_error_text(exc: MuxCommandErrorV2) -> str:
    if exc.backend_impl == 'herdr':
        return f'Herdr backend {exc.category} during {exc.operation}: {exc.detail}'
    return f'{exc.detail}'


def _session_data(session) -> dict[str, object]:
    data = getattr(session, 'data', None)
    return data if isinstance(data, dict) else {}


def activate_rebound_pane(
    session,
    backend: object,
    pane_id: str,
    *,
    now_str_fn: Callable[[], str],
    attach_pane_log_fn: Callable[[object, object, str], None],
) -> None:
    bind_session_to_pane(session, pane_id, now_str_fn=now_str_fn)
    apply_session_tmux_identity(session, backend, pane_id)
    attach_pane_log_fn(session, backend, pane_id)


def persist_crash_log(session, backend: object, pane_id: str) -> str | None:
    saver = getattr(backend, 'save_crash_log', None)
    if not callable(saver):
        return None
    try:
        runtime = session.runtime_dir
        runtime.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        crash_log = runtime / f'pane-crash-{ts}.log'
        saver(pane_id, str(crash_log), lines=1000)
        reason = _persist_crash_reason(runtime, crash_log, ts)
        _prune_crash_artifacts(runtime)
        return reason
    except Exception:
        return None


def _persist_crash_reason(runtime, crash_log, ts: int) -> str | None:
    """Classify the freshly captured crash log and, when it matches a known
    known condition, drop an actionable ``pane-crash-<ts>.reason.json``
    sidecar next to it. Returns the reason code, or ``None`` when unclassified.

    Best-effort: any failure here must not disrupt crash-log capture or the
    downstream pane respawn, so all errors are swallowed.
    """
    try:
        text = crash_log.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return None
    reason = classify_crash_reason(text)
    if reason is None:
        return None
    payload = {
        'schema_version': 1,
        'record_type': 'pane_crash_reason',
        'reason': reason,
        'detail': _CRASH_REASON_DETAIL.get(reason, ''),
        'matched_signature': _matched_signature(text),
        'crash_log': crash_log.name,
        'detected_at': ts,
    }
    try:
        reason_path = runtime / f'pane-crash-{ts}.reason.json'
        reason_path.write_text(
            json.dumps(payload, ensure_ascii=False) + '\n', encoding='utf-8'
        )
    except Exception:
        pass
    return reason


def _matched_signature(text: str) -> str | None:
    haystack = text.lower()
    for signatures in _CRASH_SIGNATURES.values():
        for signature in signatures:
            if signature in haystack:
                return signature
    return None


def _prune_crash_artifacts(runtime) -> None:
    try:
        logs = sorted(
            runtime.glob('pane-crash-*.log'),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
    except Exception:
        return
    retained = {path.stem for path in logs[:MAX_PANE_CRASH_LOGS]}
    for crash_log in logs[MAX_PANE_CRASH_LOGS:]:
        try:
            crash_log.unlink()
        except Exception:
            pass
    try:
        reason_paths = tuple(runtime.glob('pane-crash-*.reason.json'))
    except Exception:
        return
    for reason_path in reason_paths:
        crash_stem = reason_path.name.removesuffix('.reason.json')
        if crash_stem in retained:
            continue
        try:
            reason_path.unlink()
        except Exception:
            pass


def pane_exists(backend: object, pane_id: str) -> bool:
    checker = getattr(backend, 'pane_exists', None)
    if not callable(checker):
        return True
    try:
        return bool(checker(pane_id))
    except Exception:
        return True


def bind_session_to_pane(session, pane_id: str, *, now_str_fn: Callable[[], str]) -> None:
    data = getattr(session, 'data', None)
    if not isinstance(data, dict):
        return
    data['pane_id'] = str(pane_id)
    if str(getattr(session, 'terminal', '') or '').strip().lower() == 'tmux':
        data['tmux_session'] = str(pane_id)
    data['updated_at'] = now_str_fn()
    writer = getattr(session, '_write_back', None)
    if callable(writer):
        writer()


__all__ = [
    'activate_rebound_pane',
    'attach_pane_log',
    'backend_is_alive',
    'bind_session_to_pane',
    'classify_crash_reason',
    'lifecycle_backend_error_text',
    'live_owned_pane',
    'MAX_PANE_CRASH_LOGS',
    'pane_exists',
    'pane_lifecycle_target',
    'persist_crash_log',
]
