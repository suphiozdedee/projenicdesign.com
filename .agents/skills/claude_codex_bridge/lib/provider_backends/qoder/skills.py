from __future__ import annotations

from pathlib import Path

from provider_core.inherited_skills import (
    inherits_skills,
    materialize_required_control_skills,
    required_control_skill_names,
    route_inherited_skill_entries,
)
from provider_core.source_home import current_provider_source_home
from rolepacks.projection import project_role_skills_to_home


_SOURCE_CONFIG_ROOTS = {
    'qoder': Path('.qoder'),
    'qoderclicn': Path('.qoder-cn'),
}


def materialize_qoder_skills(
    *,
    provider: str,
    config_dir: Path,
    profile=None,
    source_home: Path | None = None,
    project_root: Path | None = None,
    agent_name: str | None = None,
) -> tuple[str, ...]:
    """Project optional and required skills into Qoder's effective config root."""
    normalized = str(provider or '').strip().lower()
    source_relative = _SOURCE_CONFIG_ROOTS.get(normalized)
    if source_relative is None:
        raise ValueError(f'unsupported Qoder provider: {provider}')

    source_root = (
        Path(source_home).expanduser()
        if source_home is not None
        else current_provider_source_home()
    ) / source_relative
    target_root = Path(config_dir).expanduser()

    # An explicit --config-dir may point directly at the user's source config.
    # It is not a CCB-owned projection target, so never inject markers or
    # replace reserved names in place.
    if _same_path(source_root, target_root):
        return ()

    target_skills = target_root / 'skills'
    optional = route_inherited_skill_entries(
        source_root / 'skills',
        target_skills,
        enabled=inherits_skills(profile),
        label=f'{normalized}-inherited-skills',
        exclude=required_control_skill_names(normalized),
    )
    project_role_skills_to_home(
        project_root=project_root,
        agent_name=agent_name,
        provider=normalized,
        target_skills_dir=target_skills,
    )
    required = materialize_required_control_skills(
        provider=normalized,
        target_dir=target_skills,
    )
    return (*optional, *required)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except Exception:
        return left.absolute() == right.absolute()


__all__ = ['materialize_qoder_skills']
