from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, replace
from threading import RLock

from agents.models import AgentRuntime, AgentSpec, AgentState, normalize_agent_name
from agents.store import AgentRuntimeStore
from provider_runtime.helper_cleanup import cleanup_stale_runtime_helper
from provider_runtime.helper_manifest import clear_helper_manifest
from provider_runtime.helper_manifest import sync_runtime_helper_manifest
from storage.paths import PathLayout

_ACTIVE_STATES = {AgentState.STARTING, AgentState.IDLE, AgentState.BUSY, AgentState.DEGRADED}
_STATE_MUTATION_FIELDS = frozenset(
    {
        'state',
        'health',
        'queue_depth',
        'active_pane_id',
        'pane_state',
        'desired_state',
        'reconcile_state',
        'restart_count',
        'recovery_failure_count',
        'last_seen_at',
        'last_reconcile_at',
        'last_failure_reason',
        'lifecycle_state',
        'pid',
    }
)
_RUNTIME_FIELD_NAMES = tuple(field.name for field in fields(AgentRuntime))
_AUTHORITY_MUTATION_FIELDS = frozenset(set(_RUNTIME_FIELD_NAMES) - _STATE_MUTATION_FIELDS)


class AgentRegistry:
    def __init__(self, layout: PathLayout, config, runtime_store: AgentRuntimeStore | None = None) -> None:
        self._layout = layout
        self._config = config
        self._runtime_store = runtime_store or AgentRuntimeStore(layout)
        self._cache: dict[str, AgentRuntime] = {}
        self._lock = RLock()
        self._load_existing()

    def _load_existing(self) -> None:
        with self._lock:
            for agent_name in self._config.agents:
                runtime = self._runtime_store.load(agent_name)
                if runtime is not None:
                    self._cache[agent_name] = runtime

    def spec_for(self, agent_name: str) -> AgentSpec:
        normalized = normalize_agent_name(agent_name)
        try:
            return self._config.agents[normalized]
        except KeyError as exc:
            raise KeyError(f'unknown agent: {normalized}') from exc

    def get(self, agent_name: str) -> AgentRuntime | None:
        normalized = normalize_agent_name(agent_name)
        with self._lock:
            return self._get_locked(normalized)

    def upsert(self, runtime: AgentRuntime, *, authority_write: bool = False) -> AgentRuntime:
        with self._lock:
            return self._save_locked(runtime, authority_write=authority_write)

    def upsert_authority(self, runtime: AgentRuntime) -> AgentRuntime:
        with self._lock:
            existing = self._get_locked(runtime.agent_name)
            if existing is not None:
                runtime = _merge_stale_authority_candidate(existing, runtime)
            return self._save_locked(runtime, authority_write=True, existing=existing)

    def update(
        self,
        agent_name: str,
        update_fn: Callable[[AgentRuntime | None], AgentRuntime | None],
        *,
        authority_write: bool = False,
    ) -> AgentRuntime | None:
        normalized = normalize_agent_name(agent_name)
        with self._lock:
            current = self._get_locked(normalized)
            updated = update_fn(current)
            if updated is None:
                return current
            if current is not None and updated == current:
                return current
            if authority_write and current is not None:
                updated = _merge_stale_authority_candidate(current, updated)
            return self._save_locked(updated, authority_write=authority_write, existing=current)

    def remove(self, agent_name: str) -> AgentRuntime | None:
        with self._lock:
            runtime = self._get_locked(agent_name)
            if runtime is None:
                clear_helper_manifest(self._layout.agent_helper_path(agent_name))
                return None
            stopped = replace(
                runtime,
                state=AgentState.STOPPED,
                pid=None,
                runtime_ref=None,
                session_ref=None,
                socket_path=None,
                queue_depth=0,
                health='stopped',
                runtime_pid=None,
                pane_id=None,
                active_pane_id=None,
                pane_state=None,
                desired_state='stopped',
                reconcile_state='stopped',
                recovery_failure_count=0,
                last_failure_reason=None,
            )
            saved = self.upsert_authority(stopped)
            clear_helper_manifest(self._layout.agent_helper_path(agent_name))
            return saved

    def list_all(self) -> tuple[AgentRuntime, ...]:
        with self._lock:
            runtimes: list[AgentRuntime] = []
            for agent_name in sorted(self._config.agents):
                runtime = self._get_locked(agent_name)
                if runtime is not None:
                    runtimes.append(runtime)
            return tuple(runtimes)

    def list_alive(self) -> tuple[AgentRuntime, ...]:
        return tuple(runtime for runtime in self.list_all() if runtime.state in _ACTIVE_STATES)

    def list_known_agents(self) -> tuple[str, ...]:
        return tuple(sorted(self._config.agents))

    def _get_locked(self, agent_name: str) -> AgentRuntime | None:
        normalized = normalize_agent_name(agent_name)
        cached = self._cache.get(normalized)
        if cached is not None:
            return cached
        runtime = self._runtime_store.load(normalized)
        if runtime is not None:
            self._cache[normalized] = runtime
        return runtime

    def _save_locked(
        self,
        runtime: AgentRuntime,
        *,
        authority_write: bool,
        existing: AgentRuntime | None = None,
    ) -> AgentRuntime:
        self.spec_for(runtime.agent_name)
        current = existing if existing is not None else self._get_locked(runtime.agent_name)
        changed_fields = _changed_runtime_fields(current, runtime)
        if current is not None and not changed_fields:
            return current
        if current is not None and changed_fields == ('last_seen_at',):
            self._cache[runtime.agent_name] = runtime
            return runtime
        if changed_fields and not authority_write:
            authority_fields = tuple(sorted(set(changed_fields) - _STATE_MUTATION_FIELDS))
            if authority_fields:
                raise ValueError(
                    'authority write required for runtime fields: '
                    + ', '.join(authority_fields)
                )
        cleanup_stale_runtime_helper(self._layout, runtime)
        self._runtime_store.save(runtime)
        sync_runtime_helper_manifest(self._layout, runtime)
        self._cache[runtime.agent_name] = runtime
        return runtime


def _changed_runtime_fields(existing: AgentRuntime | None, runtime: AgentRuntime) -> tuple[str, ...]:
    if existing is None:
        return ()
    changed: list[str] = []
    for field_name in _RUNTIME_FIELD_NAMES:
        if getattr(existing, field_name) != getattr(runtime, field_name):
            changed.append(field_name)
    return tuple(changed)


def _positive_generation(value: object) -> int:
    try:
        generation = int(value or 0)
    except Exception:
        return 0
    return generation if generation > 0 else 0


def _merge_stale_authority_candidate(existing: AgentRuntime, candidate: AgentRuntime) -> AgentRuntime:
    existing_generation = _positive_generation(getattr(existing, 'runtime_generation', None))
    candidate_generation = _positive_generation(getattr(candidate, 'runtime_generation', None))
    if candidate_generation >= existing_generation or existing_generation <= 0:
        return candidate
    merged_fields = {
        field_name: getattr(existing, field_name)
        for field_name in _AUTHORITY_MUTATION_FIELDS
        if field_name not in _STATE_MUTATION_FIELDS
    }
    return replace(candidate, **merged_fields)


__all__ = ['AgentRegistry']
