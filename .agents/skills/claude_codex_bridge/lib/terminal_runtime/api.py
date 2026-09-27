from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Optional, cast

from terminal_runtime.backend_types import TerminalBackend
from terminal_runtime.detect import current_tty as _current_tty_impl
from terminal_runtime.detect import detect_terminal as _detect_terminal_impl
from terminal_runtime.detect import inside_tmux as _inside_tmux_impl
from terminal_runtime.env import default_shell as _default_shell_impl
from terminal_runtime.env import env_float as _env_float_impl
from terminal_runtime.env import env_int as _env_int_impl
from terminal_runtime.env import is_windows as _is_windows_impl
from terminal_runtime.env import is_wsl as _is_wsl_impl
from terminal_runtime.env import sanitize_filename as _sanitize_filename_impl
from terminal_runtime.env import subprocess_kwargs as _subprocess_kwargs_impl
from terminal_runtime.layouts import LayoutResult
from terminal_runtime.pane_logs import cleanup_pane_logs as _cleanup_pane_logs_impl
from terminal_runtime.pane_logs import maybe_trim_log as _maybe_trim_log_impl
from terminal_runtime.pane_logs import pane_log_dir as _pane_log_dir_impl
from terminal_runtime.pane_logs import pane_log_path_for as _pane_log_path_for_impl
from terminal_runtime.pane_logs import pane_log_root as _pane_log_root_impl
from platforms.windows.herdr.backend import HerdrBackend
from platforms.windows.herdr.runtime.capabilities import HerdrCapabilityGate
from platforms.windows.herdr.runtime.capabilities import herdr_capability_report_supported
from platforms.windows.herdr.runtime.cli import HerdrCliRequestAdapter
from platforms.windows.herdr.runtime.client import HerdrSocketClient
from terminal_runtime.mux_backend_contract import MuxCapabilitiesV2
from terminal_runtime.tmux import default_detached_session_name as _default_detached_session_name_impl
from terminal_runtime.tmux_backend import TmuxBackend

from .api_selection import (
    create_layout as _create_layout,
    resolve_backend as _resolve_backend,
    resolve_backend_for_session as _resolve_backend_for_session,
    resolve_pane_id_from_session as _resolve_pane_id_from_session,
)

_env_float = _env_float_impl
_env_int = _env_int_impl
_sanitize_filename = _sanitize_filename_impl
_pane_log_root = _pane_log_root_impl
_pane_log_dir = _pane_log_dir_impl
_pane_log_path_for = _pane_log_path_for_impl
_maybe_trim_log = _maybe_trim_log_impl
_cleanup_pane_logs = _cleanup_pane_logs_impl
is_windows = _is_windows_impl
_subprocess_kwargs = _subprocess_kwargs_impl
is_wsl = _is_wsl_impl
_current_tty = _current_tty_impl


def _extract_wsl_path_from_unc_like_path(path: str) -> str | None:
    normalized = str(path or "").replace("\\", "/")
    prefixes = ("/wsl.localhost/", "//wsl.localhost/", "/wsl$/", "//wsl$/")
    for prefix in prefixes:
        if not normalized.startswith(prefix):
            continue
        remainder = normalized[len(prefix):]
        parts = remainder.split("/", 1)
        if len(parts) != 2 or not parts[1].startswith("home/"):
            return None
        return "/" + parts[1]
    return None


def _run(*args, **kwargs):
    kwargs.update(_subprocess_kwargs())
    import subprocess as _sp

    return _sp.run(*args, **kwargs)


def _extract_wsl_path_from_unc_like_path(path: str) -> str | None:
    normalized = str(path or "").strip()
    if not normalized:
        return None

    candidate = normalized.replace("\\", "/")
    prefixes = ("/wsl.localhost/", "//wsl.localhost/", "/wsl$/", "//wsl$/")
    for prefix in prefixes:
        if not candidate.lower().startswith(prefix):
            continue
        remainder = candidate[len(prefix):]
        parts = [part for part in remainder.split("/") if part]
        if len(parts) < 2:
            return None
        return "/" + "/".join(parts[1:])
    return None

def _default_shell() -> tuple[str, str]:
    return _default_shell_impl(is_wsl_fn=is_wsl, is_windows_fn=is_windows)


def get_shell_type() -> str:
    if is_windows() and os.environ.get("CCB_BACKEND_ENV", "").lower() == "wsl":
        return "bash"
    shell, _ = _default_shell()
    if shell in ("pwsh", "powershell"):
        return "powershell"
    return "bash"


_backend_cache: Optional[TerminalBackend] = None
_backend_cache_key: str | None = None
_backend_config_preference: str | None = None
_ROOT_DIR = Path(__file__).resolve().parents[2]


def set_backend_config_preference(backend: str | None) -> None:
    """设置 config ``runtime.mux.backend`` 驱动的后端偏好（design D2 单一事实源）。

    config 显式声明优先于终端/环境检测。
    调用时机：项目 config 加载后、首次 ``get_backend()`` 前。
    """
    global _backend_config_preference, _backend_cache, _backend_cache_key
    value = str(backend or '').strip().lower() or None
    if value not in {None, 'herdr', 'rmux'}:
        raise ValueError(
            f'invalid runtime.mux.backend config value: {backend!r} '
            f'(expected herdr, rmux, or absent)'
        )
    if value != _backend_config_preference:
        _backend_config_preference = value
        _backend_cache = None
        _backend_cache_key = None


def _inside_tmux() -> bool:
    return _inside_tmux_impl(
        env=os.environ,
        which_fn=shutil.which,
        run_fn=_run,
        current_tty_fn=_current_tty,
    )


def detect_terminal() -> Optional[str]:
    return _detect_terminal_impl(
        env=os.environ,
        which_fn=shutil.which,
        run_fn=_run,
        current_tty_fn=_current_tty,
    )


def get_backend(terminal_type: Optional[str] = None) -> Optional[TerminalBackend]:
    global _backend_cache, _backend_cache_key
    # design D2: config runtime.mux.backend 为声明式单一事实源，优先于终端/环境检测。
    # 优先级：显式 terminal_type > set_backend_config_preference() > env CCB_RUNTIME_MUX_BACKEND
    env_pref = None
    if terminal_type is None:
        candidate = os.environ.get('CCB_RUNTIME_MUX_BACKEND', '').strip().lower()
        if candidate in ('herdr', 'rmux'):
            env_pref = candidate
    if terminal_type is None:
        terminal_type = _backend_config_preference or env_pref
    detected_terminal = detect_terminal() if terminal_type is None else None
    use_cache = (
        terminal_type is None
        and detected_terminal == "tmux"
        and not _herdr_runtime_configured()
        and not _backend_config_preference
    )
    if use_cache and _backend_cache is not None and _backend_cache_key == detected_terminal:
        return _backend_cache
    backend = _resolve_backend(
        cached_backend=None,
        terminal_type=terminal_type,
        detect_terminal_fn=lambda: detected_terminal,
        tmux_backend_factory=TmuxBackend,
        herdr_backend_factory=_herdr_backend_factory,
        platform_gate_fn=_herdr_platform_gate,
        herdr_capability_report_fn=_herdr_capability_report,
        herdr_capability_report_ref_fn=_herdr_capability_report_ref,
    )
    if use_cache:
        _backend_cache = backend
        _backend_cache_key = detected_terminal if backend is not None else None
    return backend


def get_backend_for_session(session_data: dict) -> Optional[TerminalBackend]:
    return _resolve_backend_for_session(
        session_data=session_data,
        detect_terminal_fn=detect_terminal,
        tmux_backend_factory=TmuxBackend,
        herdr_backend_factory=_herdr_backend_for_persisted_session_factory,
    )


def get_pane_id_from_session(session_data: dict) -> Optional[str]:
    return _resolve_pane_id_from_session(session_data)


def create_auto_layout(
    providers: list[str],
    *,
    cwd: str,
    root_pane_id: str | None = None,
    tmux_session_name: str | None = None,
    percent: int = 50,
    set_markers: bool = True,
    marker_prefix: str = "CCB",
) -> LayoutResult:
    return _create_layout(
        providers=providers,
        cwd=cwd,
        root_pane_id=root_pane_id,
        tmux_session_name=tmux_session_name,
        percent=percent,
        set_markers=set_markers,
        marker_prefix=marker_prefix,
        tmux_backend_factory=TmuxBackend,
        detached_session_name_fn=_default_detached_session_name_impl,
        env=os.environ,
    )


def _herdr_backend_factory() -> HerdrBackend:
    capabilities = _herdr_capability_report()
    request_adapter = _herdr_request_adapter()
    return HerdrBackend(
        client=HerdrSocketClient(
            request_fn=request_adapter,
            socket_ref=request_adapter.socket_ref,
            allow_session_scoped_ipc_refs=bool(
                getattr(request_adapter, "allow_session_scoped_ipc_refs", False)
            ),
        ),
        capability_gate=_herdr_capability_gate(capabilities),
    )


def _herdr_backend_for_persisted_session_factory() -> HerdrBackend:
    capabilities = _herdr_persisted_session_capability_report()
    request_adapter = _herdr_request_adapter()
    return HerdrBackend(
        client=HerdrSocketClient(
            request_fn=request_adapter,
            socket_ref=request_adapter.socket_ref,
            allow_session_scoped_ipc_refs=bool(
                getattr(request_adapter, "allow_session_scoped_ipc_refs", False)
            ),
        ),
        capability_gate=_herdr_capability_gate(capabilities),
    )


def get_backend_for_namespace_teardown(namespace_ref: Mapping[str, object]) -> HerdrBackend:
    """Build a Herdr backend that can tear down an already-persisted namespace.

    ``ccb kill`` runs in a process that does not carry the Herdr capability
    evidence env (``CCB_HERDR_CAPABILITY_REPORT``), which CCB only injects into
    daemon-spawned agent processes.  A persisted herdr namespace already proves
    Herdr was validated at creation time, so teardown must not re-run the
    backend-selection gate: re-attach directly from the persisted
    ``namespace_ref`` with a teardown-only capability gate instead.
    """
    request_adapter = _herdr_request_adapter()
    socket_ref = str(namespace_ref.get("ipc_ref") or "").strip() or request_adapter.socket_ref
    backend = HerdrBackend(
        client=HerdrSocketClient(
            request_fn=request_adapter,
            socket_ref=socket_ref,
            allow_session_scoped_ipc_refs=bool(
                getattr(request_adapter, "allow_session_scoped_ipc_refs", False)
            ),
        ),
        capability_gate=_herdr_capability_gate(_herdr_teardown_capability_report()),
    )
    setattr(backend, "_ccb_project_namespace_ref", dict(namespace_ref))
    return backend


def _herdr_persisted_session_capability_report() -> dict[str, object]:
    """Capability evidence for re-attaching to an already persisted Herdr session."""
    statuses = {
        "session_attach": "supported",
        "pane_spawn": "supported",
        "send_input": "supported",
        "read_output": "supported",
        "kill_pane": "supported",
        "workspace_create": "supported",
        "workspace_list": "supported",
        "workspace_focus": "supported",
        "workspace_close": "supported",
        "workspace_metadata": "supported",
        "pane_metadata": "supported",
        "pane_list": "supported",
        "pane_split": "supported",
        "pane_run": "supported",
    }
    report: dict[str, object] = {
        "backend_impl": "herdr",
        "command_status": dict(statuses),
        "semantic_status": dict(statuses),
        "blocking_gaps": [],
        "windows_beta_gaps": [],
        "adapter_recommendation": "continue-with-gaps",
        "verdict": "partial",
        "failure_class": "windows-beta-gap",
        "source_ref": "persisted-session",
    }
    live_report = _herdr_capability_report()
    if live_report is None:
        return report
    if live_report.get("blocked") is True:
        return dict(live_report)
    return _intersect_herdr_capability_reports(report, live_report)


def _intersect_herdr_capability_reports(
    persisted_report: dict[str, object],
    live_report: Mapping[str, object],
) -> dict[str, object]:
    merged = dict(persisted_report)
    merged["source_ref"] = (
        str(live_report.get("source_ref") or "").strip()
        or str(persisted_report.get("source_ref") or "").strip()
    )
    for key in ("command_status", "semantic_status"):
        persisted_status = persisted_report.get(key)
        live_status = live_report.get(key)
        if not isinstance(persisted_status, Mapping) or not isinstance(live_status, Mapping):
            merged[key] = {}
            continue
        merged[key] = {
            str(name): (
                "supported"
                if status == "supported" and live_status.get(name) == "supported"
                else str(live_status.get(name) or "unsupported")
            )
            for name, status in persisted_status.items()
        }
    merged["blocking_gaps"] = sorted(
        {
            str(item)
            for item in _list_items(persisted_report.get("blocking_gaps"))
            + _list_items(live_report.get("blocking_gaps"))
            if str(item).strip()
        }
    )
    merged["windows_beta_gaps"] = sorted(
        {
            str(item)
            for item in _list_items(persisted_report.get("windows_beta_gaps"))
            + _list_items(live_report.get("windows_beta_gaps"))
            if str(item).strip()
        }
    )
    for key in ("adapter_recommendation", "verdict", "failure_class"):
        value = live_report.get(key)
        if isinstance(value, str) and value.strip():
            merged[key] = value
    return {str(key): value for key, value in merged.items()}


def _list_items(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _herdr_teardown_capability_report() -> dict[str, object]:
    """Minimal capability evidence permitting teardown of a persisted namespace.

    Covers the operations the destroy path performs on a re-attached herdr
    namespace (``capabilities``, ``namespace_alive``, ``destroy_namespace`` /
    ``kill_server``) without requiring ambient capability evidence.  Only used
    when re-attaching to a namespace that persisted state already proves is
    Herdr.
    """
    statuses = {
        "session_attach": "supported",
        "workspace_list": "supported",
        "pane_list": "supported",
        "workspace_close": "supported",
    }
    return {
        "backend_impl": "herdr",
        "command_status": dict(statuses),
        "semantic_status": dict(statuses),
        "blocking_gaps": [],
        "windows_beta_gaps": [],
        "adapter_recommendation": "continue-with-gaps",
        "verdict": "partial",
        "failure_class": "windows-beta-gap",
    }


def _herdr_capability_gate(capabilities: dict[str, object] | None) -> HerdrCapabilityGate:
    if not capabilities or capabilities.get("blocked") is True:
        return HerdrCapabilityGate(
            capabilities=None,
            failure_reason=(
                str(capabilities.get("failure_reason"))
                if capabilities and capabilities.get("blocked") is True
                else "herdr-capability-missing"
            ),
            diagnostic=(
                str(capabilities.get("diagnostic"))
                if capabilities and capabilities.get("blocked") is True
                else "Herdr capability evidence is unavailable"
            ),
            capability_report_ref=_herdr_capability_report_ref(),
    )
    if not herdr_capability_report_supported(capabilities):
        return HerdrCapabilityGate(
            capabilities=None,
            failure_reason="invalid-request",
            diagnostic="Herdr capability evidence is malformed",
            capability_report_ref=_herdr_capability_report_ref(),
        )
    return HerdrCapabilityGate(
        capabilities=cast(MuxCapabilitiesV2, capabilities),
        capability_report_ref=_herdr_capability_report_ref(),
    )


def _herdr_platform_gate() -> dict[str, object] | None:
    return _live_herdr_platform_gate()


def _herdr_capability_report() -> dict[str, object] | None:
    path = _herdr_capability_report_path()
    if path is None:
        return None
    if not path.exists():
        return _invalid_herdr_capability_report()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _invalid_herdr_capability_report()
    if not isinstance(payload, dict):
        return _invalid_herdr_capability_report()
    _normalize_herdr_capability_projection(payload)
    payload["source_ref"] = _herdr_capability_report_ref()
    return payload


def _herdr_capability_report_ref() -> str | None:
    path = _herdr_capability_report_path()
    if path is None:
        return None
    if not path.exists():
        return None
    try:
        return path.relative_to(_ROOT_DIR).as_posix()
    except ValueError:
        return path.name


def _herdr_capability_report_path() -> Path | None:
    override = os.environ.get("CCB_HERDR_CAPABILITY_REPORT", "").strip()
    if not override:
        return None
    try:
        return Path(override).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _normalize_herdr_capability_projection(payload: dict[str, object]) -> None:
    projection = payload.get("capability_projection")
    if not isinstance(projection, dict):
        return
    payload.setdefault("backend_impl", "herdr")
    for key in ("command_status", "semantic_status", "windows_beta_gaps", "blocking_gaps"):
        if key not in payload and key in projection:
            payload[key] = projection[key]
    _derive_herdr_facade_capabilities(payload)
    _deduce_herdr_verdict(payload)


_DERIVED_HERDR_FACADE_CAPABILITIES = {
    "workspace_create": ("session_attach",),
    "workspace_list": ("session_attach",),
    "workspace_focus": ("session_attach",),
    "workspace_metadata": ("session_attach",),
    "workspace_close": ("session_attach", "kill_pane"),
    "pane_list": ("session_attach", "pane_spawn"),
    "pane_split": ("pane_spawn",),
    "pane_run": ("send_input",),
    "pane_metadata": ("session_attach", "pane_spawn"),
}


def _derive_herdr_facade_capabilities(payload: dict[str, object]) -> None:
    if payload.get("backend_impl") != "herdr":
        return
    command_status = payload.get("command_status")
    semantic_status = payload.get("semantic_status")
    if not isinstance(command_status, dict) or not isinstance(semantic_status, dict):
        return
    for facade_capability, prerequisites in _DERIVED_HERDR_FACADE_CAPABILITIES.items():
        if _all_capabilities_supported(command_status, prerequisites):
            command_status.setdefault(facade_capability, "supported")
        if _all_capabilities_supported(semantic_status, prerequisites):
            semantic_status.setdefault(facade_capability, "supported")


def _all_capabilities_supported(statuses: dict[object, object], names: tuple[str, ...]) -> bool:
    return all(statuses.get(name) == "supported" for name in names)


def _deduce_herdr_verdict(payload: dict[str, object]) -> None:
    if payload.get("verdict", ""):
        return
    adapter_recommendation = str(payload.get("adapter_recommendation") or "").strip()
    failure_class = str(payload.get("failure_class") or "").strip()
    if adapter_recommendation == "continue-with-gaps" and failure_class == "windows-beta-gap":
        payload["verdict"] = "partial"


def _herdr_socket_ref() -> str:
    return os.environ.get("CCB_HERDR_SOCKET_REF", "").strip() or "herdr://local"


def _herdr_request_adapter() -> HerdrCliRequestAdapter:
    session_name = os.environ.get("CCB_HERDR_SESSION", "").strip() or "ccb-herdr"
    executable = os.environ.get("CCB_HERDR_EXE", "").strip() or None
    return HerdrCliRequestAdapter(
        session_name=session_name,
        herdr_executable=executable,
        socket_ref=_herdr_socket_ref(),
        run_fn=_run,
    )


def _herdr_runtime_configured() -> bool:
    return any(
        os.environ.get(name, "").strip()
        for name in (
            "CCB_HERDR_CAPABILITY_REPORT",
            "CCB_HERDR_SOCKET_REF",
            "CCB_HERDR_SESSION",
            "CCB_HERDR_EXE",
        )
    )


def _live_herdr_platform_gate() -> dict[str, object]:
    os_platform = _runtime_os_platform()
    cpu_arch = _runtime_cpu_arch()
    python_bitness = platform.architecture()[0]
    is_wsl_runtime = _is_wsl_impl()
    return {
        "supported": os_platform == "win32"
        and cpu_arch == "x64"
        and python_bitness == "64bit"
        and is_wsl_runtime is False,
        "os_platform": os_platform,
        "cpu_arch": cpu_arch,
        "python_bitness": python_bitness,
        "is_wsl": is_wsl_runtime,
        "platform_gate_ref": "runtime",
        "failure_reason": None,
        "diagnostic": None,
    }


def _runtime_cpu_arch() -> str:
    machine = platform.machine().lower()
    if not machine:
        machine = (
            os.environ.get("PROCESSOR_ARCHITEW6432", "")
            or os.environ.get("PROCESSOR_ARCHITECTURE", "")
        ).lower()
    if machine in {"amd64", "x86_64"}:
        return "x64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return machine


def _runtime_os_platform() -> str:
    if is_windows():
        return "win32"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    return sys.platform or os.name


def _invalid_herdr_capability_report() -> dict[str, object]:
    return {
        "blocked": True,
        "failure_reason": "invalid-request",
        "diagnostic": "Herdr capability evidence is malformed",
    }


__all__ = [
    "LayoutResult",
    "TerminalBackend",
    "TmuxBackend",
    "HerdrBackend",
    "create_auto_layout",
    "detect_terminal",
    "get_backend",
    "get_backend_for_namespace_teardown",
    "get_backend_for_session",
    "get_pane_id_from_session",
    "get_shell_type",
    "is_windows",
    "is_wsl",
    "set_backend_config_preference",
]
