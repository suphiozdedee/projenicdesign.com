from __future__ import annotations

from pathlib import Path

from .common import load_first_entry


def extract_cwd_from_log_file(log_path: Path) -> str | None:
    payload = codex_session_meta_payload(log_path)
    if payload is None:
        return None
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    return None


def codex_session_meta_payload(log_path: Path) -> dict[str, object] | None:
    entry = load_first_entry(log_path)
    if entry is None or entry.get("type") != "session_meta":
        return None
    payload = entry.get("payload")
    return dict(payload) if isinstance(payload, dict) else None


def is_codex_subagent_log(log_path: Path) -> bool:
    payload = codex_session_meta_payload(log_path)
    if payload is None:
        return False
    if str(payload.get("thread_source") or "").strip().lower() == "subagent":
        return True
    source = payload.get("source")
    return isinstance(source, dict) and isinstance(source.get("subagent"), dict)


__all__ = ["codex_session_meta_payload", "extract_cwd_from_log_file", "is_codex_subagent_log"]
