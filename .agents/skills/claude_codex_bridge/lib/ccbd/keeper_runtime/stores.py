from __future__ import annotations

import os
from dataclasses import replace

from ccbd.system import parse_utc_timestamp
from storage.json_store import JsonStore
from storage.paths import PathLayout

from .records import KeeperState, ShutdownIntent


DEFAULT_KEEPER_STATE_WRITE_INTERVAL_S = 5.0


class KeeperStateStore:
    def __init__(self, layout: PathLayout, store: JsonStore | None = None) -> None:
        self._layout = layout
        self._store = store or JsonStore()
        self._last_saved: KeeperState | None = None

    def load(self) -> KeeperState | None:
        path = self._layout.ccbd_keeper_path
        if not path.exists():
            return None
        state = self._store.load(path, loader=KeeperState.from_record)
        self._last_saved = state
        return state

    def save(self, state: KeeperState) -> None:
        current = self._last_saved
        if current is None and self._layout.ccbd_keeper_path.exists():
            current = self.load()
        if current is not None and _keeper_state_write_can_skip(current, state):
            return
        self._store.save(self._layout.ccbd_keeper_path, state, serializer=lambda value: value.to_record())
        self._last_saved = state


class ShutdownIntentStore:
    def __init__(self, layout: PathLayout, store: JsonStore | None = None) -> None:
        self._layout = layout
        self._store = store or JsonStore()

    def load(self) -> ShutdownIntent | None:
        path = self._layout.ccbd_shutdown_intent_path
        if not path.exists():
            return None
        return self._store.load(path, loader=ShutdownIntent.from_record)

    def save(self, intent: ShutdownIntent) -> None:
        self._store.save(self._layout.ccbd_shutdown_intent_path, intent, serializer=lambda value: value.to_record())

    def clear(self) -> None:
        try:
            self._layout.ccbd_shutdown_intent_path.unlink()
        except FileNotFoundError:
            pass


def _keeper_state_write_interval_s() -> float:
    raw = os.environ.get('CCB_KEEPER_STATE_WRITE_INTERVAL_S')
    if raw is None:
        return DEFAULT_KEEPER_STATE_WRITE_INTERVAL_S
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_KEEPER_STATE_WRITE_INTERVAL_S


def _keeper_state_write_can_skip(current: KeeperState, next_state: KeeperState) -> bool:
    if current == next_state:
        return True
    if current != replace(next_state, last_check_at=current.last_check_at):
        return False
    interval_s = _keeper_state_write_interval_s()
    if interval_s <= 0:
        return False
    try:
        elapsed = (parse_utc_timestamp(next_state.last_check_at) - parse_utc_timestamp(current.last_check_at)).total_seconds()
    except Exception:
        return False
    return 0 <= elapsed < interval_s


__all__ = ['KeeperStateStore', 'ShutdownIntentStore']
