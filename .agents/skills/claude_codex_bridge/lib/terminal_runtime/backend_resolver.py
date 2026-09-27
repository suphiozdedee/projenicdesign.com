from __future__ import annotations

from typing import Literal, Mapping, cast

from platforms.windows.herdr.runtime.capabilities import (
    herdr_capability_report_supported,
    unsupported_capability_names,
)
from terminal_runtime.mux_backend_contract import (
    BackendImplV2,
    HerdrFailureReasonV2,
    MuxBackendSelectionFailureV2,
    MuxBackendSelectionV2,
    MuxCapabilitiesV2,
    MuxSelectionSourceV2,
    backend_family_for_impl,
)

RequestedBackendV2 = Literal["tmux", "rmux", "herdr", "auto"]
_HERDR_FAILURE_REASONS = {
    "platform-gate-blocked",
    "herdr-capability-missing",
    "herdr-unavailable",
    "schema-mismatch",
    "unsupported-capability",
    "invalid-request",
}
_HERDR_REQUIRED_CAPABILITIES = frozenset(
    {
        "session_attach",
        "pane_spawn",
        "send_input",
        "read_output",
        "kill_pane",
    }
)
_HERDR_SELECTION_REQUIRED_OPERATIONS = (
    "prepare_server",
    "create_session",
    "restore_session",
    "namespace_alive",
    "list_windows",
    "ensure_window",
    "window_root_pane",
    "set_pane_identity",
    "describe_pane",
    "list_panes_by_user_options",
    "create_pane",
    "respawn_pane",
    "move_pane",
    "reflow_window",
    "send_text",
    "capture_pane",
    "kill_pane",
    "select_window",
    "kill_window",
    "destroy_namespace",
    "kill_server",
    "rename_window",
    "attach_namespace",
)


def resolve_mux_backend_v2(
    *,
    requested_backend: RequestedBackendV2,
    source: MuxSelectionSourceV2,
    platform_gate: Mapping[str, object] | None,
    capability_report: MuxCapabilitiesV2 | MuxBackendSelectionFailureV2 | None,
    capability_report_ref: str | None,
    legacy_default_backend: Literal["tmux", "rmux"] = "tmux",
) -> MuxBackendSelectionV2 | MuxBackendSelectionFailureV2:
    if requested_backend in {"tmux", "rmux"}:
        return _selection(
            backend_impl=requested_backend,
            requested_backend=requested_backend,
            source=source,
            platform_gate=platform_gate,
            capability_report_ref=capability_report_ref,
            diagnostic=f"explicit {requested_backend} backend selected",
        )

    if requested_backend == "auto":
        if not _has_known_platform_gate(platform_gate):
            return _blocked_selection(
                requested_backend=requested_backend,
                source=source,
                platform_gate=platform_gate,
                capability_report_ref=capability_report_ref,
                failure_reason="platform-gate-blocked",
                diagnostic="Herdr auto selection requires a known platform gate",
            )
        if not _is_native_windows_x64(platform_gate):
            return _selection(
                backend_impl=legacy_default_backend,
                requested_backend=requested_backend,
                source=source,
                platform_gate=platform_gate,
                capability_report_ref=capability_report_ref,
                diagnostic=f"non-Windows auto selected legacy {legacy_default_backend} backend",
            )

    should_route_to_herdr = requested_backend == "herdr" or _is_native_windows_x64(platform_gate)
    if not should_route_to_herdr:
        return _selection(
            backend_impl=legacy_default_backend,
            requested_backend=requested_backend,
            source=source,
            platform_gate=platform_gate,
            capability_report_ref=capability_report_ref,
            diagnostic=f"non-Windows auto selected legacy {legacy_default_backend} backend",
        )

    if not _has_supported_native_windows_x64_gate(platform_gate):
        return _blocked_selection(
            requested_backend=requested_backend,
            source=source,
            platform_gate=platform_gate,
            capability_report_ref=capability_report_ref,
            failure_reason="platform-gate-blocked",
            diagnostic="Herdr selection requires a supported Native Windows x64 platform gate",
        )

    if capability_report is None:
        return _blocked_selection(
            requested_backend=requested_backend,
            source=source,
            platform_gate=platform_gate,
            capability_report_ref=capability_report_ref,
            failure_reason="herdr-capability-missing",
            diagnostic="Herdr selection is blocked because capability evidence is unavailable",
        )

    if not isinstance(capability_report, Mapping):
        return _blocked_selection(
            requested_backend=requested_backend,
            source=source,
            platform_gate=platform_gate,
            capability_report_ref=capability_report_ref,
            failure_reason="invalid-request",
            diagnostic="Herdr selection is blocked by malformed capability evidence",
        )

    if capability_report.get("blocked") is True:
        failure_reason = capability_report.get("failure_reason")
        diagnostic = capability_report.get("diagnostic")
        if (
            failure_reason not in _HERDR_FAILURE_REASONS
            or not isinstance(diagnostic, str)
            or not diagnostic.strip()
        ):
            failure_reason = "invalid-request"
            diagnostic = "Herdr selection is blocked by malformed capability evidence"
        return _blocked_selection(
            requested_backend=requested_backend,
            source=source,
            platform_gate=platform_gate,
            capability_report_ref=capability_report_ref,
            failure_reason=failure_reason,
            diagnostic=diagnostic,
        )

    if not _has_supported_herdr_capabilities(capability_report, capability_report_ref):
        return _blocked_selection(
            requested_backend=requested_backend,
            source=source,
            platform_gate=platform_gate,
            capability_report_ref=capability_report_ref,
            failure_reason="unsupported-capability",
            diagnostic="Herdr selection is blocked by unsupported, partial, or blocking capability evidence",
        )

    return _selection(
        backend_impl="herdr",
        requested_backend=requested_backend,
        source=source,
        platform_gate=platform_gate,
        capability_report_ref=capability_report_ref,
        diagnostic="Native Windows x64 selected Herdr after platform and capability validation",
    )


def build_herdr_capability_blocked_fixture(
    spike_evidence: Mapping[str, object] | None,
    *,
    requested_backend: Literal["herdr", "auto"] = "auto",
    source: MuxSelectionSourceV2 = "auto_probe",
    capability_report_ref: str | None = None,
) -> MuxBackendSelectionFailureV2:
    platform_gate = _platform_gate_from_spike(spike_evidence)
    reason = _failure_reason_from_spike(spike_evidence)
    return _blocked_selection(
        requested_backend=requested_backend,
        source=source,
        platform_gate=platform_gate,
        capability_report_ref=capability_report_ref,
        failure_reason=reason,
        diagnostic=_diagnostic_from_spike(spike_evidence, reason),
    )


def _selection(
    *,
    backend_impl: BackendImplV2,
    requested_backend: RequestedBackendV2,
    source: MuxSelectionSourceV2,
    platform_gate: Mapping[str, object] | None,
    capability_report_ref: str | None,
    diagnostic: str,
) -> MuxBackendSelectionV2:
    return {
        "backend_family": backend_family_for_impl(backend_impl),
        "backend_impl": backend_impl,
        "requested_backend": requested_backend,
        "effective_backend": backend_impl,
        "source": source,
        "platform_gate": dict(platform_gate) if platform_gate is not None else None,
        "fallback_used": False,
        "fallback_reason": None,
        "capability_report_ref": capability_report_ref,
        "diagnostic": diagnostic,
    }


def _blocked_selection(
    *,
    requested_backend: RequestedBackendV2,
    source: MuxSelectionSourceV2,
    platform_gate: Mapping[str, object] | None,
    capability_report_ref: str | None,
    failure_reason: HerdrFailureReasonV2,
    diagnostic: str,
) -> MuxBackendSelectionFailureV2:
    return {
        "blocked": True,
        "backend_family": "herdr-native",
        "backend_impl": "herdr",
        "requested_backend": requested_backend,
        "effective_backend": None,
        "source": source,
        "platform_gate": dict(platform_gate) if platform_gate is not None else None,
        "fallback_used": False,
        "fallback_reason": None,
        "capability_report_ref": capability_report_ref,
        "failure_reason": failure_reason,
        "diagnostic": diagnostic,
    }


def _is_native_windows_x64(platform_gate: Mapping[str, object] | None) -> bool:
    if platform_gate is None:
        return False
    return (
        platform_gate.get("os_platform") == "win32"
        and platform_gate.get("cpu_arch") == "x64"
    )


def _has_known_platform_gate(platform_gate: Mapping[str, object] | None) -> bool:
    if platform_gate is None:
        return False
    return platform_gate.get("os_platform") is not None and platform_gate.get("cpu_arch") is not None


def _has_supported_native_windows_x64_gate(platform_gate: Mapping[str, object] | None) -> bool:
    return (
        _is_native_windows_x64(platform_gate)
        and platform_gate is not None
        and platform_gate.get("supported") is True
        and platform_gate.get("python_bitness") == "64bit"
        and platform_gate.get("is_wsl") is False
    )


def _has_supported_herdr_capabilities(
    capability_report: Mapping[str, object],
    capability_report_ref: str | None,
) -> bool:
    command_status = capability_report.get("command_status")
    semantic_status = capability_report.get("semantic_status")
    source_ref = capability_report.get("source_ref")
    has_evidence_ref = (
        isinstance(capability_report_ref, str)
        and bool(capability_report_ref.strip())
    ) or (
        isinstance(source_ref, str)
        and bool(source_ref.strip())
    )
    return (
        capability_report.get("backend_impl") == "herdr"
        and isinstance(command_status, Mapping)
        and isinstance(semantic_status, Mapping)
        and _HERDR_REQUIRED_CAPABILITIES.issubset(command_status)
        and _HERDR_REQUIRED_CAPABILITIES.issubset(semantic_status)
        and has_evidence_ref
        and herdr_capability_report_supported(capability_report)
        and all(
            not unsupported_capability_names(capability_report, operation)
            for operation in _HERDR_SELECTION_REQUIRED_OPERATIONS
        )
    )


def _platform_gate_from_spike(spike_evidence: Mapping[str, object] | None) -> dict[str, object] | None:
    if not spike_evidence:
        return None
    host = spike_evidence.get("host")
    if not isinstance(host, Mapping):
        return None
    return {
        "supported": host.get("platform_gate_supported") is True,
        "os_platform": host.get("platform_gate_os_platform") or host.get("os_platform"),
        "cpu_arch": host.get("platform_gate_cpu_arch") or host.get("cpu_arch"),
        "python_bitness": host.get("platform_gate_python_bitness") or host.get("python_bitness"),
        "is_wsl": host.get("is_wsl"),
        "platform_gate_ref": host.get("platform_gate_ref"),
        "failure_reason": host.get("platform_gate_failure_reason"),
        "diagnostic": host.get("platform_gate_detail_reason"),
    }


def _failure_reason_from_spike(spike_evidence: Mapping[str, object] | None) -> HerdrFailureReasonV2:
    if not spike_evidence:
        return "herdr-capability-missing"
    failure_class = str(spike_evidence.get("failure_class") or "").strip()
    if failure_class in _HERDR_FAILURE_REASONS:
        return cast(HerdrFailureReasonV2, failure_class)
    platform_gate = _platform_gate_from_spike(spike_evidence)
    if platform_gate is not None and not _has_supported_native_windows_x64_gate(platform_gate):
        return "platform-gate-blocked"
    return "unsupported-capability"


def _diagnostic_from_spike(
    spike_evidence: Mapping[str, object] | None,
    failure_reason: HerdrFailureReasonV2,
) -> str:
    if not spike_evidence:
        return "Herdr selection is blocked because no spike evidence is available"
    projection = spike_evidence.get("capability_projection")
    gaps: list[str] = []
    if isinstance(projection, Mapping):
        raw_gaps = projection.get("blocking_gaps")
        if isinstance(raw_gaps, list):
            gaps.extend(str(item) for item in raw_gaps if str(item).strip())
    if gaps:
        return f"Herdr selection is blocked by capability gaps: {', '.join(gaps)}"
    return f"Herdr selection is blocked by spike evidence ({failure_reason})"


__all__ = [
    "RequestedBackendV2",
    "build_herdr_capability_blocked_fixture",
    "resolve_mux_backend_v2",
]
