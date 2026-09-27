from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from terminal_runtime.mux_backend_contract import (
    BackendFamilyV2,
    BackendImplV2,
    MuxCapabilitiesV2,
    MuxNamespaceRefV2,
    MuxOperationEvidenceV2,
    MuxPaneRefV2,
    backend_family_for_impl,
    make_capabilities,
    make_namespace_ref,
    make_operation_evidence,
    make_pane_ref,
)


@dataclass
class _FakePane:
    namespace_id: str
    ref: MuxPaneRefV2
    content: list[str] = field(default_factory=list)
    alive: bool = True


class FakeMuxBackend:
    def __init__(
        self,
        *,
        backend_impl: BackendImplV2 = "herdr",
        capabilities: MuxCapabilitiesV2 | None = None,
    ) -> None:
        self.backend_impl = backend_impl
        self.backend_family: BackendFamilyV2 = backend_family_for_impl(backend_impl)
        self._capabilities = capabilities or make_capabilities(
            backend_impl=backend_impl,
            command_status={
                "session_attach": "supported",
                "pane_spawn": "supported",
                "send_input": "supported",
                "read_output": "supported",
                "kill_pane": "supported",
            },
            semantic_status={
                "session_attach": "supported",
                "pane_spawn": "supported",
                "send_input": "supported",
                "read_output": "supported",
                "kill_pane": "supported",
            },
        )
        self._namespace_count = 0
        self._pane_count = 0
        self._namespaces: dict[str, MuxNamespaceRefV2] = {}
        self._panes: dict[str, _FakePane] = {}

    def capabilities(self) -> MuxCapabilitiesV2:
        return self._capabilities

    def create_session(
        self,
        *,
        project_id: str,
        cwd: str,
        title: str,
    ) -> MuxNamespaceRefV2:
        del cwd
        self._namespace_count += 1
        namespace_id = f"{self.backend_impl}-namespace-{self._namespace_count}"
        restore_token = f"{self.backend_impl}-restore-{self._namespace_count}"
        ref = make_namespace_ref(
            backend_impl=self.backend_impl,
            namespace_id=namespace_id,
            session_name=title or project_id,
            ipc_kind="herdr_socket" if self.backend_impl == "herdr" else "socket_name",
            ipc_ref=f"{self.backend_impl}://{namespace_id}",
            restore_token=restore_token,
        )
        self._namespaces[namespace_id] = ref
        return ref

    def restore_session(self, *, restore_token: str) -> MuxNamespaceRefV2:
        for namespace in self._namespaces.values():
            if namespace["restore_token"] == restore_token:
                return namespace
        raise KeyError(f"unknown fake restore token {restore_token!r}")

    def create_pane(
        self,
        namespace: MuxNamespaceRefV2,
        *,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        title: str,
    ) -> MuxPaneRefV2:
        del cwd, env
        namespace_id = namespace["namespace_id"]
        if namespace_id not in self._namespaces:
            raise KeyError(f"unknown fake namespace {namespace_id!r}")
        self._pane_count += 1
        pane_id = f"{self.backend_impl}-pane-{self._pane_count}"
        ref = make_pane_ref(
            backend_impl=self.backend_impl,
            pane_id=pane_id,
            session_name=namespace["session_name"],
            window_name=title or None,
        )
        pane = _FakePane(namespace_id=namespace_id, ref=ref)
        if command:
            pane.content.append("$ " + " ".join(command))
        self._panes[pane_id] = pane
        return ref

    def split_pane(
        self,
        pane: MuxPaneRefV2,
        *,
        direction: Literal["left", "right", "up", "down"] = "right",
        percent: int = 50,
        command: list[str] | None = None,
        cwd: str = "",
        env: dict[str, str] | None = None,
        title: str = "",
    ) -> MuxPaneRefV2:
        del direction, percent
        state = self._pane(pane)
        namespace = self._namespaces[state.namespace_id]
        return self.create_pane(
            namespace,
            command=command or [],
            cwd=cwd,
            env=env or {},
            title=title,
        )

    def send_text(self, pane: MuxPaneRefV2, text: str) -> MuxOperationEvidenceV2:
        state = self._pane(pane)
        if not state.alive:
            return make_operation_evidence(
                operation="send_text",
                backend_impl=self.backend_impl,
                pane_id=pane["pane_id"],
                status="failed",
                detail="pane is not alive",
            )
        state.content.append(text)
        return make_operation_evidence(
            operation="send_text",
            backend_impl=self.backend_impl,
            pane_id=pane["pane_id"],
            detail="text appended",
        )

    def capture_pane(
        self,
        pane: MuxPaneRefV2,
        *,
        lines: int,
    ) -> tuple[str, MuxOperationEvidenceV2]:
        state = self._pane(pane)
        limit = max(0, int(lines))
        captured = "\n".join(state.content[-limit:] if limit else [])
        return (
            captured,
            make_operation_evidence(
                operation="capture_pane",
                backend_impl=self.backend_impl,
                pane_id=pane["pane_id"],
                detail="captured fake pane output",
            ),
        )

    def kill_pane(self, pane: MuxPaneRefV2) -> MuxOperationEvidenceV2:
        state = self._pane(pane)
        state.alive = False
        return make_operation_evidence(
            operation="kill_pane",
            backend_impl=self.backend_impl,
            pane_id=pane["pane_id"],
            detail="fake pane marked dead",
        )

    def _pane(self, pane: MuxPaneRefV2) -> _FakePane:
        pane_id = pane["pane_id"]
        try:
            return self._panes[pane_id]
        except KeyError as exc:
            raise KeyError(f"unknown fake pane {pane_id!r}") from exc


__all__ = ["FakeMuxBackend"]
