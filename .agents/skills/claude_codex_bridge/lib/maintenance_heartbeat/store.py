from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from storage.json_store import JsonStore
from storage.jsonl_store import JsonlStore
from storage.paths import PathLayout

from .models import (
    MaintenanceHeartbeatActivation,
    MaintenanceHeartbeatRunner,
    MaintenanceHeartbeatSchedule,
    MaintenanceHeartbeatStatus,
)

T = TypeVar('T')


@dataclass(frozen=True)
class MaintenanceHeartbeatReadResult(Generic[T]):
    state: str
    path: str
    value: T | None = None
    error: str | None = None

    def to_record(self) -> dict[str, Any]:
        record = {
            'state': self.state,
            'path': self.path,
            'error': self.error,
        }
        if self.value is not None:
            record['record'] = self.value.to_record()
        return record


class MaintenanceHeartbeatStore:
    def __init__(
        self,
        layout: PathLayout,
        *,
        project_id: str,
        store: JsonStore | None = None,
        jsonl_store: JsonlStore | None = None,
    ) -> None:
        self._layout = layout
        self._project_id = str(project_id or '').strip()
        if not self._project_id:
            raise ValueError('project_id cannot be empty')
        self._store = store or JsonStore()
        self._jsonl_store = jsonl_store or JsonlStore()

    def load_schedule(self) -> MaintenanceHeartbeatReadResult[MaintenanceHeartbeatSchedule]:
        return self._load(
            self._layout.ccbd_maintenance_heartbeat_schedule_path,
            loader=MaintenanceHeartbeatSchedule.from_record,
            expected_project_id=self._project_id,
        )

    def save_schedule(self, schedule: MaintenanceHeartbeatSchedule) -> None:
        self._ensure_project(schedule.project_id)
        self._store.save(
            self._layout.ccbd_maintenance_heartbeat_schedule_path,
            schedule,
            serializer=lambda value: value.to_record(),
        )

    def load_status(self) -> MaintenanceHeartbeatReadResult[MaintenanceHeartbeatStatus]:
        return self._load(
            self._layout.ccbd_maintenance_heartbeat_status_path,
            loader=MaintenanceHeartbeatStatus.from_record,
            expected_project_id=self._project_id,
        )

    def save_status(self, status: MaintenanceHeartbeatStatus) -> None:
        self._ensure_project(status.project_id)
        self._store.save(
            self._layout.ccbd_maintenance_heartbeat_status_path,
            status,
            serializer=lambda value: value.to_record(),
        )

    def load_runner(self) -> MaintenanceHeartbeatReadResult[MaintenanceHeartbeatRunner]:
        return self._load(
            self._layout.ccbd_maintenance_heartbeat_runner_path,
            loader=MaintenanceHeartbeatRunner.from_record,
            expected_project_id=self._project_id,
        )

    def save_runner(self, runner: MaintenanceHeartbeatRunner) -> None:
        self._ensure_project(runner.project_id)
        self._store.save(
            self._layout.ccbd_maintenance_heartbeat_runner_path,
            runner,
            serializer=lambda value: value.to_record(),
        )

    def append_activation(self, activation: MaintenanceHeartbeatActivation) -> None:
        self._ensure_project(activation.project_id)
        self._jsonl_store.append(
            self._layout.ccbd_maintenance_heartbeat_activations_path,
            activation,
            serializer=lambda value: value.to_record(),
        )

    def load_activation_tail(self, limit: int = 50) -> tuple[MaintenanceHeartbeatActivation, ...]:
        rows = self._jsonl_store.read_tail(
            self._layout.ccbd_maintenance_heartbeat_activations_path,
            max(0, int(limit)),
            loader=MaintenanceHeartbeatActivation.from_record,
        )
        activations: list[MaintenanceHeartbeatActivation] = []
        for row in rows:
            if not isinstance(row, MaintenanceHeartbeatActivation):
                continue
            self._ensure_project(row.project_id)
            activations.append(row)
        return tuple(activations)

    def _load(self, path, *, loader, expected_project_id: str):
        if not path.exists():
            return MaintenanceHeartbeatReadResult(state='missing', path=str(path))
        try:
            value = self._store.load(path, loader=loader)
            if getattr(value, 'project_id', None) != expected_project_id:
                raise ValueError('project_id mismatch')
        except Exception as exc:
            return MaintenanceHeartbeatReadResult(
                state='corrupt',
                path=str(path),
                error=str(exc),
            )
        return MaintenanceHeartbeatReadResult(state='ok', path=str(path), value=value)

    def _ensure_project(self, project_id: str) -> None:
        if str(project_id or '').strip() != self._project_id:
            raise ValueError('project_id mismatch')


__all__ = ['MaintenanceHeartbeatReadResult', 'MaintenanceHeartbeatStore']
