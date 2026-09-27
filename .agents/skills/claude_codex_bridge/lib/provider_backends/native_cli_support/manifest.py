from __future__ import annotations

from agents.models import RuntimeMode
from completion.models import CompletionFamily, CompletionSourceKind, SelectorFamily
from completion.profiles import CompletionManifest
from provider_core.manifests import ProviderManifest


def build_native_cli_manifest(*, provider: str, supports_subagents: bool = False) -> ProviderManifest:
    provider = str(provider or "").strip().lower()
    return ProviderManifest(
        provider=provider,
        supports_resume=False,
        supports_permission_auto=False,
        supports_stream_watch=False,
        supports_subagents=supports_subagents,
        supports_workspace_attach=True,
        runtime_profiles={
            RuntimeMode.PANE_BACKED: CompletionManifest(
                provider=provider,
                runtime_mode=RuntimeMode.PANE_BACKED.value,
                completion_family=CompletionFamily.STRUCTURED_RESULT,
                completion_source_kind=CompletionSourceKind.STRUCTURED_RESULT_STREAM,
                supports_exact_completion=False,
                supports_observed_completion=True,
                supports_anchor_binding=True,
                supports_reply_stability=False,
                supports_terminal_reason=True,
                selector_family=SelectorFamily.STRUCTURED_RESULT,
            ),
        },
    )


__all__ = ["build_native_cli_manifest"]
