from __future__ import annotations

from typing import Literal, Mapping, TypedDict

BackendFamilyV2 = Literal["tmux-family", "herdr-native"]
BackendImplV2 = Literal["tmux", "psmux", "rmux", "herdr"]
IpcKindV2 = Literal[
    "unix_socket",
    "named_pipe",
    "socket_name",
    "socket_path",
    "herdr_socket",
    "tcp_loopback",
    "none",
]
MuxCapabilityStatusV2 = Literal["supported", "partial", "unsupported", "workaround"]
MuxErrorCategoryV2 = Literal[
    "transient-unavailable",
    "unsupported",
    "not-found",
    "permission",
    "command-failed",
    "schema-mismatch",
]
MuxSelectionSourceV2 = Literal[
    "cli",
    "project_config",
    "user_config",
    "env",
    "platform_default",
    "auto_probe",
]
HerdrFailureReasonV2 = Literal[
    "platform-gate-blocked",
    "herdr-capability-missing",
    "herdr-unavailable",
    "schema-mismatch",
    "unsupported-capability",
    "invalid-request",
]
_REQUIRED_CAPABILITIES_V2 = frozenset(
    {
        "session_attach",
        "pane_spawn",
        "send_input",
        "read_output",
        "kill_pane",
    }
)


class MuxNamespaceRefV2(TypedDict):
    backend_family: BackendFamilyV2
    backend_impl: BackendImplV2
    namespace_id: str
    session_name: str
    ipc_kind: IpcKindV2
    ipc_ref: str
    restore_token: str | None


class MuxPaneRefV2(TypedDict):
    backend_impl: BackendImplV2
    pane_id: str
    session_name: str
    window_name: str | None
    agent_slug: str | None


class MuxCapabilitiesV2(TypedDict):
    backend_impl: BackendImplV2
    command_status: dict[str, MuxCapabilityStatusV2]
    semantic_status: dict[str, MuxCapabilityStatusV2]
    windows_beta_gaps: list[str]
    blocking_gaps: list[str]
    source_ref: str | None


class MuxOperationEvidenceV2(TypedDict):
    operation: str
    backend_impl: BackendImplV2
    pane_id: str | None
    status: Literal["ok", "failed", "unsupported", "schema-mismatch"]
    detail: str | None


class MuxBackendSelectionV2(TypedDict):
    backend_family: BackendFamilyV2
    backend_impl: BackendImplV2
    requested_backend: Literal["tmux", "rmux", "herdr", "auto"]
    effective_backend: BackendImplV2
    source: MuxSelectionSourceV2
    platform_gate: dict[str, object] | None
    fallback_used: bool
    fallback_reason: str | None
    capability_report_ref: str | None
    diagnostic: str


class MuxBackendSelectionFailureV2(TypedDict):
    blocked: Literal[True]
    backend_family: BackendFamilyV2
    backend_impl: BackendImplV2
    requested_backend: Literal["tmux", "rmux", "herdr", "auto"]
    effective_backend: BackendImplV2 | None
    source: MuxSelectionSourceV2
    platform_gate: dict[str, object] | None
    fallback_used: Literal[False]
    fallback_reason: None
    capability_report_ref: str | None
    failure_reason: HerdrFailureReasonV2
    diagnostic: str


class MuxCommandErrorV2(Exception):
    def __init__(
        self,
        *,
        category: MuxErrorCategoryV2,
        backend_impl: BackendImplV2,
        operation: str,
        detail: str,
        ipc_ref: str | None = None,
        evidence: dict[str, object] | None = None,
    ) -> None:
        super().__init__(detail)
        self.category = category
        self.backend_impl = backend_impl
        self.operation = operation
        self.detail = detail
        self.ipc_ref = ipc_ref
        self.evidence = evidence or {}


def backend_family_for_impl(backend_impl: BackendImplV2) -> BackendFamilyV2:
    if backend_impl == "herdr":
        return "herdr-native"
    return "tmux-family"


def make_namespace_ref(
    *,
    backend_impl: BackendImplV2,
    namespace_id: str,
    session_name: str,
    ipc_kind: IpcKindV2,
    ipc_ref: str,
    restore_token: str | None = None,
) -> MuxNamespaceRefV2:
    if not namespace_id.strip():
        raise ValueError("Namespace refs require a non-empty namespace_id")
    if not session_name.strip():
        raise ValueError("Namespace refs require a non-empty session_name")
    if backend_impl == "herdr":
        if ipc_kind not in {"herdr_socket", "tcp_loopback"}:
            raise ValueError("Herdr namespace refs require herdr_socket or tcp_loopback IPC")
        if not ipc_ref.strip():
            raise ValueError("Herdr namespace refs require a non-empty IPC reference")
    return {
        "backend_family": backend_family_for_impl(backend_impl),
        "backend_impl": backend_impl,
        "namespace_id": namespace_id,
        "session_name": session_name,
        "ipc_kind": ipc_kind,
        "ipc_ref": ipc_ref,
        "restore_token": restore_token,
    }


def make_pane_ref(
    *,
    backend_impl: BackendImplV2,
    pane_id: str,
    session_name: str,
    window_name: str | None = None,
    agent_slug: str | None = None,
) -> MuxPaneRefV2:
    if not pane_id.strip():
        raise ValueError("Pane refs require a non-empty pane_id")
    if not session_name.strip():
        raise ValueError("Pane refs require a non-empty session_name")
    return {
        "backend_impl": backend_impl,
        "pane_id": pane_id,
        "session_name": session_name,
        "window_name": window_name,
        "agent_slug": agent_slug,
    }


def make_capabilities(
    *,
    backend_impl: BackendImplV2,
    command_status: dict[str, MuxCapabilityStatusV2],
    semantic_status: dict[str, MuxCapabilityStatusV2],
    windows_beta_gaps: list[str] | None = None,
    blocking_gaps: list[str] | None = None,
    source_ref: str | None = None,
) -> MuxCapabilitiesV2:
    return {
        "backend_impl": backend_impl,
        "command_status": dict(command_status),
        "semantic_status": dict(semantic_status),
        "windows_beta_gaps": list(windows_beta_gaps or ()),
        "blocking_gaps": list(blocking_gaps or ()),
        "source_ref": source_ref,
    }


def make_operation_evidence(
    *,
    operation: str,
    backend_impl: BackendImplV2,
    pane_id: str | None,
    status: Literal["ok", "failed", "unsupported", "schema-mismatch"] = "ok",
    detail: str | None = None,
) -> MuxOperationEvidenceV2:
    return {
        "operation": operation,
        "backend_impl": backend_impl,
        "pane_id": pane_id,
        "status": status,
        "detail": detail,
    }


def capability_statuses_supported(capabilities: Mapping[str, object]) -> bool:
    backend_impl = capabilities.get("backend_impl")
    blocking_gaps = capabilities.get("blocking_gaps")
    windows_beta_gaps = capabilities.get("windows_beta_gaps")
    command_status = capabilities.get("command_status")
    semantic_status = capabilities.get("semantic_status")
    return (
        backend_impl == "herdr"
        and isinstance(blocking_gaps, list)
        and isinstance(windows_beta_gaps, list)
        and isinstance(command_status, Mapping)
        and isinstance(semantic_status, Mapping)
        and _REQUIRED_CAPABILITIES_V2.issubset(command_status)
        and _REQUIRED_CAPABILITIES_V2.issubset(semantic_status)
        and not blocking_gaps
        and not windows_beta_gaps
        and all(value == "supported" for value in command_status.values())
        and all(value == "supported" for value in semantic_status.values())
    )


__all__ = [
    "BackendFamilyV2",
    "BackendImplV2",
    "HerdrFailureReasonV2",
    "IpcKindV2",
    "MuxBackendSelectionFailureV2",
    "MuxBackendSelectionV2",
    "MuxCapabilitiesV2",
    "MuxCapabilityStatusV2",
    "MuxCommandErrorV2",
    "MuxErrorCategoryV2",
    "MuxNamespaceRefV2",
    "MuxOperationEvidenceV2",
    "MuxPaneRefV2",
    "MuxSelectionSourceV2",
    "backend_family_for_impl",
    "capability_statuses_supported",
    "make_capabilities",
    "make_namespace_ref",
    "make_operation_evidence",
    "make_pane_ref",
]
