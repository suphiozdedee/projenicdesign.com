from __future__ import annotations

import os
from pathlib import Path
import threading
import time

from provider_backends.codex.comm_runtime.binding import extract_session_id
from provider_backends.codex.comm_runtime.log_reader_facade import CodexLogReader
from provider_backends.codex.session import CodexProjectSession
from provider_backends.codex.session_runtime.follow_policy import codex_session_root_path, should_follow_workspace_sessions
from provider_backends.codex.session_switch import STATE_AUTO_REBINDABLE, STATE_BOUND, STATE_SWITCHED_UNBOUND, commit_rebind, resolve_switch_decision, write_decision
from provider_core.comm_logging import get_comm_logger, log_comm_event

from .env import env_float, path_or_none, read_session_data, session_root, session_work_dir

_logger = get_comm_logger('codex.bridge')


class CodexBindingTracker:
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = runtime_dir
        self.session_file = session_file_from_env()
        self._poll_interval = env_float("CCB_CODEX_BIND_POLL_INTERVAL", 5.0)
        self._thread: threading.Thread | None = None
        self._running = False
        self._deferred_switch_signature: tuple[object, ...] | None = None

    def start(self) -> None:
        if self.session_file is None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="codex-binding-tracker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _loop(self) -> None:
        while self._running:
            try:
                self.refresh_once()
            except Exception as exc:
                log_comm_event(
                    _logger,
                    provider='codex',
                    direction='recv',
                    endpoint=str(self.session_file),
                    event='binding_refresh_failed',
                    error=exc,
            )
            time.sleep(max(1.0, self._poll_interval))

    def _should_skip_deferred_switch_scan(self, data: dict[str, object]) -> bool:
        signature = self._deferred_switch_signature
        return signature is not None and signature == _switch_scan_signature(data)

    def _remember_deferred_switch_scan(self, data: dict[str, object], decision) -> None:
        if (
            decision is not None
            and decision.state in {STATE_BOUND, STATE_SWITCHED_UNBOUND}
            and int(getattr(decision.evidence, "running_job_count", 0) or 0) == 0
        ):
            self._deferred_switch_signature = _switch_scan_signature(data)
            return
        self._deferred_switch_signature = None

    def refresh_once(self) -> bool:
        context = refresh_context(self.session_file)
        if context is None:
            return False

        if self._should_skip_deferred_switch_scan(context["data"]):
            return False

        switched, decision = _resolve_and_maybe_rebind(
            context["data"],
            session_file=context["session_file"],
            runtime_dir=self.runtime_dir,
        )
        self._remember_deferred_switch_scan(context["data"], decision)
        if switched:
            return True

        log_path = _resolved_log_path_after_switch_check(
            context["data"],
            decision=decision,
            session_file=context["session_file"],
        )
        if log_path is None:
            return False

        session = CodexProjectSession(session_file=self.session_file, data=context["data"])
        before = binding_snapshot(context["data"])
        session.update_codex_log_binding(log_path=str(log_path), session_id=extract_session_id(log_path))
        return before != binding_snapshot(session.data)


def session_file_from_env() -> Path | None:
    raw_session_file = str(os.environ.get("CCB_SESSION_FILE") or "").strip()
    if not raw_session_file:
        return None
    return Path(raw_session_file).expanduser()


def refresh_context(session_file: Path | None) -> dict[str, object] | None:
    if session_file is None or not session_file.is_file():
        return None
    data = read_session_data(session_file)
    if not isinstance(data, dict):
        return None
    work_dir = session_work_dir(data)
    if work_dir is None:
        return None
    return {"data": data, "work_dir": work_dir, "session_file": session_file}


def _resolved_log_path_after_switch_check(data: dict[str, object], *, decision, session_file: Path | None) -> Path | None:
    if decision is not None and decision.state in {"bound", STATE_SWITCHED_UNBOUND}:
        current = path_or_none(data.get("codex_session_path"))
        if current is not None and current.is_file():
            return current
    return current_log_path(data, session_file=session_file)


def current_log_path(data: dict[str, object], *, session_file: Path | None) -> Path | None:
    work_dir = session_work_dir(data)
    log_reader = CodexLogReader(
        root=session_root(data),
        log_path=path_or_none(data.get("codex_session_path")),
        session_id_filter=str(data.get("codex_session_id") or "").strip() or None,
        work_dir=work_dir,
        follow_workspace_sessions=should_follow_workspace_sessions(
            work_dir=work_dir,
            session_file=session_file,
            session_data=data,
        ),
    )
    log_path = log_reader.current_log_path()
    if log_path is None or not log_path.is_file():
        return None
    return log_path


def auto_rebind_switched_session(
    data: dict[str, object],
    *,
    session_file: Path,
    runtime_dir: Path,
) -> bool:
    switched, _decision = _resolve_and_maybe_rebind(data, session_file=session_file, runtime_dir=runtime_dir)
    return switched


def _resolve_and_maybe_rebind(
    data: dict[str, object],
    *,
    session_file: Path,
    runtime_dir: Path,
):
    decision = resolve_switch_decision(data, session_file=session_file, runtime_dir=runtime_dir)
    if decision.state != STATE_AUTO_REBINDABLE or decision.candidate is None:
        if decision.state != "bound":
            write_decision(runtime_dir, decision, committed=False)
        return False, decision
    return commit_rebind(
        session_file=session_file,
        session_data=data,
        candidate=decision.candidate,
        runtime_dir=runtime_dir,
        reason=decision.reason,
    ), decision


def _switch_scan_signature(data: dict[str, object]) -> tuple[object, ...]:
    root = codex_session_root_path(data) or session_root(data)
    current_path = path_or_none(data.get("codex_session_path"))
    current_mtime = _path_mtime(current_path)
    count = 0
    newest = -1.0
    try:
        paths = root.glob("**/*.jsonl")
        for path in paths:
            if not path.is_file():
                continue
            count += 1
            mtime = _path_mtime(path)
            if mtime is not None and mtime > newest:
                newest = mtime
    except OSError:
        return (str(root), str(current_path or ""), current_mtime, "scan-error")
    return (str(root), str(current_path or ""), current_mtime, count, newest)


def _path_mtime(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def binding_snapshot(data: dict[str, object]) -> tuple[str, str, str, str]:
    return (
        str(data.get("codex_session_path") or "").strip(),
        str(data.get("codex_session_id") or "").strip(),
        str(data.get("codex_start_cmd") or "").strip(),
        str(data.get("start_cmd") or "").strip(),
    )


__all__ = ["CodexBindingTracker", "auto_rebind_switched_session"]
