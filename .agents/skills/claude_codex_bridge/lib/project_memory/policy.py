from __future__ import annotations

from dataclasses import dataclass

SOURCE_RUNTIME_COORDINATION_RULES = 'runtime_coordination_rules'
SOURCE_CCB_SHARED = 'ccb_shared'
SOURCE_PROVIDER_USER_MEMORY = 'provider_user_memory'
SOURCE_PROVIDER_NATIVE_PROJECT = 'provider_native_project'
SOURCE_AGENT_PRIVATE = 'agent_private'
SOURCE_RULES_DIR = 'rules_dir'

FILTER_CCB_INSTALL_BLOCKS = 'ccb_install_blocks'


@dataclass(frozen=True)
class MemorySourcePolicy:
    include_in_bundle: bool
    filters: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderMemoryPolicy:
    provider: str
    sources: dict[str, MemorySourcePolicy]

    def source_policy(self, kind: str) -> MemorySourcePolicy:
        return self.sources.get(kind, MemorySourcePolicy(include_in_bundle=True))


def memory_policy_for_provider(provider: str) -> ProviderMemoryPolicy:
    key = str(provider or '').strip().lower()
    return _PROVIDER_POLICIES.get(key, _DEFAULT_POLICY)


def should_include_source(provider: str, kind: str) -> bool:
    return memory_policy_for_provider(provider).source_policy(kind).include_in_bundle


def filters_for_source(provider: str, kind: str) -> tuple[str, ...]:
    return memory_policy_for_provider(provider).source_policy(kind).filters


def _policy(
    provider: str,
    *,
    include_provider_native_project: bool,
    filter_provider_user_memory: bool = True,
) -> ProviderMemoryPolicy:
    user_filters = (FILTER_CCB_INSTALL_BLOCKS,) if filter_provider_user_memory else ()
    return ProviderMemoryPolicy(
        provider=provider,
        sources={
            SOURCE_RUNTIME_COORDINATION_RULES: MemorySourcePolicy(include_in_bundle=True),
            SOURCE_CCB_SHARED: MemorySourcePolicy(include_in_bundle=True),
            SOURCE_PROVIDER_USER_MEMORY: MemorySourcePolicy(
                include_in_bundle=True,
                filters=user_filters,
            ),
            SOURCE_PROVIDER_NATIVE_PROJECT: MemorySourcePolicy(
                include_in_bundle=include_provider_native_project,
            ),
            SOURCE_AGENT_PRIVATE: MemorySourcePolicy(include_in_bundle=True),
            SOURCE_RULES_DIR: MemorySourcePolicy(include_in_bundle=False),
        },
    )


_DEFAULT_POLICY = _policy(
    'default',
    include_provider_native_project=True,
    filter_provider_user_memory=False,
)

_PROVIDER_POLICIES = {
    'claude': _policy('claude', include_provider_native_project=False),
    'codex': _policy('codex', include_provider_native_project=False),
    'opencode': _policy('opencode', include_provider_native_project=False),
    # Gemini's managed context-file behavior has not been audited for native
    # project loading, so preserve existing project-memory inclusion for now.
    'gemini': _policy('gemini', include_provider_native_project=True),
}


__all__ = [
    'FILTER_CCB_INSTALL_BLOCKS',
    'MemorySourcePolicy',
    'ProviderMemoryPolicy',
    'SOURCE_AGENT_PRIVATE',
    'SOURCE_CCB_SHARED',
    'SOURCE_PROVIDER_NATIVE_PROJECT',
    'SOURCE_PROVIDER_USER_MEMORY',
    'SOURCE_RULES_DIR',
    'SOURCE_RUNTIME_COORDINATION_RULES',
    'filters_for_source',
    'memory_policy_for_provider',
    'should_include_source',
]
