from __future__ import annotations

from pathlib import Path
from typing import Optional

from provider_backends.native_cli_support.session import (
    build_native_session_binding,
    compute_session_key,
    find_project_session_file as _find_project_session_file,
    load_native_project_session,
)
from provider_core.contracts import ProviderSessionBinding


def find_project_session_file(work_dir: Path, instance: Optional[str] = None) -> Optional[Path]:
    return _find_project_session_file(work_dir, provider="grok", session_filename=".grok-session", instance=instance)


def load_project_session(work_dir: Path, instance: Optional[str] = None):
    return load_native_project_session(work_dir, provider="grok", session_filename=".grok-session", instance=instance)


def build_session_binding() -> ProviderSessionBinding:
    return build_native_session_binding(provider="grok", session_filename=".grok-session")


__all__ = ["build_session_binding", "compute_session_key", "find_project_session_file", "load_project_session"]
