from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from storage.atomic import atomic_write_json


PROJECT_ACTIVITY_FILENAME = 'project_activity.json'
PROJECT_ACTIVITY_RECORD_TYPE = 'ccb_mobile_project_activity'


class MobileGatewayProjectActivityStore:
    def __init__(self, mobile_dir: Path) -> None:
        self.path = Path(mobile_dir).expanduser() / PROJECT_ACTIVITY_FILENAME

    def record_opened(self, *, project_id: str, opened_at: str) -> None:
        project_id = str(project_id or '').strip()
        opened_at = str(opened_at or '').strip()
        if not project_id or not opened_at:
            return
        payload = self._read()
        projects = _projects(payload)
        prior = dict(projects.get(project_id) or {})
        prior['project_id'] = project_id
        prior['last_opened_at'] = opened_at
        projects[project_id] = prior
        self._write_projects(projects)

    def record_activity(self, *, project_id: str, activity_at: str) -> None:
        project_id = str(project_id or '').strip()
        activity_at = str(activity_at or '').strip()
        if not project_id or not activity_at:
            return
        payload = self._read()
        projects = _projects(payload)
        prior = dict(projects.get(project_id) or {})
        prior['project_id'] = project_id
        current = str(prior.get('last_activity_at') or '').strip()
        prior['last_activity_at'] = _latest_timestamp(current, activity_at) or activity_at
        projects[project_id] = prior
        self._write_projects(projects)

    def record_summary(
        self,
        *,
        project_id: str,
        summary: dict[str, object],
        checked_at: str,
    ) -> None:
        project_id = str(project_id or '').strip()
        checked_at = str(checked_at or '').strip()
        if not project_id or not checked_at:
            return
        payload = self._read()
        projects = _projects(payload)
        prior = dict(projects.get(project_id) or {})
        prior['project_id'] = project_id
        prior['summary_checked_at'] = checked_at

        last_activity_at = str(summary.get('last_activity_at') or '').strip()
        if last_activity_at:
            current = str(prior.get('last_activity_at') or '').strip()
            prior['last_activity_at'] = (
                _latest_timestamp(current, last_activity_at) or last_activity_at
            )

        has_working_agents = bool(summary.get('has_working_agents'))
        prior['has_working_agents'] = has_working_agents
        working_agent_count = _optional_int(summary.get('working_agent_count')) or 0
        prior['working_agent_count'] = working_agent_count if has_working_agents else 0

        projects[project_id] = prior
        self._write_projects(projects)

    def project(self, project_id: str) -> dict[str, object]:
        return dict(_projects(self._read()).get(str(project_id or '').strip()) or {})

    def _read(self) -> dict[str, object]:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        if str(data.get('record_type') or '').strip() != PROJECT_ACTIVITY_RECORD_TYPE:
            return {}
        return data

    def _write_projects(self, projects: dict[str, dict[str, object]]) -> None:
        atomic_write_json(
            self.path,
            {
                'schema_version': 1,
                'record_type': PROJECT_ACTIVITY_RECORD_TYPE,
                'projects': [projects[key] for key in sorted(projects)],
            },
        )


def _projects(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    records = payload.get('projects')
    projects: dict[str, dict[str, object]] = {}
    for item in records if isinstance(records, list) else []:
        if not isinstance(item, dict):
            continue
        project_id = str(item.get('project_id') or '').strip()
        if project_id:
            projects[project_id] = dict(item)
    return projects


def _optional_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _latest_timestamp(left: str, right: str) -> str | None:
    left_parsed = _parse_timestamp(left)
    right_parsed = _parse_timestamp(right)
    if left_parsed is None:
        return right if right_parsed is not None else left or right or None
    if right_parsed is None:
        return left
    return right if right_parsed > left_parsed else left


def _parse_timestamp(value: str) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
