from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import ClassVar, Optional

from provider_backends.native_cli_support.session import (
    NativeCliProjectSession,
    compute_session_key,
    find_project_session_file as _find_project_session_file,
)
from provider_backends.pane_log_support.session import (
    build_session_binding_for_provider,
    load_project_session_for_provider,
    read_session_json,
)
from provider_core.contracts import ProviderSessionBinding
from provider_sessions.files import safe_write_session
from project.identity import normalize_work_dir


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PI_RESTART_SESSION_MARKER = "__CCB_PI_EXACT_SESSION_6E9A2F41__"
_NATIVE_BINDING_KEYS = (
    "pi_session_id",
    "pi_session_path",
    "pi_session_work_dir_norm",
    "pi_session_bound_at",
    "pi_session_binding_source",
    "pi_resume_status",
)


class PiProjectSession(NativeCliProjectSession):
    provider_name: ClassVar[str] = "pi"

    @property
    def pi_session_id(self) -> str:
        return str(self.data.get("pi_session_id") or "").strip()

    @property
    def pi_session_path(self) -> str:
        # The CCB record is not a Pi transcript. Keep the native path empty
        # until Pi has reported one so provider binding consumers cannot
        # mistake `.pi-session` for a JSONL conversation file.
        return str(self.data.get("pi_session_path") or "").strip()

    @property
    def start_cmd(self) -> str:
        return prepare_restart_start_cmd(self)

    def backend(self):
        from terminal_runtime import get_backend_for_session

        return get_backend_for_session(self.data)


def find_project_session_file(work_dir: Path, instance: Optional[str] = None) -> Optional[Path]:
    return _find_project_session_file(work_dir, provider="pi", session_filename=".pi-session", instance=instance)


def load_project_session(work_dir: Path, instance: Optional[str] = None) -> Optional[PiProjectSession]:
    return load_project_session_for_provider(
        work_dir,
        session_filename=".pi-session",
        session_cls=PiProjectSession,
        instance=instance,
    )


def build_session_binding() -> ProviderSessionBinding:
    return build_session_binding_for_provider(
        provider="pi",
        load_session=load_project_session,
    )


def resume_binding_for_launch(
    session_file: Path,
    *,
    agent_name: str,
    project_id: str,
    work_dir: Path,
    session_dir: Path,
) -> dict[str, object]:
    if not session_file.is_file():
        return {"pi_resume_status": "fresh_no_binding"}
    data = read_session_json(session_file)
    if not data:
        return {"pi_resume_status": "fresh_invalid_session_record"}
    mismatch = _ccb_binding_mismatch(data, agent_name=agent_name, project_id=project_id, work_dir=work_dir)
    if mismatch:
        return {"pi_resume_status": f"fresh_{mismatch}"}
    session_id = str(data.get("pi_session_id") or "").strip()
    session_path_raw = str(data.get("pi_session_path") or "").strip()
    if session_id and session_path_raw and not _is_legacy_ccb_session_id(session_id):
        session_path = _native_path_from_record(session_path_raw, session_dir=session_dir)
        valid, reason = validate_native_session_binding(
            session_id=session_id,
            session_path=session_path,
            work_dir=work_dir,
            session_dir=session_dir,
        )
        if not valid:
            return {"pi_resume_status": f"fresh_{reason}"}
        return {
            "pi_resume_status": "exact_session_ready",
            "pi_resume_session_id": session_id,
            "pi_resume_session_path": str(session_path),
            "pi_resume_session_work_dir_norm": str(data.get("pi_session_work_dir_norm") or normalize_work_dir(work_dir)),
            "pi_resume_session_bound_at": str(data.get("pi_session_bound_at") or "").strip(),
            "pi_resume_binding_source": str(data.get("pi_session_binding_source") or "").strip(),
        }

    # Before CCB persisted a native Pi binding, it stored the CCB launch id
    # as pi_session_id, or left the native path absent. Recover the latest
    # valid Pi transcript for either legacy record shape.
    if session_path_raw and not _is_legacy_ccb_session_id(session_id):
        return {"pi_resume_status": "fresh_no_observed_native_session"}
    preferred_id = session_id if session_id and not _is_legacy_ccb_session_id(session_id) else None
    discovered = _discover_latest_native_session(
        session_dir,
        work_dir,
        preferred_id=preferred_id,
    )
    if discovered is None:
        return {"pi_resume_status": "fresh_no_observed_native_session"}
    discovered_id, discovered_path = discovered
    return {
        "pi_resume_status": "exact_session_ready",
        "pi_resume_session_id": discovered_id,
        "pi_resume_session_path": str(discovered_path),
        "pi_resume_session_work_dir_norm": normalize_work_dir(work_dir),
        "pi_resume_session_bound_at": "",
        "pi_resume_binding_source": "legacy_native_session_discovery",
    }


def _discover_latest_native_session(
    session_dir: Path,
    work_dir: Path,
    *,
    preferred_id: str | None = None,
) -> tuple[str, Path] | None:
    try:
        candidates = sorted(
            (path for path in session_dir.glob("*.jsonl") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for candidate in candidates:
        try:
            with candidate.open("r", encoding="utf-8-sig") as stream:
                header = json.loads(stream.readline())
        except (OSError, ValueError):
            continue
        if not isinstance(header, dict) or header.get("type") != "session":
            continue
        native_id = str(header.get("id") or "").strip()
        if preferred_id and native_id != preferred_id:
            continue
        valid, _reason = validate_native_session_binding(
            session_id=native_id,
            session_path=candidate,
            work_dir=work_dir,
            session_dir=session_dir,
        )
        if valid:
            return native_id, candidate
    return None


def _is_legacy_ccb_session_id(session_id: str) -> bool:
    return str(session_id or "").strip().lower().startswith("ccb-")


def _native_path_from_record(raw_path: str, *, session_dir: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    return candidate if candidate.is_absolute() else Path(session_dir).expanduser() / candidate


def persist_native_session_binding(
    session_file: Path,
    *,
    expected_ccb_session_id: str,
    agent_name: str,
    project_id: str = "",
    work_dir: Path,
    session_dir: Path,
    native_session_id: str,
    native_session_path: Path,
    observed_at: str,
) -> tuple[bool, str | None]:
    data = read_session_json(session_file)
    if not data:
        return False, "session_record_missing_or_invalid"
    if str(data.get("ccb_session_id") or "").strip() != str(expected_ccb_session_id or "").strip():
        return False, "ccb_launch_session_changed"
    mismatch = _ccb_binding_mismatch(
        data,
        agent_name=agent_name,
        project_id=project_id,
        work_dir=work_dir,
    )
    if mismatch:
        return False, mismatch
    valid, reason = validate_native_session_binding(
        session_id=native_session_id,
        session_path=native_session_path,
        work_dir=work_dir,
        session_dir=session_dir,
    )
    if not valid:
        return False, reason
    latest = read_session_json(session_file)
    if not latest:
        return False, "session_record_missing_or_invalid"
    if str(latest.get("ccb_session_id") or "").strip() != str(expected_ccb_session_id or "").strip():
        return False, "ccb_launch_session_changed"
    mismatch = _ccb_binding_mismatch(
        latest,
        agent_name=agent_name,
        project_id=project_id,
        work_dir=work_dir,
    )
    if mismatch:
        return False, mismatch
    data = latest
    data.update(
        {
            "pi_session_id": str(native_session_id).strip(),
            "pi_session_path": str(native_session_path),
            "pi_session_work_dir_norm": normalize_work_dir(work_dir),
            "pi_session_bound_at": str(observed_at or ""),
            "pi_session_binding_source": "pi_session_start_observation",
            "pi_resume_status": "exact_session_bound",
        }
    )
    ok, error = safe_write_session(session_file, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return ok, error


def validate_native_session_binding(
    *,
    session_id: str,
    session_path: Path,
    work_dir: Path,
    session_dir: Path,
) -> tuple[bool, str]:
    normalized_id = str(session_id or "").strip()
    if not _SESSION_ID_RE.fullmatch(normalized_id):
        return False, "native_session_id_invalid"
    candidate = Path(session_path).expanduser()
    managed_root = Path(session_dir).expanduser()
    if candidate.is_symlink() or managed_root.is_symlink():
        return False, "native_session_path_symlinked"
    try:
        relative = candidate.absolute().relative_to(managed_root.absolute())
    except ValueError:
        return False, "native_session_path_mismatch"
    session_name = candidate.name
    if (
        len(relative.parts) != 1
        or candidate.suffix != ".jsonl"
        or not (
            session_name == f"{normalized_id}.jsonl"
            or session_name.endswith(f"_{normalized_id}.jsonl")
        )
    ):
        return False, "native_session_path_mismatch"
    components = [managed_root]
    for part in relative.parts:
        components.append(components[-1] / part)
    if any(component.is_symlink() for component in components):
        return False, "native_session_path_symlinked"
    if _resolved_path(candidate) != _resolved_path(components[-1]):
        return False, "native_session_path_mismatch"
    if not candidate.is_file():
        return False, "native_session_missing"
    try:
        with candidate.open("r", encoding="utf-8-sig") as stream:
            header = json.loads(stream.readline())
    except (OSError, ValueError):
        return False, "native_session_header_invalid"
    if not isinstance(header, dict) or header.get("type") != "session" or str(header.get("id") or "") != normalized_id:
        return False, "native_session_header_mismatch"
    recorded_cwd = str(header.get("cwd") or "").strip()
    if not recorded_cwd or normalize_work_dir(Path(recorded_cwd)) != normalize_work_dir(work_dir):
        return False, "native_work_dir_mismatch"
    return True, ""


def prepare_restart_start_cmd(session: PiProjectSession) -> str:
    data = session.data
    current_cmd = str(data.get("start_cmd") or "").strip()
    if not current_cmd or bool(data.get("pi_explicit_session_control")):
        return current_cmd
    command_template = str(data.get("pi_restart_start_cmd_template") or "")
    fresh_cmd = render_restart_command(command_template, exact_args="") or current_cmd
    session_dir_text = str(data.get("pi_session_dir") or "").strip()
    if not session_dir_text:
        _persist_fresh_restart(session, fresh_cmd, status="fresh_session_dir_missing")
        return fresh_cmd
    binding = resume_binding_for_launch(
        session.session_file,
        agent_name=str(data.get("agent_name") or ""),
        project_id=str(data.get("ccb_project_id") or ""),
        work_dir=Path(session.work_dir),
        session_dir=Path(session_dir_text),
    )
    if binding.get("pi_resume_status") != "exact_session_ready":
        _persist_fresh_restart(session, fresh_cmd, status=str(binding.get("pi_resume_status") or "fresh_binding_invalid"))
        return fresh_cmd
    if command_template.count(PI_RESTART_SESSION_MARKER) != 1:
        _persist_fresh_restart(session, fresh_cmd, status="fresh_restart_template_missing")
        return fresh_cmd
    exact_cmd = render_restart_command(
        command_template,
        exact_args=" ".join(
            ("--session", shlex.quote(str(binding.get("pi_resume_session_path") or "")))
        ),
    )
    if not exact_cmd:
        _persist_fresh_restart(session, fresh_cmd, status="fresh_restart_template_invalid")
        return fresh_cmd
    data["start_cmd"] = exact_cmd
    data["pi_resume_status"] = "exact_session_selected"
    session._write_back()
    return exact_cmd


def render_restart_command(command_template: str, *, exact_args: str) -> str:
    if command_template.count(PI_RESTART_SESSION_MARKER) != 1:
        return ""
    if exact_args:
        return command_template.replace(PI_RESTART_SESSION_MARKER, exact_args).strip()
    without_marker = command_template.replace(f"{PI_RESTART_SESSION_MARKER} ", "", 1)
    if without_marker == command_template:
        without_marker = command_template.replace(f" {PI_RESTART_SESSION_MARKER}", "", 1)
    return without_marker.strip()


def _persist_fresh_restart(session: PiProjectSession, start_cmd: str, *, status: str) -> None:
    for key in _NATIVE_BINDING_KEYS:
        session.data.pop(key, None)
    session.data["start_cmd"] = start_cmd
    session.data["pi_resume_status"] = status
    session._write_back()


def _ccb_binding_mismatch(data: dict[str, object], *, agent_name: str, project_id: str, work_dir: Path) -> str | None:
    if data.get("active") is False:
        return "inactive_session_record"
    if str(data.get("agent_name") or "").strip() != str(agent_name or "").strip():
        return "agent_mismatch"
    recorded_project = str(data.get("ccb_project_id") or "").strip()
    if project_id and recorded_project != project_id:
        return "project_mismatch"
    recorded_work_dir = str(data.get("work_dir_norm") or data.get("work_dir") or "").strip()
    if not recorded_work_dir or recorded_work_dir != normalize_work_dir(work_dir):
        return "work_dir_mismatch"
    native_work_dir = str(data.get("pi_session_work_dir_norm") or "").strip()
    if native_work_dir and native_work_dir != normalize_work_dir(work_dir):
        return "native_work_dir_mismatch"
    return None


def _resolved_path(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except Exception:
        return Path(path).expanduser().absolute()


__all__ = [
    "PI_RESTART_SESSION_MARKER",
    "PiProjectSession",
    "build_session_binding",
    "compute_session_key",
    "find_project_session_file",
    "load_project_session",
    "persist_native_session_binding",
    "prepare_restart_start_cmd",
    "render_restart_command",
    "resume_binding_for_launch",
    "validate_native_session_binding",
]
