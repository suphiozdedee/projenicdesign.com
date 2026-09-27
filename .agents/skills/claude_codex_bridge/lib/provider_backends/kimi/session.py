from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Optional

from provider_backends.pane_log_support.session import (
    PaneLogProjectSessionBase,
    build_session_binding_for_provider,
    compute_session_key_for_provider,
    load_project_session_for_provider,
    read_session_json,
)
from provider_core.contracts import ProviderSessionBinding
from provider_sessions.files import safe_write_session
from project.identity import normalize_work_dir

from .native_log import kimi_code_project_dirname, kimi_project_hash


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
KIMI_RESTART_SESSION_MARKER = "__CCB_KIMI_EXACT_SESSION_7D5E2A19__"
_NATIVE_BINDING_KEYS = (
    "kimi_session_id",
    "kimi_session_path",
    "kimi_session_work_dir_norm",
    "kimi_session_bound_at",
    "kimi_session_binding_source",
    "kimi_session_store",
)


@dataclass
class KimiProjectSession(PaneLogProjectSessionBase):
    @property
    def kimi_session_id(self) -> str:
        return str(self.data.get("kimi_session_id") or "").strip()

    @property
    def kimi_session_path(self) -> str:
        return str(self.data.get("kimi_session_path") or self.session_file)

    @property
    def kimi_share_dir(self) -> str:
        return str(self.data.get("kimi_share_dir") or "").strip()

    @property
    def kimi_code_home(self) -> str:
        return str(self.data.get("kimi_code_home") or "").strip()

    @property
    def start_cmd(self) -> str:
        return prepare_restart_start_cmd(self)

    def backend(self):
        from terminal_runtime import get_backend_for_session

        return get_backend_for_session(self.data)


def find_project_session_file(work_dir: Path, instance: Optional[str] = None) -> Optional[Path]:
    from provider_backends.pane_log_support.session import find_project_session_file_for_provider

    return find_project_session_file_for_provider(
        work_dir,
        session_filename=".kimi-session",
        instance=instance,
    )


def load_project_session(work_dir: Path, instance: Optional[str] = None) -> Optional[KimiProjectSession]:
    return load_project_session_for_provider(
        work_dir,
        session_filename=".kimi-session",
        session_cls=KimiProjectSession,
        instance=instance,
    )


def compute_session_key(session: KimiProjectSession, instance: Optional[str] = None) -> str:
    return compute_session_key_for_provider(session, provider="kimi", instance=instance)


def build_session_binding() -> ProviderSessionBinding:
    return build_session_binding_for_provider(provider="kimi", load_session=load_project_session)


def resume_binding_for_launch(
    session_file: Path,
    *,
    agent_name: str,
    project_id: str,
    work_dir: Path,
    share_dir: Path,
    code_home: Path | None = None,
) -> dict[str, object]:
    if not session_file.is_file():
        return {"kimi_resume_status": "fresh_no_binding"}
    data = read_session_json(session_file)
    if not data:
        return {"kimi_resume_status": "fresh_invalid_session_record"}
    mismatch = _ccb_binding_mismatch(
        data,
        agent_name=agent_name,
        project_id=project_id,
        work_dir=work_dir,
    )
    if mismatch:
        return {"kimi_resume_status": f"fresh_{mismatch}"}
    session_id = str(data.get("kimi_session_id") or "").strip()
    session_path = str(data.get("kimi_session_path") or "").strip()
    recorded_share = str(data.get("kimi_share_dir") or "").strip()
    if not session_id or not session_path or not recorded_share:
        return {"kimi_resume_status": "fresh_no_observed_native_session"}
    if _resolved_path(Path(recorded_share)) != _resolved_path(share_dir):
        return {"kimi_resume_status": "fresh_share_dir_changed"}
    recorded_store = str(data.get("kimi_session_store") or "legacy").strip()
    recorded_code_home = str(data.get("kimi_code_home") or "").strip()
    if recorded_store == "kimi_code":
        if not code_home or not recorded_code_home:
            return {"kimi_resume_status": "fresh_code_home_missing"}
        if _resolved_path(Path(recorded_code_home)) != _resolved_path(code_home):
            return {"kimi_resume_status": "fresh_code_home_changed"}
    valid, reason = validate_native_session_binding(
        session_id=session_id,
        session_path=Path(session_path),
        work_dir=work_dir,
        share_dir=share_dir,
        code_home=code_home,
    )
    if not valid:
        return {"kimi_resume_status": f"fresh_{reason}"}
    return {
        "kimi_resume_status": "exact_session_ready",
        "kimi_resume_session_id": session_id,
        "kimi_resume_session_path": session_path,
        "kimi_resume_session_bound_at": str(data.get("kimi_session_bound_at") or "").strip(),
        "kimi_resume_binding_source": str(data.get("kimi_session_binding_source") or "").strip(),
    }


def persist_native_session_binding(
    session_file: Path,
    *,
    expected_ccb_session_id: str,
    agent_name: str,
    work_dir: Path,
    share_dir: Path,
    native_session_id: str,
    native_session_path: Path,
    observed_at: str,
    code_home: Path | None = None,
) -> tuple[bool, str | None]:
    data = read_session_json(session_file)
    if not data:
        return False, "session_record_missing_or_invalid"
    if str(data.get("ccb_session_id") or "").strip() != str(expected_ccb_session_id or "").strip():
        return False, "ccb_launch_session_changed"
    mismatch = _ccb_binding_mismatch(
        data,
        agent_name=agent_name,
        project_id=str(data.get("ccb_project_id") or "").strip(),
        work_dir=work_dir,
    )
    if mismatch:
        return False, mismatch
    valid, reason = validate_native_session_binding(
        session_id=native_session_id,
        session_path=native_session_path,
        work_dir=work_dir,
        share_dir=share_dir,
        code_home=code_home,
    )
    if not valid:
        return False, reason
    session_store = _native_session_store(
        session_id=native_session_id,
        session_path=native_session_path,
        work_dir=work_dir,
        share_dir=share_dir,
        code_home=code_home,
    )
    data.update(
        {
            "kimi_session_id": native_session_id,
            "kimi_session_path": str(native_session_path),
            "kimi_session_work_dir_norm": normalize_work_dir(work_dir),
            "kimi_share_dir": str(share_dir),
            "kimi_session_bound_at": str(observed_at or ""),
            "kimi_session_binding_source": "native_req_id_observation",
            "kimi_session_store": session_store or "legacy",
            "kimi_resume_status": "exact_session_bound",
        }
    )
    if code_home is not None:
        data["kimi_code_home"] = str(code_home)
    ok, error = safe_write_session(session_file, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return ok, error


def validate_native_session_binding(
    *,
    session_id: str,
    session_path: Path,
    work_dir: Path,
    share_dir: Path,
    code_home: Path | None = None,
) -> tuple[bool, str]:
    normalized_id = str(session_id or "").strip()
    if not _SESSION_ID_RE.fullmatch(normalized_id):
        return False, "native_session_id_invalid"
    candidate = Path(session_path).expanduser()
    if candidate.is_symlink() or candidate.parent.is_symlink():
        return False, "native_session_path_symlinked"
    session_store = _native_session_store(
        session_id=normalized_id,
        session_path=candidate,
        work_dir=work_dir,
        share_dir=share_dir,
        code_home=code_home,
    )
    if session_store is None:
        return False, "native_session_path_mismatch"
    managed_root = (
        Path(share_dir).expanduser()
        if session_store == "legacy"
        else Path(code_home).expanduser()
    )
    try:
        relative_parts = candidate.absolute().relative_to(managed_root.absolute()).parts
    except ValueError:
        return False, "native_session_path_mismatch"
    components = [managed_root]
    for part in relative_parts:
        components.append(components[-1] / part)
    if any(component.is_symlink() for component in components):
        return False, "native_session_path_symlinked"
    if _resolved_path(candidate) != _resolved_path(components[-1]):
        return False, "native_session_path_mismatch"
    if not candidate.is_file():
        return False, "native_session_missing"
    return True, ""


def _native_session_store(
    *,
    session_id: str,
    session_path: Path,
    work_dir: Path,
    share_dir: Path,
    code_home: Path | None,
) -> str | None:
    legacy_expected = (
        Path(share_dir).expanduser()
        / "sessions"
        / kimi_project_hash(work_dir)
        / session_id
        / "wire.jsonl"
    )
    candidate = _lexical_absolute_path(Path(session_path).expanduser())
    if candidate == _lexical_absolute_path(legacy_expected):
        return "legacy"
    if code_home is None:
        return None
    code_session_root = (
        Path(code_home).expanduser()
        / "sessions"
        / kimi_code_project_dirname(work_dir)
        / session_id
        / "agents"
    )
    try:
        relative = Path(candidate).relative_to(Path(_lexical_absolute_path(code_session_root)))
    except ValueError:
        return None
    if len(relative.parts) != 2 or relative.parts[1] != "wire.jsonl":
        return None
    if not _SESSION_ID_RE.fullmatch(relative.parts[0]):
        return None
    return "kimi_code"


def prepare_restart_start_cmd(session: KimiProjectSession) -> str:
    data = session.data
    current_cmd = str(data.get("start_cmd") or "").strip()
    if not current_cmd:
        return ""
    if bool(data.get("kimi_explicit_session_control")):
        return current_cmd

    command_template = str(data.get("kimi_restart_start_cmd_template") or "")
    fresh_cmd = render_restart_command(command_template, exact_args="") or current_cmd
    share_dir_text = str(data.get("kimi_share_dir") or "").strip()
    if not share_dir_text:
        _persist_fresh_restart(session, fresh_cmd, status="fresh_share_dir_missing")
        return fresh_cmd
    binding = resume_binding_for_launch(
        session.session_file,
        agent_name=str(data.get("agent_name") or ""),
        project_id=str(data.get("ccb_project_id") or ""),
        work_dir=Path(session.work_dir),
        share_dir=Path(share_dir_text),
        code_home=(
            Path(str(data.get("kimi_code_home") or ""))
            if str(data.get("kimi_code_home") or "").strip()
            else None
        ),
    )
    if binding.get("kimi_resume_status") != "exact_session_ready":
        _persist_fresh_restart(
            session,
            fresh_cmd,
            status=str(binding.get("kimi_resume_status") or "fresh_binding_invalid"),
        )
        return fresh_cmd
    if command_template.count(KIMI_RESTART_SESSION_MARKER) != 1:
        _persist_fresh_restart(session, fresh_cmd, status="fresh_restart_template_missing")
        return fresh_cmd
    capability_parts = data.get("kimi_capability_command_parts")
    if not isinstance(capability_parts, list):
        _persist_fresh_restart(session, fresh_cmd, status="fresh_capability_command_missing")
        return fresh_cmd
    capability_environ = dict(os.environ)
    capability_path = str(data.get("kimi_capability_path") or "").strip()
    if capability_path:
        capability_environ["PATH"] = capability_path
    resume_flag = resolve_exact_resume_flag(
        [str(part) for part in capability_parts if str(part).strip()],
        environ=capability_environ,
    )
    native_session_id = str(binding.get("kimi_resume_session_id") or "").strip()
    if resume_flag not in {"--session", "--resume"} or not native_session_id:
        _persist_fresh_restart(session, fresh_cmd, status="fresh_exact_session_unsupported")
        return fresh_cmd
    exact_cmd = render_restart_command(
        command_template,
        exact_args=f"{shlex.quote(resume_flag)} {shlex.quote(native_session_id)}",
    )
    if not exact_cmd:
        _persist_fresh_restart(session, fresh_cmd, status="fresh_restart_template_invalid")
        return fresh_cmd
    data["start_cmd"] = exact_cmd
    data["kimi_resume_status"] = "exact_session_selected"
    session._write_back()
    return exact_cmd


def resolve_exact_resume_flag(parts: list[str], *, environ: dict[str, str]) -> str | None:
    if not parts:
        return None
    try:
        result = subprocess.run(
            [*parts, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
            env=environ,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    help_text = f"{result.stdout}\n{result.stderr}"
    if "--session" in help_text:
        return "--session"
    if "--resume" in help_text:
        return "--resume"
    return None


def render_restart_command(command_template: str, *, exact_args: str) -> str:
    if command_template.count(KIMI_RESTART_SESSION_MARKER) != 1:
        return ""
    if exact_args:
        return command_template.replace(KIMI_RESTART_SESSION_MARKER, exact_args).strip()
    without_marker = command_template.replace(f"{KIMI_RESTART_SESSION_MARKER} ", "", 1)
    if without_marker == command_template:
        without_marker = command_template.replace(f" {KIMI_RESTART_SESSION_MARKER}", "", 1)
    return without_marker.strip()


def _persist_fresh_restart(session: KimiProjectSession, start_cmd: str, *, status: str) -> None:
    for key in _NATIVE_BINDING_KEYS:
        session.data.pop(key, None)
    session.data["start_cmd"] = start_cmd
    session.data["kimi_resume_status"] = status
    session._write_back()


def _ccb_binding_mismatch(
    data: dict[str, object],
    *,
    agent_name: str,
    project_id: str,
    work_dir: Path,
) -> str | None:
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
    native_work_dir = str(data.get("kimi_session_work_dir_norm") or "").strip()
    if native_work_dir and native_work_dir != normalize_work_dir(work_dir):
        return "native_work_dir_mismatch"
    return None


def _resolved_path(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except Exception:
        return Path(path).expanduser().absolute()


def _lexical_absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(str(Path(path).expanduser())))


__all__ = [
    "KIMI_RESTART_SESSION_MARKER",
    "KimiProjectSession",
    "build_session_binding",
    "compute_session_key",
    "find_project_session_file",
    "load_project_session",
    "persist_native_session_binding",
    "prepare_restart_start_cmd",
    "render_restart_command",
    "resolve_exact_resume_flag",
    "resume_binding_for_launch",
    "validate_native_session_binding",
]
