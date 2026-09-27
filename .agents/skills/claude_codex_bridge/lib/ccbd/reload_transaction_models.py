from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReloadPublishTransactionResult:
    status: str
    published_graph_version: int | None = None
    old_graph_version: int | None = None
    old_config_signature: str | None = None
    new_config_signature: str | None = None
    namespace_patch: dict[str, object] | None = None
    runtime_mount: dict[str, object] | None = None
    lease: dict[str, object] | None = None
    lifecycle: dict[str, object] | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_record(self) -> dict[str, object]:
        return {
            'status': self.status,
            'published_graph_version': self.published_graph_version,
            'old_graph_version': self.old_graph_version,
            'old_config_signature': self.old_config_signature,
            'new_config_signature': self.new_config_signature,
            'namespace_patch': _dict_or_none(self.namespace_patch),
            'runtime_mount': _dict_or_none(self.runtime_mount),
            'lease': _dict_or_none(self.lease),
            'lifecycle': _dict_or_none(self.lifecycle),
            'diagnostics': dict(self.diagnostics),
        }


def _dict_or_none(value: dict[str, object] | None) -> dict[str, object] | None:
    return dict(value) if value is not None else None


__all__ = ['ReloadPublishTransactionResult']
