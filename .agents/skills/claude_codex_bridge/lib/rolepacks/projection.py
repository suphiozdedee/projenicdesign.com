from __future__ import annotations

import json
from pathlib import Path

from provider_core.projected_assets import remove_projected_path, route_projected_tree

from .runtime_lookup import project_role_skill_sources


def project_role_skills_to_home(
    *,
    project_root: Path | None,
    agent_name: str | None,
    provider: str,
    target_skills_dir: Path,
) -> tuple[dict[str, object], ...]:
    if project_root is None or agent_name is None:
        return ()
    results: list[dict[str, object]] = []
    provider_name = str(provider or '').strip().lower()
    desired_labels: set[str] = set()
    for skill_name, source, role_id in project_role_skill_sources(project_root, agent_name, provider):
        target = Path(target_skills_dir) / skill_name
        label = f'{provider}-role-skill:{role_id}:{skill_name}'
        desired_labels.add(label)
        ok = route_projected_tree(
            source,
            target,
            enabled=True,
            label=label,
            allow_unmarked_replace=False,
        )
        results.append(
            {
                'role_id': role_id,
                'provider': provider,
                'skill': skill_name,
                'source': str(source),
                'target': str(target),
                'status': 'ok' if ok else 'failed',
            }
        )
    if provider_name:
        _remove_stale_role_skill_projections(
            Path(target_skills_dir),
            label_prefix=f'{provider_name}-role-skill:',
            desired_labels=desired_labels,
        )
    return tuple(results)


def _remove_stale_role_skill_projections(
    target_skills_dir: Path,
    *,
    label_prefix: str,
    desired_labels: set[str],
) -> None:
    target_skills_dir = Path(target_skills_dir).expanduser()
    if not target_skills_dir.is_dir() or target_skills_dir.is_symlink():
        return
    for marker in sorted(target_skills_dir.glob('*.ccb-projection.json')):
        try:
            payload = json.loads(marker.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get('record_type') != 'ccb_projected_asset':
            continue
        label = str(payload.get('label') or '')
        if not label.startswith(label_prefix) or label in desired_labels:
            continue
        skill_name = marker.name.removesuffix('.ccb-projection.json')
        remove_projected_path(
            target_skills_dir / skill_name,
            label=label,
            marker_path=marker,
        )


__all__ = ['project_role_skills_to_home']
