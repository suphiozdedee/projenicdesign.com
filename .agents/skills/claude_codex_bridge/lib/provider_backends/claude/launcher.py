from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from agents.models import AgentSpec
from cli.context import CliContext
from cli.models import ParsedStartCommand
from cli.services.role_command_policy import ensure_role_command_policy_supported
from provider_backends.runtime_restore import ProviderRestoreTarget
from provider_backends.session_authority import current_provider_authority_fingerprint
from provider_core.runtime_shared import provider_start_parts
from provider_profiles import ResolvedProviderProfile, load_resolved_provider_profile
from workspace.models import WorkspacePlan

from .launcher_runtime import (
    build_claude_env_prefix as _build_claude_env_prefix_impl,
    build_runtime_launcher as _build_runtime_launcher_impl,
    build_session_payload as _build_session_payload_impl,
    build_start_cmd as _build_start_cmd_impl,
    claude_history_state as _claude_history_state_impl,
    claude_user_api_env as _claude_user_api_env_impl,
    claude_user_base_url as _claude_user_base_url_impl,
    local_tcp_listener_available as _local_tcp_listener_available_impl,
    prepare_launch_context as _prepare_launch_context_impl,
    prepare_claude_home_overrides as _prepare_claude_home_overrides_impl,
    prepare_runtime as _prepare_runtime_impl,
    project_session_restore_target as _project_session_restore_target_impl,
    resolve_claude_restore_target as _resolve_claude_restore_target_impl,
    resolve_claude_home_layout as _resolve_claude_home_layout_impl,
    resolve_run_cwd as _resolve_run_cwd_impl,
    should_drop_claude_base_url as _should_drop_claude_base_url_impl,
    write_claude_settings_overlay as _write_claude_settings_overlay_impl,
)
from .session import load_project_session


def build_runtime_launcher():
    return _build_runtime_launcher_impl(
        prepare_runtime_fn=prepare_runtime,
        prepare_launch_context_fn=prepare_launch_context,
        build_start_cmd_fn=build_start_cmd,
        build_session_payload_fn=build_session_payload,
        resolve_run_cwd_fn=resolve_run_cwd,
    )


def prepare_runtime(runtime_dir: Path) -> dict[str, object]:
    return _prepare_runtime_impl(runtime_dir)


def prepare_launch_context(
    context: CliContext,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir: Path,
    prepared_state: dict[str, object],
) -> dict[str, object]:
    return _prepare_launch_context_impl(context, spec, plan, runtime_dir, prepared_state)


def build_start_cmd(
    command: ParsedStartCommand,
    spec: AgentSpec,
    runtime_dir: Path,
    launch_session_id: str,
    *,
    prepared_state: dict[str, object] | None = None,
) -> str:
    project_root = _path_or_none((prepared_state or {}).get('project_root'))
    if project_root is None:
        raise RuntimeError('Claude launch requires prepare_launch_context before build_start_cmd')
    agent_events_path = _path_or_none((prepared_state or {}).get('agent_events_path'))
    command_policy = ensure_role_command_policy_supported(spec=spec)
    return _build_start_cmd_impl(
        command,
        spec,
        runtime_dir,
        launch_session_id,
        load_profile_fn=load_resolved_provider_profile,
        prepare_home_overrides_fn=lambda runtime, profile, **kwargs: _prepare_claude_home_overrides_impl(
            runtime,
            profile,
            refresh_home=False,
            project_root=project_root,
            memory_projection_event_path=agent_events_path,
            memory_projection_marker_path=Path(runtime) / 'claude-memory-projection.json',
            command_policy=command_policy,
            **kwargs,
        ),
        write_settings_overlay_fn=write_claude_settings_overlay,
        build_env_prefix_fn=build_claude_env_prefix,
        resolve_restore_target_fn=_resolve_claude_restore_target,
        provider_start_parts_fn=provider_start_parts,
        cli_supports_flag_fn=claude_cli_supports_flag,
        is_root_user_fn=is_root_user,
        prepared_state=prepared_state,
    )


def _path_or_none(value: object) -> Path | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser()
    except Exception:
        return None


def resolve_run_cwd(
    command: ParsedStartCommand,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir: Path,
    launch_session_id: str | None,
) -> Path | str | None:
    return _resolve_run_cwd_impl(
        command,
        spec,
        plan,
        runtime_dir,
        launch_session_id,
        resolve_restore_target_fn=_resolve_claude_restore_target,
    )


def build_session_payload(
    context: CliContext,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir: Path,
    run_cwd: Path,
    pane_id: str,
    pane_title_marker: str,
    start_cmd: str,
    launch_session_id: str,
    prepared_state: dict[str, object],
) -> dict[str, object]:
    profile = load_resolved_provider_profile(runtime_dir)
    prepared_state = dict(prepared_state or {})
    prepared_state['claude_home_layout'] = _resolve_claude_home_layout_impl(runtime_dir, profile)
    prepared_state['claude_provider_authority_fingerprint'] = (
        current_provider_authority_fingerprint('claude', profile, runtime_dir)
    )
    return _build_session_payload_impl(
        context,
        spec,
        plan,
        runtime_dir,
        run_cwd,
        pane_id,
        pane_title_marker,
        start_cmd,
        launch_session_id,
        prepared_state,
    )


def _resolve_claude_restore_target(
    *,
    spec: AgentSpec,
    runtime_dir: Path,
    restore: bool,
    workspace_path: Path | None = None,
) -> ProviderRestoreTarget:
    return _resolve_claude_restore_target_impl(
        spec=spec,
        runtime_dir=runtime_dir,
        restore=restore,
        workspace_path=workspace_path,
        project_session_restore_target_fn=_project_session_restore_target,
        claude_history_state_fn=_claude_history_state,
        claude_home_layout_fn=_resolve_claude_home_layout_impl,
        load_profile_fn=load_resolved_provider_profile,
    )


def _project_session_restore_target(
    workspace_path: Path,
    session_instance: str | None,
    *,
    authority_fingerprint: str,
    managed_home: Path,
) -> ProviderRestoreTarget | None:
    return _project_session_restore_target_impl(
        workspace_path,
        session_instance,
        load_project_session_fn=load_project_session,
        claude_history_state_fn=_claude_history_state,
        managed_home=managed_home,
        authority_fingerprint=authority_fingerprint,
    )


def _claude_history_state(
    *,
    invocation_dir: Path,
    project_root: Path,
    include_env_pwd: bool,
    home_dir: Path | None = None,
) -> tuple[str | None, bool, Path | None]:
    return _claude_history_state_impl(
        invocation_dir=invocation_dir,
        project_root=project_root,
        env=os.environ if include_env_pwd else {},
        home_dir=home_dir or Path.home(),
    )


def write_claude_settings_overlay(
    runtime_dir: Path,
    *,
    profile: ResolvedProviderProfile | None = None,
) -> Path | None:
    return _write_claude_settings_overlay_impl(
        runtime_dir,
        profile=profile,
    )


def build_claude_env_prefix(
    *,
    profile: ResolvedProviderProfile | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    return _build_claude_env_prefix_impl(
        profile=profile,
        extra_env=extra_env,
        env=os.environ,
        should_drop_base_url_fn=should_drop_claude_base_url,
        claude_user_api_env_fn=claude_user_api_env,
        claude_user_base_url_fn=claude_user_base_url,
    )


def claude_user_api_env() -> dict[str, str]:
    return _claude_user_api_env_impl(user_settings_path=Path.home() / '.claude' / 'settings.json')


def claude_user_base_url() -> str:
    return _claude_user_base_url_impl(user_settings_path=Path.home() / '.claude' / 'settings.json')


def should_drop_claude_base_url(value: str) -> bool:
    return _should_drop_claude_base_url_impl(
        value,
        local_tcp_listener_available_fn=local_tcp_listener_available,
    )


def local_tcp_listener_available(host: str, port: int) -> bool:
    return _local_tcp_listener_available_impl(host, port)


def claude_cli_supports_flag(cmd_parts: list[str], flag: str) -> bool:
    normalized = str(flag or '').strip()
    if not normalized:
        return False
    help_text = _claude_help_text(tuple(str(part) for part in cmd_parts if str(part or '').strip()))
    return normalized in help_text


def _claude_help_text(cmd_parts: tuple[str, ...]) -> str:
    command = tuple(cmd_parts or ('claude',))
    try:
        # Some native Claude builds truncate help output at 8 KiB when stdout
        # is a pipe. Regular files preserve the complete option list.
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8', errors='replace') as stdout_file:
            with tempfile.TemporaryFile(mode='w+', encoding='utf-8', errors='replace') as stderr_file:
                completed = subprocess.run(
                    [*command, '--help'],
                    check=False,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    timeout=3,
                )
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read()
                stderr = stderr_file.read()
    except Exception:
        return ''
    return f'{completed.stdout or stdout}\n{completed.stderr or stderr}'


def is_root_user() -> bool:
    geteuid = getattr(os, 'geteuid', None)
    if geteuid is None:
        return False
    try:
        return int(geteuid()) == 0
    except Exception:
        return False


__all__ = [
    'build_claude_env_prefix',
    'claude_user_api_env',
    'claude_cli_supports_flag',
    'build_runtime_launcher',
    'build_start_cmd',
    'resolve_run_cwd',
    'write_claude_settings_overlay',
]
