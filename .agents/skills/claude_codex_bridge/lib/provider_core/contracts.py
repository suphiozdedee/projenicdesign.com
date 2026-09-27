from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from agents.models import AgentSpec
from cli.context import CliContext
from cli.models import ParsedStartCommand
from provider_execution.base import ProviderExecutionAdapter
from workspace.models import WorkspacePlan

from .manifests import ProviderManifest


@dataclass(frozen=True)
class ProviderRuntimeIdentity:
    state: str
    reason: str | None = None


@dataclass(frozen=True)
class ProviderSessionBinding:
    provider: str
    load_session: Callable[[Path, str | None], object | None]
    session_id_attr: str
    session_path_attr: str
    live_runtime_identity: Callable[[object], ProviderRuntimeIdentity | None] | None = None

    def __post_init__(self) -> None:
        provider = str(self.provider or '').strip().lower()
        if not provider:
            raise ValueError('provider cannot be empty')
        object.__setattr__(self, 'provider', provider)


@dataclass(frozen=True)
class ProviderRuntimeLauncher:
    provider: str
    launch_mode: Literal['simple_tmux', 'codex_tmux']
    # prepare_runtime creates provider-local runtime artifacts.
    # prepare_launch_context may then add project/agent/workspace/event context needed
    # by launch command assembly. Callers must pass the final prepared_state to
    # build_start_cmd; providers that depend on launch context should fail fast
    # when required keys are missing instead of inferring identity from paths.
    # resolve_run_cwd is also used during pre-launch provider preparation; in that
    # phase launch_session_id is None and must not be treated as session authority.
    build_start_cmd: Callable[..., str]
    build_session_payload: Callable[[CliContext, AgentSpec, WorkspacePlan, Path, Path, str, str, str, dict[str, object]], dict[str, object]]
    prepare_runtime: Callable[[Path], dict[str, object]] | None = None
    prepare_launch_context: Callable[[CliContext, AgentSpec, WorkspacePlan, Path, dict[str, object]], dict[str, object]] | None = None
    post_launch: Callable[[object, str, Path, str, dict[str, object]], None] | None = None
    resolve_run_cwd: Callable[[ParsedStartCommand, AgentSpec, WorkspacePlan, Path, str | None], Path | str | None] | None = None

    def __post_init__(self) -> None:
        provider = str(self.provider or '').strip().lower()
        if not provider:
            raise ValueError('provider cannot be empty')
        object.__setattr__(self, 'provider', provider)


@dataclass(frozen=True)
class ProviderBackend:
    manifest: ProviderManifest
    execution_adapter: ProviderExecutionAdapter | None = None
    session_binding: ProviderSessionBinding | None = None
    runtime_launcher: ProviderRuntimeLauncher | None = None

    @property
    def provider(self) -> str:
        return self.manifest.provider


__all__ = ['ProviderBackend', 'ProviderRuntimeIdentity', 'ProviderRuntimeLauncher', 'ProviderSessionBinding']
