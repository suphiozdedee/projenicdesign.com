from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Literal, TypedDict


PROJECTION_RELATIVE_PATH = Path("lib/platforms/windows/release/projection.json")
SCHEMA_VERSION = 1

HostGateOp = Literal["equals", "in", "not_equals", "is_false", "exists"]


class WindowsX64ReleaseHostEvidence(TypedDict, total=False):
    os_platform: str
    cpu_arch: str
    node_arch: str
    process_arch: str
    wow64: bool | None
    python_executable: str | None
    python_bitness: str | None
    managed_python_ref: str | None
    helper_arch: dict[str, str]
    helper_probe_ref: str | None
    npm_lifecycle_event: str | None
    installer_entrypoint: str | None


class WindowsX64ReleaseHostGateRule(TypedDict):
    field: str
    op: HostGateOp
    value: object
    failure_reason: str
    diagnostic: str
    next_action: str


class WindowsX64ReleaseHostGate(TypedDict):
    rules: list[WindowsX64ReleaseHostGateRule]
    default_failure_reason: str
    default_next_action: str


class WindowsX64ReleaseSurfaceProjection(TypedDict):
    schema_version: int
    projection_source: str
    baseline_gate_ref: str | None
    user_surfaces_parity_ref: str | None
    packaged_projection_ref: str | None
    implementation_admission: str
    baseline_version_ref: str | None
    baseline_version_status: str
    package_os: list[str]
    package_cpu: list[str]
    package_metadata_policy: str
    host_gate: WindowsX64ReleaseHostGate
    windows_npm_enabled: bool
    artifact_status: str
    artifact_basename: str | None
    archive_name: str | None
    extract_dir: str | None
    checksum_entry: str | None
    release_artifact_ref: str | None
    windows_installer_entry: str | None
    windows_executable_entry: str | None
    windows_bin_entries: dict[str, str]
    release_install_entry: str
    source_install_allowed: bool
    source_install_entry: str
    update_entry: str
    managed_python_status: str
    native_helper_status: str
    upstream_gate_status: str
    upstream_failure_ref: str | None
    upstream_detail_reason: str | None
    beta_gaps: list[str]
    surface_state: str
    failure_reason: str | None
    release_gate_detail: str
    diagnostic: str
    next_action: str | None


_ENUMS: dict[str, set[str]] = {
    "projection_source": {"repo_evidence", "packaged_json", "default_blocked"},
    "implementation_admission": {"admitted", "blocked_upstream_pending", "blocked_baseline_mismatch"},
    "baseline_version_status": {"matching", "mismatch", "unknown"},
    "package_metadata_policy": {"win32-enabled-postinstall-gated", "win32-disabled", "blocked"},
    "artifact_status": {"ready", "missing", "mismatch", "unknown"},
    "release_install_entry": {"npm", "install_ps1", "diagnostic_only"},
    "source_install_entry": {"install_ps1", "none"},
    "update_entry": {"npm", "install_ps1", "source", "diagnostic_only"},
    "managed_python_status": {"ready", "missing", "degraded", "unknown"},
    "native_helper_status": {"ready", "partial", "missing", "unknown"},
    "upstream_gate_status": {"ready", "blocked", "pending", "unknown"},
    "surface_state": {"blocked", "degraded", "available"},
}

_FAILURE_REASONS = {
    "not-windows",
    "not-x64",
    "wow64",
    "python-not-x64",
    "managed-python-missing",
    "managed-python-degraded",
    "helper-missing",
    "helper-not-x64",
    "release-artifact-missing",
    "release-artifact-mismatch",
    "installer-entry-invalid",
    "projection-schema-invalid",
    "baseline-gate-missing",
    "baseline-version-mismatch",
    "upstream-not-admitted",
    "user-surfaces-parity-missing",
    "unknown",
}

_REQUIRED_FIELDS = {
    "schema_version",
    "projection_source",
    "baseline_gate_ref",
    "user_surfaces_parity_ref",
    "packaged_projection_ref",
    "implementation_admission",
    "baseline_version_ref",
    "baseline_version_status",
    "package_os",
    "package_cpu",
    "package_metadata_policy",
    "host_gate",
    "windows_npm_enabled",
    "artifact_status",
    "artifact_basename",
    "archive_name",
    "extract_dir",
    "checksum_entry",
    "release_artifact_ref",
    "windows_installer_entry",
    "windows_executable_entry",
    "windows_bin_entries",
    "release_install_entry",
    "source_install_allowed",
    "source_install_entry",
    "update_entry",
    "managed_python_status",
    "native_helper_status",
    "upstream_gate_status",
    "upstream_failure_ref",
    "upstream_detail_reason",
    "beta_gaps",
    "surface_state",
    "failure_reason",
    "release_gate_detail",
    "diagnostic",
    "next_action",
}


def canonical_projection_json(projection: dict[str, object]) -> str:
    return json.dumps(projection, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def assert_windows_x64_release_surface_projection_fresh(root: str | Path) -> None:
    root_path = Path(root)
    projection_path = root_path / PROJECTION_RELATIVE_PATH
    current = projection_path.read_text(encoding="utf-8")
    version = _read_text(root_path / "VERSION").strip()
    expected = canonical_projection_json(windows_beta_projection(version))
    if current != expected:
        raise ValueError(f"{PROJECTION_RELATIVE_PATH.as_posix()} is not fresh")


def windows_x64_release_surface_baseline_version_admission(root: str | Path) -> dict[str, object]:
    root_path = Path(root)
    version = _read_text(root_path / "VERSION").strip()
    package_version = _package_version(root_path / "package.json")
    if version and package_version == version:
        return {
            "status": "ready",
            "implementation_admission": "admitted",
            "baseline_version_status": "matching",
            "version": version,
            "version_ref": "VERSION",
            "package_version_ref": "package.json",
        }
    return {
        "status": "blocked",
        "implementation_admission": "blocked_baseline_mismatch",
        "baseline_version_status": "mismatch",
        "version_ref": "VERSION" if version else None,
        "package_version_ref": "package.json" if package_version else None,
        "reason": f"VERSION/package.json mismatch: {version or 'missing'}/{package_version or 'missing'}",
    }


def windows_x64_release_surface_dependency_admission(root: str | Path) -> dict[str, object]:
    root_path = Path(root)
    required = {
        "launcher": root_path / "platforms/windows/launcher/Cargo.toml",
        "builder": root_path / "platforms/windows/packaging/build_release.py",
        "installer": root_path / "platforms/windows/installer/install.ps1",
        "projection": root_path / PROJECTION_RELATIVE_PATH,
        "workflow": root_path / ".github/workflows/release-windows.yml",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        return _blocked_dependency_admission(f"Windows release dependency missing: {', '.join(missing)}")
    return {
        "status": "ready",
        "implementation_admission": "admitted",
        "dependency_refs": {name: _rel(root_path, path) for name, path in required.items()},
    }


def windows_beta_projection(version: str) -> WindowsX64ReleaseSurfaceProjection:
    release_version = str(version or "").strip()
    if not release_version:
        raise ValueError("Windows beta projection requires a release version")
    return {
        "schema_version": SCHEMA_VERSION,
        "projection_source": "repo_evidence",
        "baseline_gate_ref": "VERSION",
        "user_surfaces_parity_ref": "platforms/windows/README.md",
        "packaged_projection_ref": PROJECTION_RELATIVE_PATH.as_posix(),
        "implementation_admission": "admitted",
        "baseline_version_ref": "VERSION",
        "baseline_version_status": "matching",
        "package_os": ["win32"],
        "package_cpu": ["x64"],
        "package_metadata_policy": "win32-disabled",
        "host_gate": _default_host_gate(),
        "windows_npm_enabled": False,
        "artifact_status": "ready",
        "artifact_basename": "ccb-windows-x86_64",
        "archive_name": "ccb-windows-x86_64.zip",
        "extract_dir": "ccb-windows-x86_64",
        "checksum_entry": "ccb-windows-x86_64.zip.sha256",
        "release_artifact_ref": f"v{release_version}",
        "windows_installer_entry": "install.ps1",
        "windows_executable_entry": "bin/ccb.exe",
        "windows_bin_entries": {
            "ccb": "bin/ccb.exe",
            "ask": "bin/ask.exe",
            "autonew": "bin/autonew.exe",
            "ctx-transfer": "bin/ctx-transfer.exe",
        },
        "release_install_entry": "install_ps1",
        "source_install_allowed": True,
        "source_install_entry": "install_ps1",
        "update_entry": "diagnostic_only",
        "managed_python_status": "degraded",
        "native_helper_status": "ready",
        "upstream_gate_status": "ready",
        "upstream_failure_ref": None,
        "upstream_detail_reason": None,
        "beta_gaps": [
            "Python 3.10+ remains an external prerequisite.",
            "The native launchers are unsigned beta binaries.",
            "Real Windows, WezTerm, and Herdr acceptance remains pending.",
        ],
        "surface_state": "degraded",
        "failure_reason": "managed-python-degraded",
        "release_gate_detail": (
            "The Windows x64 ZIP is attached to the stable CCB release but retains beta support status."
        ),
        "diagnostic": "Native Windows x64 beta artifact is ready; external prerequisites still apply.",
        "next_action": "Verify the SHA256 file, reinstall with install.ps1, and report beta results.",
    }


def default_blocked_projection(
    *,
    failure_reason: str = "release-artifact-missing",
    diagnostic: str | None = None,
    next_action: str | None = None,
) -> WindowsX64ReleaseSurfaceProjection:
    reason = failure_reason if failure_reason in _FAILURE_REASONS else "unknown"
    return {
        "schema_version": SCHEMA_VERSION,
        "projection_source": "default_blocked",
        "baseline_gate_ref": None,
        "user_surfaces_parity_ref": None,
        "packaged_projection_ref": PROJECTION_RELATIVE_PATH.as_posix(),
        "implementation_admission": "admitted",
        "baseline_version_ref": "VERSION",
        "baseline_version_status": "unknown",
        "package_os": ["win32"],
        "package_cpu": ["x64"],
        "package_metadata_policy": "win32-disabled",
        "host_gate": _default_host_gate(),
        "windows_npm_enabled": False,
        "artifact_status": "missing",
        "artifact_basename": None,
        "archive_name": None,
        "extract_dir": None,
        "checksum_entry": None,
        "release_artifact_ref": None,
        "windows_installer_entry": None,
        "windows_executable_entry": None,
        "windows_bin_entries": {},
        "release_install_entry": "diagnostic_only",
        "source_install_allowed": True,
        "source_install_entry": "install_ps1",
        "update_entry": "diagnostic_only",
        "managed_python_status": "unknown",
        "native_helper_status": "unknown",
        "upstream_gate_status": "blocked",
        "upstream_failure_ref": None,
        "upstream_detail_reason": reason,
        "beta_gaps": [],
        "surface_state": "blocked",
        "failure_reason": reason,
        "release_gate_detail": "Windows x64 release route is blocked until release artifact evidence is present.",
        "diagnostic": diagnostic
        or "Windows x64 release route is blocked by the current release-surface projection.",
        "next_action": next_action
        or "Use install.ps1 from a validated Windows release ZIP or source checkout.",
    }


def load_windows_x64_release_surface_projection(
    root: str | Path,
    host_evidence: WindowsX64ReleaseHostEvidence | dict[str, object] | None = None,
) -> WindowsX64ReleaseSurfaceProjection:
    root_path = Path(root)
    projection_path = root_path / PROJECTION_RELATIVE_PATH
    if not projection_path.exists():
        return default_blocked_projection()
    try:
        raw = json.loads(projection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_blocked_projection(failure_reason="projection-schema-invalid")
    if not isinstance(raw, dict):
        return default_blocked_projection(failure_reason="projection-schema-invalid")
    try:
        projection = _validate_projection(raw)
    except ValueError:
        return default_blocked_projection(failure_reason="projection-schema-invalid")
    return _apply_host_gate(projection, dict(host_evidence or {}))


def _default_host_gate() -> WindowsX64ReleaseHostGate:
    return {
        "default_failure_reason": "projection-schema-invalid",
        "default_next_action": "Regenerate the Windows x64 release-surface projection.",
        "rules": [
            {
                "field": "os_platform",
                "op": "equals",
                "value": "win32",
                "failure_reason": "not-windows",
                "diagnostic": "Windows x64 release route requires os_platform=win32.",
                "next_action": "Use Linux/macOS release routes or retry on native Windows x64.",
            },
            {
                "field": "cpu_arch",
                "op": "equals",
                "value": "x64",
                "failure_reason": "not-x64",
                "diagnostic": "Windows x64 release route requires cpu_arch=x64.",
                "next_action": "Retry from a native Windows x64 host.",
            },
            {
                "field": "wow64",
                "op": "is_false",
                "value": False,
                "failure_reason": "wow64",
                "diagnostic": "WOW64 is not a native Windows x64 process.",
                "next_action": "Use a native 64-bit shell and Node runtime.",
            },
        ],
    }


def _validate_projection(raw: dict[str, Any]) -> WindowsX64ReleaseSurfaceProjection:
    if set(raw) < _REQUIRED_FIELDS:
        raise ValueError("projection missing required fields")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported projection schema version")
    for field, allowed in _ENUMS.items():
        if raw.get(field) not in allowed:
            raise ValueError(f"invalid enum for {field}")
    failure_reason = raw.get("failure_reason")
    if failure_reason is not None and failure_reason not in _FAILURE_REASONS:
        raise ValueError("invalid failure_reason")
    if not isinstance(raw.get("windows_npm_enabled"), bool):
        raise ValueError("windows_npm_enabled must be bool")
    if not isinstance(raw.get("source_install_allowed"), bool):
        raise ValueError("source_install_allowed must be bool")
    for field in ("package_os", "package_cpu", "beta_gaps"):
        if not _is_string_list(raw.get(field)):
            raise ValueError(f"{field} must be a string list")
    if not _is_string_mapping(raw.get("windows_bin_entries")):
        raise ValueError("windows_bin_entries must be a string mapping")
    _validate_host_gate(raw.get("host_gate"))
    if raw["package_metadata_policy"] == "blocked" and raw["windows_npm_enabled"]:
        raise ValueError("blocked package metadata cannot enable Windows npm")
    _validate_projection_invariants(raw)
    return copy.deepcopy(raw)


def _validate_projection_invariants(raw: dict[str, Any]) -> None:
    if raw["surface_state"] != "available":
        return
    if raw["failure_reason"] is not None:
        raise ValueError("available projection cannot carry failure_reason")
    if raw["artifact_status"] != "ready":
        raise ValueError("available projection requires artifact_status=ready")
    if raw["managed_python_status"] != "ready":
        raise ValueError("available projection requires managed_python_status=ready")
    if raw["native_helper_status"] != "ready":
        raise ValueError("available projection requires native_helper_status=ready")
    if "win32" not in raw["package_os"] or "x64" not in raw["package_cpu"]:
        raise ValueError("available projection requires win32/x64 package metadata")
    for field in (
        "artifact_basename",
        "archive_name",
        "extract_dir",
        "checksum_entry",
        "release_artifact_ref",
        "windows_installer_entry",
        "windows_executable_entry",
    ):
        if not _non_empty_string(raw.get(field)):
            raise ValueError(f"available projection requires {field}")
    if not raw["windows_bin_entries"]:
        raise ValueError("available projection requires windows_bin_entries")


def _validate_host_gate(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise ValueError("host_gate must be an object")
    if not isinstance(raw.get("default_failure_reason"), str):
        raise ValueError("host_gate default_failure_reason must be string")
    if raw["default_failure_reason"] not in _FAILURE_REASONS:
        raise ValueError("invalid host_gate default_failure_reason")
    if not _non_empty_string(raw.get("default_next_action")):
        raise ValueError("host_gate default_next_action must be non-empty")
    rules = raw.get("rules")
    if not isinstance(rules, list):
        raise ValueError("host_gate rules must be a list")
    for rule in rules:
        _validate_host_gate_rule(rule)


def _validate_host_gate_rule(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise ValueError("host_gate rule must be an object")
    for field in ("field", "op", "failure_reason", "diagnostic", "next_action"):
        if not _non_empty_string(raw.get(field)):
            raise ValueError(f"host_gate rule {field} must be non-empty")
    if raw["op"] not in {"equals", "in", "not_equals", "is_false", "exists"}:
        raise ValueError("invalid host_gate rule op")
    if raw["failure_reason"] not in _FAILURE_REASONS:
        raise ValueError("invalid host_gate failure_reason")
    if raw["op"] in {"equals", "not_equals"} and "value" not in raw:
        raise ValueError("host_gate comparison rule value is required")
    if raw["op"] == "in" and not isinstance(raw.get("value"), list):
        raise ValueError("host_gate in rule value must be a list")


def _apply_host_gate(
    projection: WindowsX64ReleaseSurfaceProjection,
    host_evidence: dict[str, object],
) -> WindowsX64ReleaseSurfaceProjection:
    host_gate = projection["host_gate"]
    for rule in host_gate["rules"]:
        if _rule_passes(rule, host_evidence):
            continue
        blocked = copy.deepcopy(projection)
        blocked["surface_state"] = "blocked"
        blocked["windows_npm_enabled"] = False
        blocked["release_install_entry"] = "diagnostic_only"
        blocked["update_entry"] = "diagnostic_only"
        blocked["failure_reason"] = rule["failure_reason"]
        blocked["diagnostic"] = rule["diagnostic"]
        blocked["next_action"] = rule["next_action"]
        return blocked
    return projection


def _rule_passes(rule: WindowsX64ReleaseHostGateRule, host_evidence: dict[str, object]) -> bool:
    value = host_evidence.get(rule["field"])
    op = rule["op"]
    if op == "exists":
        return _present(value)
    if not _present(value):
        return False
    if op == "is_false":
        return value is False
    if op == "in":
        expected = rule["value"]
        return isinstance(expected, list) and _normalized(value) in {_normalized(item) for item in expected}
    if op == "equals":
        return _normalized(value) == _normalized(rule.get("value"))
    if op == "not_equals":
        return _normalized(value) != _normalized(rule.get("value"))
    return False


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _normalized(value: object) -> object:
    if isinstance(value, str):
        return value.strip().lower()
    return value


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _is_string_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and key and isinstance(item, str) and item for key, item in value.items()
    )


def _package_version(path: Path) -> str:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    value = raw.get("version") if isinstance(raw, dict) else ""
    return str(value or "").strip()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def _blocked_dependency_admission(reason: str) -> dict[str, object]:
    return {
        "status": "blocked",
        "implementation_admission": "blocked_upstream_pending",
        "reason": reason,
    }


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "PROJECTION_RELATIVE_PATH",
    "WindowsX64ReleaseHostEvidence",
    "WindowsX64ReleaseHostGate",
    "WindowsX64ReleaseHostGateRule",
    "WindowsX64ReleaseSurfaceProjection",
    "assert_windows_x64_release_surface_projection_fresh",
    "canonical_projection_json",
    "default_blocked_projection",
    "load_windows_x64_release_surface_projection",
    "windows_beta_projection",
    "windows_x64_release_surface_baseline_version_admission",
    "windows_x64_release_surface_dependency_admission",
]
