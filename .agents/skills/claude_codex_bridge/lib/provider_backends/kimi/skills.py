from __future__ import annotations

import fnmatch
import json
import os
from collections.abc import Mapping
from pathlib import Path

from provider_core.inherited_skills import materialize_required_control_skills
from provider_core.projected_assets import remove_projected_path, route_projected_tree
from rolepacks.projection import project_role_skills_to_home


_KIMI_SKILL_OVERLAY_LABEL_PREFIX = 'kimi-skill-overlay:'


def kimi_skill_dirs_for_state_dir(state_dir: Path) -> tuple[Path, Path, Path]:
    root = Path(state_dir)
    return root / 'inherited-skills', root / 'role-skills', root / 'overlay-skills'


def kimi_skill_dirs_for_launch(
    *,
    project_root: Path | None,
    workspace_path: Path | None,
    state_dir: Path,
    env: Mapping[str, object] | None = None,
) -> tuple[Path, ...]:
    return _dedupe_paths(
        (
            *kimi_default_skill_dirs(project_root=project_root, workspace_path=workspace_path, env=env),
            *kimi_skill_dirs_for_state_dir(state_dir),
        )
    )


def kimi_default_skill_dirs(
    *,
    project_root: Path | None,
    workspace_path: Path | None,
    env: Mapping[str, object] | None = None,
) -> tuple[Path, ...]:
    source = dict(os.environ)
    if env:
        source.update({str(key): str(value) for key, value in env.items() if value is not None})
    home = _env_path(source, 'HOME') or Path.home()
    paths: list[Path] = []
    for root in _project_skill_roots(project_root=project_root, workspace_path=workspace_path):
        paths.extend(
            (
                root / '.kimi' / 'skills',
                root / '.claude' / 'skills',
                root / '.codex' / 'skills',
                root / '.agents' / 'skills',
            )
        )
    paths.extend(
        (
            home / '.kimi' / 'skills',
            home / '.claude' / 'skills',
            home / '.codex' / 'skills',
            home / '.config' / 'agents' / 'skills',
            home / '.agents' / 'skills',
        )
    )
    kimi_code_home = _env_path(source, 'KIMI_CODE_HOME')
    if kimi_code_home is not None:
        paths.append(kimi_code_home / 'skills')
    else:
        paths.append(home / '.kimi-code' / 'skills')
    for root in _project_skill_roots(project_root=project_root, workspace_path=workspace_path):
        paths.append(root / '.kimi-code' / 'skills')
    return _dedupe_paths(paths)


def materialize_kimi_skills(
    *,
    project_root: Path | None,
    agent_name: str,
    state_dir: Path,
    profile,
) -> tuple[Path, ...]:
    inherited_dir, role_dir, overlay_dir = kimi_skill_dirs_for_state_dir(state_dir)
    active_dirs: list[Path] = []
    if materialize_required_control_skills(
        provider='kimi',
        target_dir=inherited_dir,
    ):
        active_dirs.append(inherited_dir)
    project_role_skills_to_home(
        project_root=project_root,
        agent_name=agent_name,
        provider='kimi',
        target_skills_dir=role_dir,
    )
    if role_dir.is_dir():
        active_dirs.append(role_dir)
    if _materialize_skill_overlays(overlay_dir, profile=profile, project_root=project_root):
        active_dirs.append(overlay_dir)
    return tuple(active_dirs)


def _materialize_skill_overlays(target: Path, *, profile, project_root: Path | None) -> bool:
    overlays = getattr(profile, 'skill_overlays', {}) if profile is not None else {}
    if not isinstance(overlays, dict):
        overlays = {}
    target = Path(target).expanduser()
    desired_labels: set[str] = set()
    materialized = False
    for overlay_name, overlay in sorted(overlays.items(), key=lambda item: str(item[0])):
        source = _resolve_skill_overlay_source(getattr(overlay, 'source', ''), project_root=project_root)
        include = _profile_skill_patterns(overlay, 'include', default=('*',))
        exclude = _profile_skill_patterns(overlay, 'exclude')
        if not source.is_dir():
            continue
        target.mkdir(parents=True, exist_ok=True)
        for entry in sorted(source.iterdir(), key=lambda item: item.name):
            if not entry.is_dir():
                continue
            skill_name = entry.name
            if not _matches_skill_patterns(skill_name, include=include, exclude=exclude):
                continue
            label = f'{_KIMI_SKILL_OVERLAY_LABEL_PREFIX}{overlay_name}:{skill_name}'
            desired_labels.add(label)
            if route_projected_tree(
                entry,
                target / skill_name,
                enabled=True,
                label=label,
                allow_unmarked_replace=False,
            ):
                materialized = True
    _remove_stale_skill_projection_markers(
        target,
        label_prefix=_KIMI_SKILL_OVERLAY_LABEL_PREFIX,
        desired_labels=desired_labels,
    )
    if not materialized:
        _remove_empty_dir(target)
    return materialized


def _resolve_skill_overlay_source(source: object, *, project_root: Path | None) -> Path:
    path = Path(str(source or '')).expanduser()
    if not path.is_absolute() and project_root is not None:
        path = Path(project_root).expanduser() / path
    return path


def _profile_skill_patterns(profile, attribute: str, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = getattr(profile, attribute, default) if profile is not None else default
    if isinstance(raw, str):
        candidates = (raw,)
    else:
        try:
            candidates = tuple(raw)
        except TypeError:
            candidates = ()
    patterns = tuple(str(item or '').strip() for item in candidates if str(item or '').strip())
    return patterns or default


def _matches_skill_patterns(skill_name: str, *, include: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    if include and not any(fnmatch.fnmatchcase(skill_name, pattern) for pattern in include):
        return False
    if exclude and any(fnmatch.fnmatchcase(skill_name, pattern) for pattern in exclude):
        return False
    return True


def _remove_stale_skill_projection_markers(target: Path, *, label_prefix: str, desired_labels: set[str]) -> None:
    target = Path(target).expanduser()
    if not target.is_dir() or target.is_symlink():
        return
    for marker in sorted(target.glob('*.ccb-projection.json')):
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
        remove_projected_path(target / skill_name, label=label, marker_path=marker)


def _remove_empty_dir(path: Path) -> None:
    try:
        Path(path).rmdir()
    except OSError:
        pass


def _project_skill_roots(*, project_root: Path | None, workspace_path: Path | None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if workspace_path is not None:
        candidates.append(_nearest_git_root(Path(workspace_path)))
    if project_root is not None:
        candidates.append(Path(project_root))
    return _dedupe_paths(candidates)


def _nearest_git_root(path: Path) -> Path:
    start = path if path.is_dir() else path.parent
    current = start.resolve() if start.exists() else start
    while True:
        if (current / '.git').exists():
            return current
        parent = current.parent
        if parent == current:
            return start
        current = parent


def _env_path(env: Mapping[str, object], key: str) -> Path | None:
    value = str(env.get(key) or '').strip()
    if not value:
        return None
    return Path(value).expanduser()


def _dedupe_paths(paths) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        key = str(path)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


__all__ = [
    'kimi_default_skill_dirs',
    'kimi_skill_dirs_for_launch',
    'kimi_skill_dirs_for_state_dir',
    'materialize_kimi_skills',
]
