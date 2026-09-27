from __future__ import annotations

import os
import shlex
from pathlib import Path

from agents.models import AgentSpec
from cli.context import CliContext
from cli.models import ParsedStartCommand
from provider_core.caller_env import (
    caller_context_env,
    export_env_clause,
    join_env_prefix,
    provider_user_session_env,
)
from provider_core.contracts import ProviderRuntimeLauncher
from provider_core.one_way_inheritance import ensure_private_descendant_directory, ensure_private_directory
from provider_core.runtime_shared import apply_provider_command_template, provider_start_parts
from workspace.models import WorkspacePlan

from .home import managed_droid_home_for_runtime


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return ProviderRuntimeLauncher(
        provider='droid',
        launch_mode='simple_tmux',
        prepare_launch_context=prepare_launch_context,
        build_start_cmd=build_start_cmd,
        build_session_payload=build_session_payload,
    )


def prepare_launch_context(
    context: CliContext,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir: Path,
    prepared_state: dict[str, object],
) -> dict[str, object]:
    del context, spec, plan
    payload = dict(prepared_state or {})
    droid_home = ensure_private_directory(managed_droid_home_for_runtime(runtime_dir))
    factory_home = ensure_private_descendant_directory(droid_home, Path('.factory'))
    payload['droid_home'] = str(droid_home)
    payload['factory_home'] = str(factory_home)
    payload['droid_sessions_root'] = str(factory_home / 'sessions')
    payload['factory_sessions_root'] = str(factory_home / 'sessions')
    return payload


def build_start_cmd(
    command: ParsedStartCommand,
    spec: AgentSpec,
    runtime_dir,
    launch_session_id: str,
    *,
    prepared_state: dict[str, object] | None = None,
) -> str:
    cmd_parts = provider_start_parts('droid')
    if command.restore:
        cmd_parts.append('-r')
    cmd_parts.extend(spec.startup_args)
    cmd = ' '.join(shlex.quote(str(part)) for part in cmd_parts)
    cmd = apply_provider_command_template(cmd, spec.provider_command_template)
    runtime_dir = Path(runtime_dir)
    droid_home = _droid_home(runtime_dir, prepared_state)
    factory_home = _factory_home(droid_home, prepared_state)
    droid_sessions_root = _droid_sessions_root(factory_home, prepared_state)
    private_env = {
        'HOME': str(droid_home),
        # Current Factory releases treat FACTORY_HOME_OVERRIDE as HOME and
        # append ".factory". Keep FACTORY_HOME for older releases that expect
        # the state directory itself.
        'FACTORY_HOME_OVERRIDE': str(droid_home),
        'FACTORY_HOME': str(factory_home),
        'FACTORY_SESSIONS_ROOT': str(droid_sessions_root),
        'DROID_SESSIONS_ROOT': str(droid_sessions_root),
        # Factory's supported switch prevents a managed login/logout or token
        # refresh from using the user's OS keyring. The private auth.encrypted
        # projection remains the only mutable credential authority.
        'FACTORY_DISABLE_KEYRING': 'true',
    }
    if 'WSL_DISTRO_NAME' in os.environ:
        private_env['USERPROFILE'] = str(droid_home)
        additions = (
            'HOME/p:USERPROFILE/p:FACTORY_HOME_OVERRIDE/p:FACTORY_HOME/p:'
            'FACTORY_SESSIONS_ROOT/p:DROID_SESSIONS_ROOT/p:'
            'FACTORY_DISABLE_KEYRING'
        )
        existing = os.environ.get('WSLENV', '')
        private_env['WSLENV'] = f'{additions}:{existing}' if existing else additions
    env_prefix = join_env_prefix(
        export_env_clause(provider_user_session_env()),
        # Apply the private Factory roots after ambient session transport so
        # an inherited WSLENV cannot discard or redirect the isolation vars.
        export_env_clause(private_env),
        export_env_clause(
            caller_context_env(actor=spec.name, runtime_dir=runtime_dir, launch_session_id=launch_session_id)
        ),
    )
    if env_prefix:
        return f'{env_prefix}; {cmd}'
    return cmd


def build_session_payload(
    context: CliContext,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir,
    run_cwd,
    pane_id: str,
    pane_title_marker: str,
    start_cmd: str,
    launch_session_id: str,
    prepared_state: dict[str, object],
) -> dict[str, object]:
    droid_home = _droid_home(Path(runtime_dir), prepared_state)
    factory_home = _factory_home(droid_home, prepared_state)
    droid_sessions_root = _droid_sessions_root(factory_home, prepared_state)
    return {
        'ccb_session_id': launch_session_id,
        'agent_name': spec.name,
        'ccb_project_id': context.project.project_id,
        'runtime_dir': str(runtime_dir),
        'completion_artifact_dir': str(runtime_dir / 'completion'),
        'terminal': 'tmux',
        'tmux_session': pane_id,
        'pane_id': pane_id,
        'pane_title_marker': pane_title_marker,
        'workspace_path': str(plan.workspace_path),
        'work_dir': str(run_cwd),
        'start_dir': str(context.project.project_root),
        'droid_home': str(droid_home),
        'factory_home': str(factory_home),
        'droid_sessions_root': str(droid_sessions_root),
        'factory_sessions_root': str(droid_sessions_root),
        'start_cmd': start_cmd,
    }


def _droid_home(runtime_dir: Path, prepared_state: dict[str, object] | None) -> Path:
    raw = str((prepared_state or {}).get('droid_home') or '').strip()
    if raw:
        return Path(raw).expanduser()
    return managed_droid_home_for_runtime(runtime_dir)


def _factory_home(droid_home: Path, prepared_state: dict[str, object] | None) -> Path:
    raw = str((prepared_state or {}).get('factory_home') or '').strip()
    if raw:
        return Path(raw).expanduser()
    return droid_home / '.factory'


def _droid_sessions_root(factory_home: Path, prepared_state: dict[str, object] | None) -> Path:
    raw = str((prepared_state or {}).get('droid_sessions_root') or '').strip()
    if raw:
        return Path(raw).expanduser()
    raw = str((prepared_state or {}).get('factory_sessions_root') or '').strip()
    if raw:
        return Path(raw).expanduser()
    return factory_home / 'sessions'


__all__ = ['build_runtime_launcher', 'build_start_cmd', 'prepare_launch_context']
