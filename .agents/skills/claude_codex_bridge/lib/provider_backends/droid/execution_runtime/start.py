from __future__ import annotations

from pathlib import Path

from ccbd.api_models import JobRecord
from completion.models import CompletionSourceKind
from provider_execution.active import PreparedActiveStart, prepare_active_start
from provider_execution.base import ProviderRuntimeContext, ProviderSubmission
from provider_execution.common import no_wrap_requested, preferred_session_path, send_prompt_to_runtime_target


def start_submission(
    job: JobRecord,
    *,
    context: ProviderRuntimeContext | None,
    now: str,
    provider: str,
    load_session_fn,
    backend_for_session_fn,
    reader_cls,
    request_anchor_fn,
    wrap_prompt_fn,
) -> ProviderSubmission:
    prepared = prepare_active_start(
        job,
        context=context,
        provider=provider,
        source_kind=CompletionSourceKind.TERMINAL_TEXT,
        now=now,
        missing_session_reason="missing_droid_session",
        load_session_fn=load_session_fn,
        backend_for_session_fn=backend_for_session_fn,
    )
    if not isinstance(prepared, PreparedActiveStart):
        return prepared

    reader = _reader_for_session(reader_cls, prepared.session)
    preferred = preferred_session_path(str(getattr(prepared.session, "droid_session_path", "") or ""), context.session_ref)
    if preferred is not None:
        reader.set_preferred_session(preferred)
    session_id = str(getattr(prepared.session, "droid_session_id", "") or "").strip()
    if session_id:
        reader.set_session_id_hint(session_id)
    state = reader.capture_state()
    request_anchor = request_anchor_fn(job.job_id)
    no_wrap = no_wrap_requested(job)
    prompt = job.request.body if no_wrap else wrap_prompt_fn(job.request.body, request_anchor)
    send_prompt_to_runtime_target(prepared.backend, prepared.pane_id, prompt)

    return ProviderSubmission(
        job_id=job.job_id,
        agent_name=job.agent_name,
        provider=provider,
        accepted_at=now,
        ready_at=now,
        source_kind=CompletionSourceKind.TERMINAL_TEXT,
        reply="",
        diagnostics={"provider": provider, "mode": "active", "workspace_path": str(prepared.work_dir)},
        runtime_state={
            "mode": "active",
            "reader": reader,
            "state": state,
            "backend": prepared.backend,
            "pane_id": prepared.pane_id,
            "request_anchor": request_anchor,
            "next_seq": 1,
            "anchor_seen": no_wrap,
            "reply_buffer": "",
            "raw_buffer": "",
            "session_path": state_session_path(state),
            "no_wrap": no_wrap,
        },
    )


def state_session_path(state: dict[str, object]) -> str:
    from .helpers import state_session_path as _state_session_path

    return _state_session_path(state)


def _reader_for_session(reader_cls, session):
    work_dir = Path(session.work_dir)
    root = _sessions_root_for_session(session)
    if root is None:
        return reader_cls(work_dir=work_dir)
    try:
        return reader_cls(root=root, work_dir=work_dir)
    except TypeError:
        return reader_cls(work_dir=work_dir)


def _sessions_root_for_session(session) -> Path | None:
    data = getattr(session, 'data', None)
    if not isinstance(data, dict):
        return None
    raw = str(data.get('droid_sessions_root') or data.get('factory_sessions_root') or '').strip()
    if raw:
        return Path(raw).expanduser()
    home = str(data.get('droid_home') or data.get('factory_home') or '').strip()
    if home:
        return Path(home).expanduser() / 'sessions'
    return None


__all__ = ["start_submission"]
