from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from provider_backends.pane_log_support.session import (
    PaneLogProjectSessionBase,
    build_session_binding_for_provider,
    compute_session_key_for_provider,
    load_project_session_for_provider,
)
from provider_core.contracts import ProviderSessionBinding


@dataclass
class DeepSeekProjectSession(PaneLogProjectSessionBase):
    @property
    def deepseek_session_id(self) -> str:
        return str(self.data.get("deepseek_session_id") or self.data.get("ccb_session_id") or "").strip()

    @property
    def deepseek_session_path(self) -> str:
        return str(self.session_file)

    @property
    def deepseek_home(self) -> str:
        return str(self.data.get("deepseek_home") or "").strip()

    def backend(self):
        from terminal_runtime import get_backend_for_session

        return get_backend_for_session(self.data)


def find_project_session_file(work_dir: Path, instance: Optional[str] = None) -> Optional[Path]:
    from provider_backends.pane_log_support.session import find_project_session_file_for_provider

    return find_project_session_file_for_provider(
        work_dir,
        session_filename=".deepseek-session",
        instance=instance,
    )


def load_project_session(work_dir: Path, instance: Optional[str] = None) -> Optional[DeepSeekProjectSession]:
    return load_project_session_for_provider(
        work_dir,
        session_filename=".deepseek-session",
        session_cls=DeepSeekProjectSession,
        instance=instance,
    )


def compute_session_key(session: DeepSeekProjectSession, instance: Optional[str] = None) -> str:
    return compute_session_key_for_provider(session, provider="deepseek", instance=instance)


def build_session_binding() -> ProviderSessionBinding:
    return build_session_binding_for_provider(provider="deepseek", load_session=load_project_session)


__all__ = [
    "DeepSeekProjectSession",
    "build_session_binding",
    "compute_session_key",
    "find_project_session_file",
    "load_project_session",
]
