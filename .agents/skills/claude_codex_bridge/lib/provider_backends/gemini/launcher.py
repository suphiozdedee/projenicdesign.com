from __future__ import annotations

from pathlib import Path
import subprocess

from agents.models import AgentSpec
from cli.context import CliContext
from cli.models import ParsedStartCommand
from provider_backends.runtime_restore import ProviderRestoreTarget
from provider_backends.session_authority import current_provider_authority_fingerprint
from provider_profiles import ResolvedProviderProfile
from workspace.models import WorkspacePlan

from .launcher_runtime import (
    build_gemini_env_prefix as _build_gemini_env_prefix_impl,
    prepare_gemini_home_overrides as _prepare_gemini_home_overrides_impl,
    build_runtime_launcher as _build_runtime_launcher_impl,
    build_session_payload as _build_session_payload_impl,
    build_start_cmd as _build_start_cmd_impl,
    resolve_gemini_restore_target as _resolve_gemini_restore_target_impl,
    resolve_gemini_home_layout as _resolve_gemini_home_layout_impl,
    resolve_run_cwd as _resolve_run_cwd_impl,
)
from .session import load_project_session
from provider_profiles import load_resolved_provider_profile


def build_runtime_launcher():
    return _build_runtime_launcher_impl(
        prepare_launch_context_fn=prepare_launch_context,
        build_start_cmd_fn=build_start_cmd,
        build_session_payload_fn=build_session_payload,
        resolve_run_cwd_fn=resolve_run_cwd,
    )


def prepare_launch_context(
    context: CliContext,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir: Path,
    prepared_state: dict[str, object],
) -> dict[str, object]:
    del runtime_dir
    payload = dict(prepared_state)
    payload['project_root'] = str(context.project.project_root)
    payload['workspace_path'] = str(prepared_state.get('run_cwd') or plan.workspace_path)
    payload['agent_events_path'] = str(context.paths.agent_events_path(spec.name))
    return payload


def build_start_cmd(
    command: ParsedStartCommand,
    spec: AgentSpec,
    runtime_dir,
    launch_session_id: str,
    *,
    prepared_state: dict[str, object] | None = None,
) -> str:
    launch_context = prepared_state or {}
    return _build_start_cmd_impl(
        command,
        spec,
        runtime_dir,
        launch_session_id,
        prepared_state=launch_context,
        resolve_restore_target_fn=_resolve_gemini_restore_target,
        prepare_home_overrides_fn=_prepare_gemini_home_overrides_impl,
        cli_supports_flag_fn=gemini_cli_supports_flag,
    )


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
        resolve_restore_target_fn=_resolve_gemini_restore_target,
    )


def build_session_payload(
    context: CliContext,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir,
    run_cwd: Path,
    pane_id: str,
    pane_title_marker: str,
    start_cmd: str,
    launch_session_id: str,
    prepared_state: dict[str, object],
) -> dict[str, object]:
    profile = load_resolved_provider_profile(Path(runtime_dir))
    prepared_state = dict(prepared_state or {})
    prepared_state['gemini_home_layout'] = _resolve_gemini_home_layout_impl(Path(runtime_dir), profile)
    prepared_state['gemini_provider_authority_fingerprint'] = (
        current_provider_authority_fingerprint('gemini', profile, Path(runtime_dir))
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


def _resolve_gemini_restore_target(
    *,
    spec: AgentSpec,
    runtime_dir: Path,
    restore: bool,
    workspace_path: Path | None = None,
) -> ProviderRestoreTarget:
    return _resolve_gemini_restore_target_impl(
        spec=spec,
        runtime_dir=runtime_dir,
        restore=restore,
        workspace_path=workspace_path,
        load_project_session_fn=load_project_session,
        load_profile_fn=load_resolved_provider_profile,
    )


def build_gemini_env_prefix(
    *,
    profile: ResolvedProviderProfile | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    return _build_gemini_env_prefix_impl(profile=profile, extra_env=extra_env)


def gemini_cli_supports_flag(cmd_parts: list[str], flag: str) -> bool:
    normalized = str(flag or '').strip()
    command = [str(part) for part in cmd_parts if str(part or '').strip()]
    if not normalized or not command:
        return False
    try:
        completed = subprocess.run(
            [*command, '--help'],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return False
    return normalized in f'{completed.stdout or ""}\n{completed.stderr or ""}'


__all__ = [
    "build_gemini_env_prefix",
    "build_runtime_launcher",
    "build_start_cmd",
    "gemini_cli_supports_flag",
    "resolve_run_cwd",
]
