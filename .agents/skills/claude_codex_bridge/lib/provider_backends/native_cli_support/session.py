from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Optional

from provider_backends.pane_log_support.session import (
    PaneLogProjectSessionBase,
    build_session_binding_for_provider,
    compute_session_key_for_provider,
    load_project_session_for_provider,
)
from provider_core.contracts import ProviderSessionBinding


@dataclass
class NativeCliProjectSession(PaneLogProjectSessionBase):
    provider_name: ClassVar[str] = "native"

    @property
    def provider_session_id(self) -> str:
        return str(
            self.data.get(f"{self.provider_name}_session_id")
            or self.data.get("ccb_session_id")
            or ""
        ).strip()

    @property
    def provider_session_path(self) -> str:
        return str(self.session_file)

    def __getattr__(self, name: str):
        if name == f"{self.provider_name}_session_id":
            return self.provider_session_id
        if name == f"{self.provider_name}_session_path":
            return self.provider_session_path
        raise AttributeError(name)

    def backend(self):
        from terminal_runtime import get_backend_for_session

        return get_backend_for_session(self.data)


def make_session_class(provider: str):
    provider_name = str(provider or "").strip().lower()

    class ProviderProjectSession(NativeCliProjectSession):
        pass

    ProviderProjectSession.provider_name = provider_name
    ProviderProjectSession.__name__ = f"{provider_name.title().replace('_', '')}ProjectSession"
    return ProviderProjectSession


def find_project_session_file(
    work_dir: Path,
    *,
    provider: str,
    session_filename: str,
    instance: Optional[str] = None,
) -> Optional[Path]:
    from provider_backends.pane_log_support.session import find_project_session_file_for_provider

    del provider
    return find_project_session_file_for_provider(
        work_dir,
        session_filename=session_filename,
        instance=instance,
    )


def load_native_project_session(
    work_dir: Path,
    *,
    provider: str,
    session_filename: str,
    instance: Optional[str] = None,
):
    return load_project_session_for_provider(
        work_dir,
        session_filename=session_filename,
        session_cls=make_session_class(provider),
        instance=instance,
    )


def compute_session_key(session: NativeCliProjectSession, *, provider: str, instance: Optional[str] = None) -> str:
    return compute_session_key_for_provider(session, provider=provider, instance=instance)


def build_native_session_binding(*, provider: str, session_filename: str) -> ProviderSessionBinding:
    return build_session_binding_for_provider(
        provider=provider,
        load_session=lambda work_dir, instance=None: load_native_project_session(
            work_dir,
            provider=provider,
            session_filename=session_filename,
            instance=instance,
        ),
    )


__all__ = [
    "NativeCliProjectSession",
    "build_native_session_binding",
    "compute_session_key",
    "find_project_session_file",
    "load_native_project_session",
    "make_session_class",
]
