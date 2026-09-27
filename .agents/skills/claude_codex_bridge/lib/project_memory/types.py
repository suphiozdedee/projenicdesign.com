from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectMemoryEnsureResult:
    path: Path
    seed_path: Path
    created: bool
    seed_written: bool
    sha256: str
    warning: str = ''


@dataclass(frozen=True)
class ProjectMemorySource:
    kind: str
    title: str
    path: Path
    content: str
    exists: bool
    warning: str = ''
    filtered: bool = False
    filter_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectMemorySourceRef:
    kind: str
    path: Path
    exists: bool
    sha256: str
    warning: str = ''
    filtered: bool = False
    filter_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectMemoryMaterialization:
    path: Path
    written: bool
    unchanged: bool
    sha256: str
    sources: tuple[ProjectMemorySourceRef, ...]
    warnings: tuple[str, ...] = ()


__all__ = [
    'ProjectMemoryEnsureResult',
    'ProjectMemoryMaterialization',
    'ProjectMemorySource',
    'ProjectMemorySourceRef',
]
