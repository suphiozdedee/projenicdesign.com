from __future__ import annotations

from pathlib import Path
from typing import Callable

from agents.models import AgentSpec
from cli.context import CliContext
from cli.models import ParsedStartCommand
from provider_backends.native_cli_support.launcher import (
    NativeCliLaunchConfig,
    build_session_payload as native_build_session_payload,
    build_start_cmd as native_build_start_cmd,
    prepare_launch_context as native_prepare_launch_context,
)
from provider_core.contracts import ProviderRuntimeLauncher
from provider_core.runtime_shared import provider_start_parts
from provider_profiles import load_resolved_provider_profile
from workspace.models import WorkspacePlan

from .skills import materialize_qoder_skills


_CONFIG_OPTION = "--config-dir"
_PERMISSION_OPTIONS = {"--dangerously-skip-permissions", "--permission-mode", "--yolo"}
ManagedConfigPreparer = Callable[[Path], None]


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return build_qoder_runtime_launcher(provider="qoder")


def build_qoder_runtime_launcher(
    *,
    provider: str,
    managed_config_preparer: ManagedConfigPreparer | None = None,
) -> ProviderRuntimeLauncher:
    return ProviderRuntimeLauncher(
        provider=provider,
        launch_mode="simple_tmux",
        prepare_launch_context=lambda context, spec, plan, runtime_dir, prepared_state: _prepare_launch_context(
            provider, context, spec, plan, runtime_dir, prepared_state
        ),
        build_start_cmd=lambda command, spec, runtime_dir, launch_session_id, prepared_state=None: _build_start_cmd(
            provider,
            command,
            spec,
            runtime_dir,
            launch_session_id,
            prepared_state=prepared_state,
            managed_config_preparer=managed_config_preparer,
        ),
        build_session_payload=lambda context, spec, plan, runtime_dir, run_cwd, pane_id, pane_title_marker, start_cmd, launch_session_id, prepared_state: _build_session_payload(
            provider,
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
        ),
    )


def prepare_launch_context(
    context: CliContext,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir: Path,
    prepared_state: dict[str, object],
) -> dict[str, object]:
    return _prepare_launch_context("qoder", context, spec, plan, runtime_dir, prepared_state)


def _prepare_launch_context(
    provider: str,
    context: CliContext,
    spec: AgentSpec,
    plan: WorkspacePlan,
    runtime_dir: Path,
    prepared_state: dict[str, object],
) -> dict[str, object]:
    return native_prepare_launch_context(
        _qoder_launch_config(provider),
        context,
        spec,
        plan,
        runtime_dir,
        prepared_state,
    )


def build_start_cmd(
    command: ParsedStartCommand,
    spec: AgentSpec,
    runtime_dir,
    launch_session_id: str,
    *,
    prepared_state: dict[str, object] | None = None,
) -> str:
    return _build_start_cmd(
        "qoder",
        command,
        spec,
        runtime_dir,
        launch_session_id,
        prepared_state=prepared_state,
    )


def _build_start_cmd(
    provider: str,
    command: ParsedStartCommand,
    spec: AgentSpec,
    runtime_dir,
    launch_session_id: str,
    *,
    prepared_state: dict[str, object] | None = None,
    managed_config_preparer: ManagedConfigPreparer | None = None,
) -> str:
    launch_context = prepared_state if prepared_state is not None else {}
    parts = [*provider_start_parts(provider), *spec.startup_args]
    explicit_config = _option_value(parts, _CONFIG_OPTION)
    if explicit_config:
        config_dir = Path(explicit_config).expanduser()
        if not config_dir.is_absolute():
            config_dir = Path(str(launch_context.get("workspace_path") or ".")) / config_dir
        launch_context[f"{provider}_config_dir"] = str(config_dir)
        launch_context[f"{provider}_managed_config_arg"] = False
    else:
        config_dir = _path_from_prepared(launch_context, f"{provider}_home", provider=provider)
        launch_context[f"{provider}_config_dir"] = str(config_dir)
        launch_context[f"{provider}_managed_config_arg"] = True
        if managed_config_preparer is not None:
            managed_config_preparer(config_dir)
    materialize_qoder_skills(
        provider=provider,
        config_dir=config_dir,
        profile=load_resolved_provider_profile(Path(runtime_dir)),
        project_root=_optional_path(launch_context.get("project_root")),
        agent_name=str(launch_context.get("agent_name") or "").strip() or None,
    )
    launch_context[f"{provider}_auto_permission_enabled"] = bool(command.auto_permission)
    launch_context[f"{provider}_managed_permission_arg"] = not any(
        _has_option(parts, option) for option in _PERMISSION_OPTIONS
    )
    explicit_permission_mode = _option_value(parts, "--permission-mode")
    if explicit_permission_mode:
        headless_permission_mode = explicit_permission_mode
    elif _has_option(parts, "--dangerously-skip-permissions") or _has_option(
        parts, "--yolo"
    ):
        headless_permission_mode = "bypass_permissions"
    elif command.auto_permission:
        headless_permission_mode = "auto"
    else:
        headless_permission_mode = "dont_ask"
    launch_context[f"{provider}_headless_permission_mode"] = headless_permission_mode
    return native_build_start_cmd(
        _qoder_launch_config(provider),
        command,
        spec,
        runtime_dir,
        launch_session_id,
        prepared_state=launch_context,
    )


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
    return _build_session_payload(
        "qoder",
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


def _build_session_payload(
    provider: str,
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
    payload = native_build_session_payload(
        _qoder_launch_config(provider),
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
    payload[f"{provider}_config_dir"] = str(
        prepared_state.get(f"{provider}_config_dir") or ""
    )
    payload[f"{provider}_auto_permission_enabled"] = bool(
        prepared_state.get(f"{provider}_auto_permission_enabled")
    )
    payload[f"{provider}_headless_permission_mode"] = str(
        prepared_state.get(f"{provider}_headless_permission_mode") or "dont_ask"
    )
    return payload


def _qoder_visible_args(
    prepared_state: dict[str, object],
    *,
    provider: str = "qoder",
) -> tuple[str, ...]:
    args: list[str] = []
    if bool(prepared_state.get(f"{provider}_managed_config_arg")):
        config_dir = _path_from_prepared(
            prepared_state, f"{provider}_config_dir", provider=provider
        )
        config_dir.mkdir(parents=True, exist_ok=True)
        args.extend([_CONFIG_OPTION, str(config_dir)])
    if bool(prepared_state.get(f"{provider}_managed_permission_arg")) and bool(
        prepared_state.get(f"{provider}_auto_permission_enabled")
    ):
        args.extend(["--permission-mode", "auto"])
    return tuple(args)


def _option_value(parts: list[str], option: str) -> str | None:
    for index, part in enumerate(parts):
        if part == option:
            return parts[index + 1] if index + 1 < len(parts) else None
        if part.startswith(f"{option}="):
            return part.split("=", 1)[1]
    return None


def _has_option(parts: list[str], option: str) -> bool:
    return any(part == option or part.startswith(f"{option}=") for part in parts)


def _path_from_prepared(
    prepared_state: dict[str, object],
    key: str,
    *,
    provider: str = "qoder",
) -> Path:
    raw = str(prepared_state.get(key) or "").strip()
    if not raw:
        raise RuntimeError(f"{provider} launch requires {key} in prepared_state")
    return Path(raw).expanduser()


def _optional_path(value: object) -> Path | None:
    raw = str(value or "").strip()
    return Path(raw).expanduser() if raw else None


def _qoder_launch_config(provider: str) -> NativeCliLaunchConfig:
    return NativeCliLaunchConfig(
        provider=provider,
        visible_args_builder=lambda state: _qoder_visible_args(state, provider=provider),
    )


__all__ = [
    "build_runtime_launcher",
    "build_qoder_runtime_launcher",
    "build_session_payload",
    "build_start_cmd",
    "prepare_launch_context",
]
