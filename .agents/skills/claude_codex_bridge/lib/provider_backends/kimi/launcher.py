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
from provider_core.pathing import session_filename_for_agent
from provider_core.runtime_shared import apply_provider_command_template, provider_start_parts
from provider_backends.kimi.home import materialize_kimi_home
from provider_backends.kimi.session import (
    KIMI_RESTART_SESSION_MARKER,
    render_restart_command,
    resolve_exact_resume_flag as _resolve_exact_resume_flag,
    resume_binding_for_launch,
)
from provider_backends.kimi.skills import kimi_skill_dirs_for_launch
from project.identity import normalize_work_dir
from provider_profiles import load_resolved_provider_profile
from workspace.models import WorkspacePlan


_AUTO_FLAG = "--auto-approve"
_AUTO_FLAGS = {"--auto-approve", "--auto", "--yes", "-y", "--yolo"}
_SESSION_CONTROL_FLAGS = {"--continue", "--session", "--resume", "-C", "-S", "-r", "-c"}


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return ProviderRuntimeLauncher(
        provider="kimi",
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
    payload["kimi_skill_dirs"] = [
        str(path)
        for path in kimi_skill_dirs_for_launch(
            project_root=context.project.project_root,
            workspace_path=Path(str(payload["workspace_path"])),
            state_dir=context.paths.agent_provider_state_dir(spec.name, "kimi"),
            env=spec.env,
        )
    ]
    run_cwd = Path(str(payload["workspace_path"]))
    merged_env = dict(os.environ)
    merged_env.update({str(key): str(value) for key, value in spec.env.items()})
    state_dir = context.paths.agent_provider_state_dir(spec.name, "kimi")
    share_dir = state_dir / "home" / ".kimi"
    code_home = state_dir / "home" / ".kimi-code"
    materialize_kimi_home(
        share_dir,
        code_home,
        profile=load_resolved_provider_profile(Path(runtime_dir)),
    )
    payload["kimi_share_dir"] = str(share_dir)
    payload["kimi_code_home"] = str(code_home)
    payload["kimi_capability_path"] = str(merged_env.get("PATH") or "")
    session_file = context.paths.ccb_dir / session_filename_for_agent("kimi", spec.name)
    payload.update(
        resume_binding_for_launch(
            session_file,
            agent_name=spec.name,
            project_id=context.project.project_id,
            work_dir=run_cwd,
            share_dir=share_dir,
            code_home=code_home,
        )
    )
    if payload.get("kimi_resume_status") == "exact_session_ready":
        resume_flag = _resolve_exact_resume_flag(provider_start_parts("kimi"), environ=merged_env)
        if resume_flag:
            payload["kimi_resume_flag"] = resume_flag
        else:
            payload["kimi_resume_status"] = "fresh_exact_session_unsupported"
            payload.pop("kimi_resume_session_id", None)
            payload.pop("kimi_resume_session_path", None)
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
    runtime_dir = Path(runtime_dir)
    cmd_parts = provider_start_parts("kimi")
    launch_context["kimi_capability_command_parts"] = list(cmd_parts)
    if command.auto_permission and not _has_any(cmd_parts, _AUTO_FLAGS) and not _has_any(spec.startup_args, _AUTO_FLAGS):
        cmd_parts.append(_AUTO_FLAG)
    cmd_parts.extend(_skill_dir_args(launch_context.get("kimi_skill_dirs"), existing_parts=(*cmd_parts, *spec.startup_args)))
    session_parts = (*cmd_parts, *spec.startup_args)
    if _has_session_control(session_parts):
        launch_context["kimi_resume_status"] = "explicit_session_control"
        launch_context["kimi_explicit_session_control"] = True
    elif not command.restore:
        launch_context["kimi_resume_status"] = "fresh_restore_disabled"
    elif launch_context.get("kimi_resume_status") == "exact_session_ready":
        resume_flag = str(launch_context.get("kimi_resume_flag") or "").strip()
        resume_id = str(launch_context.get("kimi_resume_session_id") or "").strip()
        if resume_flag in {"--session", "--resume"} and resume_id:
            launch_context["kimi_resume_status"] = "exact_session_selected"
        else:
            launch_context["kimi_resume_status"] = "fresh_exact_session_unsupported"
    exact_session_args = ""
    if launch_context.get("kimi_resume_status") == "exact_session_selected":
        exact_session_args = " ".join(
            shlex.quote(str(part))
            for part in (
                launch_context.get("kimi_resume_flag"),
                launch_context.get("kimi_resume_session_id"),
            )
        )
    command_template_parts = [*cmd_parts, KIMI_RESTART_SESSION_MARKER, *spec.startup_args]
    command_template = " ".join(shlex.quote(str(part)) for part in command_template_parts)
    command_template = apply_provider_command_template(command_template, spec.provider_command_template)
    env_prefix = join_env_prefix(
        export_env_clause(provider_user_session_env()),
        export_env_clause(spec.env),
        # Root variables are deliberately applied after user/profile env so a
        # managed Kimi process cannot write into the external account home.
        export_env_clause(_kimi_private_env(launch_context)),
        export_env_clause(
            caller_context_env(actor=spec.name, runtime_dir=runtime_dir, launch_session_id=launch_session_id)
        ),
    )
    if env_prefix:
        command_template = f"{env_prefix}; {command_template}"
    if command_template.count(KIMI_RESTART_SESSION_MARKER) == 1:
        launch_context["kimi_restart_start_cmd_template"] = command_template
    else:
        launch_context.pop("kimi_restart_start_cmd_template", None)
    return render_restart_command(command_template, exact_args=exact_session_args)


def _kimi_private_env(launch_context: dict[str, object]) -> dict[str, str]:
    share = str(launch_context.get("kimi_share_dir") or "").strip()
    code_home = str(launch_context.get("kimi_code_home") or "").strip()
    if not share or not code_home:
        return {}
    home = str(Path(share).parent)
    env = {
        "HOME": home,
        "KIMI_SHARE_DIR": share,
        "KIMI_CODE_HOME": code_home,
    }
    if "WSL_DISTRO_NAME" in os.environ:
        env["USERPROFILE"] = home
        additions = "HOME/p:USERPROFILE/p:KIMI_SHARE_DIR/p:KIMI_CODE_HOME/p"
        existing = os.environ.get("WSLENV", "")
        env["WSLENV"] = f"{additions}:{existing}" if existing else additions
    return env


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
    payload: dict[str, object] = {
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
        "kimi_share_dir": str(prepared.get("kimi_share_dir") or ""),
        "kimi_code_home": str(prepared.get("kimi_code_home") or ""),
        "kimi_resume_status": str(prepared.get("kimi_resume_status") or "fresh_no_binding"),
        "kimi_explicit_session_control": bool(prepared.get("kimi_explicit_session_control")),
        "kimi_restart_start_cmd_template": str(prepared.get("kimi_restart_start_cmd_template") or ""),
        "kimi_capability_command_parts": [
            str(part)
            for part in (prepared.get("kimi_capability_command_parts") or ())
            if str(part).strip()
        ],
        "kimi_capability_path": str(prepared.get("kimi_capability_path") or ""),
    }
    if payload["kimi_resume_status"] == "exact_session_selected":
        payload.update(
            {
                "kimi_session_id": str(prepared.get("kimi_resume_session_id") or ""),
                "kimi_session_path": str(prepared.get("kimi_resume_session_path") or ""),
                "kimi_session_work_dir_norm": normalize_work_dir(run_cwd),
                "kimi_session_bound_at": str(prepared.get("kimi_resume_session_bound_at") or ""),
                "kimi_session_binding_source": str(
                    prepared.get("kimi_resume_binding_source") or "native_req_id_observation"
                ),
            }
        )
    return payload


def _has_any(parts: tuple[str, ...] | list[str], flags: set[str]) -> bool:
    normalized = {str(part).strip() for part in parts}
    return bool(flags & normalized)


def _has_session_control(parts: tuple[str, ...] | list[str]) -> bool:
    normalized = [str(part).strip() for part in parts]
    if any(part in _SESSION_CONTROL_FLAGS for part in normalized):
        return True
    return any(part.startswith(("--session=", "--resume=", "--continue=")) for part in normalized)


def _skill_dir_args(raw_dirs: object, *, existing_parts: tuple[str, ...] | list[str]) -> list[str]:
    args: list[str] = []
    if not isinstance(raw_dirs, (list, tuple)):
        return args
    for raw in raw_dirs:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_dir():
            continue
        value = str(path)
        if _has_option_value(existing_parts, "--skills-dir", value) or _has_option_value(args, "--skills-dir", value):
            continue
        args.extend(("--skills-dir", value))
    return args


def _has_option_value(parts: tuple[str, ...] | list[str], option: str, value: str) -> bool:
    normalized = [str(part).strip() for part in parts]
    for index, part in enumerate(normalized):
        if part == option and index + 1 < len(normalized) and normalized[index + 1] == value:
            return True
        if part == f"{option}={value}":
            return True
    return False


__all__ = ["build_runtime_launcher", "build_start_cmd", "prepare_launch_context"]
