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
from provider_core.runtime_shared import apply_provider_command_template, provider_start_parts
from provider_model_shortcuts import provider_model_runtime_env
from provider_profiles import load_resolved_provider_profile
from provider_thinking_shortcuts import provider_thinking_runtime_env
from workspace.models import WorkspacePlan

from .home import materialize_deepseek_home


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return ProviderRuntimeLauncher(
        provider="deepseek",
        launch_mode="simple_tmux",
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
    payload = dict(prepared_state or {})
    payload["agent_name"] = spec.name
    payload["project_root"] = str(context.project.project_root)
    payload["workspace_path"] = str(prepared_state.get("run_cwd") or plan.workspace_path)
    payload["agent_events_path"] = str(context.paths.agent_events_path(spec.name))
    payload["deepseek_home"] = str(
        context.paths.agent_provider_state_dir(spec.name, "deepseek") / "home"
    )
    return payload


def build_start_cmd(
    command: ParsedStartCommand,
    spec: AgentSpec,
    runtime_dir,
    launch_session_id: str,
    *,
    prepared_state: dict[str, object] | None = None,
) -> str:
    del command
    runtime_dir = Path(runtime_dir)
    launch_context = prepared_state or {}
    profile = load_resolved_provider_profile(runtime_dir)
    deepseek_home = Path(
        str(launch_context.get("deepseek_home") or (runtime_dir / "deepseek-home"))
    ).expanduser()
    materialize_deepseek_home(deepseek_home, profile=profile)
    private_env = {"HOME": str(deepseek_home)}
    if "WSL_DISTRO_NAME" in os.environ:
        private_env["USERPROFILE"] = str(deepseek_home)
        additions = "HOME/p:USERPROFILE/p"
        existing = os.environ.get("WSLENV", "")
        private_env["WSLENV"] = f"{additions}:{existing}" if existing else additions
    runtime_env = {
        **(dict(profile.env) if profile is not None else {}),
        **spec.env,
        **provider_model_runtime_env(spec.provider, model=spec.model),
        **provider_thinking_runtime_env(spec.provider, thinking=spec.thinking),
    }
    cmd_parts = provider_start_parts("deepseek")
    cmd_parts.extend(spec.startup_args)
    cmd = " ".join(shlex.quote(str(part)) for part in cmd_parts)
    cmd = apply_provider_command_template(cmd, spec.provider_command_template)
    env_prefix = join_env_prefix(
        export_env_clause(provider_user_session_env()),
        export_env_clause(runtime_env),
        export_env_clause(private_env),
        export_env_clause(
            caller_context_env(actor=spec.name, runtime_dir=runtime_dir, launch_session_id=launch_session_id)
        ),
    )
    if env_prefix:
        return f"{env_prefix}; {cmd}"
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
    prepared = prepared_state or {}
    return {
        "ccb_session_id": launch_session_id,
        "agent_name": spec.name,
        "ccb_project_id": context.project.project_id,
        "runtime_dir": str(runtime_dir),
        "completion_artifact_dir": str(runtime_dir / "completion"),
        "terminal": "tmux",
        "tmux_session": pane_id,
        "pane_id": pane_id,
        "pane_title_marker": pane_title_marker,
        "workspace_path": str(plan.workspace_path),
        "work_dir": str(run_cwd),
        "start_dir": str(context.project.project_root),
        "start_cmd": start_cmd,
        "deepseek_home": str(prepared.get("deepseek_home") or ""),
    }


__all__ = ["build_runtime_launcher", "build_start_cmd", "prepare_launch_context"]
