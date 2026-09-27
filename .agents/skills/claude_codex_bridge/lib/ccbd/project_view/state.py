from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ccbd.models import SCHEMA_VERSION
from storage.json_store import JsonStore
from storage.paths import PathLayout

_PROJECT_VIEW_STATE_RECORD_TYPE = 'ccbd_project_view_state'


@dataclass(frozen=True)
class ProjectViewState:
    project_id: str
    dismissed_comms: frozenset[str]

    def to_record(self) -> dict[str, Any]:
        return {
            'schema_version': SCHEMA_VERSION,
            'record_type': _PROJECT_VIEW_STATE_RECORD_TYPE,
            'project_id': self.project_id,
            'dismissed_comms': sorted(self.dismissed_comms),
        }

    @classmethod
    def from_record(cls, payload: dict[str, Any]) -> ProjectViewState:
        if payload.get('schema_version') != SCHEMA_VERSION:
            raise ValueError(f'schema_version must be {SCHEMA_VERSION}')
        if payload.get('record_type') != _PROJECT_VIEW_STATE_RECORD_TYPE:
            raise ValueError(f"record_type must be '{_PROJECT_VIEW_STATE_RECORD_TYPE}'")
        dismissed = {
            text
            for item in payload.get('dismissed_comms', ())
            if (text := str(item or '').strip())
        }
        return cls(
            project_id=str(payload.get('project_id') or ''),
            dismissed_comms=frozenset(dismissed),
        )


class ProjectViewStateStore:
    def __init__(self, layout: PathLayout, *, project_id: str, store: JsonStore | None = None) -> None:
        self._layout = layout
        self._project_id = project_id
        self._store = store or JsonStore()

    def load(self) -> ProjectViewState:
        path = self._layout.ccbd_project_view_state_path
        if not path.exists():
            return ProjectViewState(project_id=self._project_id, dismissed_comms=frozenset())
        try:
            state = self._store.load(path, loader=ProjectViewState.from_record)
        except Exception:
            return ProjectViewState(project_id=self._project_id, dismissed_comms=frozenset())
        if state.project_id != self._project_id:
            return ProjectViewState(project_id=self._project_id, dismissed_comms=frozenset())
        return state

    def dismiss_comms(self, comms_id: str) -> ProjectViewState:
        item_id = str(comms_id or '').strip()
        if not item_id:
            raise ValueError('comms_id cannot be empty')
        current = self.load()
        next_state = ProjectViewState(
            project_id=self._project_id,
            dismissed_comms=frozenset((*current.dismissed_comms, item_id)),
        )
        self._save(next_state)
        return next_state

    def _save(self, state: ProjectViewState) -> None:
        self._store.save(
            self._layout.ccbd_project_view_state_path,
            state,
            serializer=lambda value: value.to_record(),
        )


__all__ = ['ProjectViewState', 'ProjectViewStateStore']
