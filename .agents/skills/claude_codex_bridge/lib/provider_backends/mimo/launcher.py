from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import shlex
from pathlib import Path

from agents.models import AgentSpec, normalize_agent_name
from cli.context import CliContext
from cli.models import ParsedStartCommand
from provider_core.caller_env import (
    caller_context_env,
    export_env_clause,
    join_env_prefix,
    provider_user_session_env,
)
from provider_core.contracts import ProviderRuntimeLauncher
from provider_core.inherited_skills import packaged_inherited_skill_file
from provider_core.memory_projection import write_projection_event_and_marker
from provider_core.runtime_shared import apply_provider_command_template, provider_start_parts
from provider_profiles import load_resolved_provider_profile
from project_memory import materialize_runtime_memory_bundle
from project_memory.hashing import sha256_text
from storage.atomic import atomic_write_text
from workspace.models import WorkspacePlan

from .home import materialize_mimo_home


def build_runtime_launcher() -> ProviderRuntimeLauncher:
    return ProviderRuntimeLauncher(
        provider="mimo",
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
    del runtime_dir
    state_dir = context.paths.agent_provider_state_dir(spec.name, "mimo")
    mimo_home = state_dir / "home"
    payload = dict(prepared_state or {})
    payload["agent_name"] = spec.name
    payload["project_root"] = str(context.project.project_root)
    payload["workspace_path"] = str(payload.get("run_cwd") or plan.workspace_path)
    payload["agent_events_path"] = str(context.paths.agent_events_path(spec.name))
    payload["mimo_home"] = str(mimo_home)
    payload["mimo_config_path"] = str(state_dir / "mimocode.json")
    payload["mimo_storage_root"] = str(mimo_home / "data" / "storage")
    return payload


def build_start_cmd(
    command: ParsedStartCommand,
    spec: AgentSpec,
    runtime_dir,
    launch_session_id: str,
    *,
    prepared_state: dict[str, object] | None = None,
) -> str:
    runtime_dir = Path(runtime_dir)
    launch_context = prepared_state or {}
    mimo_home = _path_or_none(launch_context.get("mimo_home"))
    if mimo_home is None:
        raise RuntimeError("MiMo launch requires prepare_launch_context before build_start_cmd")
    profile = load_resolved_provider_profile(runtime_dir)
    materialize_mimo_home(mimo_home, profile=profile)
    mimo_env = {
        "HOME": str(mimo_home),
        "MIMOCODE_HOME": str(mimo_home),
        "MIMOCODE_DISABLE_AUTOUPDATE": "true",
        "MIMOCODE_ENABLE_ANALYSIS": "false",
        **_mimo_config_env(_path_or_none(launch_context.get("mimo_config_path")), profile),
    }
    if "WSL_DISTRO_NAME" in os.environ:
        mimo_env["USERPROFILE"] = str(mimo_home)
        additions = "HOME/p:USERPROFILE/p:MIMOCODE_HOME/p"
        existing = os.environ.get("WSLENV", "")
        mimo_env["WSLENV"] = f"{additions}:{existing}" if existing else additions
    cmd_parts = provider_start_parts("mimo")
    if command.restore:
        cmd_parts.append("--continue")
    cmd_parts.extend(spec.startup_args)
    cmd = " ".join(shlex.quote(str(part)) for part in cmd_parts)
    cmd = apply_provider_command_template(cmd, spec.provider_command_template)
    env_prefix = join_env_prefix(
        export_env_clause(provider_user_session_env()),
        export_env_clause(spec.env),
        export_env_clause(mimo_env),
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
        "mimo_home": str(prepared_state.get("mimo_home") or ""),
        "mimo_storage_root": str(prepared_state.get("mimo_storage_root") or ""),
        "mimo_config_path": str(prepared_state.get("mimo_config_path") or ""),
    }


@dataclass(frozen=True)
class MimoMemoryConfigResult:
    env: dict[str, str]


@dataclass(frozen=True)
class _Bridge:
    path: Path
    instruction: str
    sha256: str = ""
    unchanged: bool = True
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RenderedConfig:
    text: str
    sha256: str


def materialize_mimo_memory_config(
    *,
    project_root: Path,
    agent_name: str,
    workspace_path: Path | None,
    config_path: Path | None,
    profile,
    event_path: Path | None,
    marker_path: Path,
) -> MimoMemoryConfigResult:
    if config_path is None:
        _record_projection_event(
            _projection_result(status="failed", reason="missing_config_path", path=Path("")),
            event_path=event_path,
            marker_path=marker_path,
            agent_name=agent_name,
        )
        return MimoMemoryConfigResult(env={})

    inherit_memory = _inherits_memory(profile)
    skill_bridge = _bridge_mimo_ask_skill(
        project_root=project_root,
        agent_name=agent_name,
        enabled=True,
    )
    if not inherit_memory and not skill_bridge.instruction:
        _remove_file(config_path)
        result = _projection_result(
            status="skipped",
            reason="inherit_context_disabled",
            path=Path(""),
            config_path=config_path,
            skill_path=skill_bridge.path,
            skill_sha256=skill_bridge.sha256,
            warnings=skill_bridge.warnings,
        )
        _record_projection_event(result, event_path=event_path, marker_path=marker_path, agent_name=agent_name)
        return MimoMemoryConfigResult(env={})

    materialization = None
    memory_bridge = _Bridge(path=Path(""), instruction="", unchanged=True)
    if inherit_memory:
        materialization = materialize_runtime_memory_bundle(
            project_root,
            agent_name=agent_name,
            provider="mimo",
            workspace_path=workspace_path,
        )
        if not materialization.sha256 or not _path_is_set(materialization.path):
            result = _projection_result(
                status="failed",
                reason="bundle_write_failed",
                path=Path(materialization.path or ""),
                config_path=config_path,
                source_count=len(materialization.sources),
                warnings=(*materialization.warnings, *skill_bridge.warnings),
                skill_path=skill_bridge.path,
                skill_sha256=skill_bridge.sha256,
            )
            _record_projection_event(result, event_path=event_path, marker_path=marker_path, agent_name=agent_name)
            return MimoMemoryConfigResult(env={})

        memory_bridge = _bridge_mimo_memory_bundle(
            project_root=project_root,
            agent_name=agent_name,
            source_bundle_path=materialization.path,
        )
        if not _path_is_set(memory_bridge.path):
            result = _projection_result(
                status="failed",
                reason="bridge_write_failed",
                path=Path(""),
                config_path=config_path,
                sha256=materialization.sha256,
                source_count=len(materialization.sources),
                warnings=(*materialization.warnings, *memory_bridge.warnings, *skill_bridge.warnings),
                skill_path=skill_bridge.path,
                skill_sha256=skill_bridge.sha256,
            )
            _record_projection_event(result, event_path=event_path, marker_path=marker_path, agent_name=agent_name)
            return MimoMemoryConfigResult(env={})

    rendered = _render_mimo_config(
        memory_instruction=memory_bridge.instruction,
        skill_instructions=(skill_bridge.instruction,) if skill_bridge.instruction else (),
    )
    try:
        config_unchanged = _text_file_sha256(config_path) == rendered.sha256
        if not config_unchanged:
            atomic_write_text(config_path, rendered.text)
    except OSError as exc:
        warnings = (
            *((materialization.warnings if materialization is not None else ())),
            *memory_bridge.warnings,
            *skill_bridge.warnings,
            str(exc),
        )
        result = _projection_result(
            status="failed",
            reason=type(exc).__name__,
            path=_first_path(memory_bridge.path, skill_bridge.path),
            config_path=config_path,
            sha256=materialization.sha256 if materialization is not None else "",
            config_sha256=rendered.sha256,
            source_count=len(materialization.sources) if materialization is not None else 0,
            warnings=warnings,
            skill_path=skill_bridge.path,
            skill_sha256=skill_bridge.sha256,
        )
        _record_projection_event(result, event_path=event_path, marker_path=marker_path, agent_name=agent_name)
        return MimoMemoryConfigResult(env={})

    materialization_unchanged = True if materialization is None else materialization.unchanged
    status = "skipped" if materialization_unchanged and memory_bridge.unchanged and skill_bridge.unchanged and config_unchanged else "ok"
    result = _projection_result(
        status=status,
        reason="unchanged" if status == "skipped" else "written",
        path=_first_path(memory_bridge.path, skill_bridge.path),
        config_path=config_path,
        sha256=materialization.sha256 if materialization is not None else "",
        config_sha256=rendered.sha256,
        source_count=len(materialization.sources) if materialization is not None else 0,
        warnings=(
            *((materialization.warnings if materialization is not None else ())),
            *memory_bridge.warnings,
            *skill_bridge.warnings,
        ),
        bundle_path=materialization.path if materialization is not None else None,
        skill_path=skill_bridge.path,
        skill_sha256=skill_bridge.sha256,
    )
    _record_projection_event(result, event_path=event_path, marker_path=marker_path, agent_name=agent_name)
    return MimoMemoryConfigResult(env={"MIMOCODE_CONFIG": str(config_path)})


def _mimo_config_env(config_path: Path | None, profile) -> dict[str, str]:
    if config_path is None or not _inherits_context(profile):
        return {}
    if not Path(config_path).is_file():
        return {}
    return {"MIMOCODE_CONFIG": str(config_path)}


def _bridge_mimo_memory_bundle(*, project_root: Path, agent_name: str, source_bundle_path: Path) -> _Bridge:
    root = Path(project_root).expanduser()
    normalized_agent = normalize_agent_name(agent_name)
    bridge_path = root / ".ccb" / "runtime" / "memory" / f"{normalized_agent}.md"
    instruction = f".ccb/runtime/memory/{normalized_agent}.md"
    source_path = Path(source_bundle_path).expanduser()
    try:
        if _same_path(source_path, bridge_path):
            return _Bridge(path=bridge_path, instruction=instruction, unchanged=True)
        text = source_path.read_text(encoding="utf-8")
        digest = sha256_text(text)
        if _text_file_sha256(bridge_path) == digest:
            return _Bridge(path=bridge_path, instruction=instruction, sha256=digest, unchanged=True)
        atomic_write_text(bridge_path, text)
        return _Bridge(path=bridge_path, instruction=instruction, sha256=digest, unchanged=False)
    except Exception as exc:
        return _Bridge(
            path=Path(""),
            instruction=instruction,
            unchanged=False,
            warnings=(f"failed_to_write_mimo_memory_bridge: {exc}",),
        )


def _bridge_mimo_ask_skill(*, project_root: Path, agent_name: str, enabled: bool) -> _Bridge:
    root = Path(project_root).expanduser()
    normalized_agent = normalize_agent_name(agent_name)
    skill_path = root / ".ccb" / "runtime" / "skills" / normalized_agent / "mimo" / "ask.md"
    instruction = f".ccb/runtime/skills/{normalized_agent}/mimo/ask.md"
    if not enabled:
        _remove_file(skill_path)
        return _Bridge(path=Path(""), instruction="", unchanged=True)
    source = packaged_inherited_skill_file("mimo", "ask.md")
    if not source.is_file():
        _remove_file(skill_path)
        return _Bridge(
            path=Path(""),
            instruction="",
            unchanged=False,
            warnings=(f"mimo_ask_skill_missing: {source}",),
        )
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        _remove_file(skill_path)
        return _Bridge(
            path=Path(""),
            instruction="",
            unchanged=False,
            warnings=(f"mimo_ask_skill_read_failed: {exc}",),
        )
    digest = sha256_text(text)
    if _text_file_sha256(skill_path) == digest:
        return _Bridge(path=skill_path, instruction=instruction, sha256=digest, unchanged=True)
    try:
        atomic_write_text(skill_path, text)
    except OSError as exc:
        return _Bridge(
            path=Path(""),
            instruction="",
            unchanged=False,
            warnings=(f"mimo_ask_skill_write_failed: {exc}",),
        )
    return _Bridge(path=skill_path, instruction=instruction, sha256=digest, unchanged=False)


def _render_mimo_config(*, memory_instruction: str, skill_instructions: tuple[str, ...]) -> _RenderedConfig:
    payload: dict[str, object] = {
        "autoupdate": False,
        "instructions": _merge_instruction_entries(memory_instruction, *skill_instructions),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return _RenderedConfig(text=text, sha256=sha256_text(text))


def _merge_instruction_entries(*entries: str) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        stripped = str(entry or "").strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        merged.append(stripped)
    return merged


def _projection_result(
    *,
    status: str,
    reason: str,
    path: Path,
    sha256: str = "",
    source_count: int = 0,
    warnings: tuple[str, ...] | list[str] = (),
    bundle_path: Path | None = None,
    config_path: Path | None = None,
    config_sha256: str = "",
    skill_path: Path | None = None,
    skill_sha256: str = "",
) -> dict[str, object]:
    result = {
        "status": status,
        "reason": reason,
        "path": _text_or_empty(path),
        "sha256": sha256,
        "source_count": source_count,
        "warnings": tuple(str(item) for item in warnings if str(item)),
        "config_path": _text_or_empty(config_path),
        "config_sha256": _text_or_empty(config_sha256),
        "skill_path": _text_or_empty(skill_path),
        "skill_sha256": _text_or_empty(skill_sha256),
    }
    if bundle_path is not None:
        result["bundle_path"] = str(bundle_path)
    return result


def _record_projection_event(result: dict[str, object], *, event_path: Path | None, marker_path: Path, agent_name: str) -> None:
    if event_path is None:
        return
    signature = {
        "status": _text_or_empty(result.get("status")),
        "reason": _text_or_empty(result.get("reason")),
        "path": _text_or_empty(result.get("path")),
        "config_path": _text_or_empty(result.get("config_path")),
        "bundle_path": _text_or_empty(result.get("bundle_path")),
        "sha256": _text_or_empty(result.get("sha256")),
        "config_sha256": _text_or_empty(result.get("config_sha256")),
        "warnings": list(result.get("warnings") or ()),
        "skill_path": _text_or_empty(result.get("skill_path")),
        "skill_sha256": _text_or_empty(result.get("skill_sha256")),
    }
    marker = Path(marker_path)
    try:
        existing = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        existing = None
    if existing == signature:
        return
    event = {
        "record_type": "agent_event",
        "event_type": f"mimo_memory_projection_{signature['status'] or 'unknown'}",
        "provider": "mimo",
        "agent_name": agent_name,
        "status": signature["status"],
        "reason": signature["reason"],
        "projection_path": signature["path"],
        "config_path": signature["config_path"],
        "bundle_path": signature["bundle_path"],
        "sha256": signature["sha256"],
        "bundle_sha256": signature["sha256"],
        "config_sha256": signature["config_sha256"],
        "source_count": int(result.get("source_count") or 0),
        "warnings": signature["warnings"],
        "skill_path": signature["skill_path"],
        "skill_sha256": signature["skill_sha256"],
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    write_projection_event_and_marker(event, signature, event_path=event_path, marker_path=marker)


def _inherits_memory(profile) -> bool:
    return True if profile is None else bool(getattr(profile, "inherit_memory", True))


def _inherits_context(profile) -> bool:
    del profile
    return True


def _path_or_none(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    return Path(text).expanduser()


def _path_is_set(value: object) -> bool:
    if isinstance(value, Path):
        return bool(str(value))
    return bool(value)


def _first_path(*paths: Path) -> Path:
    for path in paths:
        if _path_is_set(path):
            return path
    return Path("")


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except Exception:
        return str(left) == str(right)


def _text_file_sha256(path: Path) -> str:
    try:
        return sha256_text(path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def _text_or_empty(value: object) -> str:
    if isinstance(value, Path) and not _path_is_set(value):
        return ""
    return str(value) if value else ""


def _remove_file(path: Path | None) -> None:
    if path is None or not _path_is_set(path):
        return
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


__all__ = [
    "MimoMemoryConfigResult",
    "build_runtime_launcher",
    "build_start_cmd",
    "materialize_mimo_memory_config",
    "prepare_launch_context",
]
