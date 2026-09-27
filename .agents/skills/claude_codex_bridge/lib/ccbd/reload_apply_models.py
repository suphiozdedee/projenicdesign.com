from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AdditiveReloadApplyResult:
    status: str
    stage: str
    plan_class: str | None = None
    old_graph_version: int | None = None
    target_graph_version: int | None = None
    published_graph_version: int | None = None
    old_config_signature: str | None = None
    new_config_signature: str | None = None
    plan: dict[str, object] | None = None
    namespace_patch: dict[str, object] | None = None
    runtime_mount: dict[str, object] | None = None
    publish_transaction: dict[str, object] | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_record(self) -> dict[str, object]:
        return {
            'status': self.status,
            'stage': self.stage,
            'plan_class': self.plan_class,
            'old_graph_version': self.old_graph_version,
            'target_graph_version': self.target_graph_version,
            'published_graph_version': self.published_graph_version,
            'old_config_signature': self.old_config_signature,
            'new_config_signature': self.new_config_signature,
            'plan': deepcopy(self.plan) if self.plan is not None else None,
            'namespace_patch': _dict_or_none(self.namespace_patch),
            'runtime_mount': _dict_or_none(self.runtime_mount),
            'publish_transaction': _dict_or_none(self.publish_transaction),
            'diagnostics': dict(self.diagnostics),
        }


def _dict_or_none(value: dict[str, object] | None) -> dict[str, object] | None:
    return dict(value) if value is not None else None


__all__ = ['AdditiveReloadApplyResult']
