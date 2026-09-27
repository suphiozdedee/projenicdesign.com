from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re

from provider_sessions.files import resolve_project_config_dir

from .pathing import read_json

_ENV_ASSIGNMENT_RE = re.compile(
    r"(?:^|[\s;])(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)="
    r"(?P<value>'[^']*'|\"[^\"]*\"|[^\s;]+)"
)


def codex_home_path(data: Mapping[str, object] | None) -> Path | None:
    if not isinstance(data, Mapping):
        return None
    explicit = _normalize_path(data.get("codex_home"))
    if explicit is not None:
        return explicit
    for command in _commands(data):
        codex_home = _extract_command_path(command, "CODEX_HOME")
        if codex_home is not None:
            return codex_home
    session_root = codex_session_root_path(data)
    if session_root is not None and session_root.name == "sessions":
        return session_root.parent
    return None


def codex_session_root_path(data: Mapping[str, object] | None) -> Path | None:
    if not isinstance(data, Mapping):
        return None
    root = _normalize_path(data.get("codex_session_root"))
    if root is not None:
        return root
    codex_home = _normalize_path(data.get("codex_home"))
    if codex_home is not None:
        return codex_home / "sessions"
    for command in _commands(data):
        session_root = _extract_command_path(command, "CODEX_SESSION_ROOT")
        if session_root is not None:
            return session_root
        command_home = _extract_command_path(command, "CODEX_HOME")
        if command_home is not None:
            return command_home / "sessions"
    return _session_root_from_log_path(data.get("codex_session_path"))


def has_bound_codex_session(data: Mapping[str, object] | None) -> bool:
    if not isinstance(data, Mapping):
        return False
    if str(data.get("codex_session_id") or "").strip():
        return True
    return bool(str(data.get("codex_session_path") or "").strip())


def should_follow_workspace_sessions(
    *, work_dir: Path | None, session_file: Path | None, session_data: Mapping[str, object] | None = None
) -> bool:
    normalized_work_dir = _normalize_path(work_dir)
    if normalized_work_dir is None:
        return False
    if has_bound_codex_session(session_data):
        return False
    if session_file is None:
        return True

    matching_files = _session_files_for_work_dir(normalized_work_dir)
    if not matching_files:
        return True

    normalized_session_file = _normalize_path(session_file)
    if normalized_session_file is None:
        return len(matching_files) == 1
    return len(matching_files) == 1 and normalized_session_file in matching_files


def _session_files_for_work_dir(work_dir: Path) -> set[Path]:
    matches: set[Path] = set()
    for candidate in _candidate_session_files(work_dir):
        candidate_work_dir = _candidate_work_dir(candidate)
        if candidate_work_dir == work_dir:
            matches.add(candidate)
    return matches


def _candidate_session_files(work_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in (resolve_project_config_dir(work_dir), work_dir):
        if not root.is_dir():
            continue
        for candidate in sorted(root.glob(".codex*-session")):
            normalized_candidate = _normalize_path(candidate)
            if normalized_candidate is None or normalized_candidate in seen or not normalized_candidate.is_file():
                continue
            seen.add(normalized_candidate)
            candidates.append(normalized_candidate)
    return candidates


def _candidate_work_dir(session_file: Path) -> Path | None:
    data = read_json(session_file)
    raw = (
        data.get("work_dir")
        or data.get("work_dir_norm")
        or data.get("workspace_path")
        or data.get("start_dir")
    )
    return _normalize_path(raw)


def _commands(data: Mapping[str, object]) -> tuple[str, str]:
    return (
        str(data.get("codex_start_cmd") or "").strip(),
        str(data.get("start_cmd") or "").strip(),
    )


def _extract_command_path(command: str, env_name: str) -> Path | None:
    if not command:
        return None
    for match in _ENV_ASSIGNMENT_RE.finditer(command):
        if match.group("name") != env_name:
            continue
        return _normalize_path(_unquote_env_value(match.group("value")))
    return None


def _unquote_env_value(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _session_root_from_log_path(value: object) -> Path | None:
    log_path = _normalize_path(value)
    if log_path is None:
        return None
    for parent in (log_path.parent, *log_path.parents):
        if parent.name == "sessions":
            return parent
    return None


def _normalize_path(value: object) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except Exception:
        try:
            return Path(raw).expanduser()
        except Exception:
            return None


__all__ = ["codex_home_path", "codex_session_root_path", "has_bound_codex_session", "should_follow_workspace_sessions"]
