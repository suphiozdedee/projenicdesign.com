from __future__ import annotations

from pathlib import Path

from provider_core.runtime_shared import provider_start_parts
from provider_profiles import load_resolved_provider_profile, provider_api_env_keys

from .command_runtime import build_codex_shell_prefix as _build_codex_shell_prefix_impl
from .command_runtime import build_start_cmd as _build_start_cmd_impl
from .command_runtime import prepare_codex_home_overrides as _prepare_codex_home_overrides_impl
from .command_runtime import resolve_codex_home_layout as _resolve_codex_home_layout_impl
from .command_runtime.managed_app_server import (
    build_managed_app_server_command,
    supports_managed_app_server,
    supports_session_fork,
)
from .session_paths import load_linked_continuation_session_id, load_resume_session_id


def build_start_cmd(
    command,
    spec,
    runtime_dir: Path,
    launch_session_id: str,
    *,
    prepared_state: dict[str, object] | None = None,
) -> str:
    return _build_start_cmd_impl(
        command,
        spec,
        runtime_dir,
        launch_session_id,
        prepared_state=prepared_state,
        load_resolved_provider_profile_fn=load_resolved_provider_profile,
        prepare_codex_home_overrides_fn=prepare_codex_home_overrides,
        provider_start_parts_fn=provider_start_parts,
        load_resume_session_id_fn=load_resume_session_id,
        load_linked_continuation_session_id_fn=load_linked_continuation_session_id,
        build_codex_shell_prefix_fn=build_codex_shell_prefix,
        supports_session_fork_fn=supports_session_fork,
        supports_managed_app_server_fn=supports_managed_app_server,
        build_managed_app_server_command_fn=build_managed_app_server_command,
    )


def build_codex_shell_prefix(*, profile) -> list[str]:
    return _build_codex_shell_prefix_impl(profile=profile, provider_api_env_keys_fn=provider_api_env_keys)


def prepare_codex_home_overrides(runtime_dir: Path, profile, **kwargs) -> dict[str, str]:
    return _prepare_codex_home_overrides_impl(runtime_dir, profile, **kwargs)


def resolve_codex_home_layout(runtime_dir: Path, profile):
    return _resolve_codex_home_layout_impl(runtime_dir, profile)


__all__ = ['build_codex_shell_prefix', 'build_start_cmd', 'prepare_codex_home_overrides', 'resolve_codex_home_layout']
