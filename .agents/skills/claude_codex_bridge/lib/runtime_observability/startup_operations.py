from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar


class StartupOperationCollector:
    """Request-scoped, diagnostics-only startup operation counters."""

    __slots__ = ('_counts',)

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def record(self, field_name: str, amount: int = 1) -> None:
        name = str(field_name or '').strip()
        if not name or isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            return
        self._counts[name] = self._counts.get(name, 0) + amount

    def snapshot(self) -> dict[str, int]:
        return dict(sorted(self._counts.items()))


_CURRENT_COLLECTOR: ContextVar[StartupOperationCollector | None] = ContextVar(
    'ccb_startup_operation_collector',
    default=None,
)
_CURRENT_SCOPES: ContextVar[tuple[str, ...]] = ContextVar(
    'ccb_startup_operation_scopes',
    default=(),
)


@contextmanager
def collect_startup_operations() -> Iterator[StartupOperationCollector]:
    """Activate one collector, reusing an enclosing startup request if present."""

    current = _CURRENT_COLLECTOR.get()
    if current is not None:
        yield current
        return
    collector = StartupOperationCollector()
    collector_token = _CURRENT_COLLECTOR.set(collector)
    scopes_token = _CURRENT_SCOPES.set(())
    try:
        yield collector
    finally:
        _CURRENT_SCOPES.reset(scopes_token)
        _CURRENT_COLLECTOR.reset(collector_token)


@contextmanager
def startup_operation_scope(scope_name: str) -> Iterator[None]:
    """Tag operations in a narrow, explicitly instrumented startup sub-scope."""

    name = str(scope_name or '').strip()
    if not name or _CURRENT_COLLECTOR.get() is None:
        yield
        return
    token = _CURRENT_SCOPES.set((*_CURRENT_SCOPES.get(), name))
    try:
        yield
    finally:
        _CURRENT_SCOPES.reset(token)


def in_startup_operation_scope(scope_name: str) -> bool:
    name = str(scope_name or '').strip()
    return bool(name and name in _CURRENT_SCOPES.get())


def record_startup_operation(field_name: str, amount: int = 1) -> None:
    collector = _CURRENT_COLLECTOR.get()
    if collector is None:
        return
    try:
        collector.record(field_name, amount)
    except Exception:
        # Diagnostics must never change startup behavior or error propagation.
        return


def record_startup_operations(counts: Mapping[str, int]) -> None:
    for field_name, amount in counts.items():
        record_startup_operation(field_name, amount)


def startup_operation_counts() -> dict[str, int]:
    collector = _CURRENT_COLLECTOR.get()
    if collector is None:
        return {}
    try:
        return collector.snapshot()
    except Exception:
        return {}


def startup_operation_collection_active() -> bool:
    return _CURRENT_COLLECTOR.get() is not None


__all__ = [
    'StartupOperationCollector',
    'collect_startup_operations',
    'in_startup_operation_scope',
    'record_startup_operation',
    'record_startup_operations',
    'startup_operation_collection_active',
    'startup_operation_counts',
    'startup_operation_scope',
]
