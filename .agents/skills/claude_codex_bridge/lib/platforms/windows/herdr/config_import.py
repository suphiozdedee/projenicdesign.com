""""ccb config import-herdr" — A-lite import mode.

Reads the current Herdr session's workspace/pane topology and generates a
``.ccb/ccb.config`` draft.  Does NOT overwrite an existing config file.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .common import herdr_command_env, resolve_herdr_executable


def import_herdr_config(
    *,
    project_dir: str,
    output_path: str | None = None,
    herdr_executable: str | None = None,
    herdr_session: str | None = None,
    dry_run: bool = True,
    force: bool = False,
) -> dict[str, object]:
    """Generate a CCB config draft from the current Herdr topology.

    Args:
        project_dir: Absolute path to the CCB project directory.
        output_path: Optional explicit output path.  Defaults to
            ``<project_dir>/.ccb/ccb.config.herdr-import``.
        herdr_executable: Path to the ``herdr`` binary.  Auto-resolved if None.
        herdr_session: Herdr session name.  Auto-detected if None.
        dry_run: If True (default), print to stdout and do NOT write to
            ``.ccb/ccb.config``.
        force: If True, overwrite an existing output file.  Defaults to False
            (fail-fast when the target already exists).

    Returns:
        A dict with keys ``ok``, ``config``, ``warnings``, ``written_path``.
    """
    exe = resolve_herdr_executable(explicit=herdr_executable)
    if not exe:
        return {"ok": False, "reason": "Herdr executable not found", "config": None, "warnings": []}

    # -- snapshot -----------------------------------------------------------
    snapshot = _herdr_snapshot(exe, session=herdr_session)
    if snapshot is None:
        return {"ok": False, "reason": "Failed to read Herdr session snapshot", "config": None, "warnings": []}

    # -- build config -------------------------------------------------------
    config, warnings = _build_ccb_config(snapshot, project_dir=project_dir)
    config["_herdr_import_meta"] = {
        "herdr_version": snapshot.get("version", "unknown"),
        "herdr_session": snapshot.get("session_name", herdr_session or "unknown"),
        "imported_at": _now_iso(),
        "source": "ccb config import-herdr (A-lite)",
    }

    # -- output -------------------------------------------------------------
    target = Path(output_path) if output_path else Path(project_dir) / ".ccb" / "ccb.config.herdr-import"
    existing_config = Path(project_dir) / ".ccb" / "ccb.config"

    if not dry_run and target.exists() and not force:
        return {
            "ok": False,
            "reason": (
                f"Output file already exists: {target}. "
                "Use --force to overwrite or choose a different --output path."
            ),
            "config": config,
            "warnings": warnings,
            "written_path": str(target),
        }

    result: dict[str, object] = {
        "ok": True,
        "config": config,
        "warnings": warnings,
        "written_path": str(target),
    }

    toml_text = _dump_toml(config)
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(toml_text, encoding="utf-8")
    else:
        sys.stdout.write(toml_text)

    if existing_config.exists():
        warnings.append(
            f"Existing .ccb/ccb.config found — import draft written to {target.name}. "
            "Review and merge manually."
        )
        result["warnings"] = warnings

    return result


def _herdr_snapshot(
    exe: str,
    *,
    session: str | None,
) -> dict[str, object] | None:
    import subprocess

    cmd = [exe]
    if session:
        cmd.extend(["--session", session])
    cmd.extend(["api", "snapshot"])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            env=herdr_command_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, Mapping):
        return None

    inner = payload.get("result", payload)
    if not isinstance(inner, Mapping):
        return None

    snapshot = inner.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    return dict(snapshot)


def _build_ccb_config(
    snapshot: Mapping[str, object],
    *,
    project_dir: str,
) -> tuple[dict[str, object], list[str]]:
    """Map Herdr workspace/pane topology to a v2 CCB agent config."""
    warnings: list[str] = []
    agents: dict[str, dict[str, object]] = {}
    window_entries: list[str] = []

    workspaces = snapshot.get("workspaces")
    panes = snapshot.get("panes")

    if not isinstance(workspaces, list) or not isinstance(panes, list):
        return {"version": 2, "windows": {}, "agents": {}}, ["No workspaces or panes found in Herdr snapshot"]

    pane_by_id: dict[str, Mapping[str, object]] = {}
    for pane in panes:
        if isinstance(pane, Mapping):
            pid = str(pane.get("pane_id") or "")
            if pid:
                pane_by_id[pid] = pane

    agent_index = 0
    for workspace in workspaces:
        if not isinstance(workspace, Mapping):
            continue
        workspace_label = str(workspace.get("label") or "").strip()
        workspace_id = str(workspace.get("workspace_id") or "").strip()

        workspace_panes = [
            p for p in panes
            if isinstance(p, Mapping) and str(p.get("workspace_id") or "") == workspace_id
        ]

        for pane in workspace_panes:
            pane_label = str(pane.get("label") or "").strip()
            cwd = str(pane.get("cwd") or project_dir)

            agent_config = _pane_to_agent_config(
                pane_label=pane_label,
                workspace_label=workspace_label,
                cwd=cwd,
            )
            if agent_config is not None:
                agent_index += 1
                agent_name = f"agent_{agent_index}"
                agents[agent_name] = agent_config
                provider = agent_config.get("provider", "unknown")
                window_entries.append(f"{agent_name}:{provider}")
            else:
                warnings.append(f"Skipped pane {pane.get('pane_id')}: unknown agent kind for label {pane_label!r}")

    if not agents:
        warnings.append("No agent mappings generated — check Herdr pane labels")
        fallback_name = "imported_agent"
        agents[fallback_name] = {
            "role": "agentroles.architect",
            "provider": "claude",
            "workspace": project_dir,
            "label": "imported-agent",
            "layout": {"position": "main"},
        }
        window_entries.append(f"{fallback_name}:claude")

    windows: dict[str, object] = {
        "main": ", ".join(window_entries),
    }

    config: dict[str, object] = {
        "version": 2,
        "windows": windows,
        "agents": agents,
    }

    return config, warnings


def _pane_to_agent_config(
    *,
    pane_label: str,
    workspace_label: str,
    cwd: str,
) -> dict[str, object] | None:
    """Map a Herdr pane label to a CCB agent config entry."""
    label_lower = pane_label.lower()

    # Known provider keywords
    provider_map: dict[str, str | None] = {
        "claude": "claude",
        "codex": "codex",
        "gemini": "gemini",
        "grok": "grok",
        "kimi": "kimi",
        "deepseek": "deepseek",
        "qwen": "qwen",
        "copilot": "copilot",
        "opencode": "opencode",
        "droid": "droid",
        "pi": "pi",
        "mimo": "mimo",
        "cursor": "cursor",
        "cmd": None,  # skip command panes
        "powershell": None,
        "pwsh": None,
    }

    provider = None
    for keyword, mapped in provider_map.items():
        if keyword in label_lower:
            provider = mapped
            break

    if provider is None:
        # Unknown agent kind — skip with warning
        return None

    if provider_map.get(label_lower) is None and label_lower in ("cmd", "powershell", "pwsh"):
        return None

    # Default role based on position
    role = "agentroles.developer"

    return {
        "role": role,
        "provider": provider,
        "workspace": cwd,
        "label": pane_label,
        "layout": {"position": "main"},
        "_herdr_source": {
            "pane_label": pane_label,
            "workspace_label": workspace_label,
        },
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# TOML serializer (no tomli_w dependency — hand-rolled for the config subset
# that import-herdr produces)
# ---------------------------------------------------------------------------

def _toml_value(val: object) -> str:
    """Format a single value as a TOML literal."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(val, (list, tuple)):
        items = ", ".join(_toml_value(v) for v in val)
        return f"[{items}]"
    if isinstance(val, Mapping):
        pairs = ", ".join(f"{k} = {_toml_value(v)}" for k, v in val.items())
        return f"{{ {pairs} }}"
    return f'"{val}"'


def _dump_toml(data: dict[str, object]) -> str:
    """Serialize a v2 CCB config dict to TOML text."""
    lines: list[str] = []

    # version
    version = data.get("version", 2)
    lines.append(f"version = {_toml_value(version)}")
    lines.append("")

    # _herdr_import_meta as comment block
    meta = data.get("_herdr_import_meta")
    if isinstance(meta, Mapping):
        lines.append("# Herdr import metadata")
        for mk, mv in meta.items():
            lines.append(f"#   {mk}: {mv}")
        lines.append("")

    # [windows]
    windows = data.get("windows")
    if isinstance(windows, Mapping):
        lines.append("[windows]")
        for wk, wv in windows.items():
            lines.append(f"{wk} = {_toml_value(wv)}")
        lines.append("")

    # [agents.<name>]
    agents = data.get("agents")
    if isinstance(agents, Mapping):
        for agent_name, agent_spec in agents.items():
            if not isinstance(agent_spec, Mapping):
                continue
            lines.append(f"[agents.{agent_name}]")
            for ak, av in agent_spec.items():
                if isinstance(av, Mapping):
                    # dotted sub-keys for nested tables like _herdr_source / layout
                    for nk, nv in av.items():
                        lines.append(f"{ak}.{nk} = {_toml_value(nv)}")
                else:
                    lines.append(f"{ak} = {_toml_value(av)}")
            lines.append("")

    return "\n".join(lines)


__all__ = ["import_herdr_config"]
