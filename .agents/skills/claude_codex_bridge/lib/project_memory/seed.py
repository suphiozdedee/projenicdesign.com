from __future__ import annotations

from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path

from storage.atomic import atomic_write_json, atomic_write_text
from storage.paths import PathLayout

from .hashing import sha256_text
from .template import DEFAULT_PROJECT_MEMORY, TEMPLATE_VERSION
from .types import ProjectMemoryEnsureResult

_SEED_SCHEMA_VERSION = 1
_SEED_RECORD_TYPE = 'ccb_project_memory_seed'
_LEGACY_GENERATED_TEMPLATES: tuple[tuple[int, str], ...] = (
    (
        4,
        """# CCB Project Memory

This project uses CCB for visible multi-agent collaboration.

## Collaboration

- You are one agent in a CCB-managed project team.
- Use CCB `ask` for project-level collaboration with configured agents.
- Delegate with the goal, scope/files, assumptions, expected output, and verification needs.
- Reply concisely with findings, changes, verification, blockers, and risks when relevant.

## Ask Communication

Preferred form:

```text
/ask <agent> <message>
```

Shell fallback:

```bash
command ask "$TARGET" <<'EOF'
$MESSAGE
EOF
```

- Submit once, then stop. Do not wait, poll, or run `pend`/`watch`/`ping` unless diagnostics were requested.
- During an active CCB ask task, use `ask --chain` when a child result is needed to finish the current task; use `ask --silence` only for independent no-result-needed work.
- Plain nested `ask` from an active task is rejected by CCB.
""",
    ),
)
_LEGACY_GENERATED_TEMPLATE_HASHES = {
    sha256_text(text): version for version, text in _LEGACY_GENERATED_TEMPLATES
}


def project_memory_path(project_root_or_layout) -> Path:
    layout = _layout(project_root_or_layout)
    return layout.project_memory_path


def seed_metadata_path(project_root_or_layout) -> Path:
    layout = _layout(project_root_or_layout)
    return layout.memory_seed_path


def ensure_project_memory(project_root_or_layout, *, now: str | None = None) -> ProjectMemoryEnsureResult:
    layout = _layout(project_root_or_layout)
    path = layout.project_memory_path
    seed_path = layout.memory_seed_path
    template = DEFAULT_PROJECT_MEMORY
    template_hash = sha256_text(template)
    created = False
    warning = ''

    try:
        created = _atomic_create_text(path, template)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            created = False
        else:
            return ProjectMemoryEnsureResult(
                path=path,
                seed_path=seed_path,
                created=False,
                seed_written=False,
                sha256='',
                warning=f'failed_to_create_project_memory: {exc}',
            )

    seed_written = False
    if created:
        seed_written, seed_warning = _write_seed_metadata(
            seed_path,
            memory_path=path,
            memory_hash=template_hash,
            now=now,
        )
        warning = seed_warning
        return ProjectMemoryEnsureResult(
            path=path,
            seed_path=seed_path,
            created=True,
            seed_written=seed_written,
            sha256=template_hash,
            warning=warning,
        )

    current_hash = _file_sha256(path)
    seed_written = False
    seed_record = read_seed_metadata(layout)
    if _should_upgrade_project_memory(seed_record, current_hash=current_hash, template_hash=template_hash):
        try:
            atomic_write_text(path, template)
        except OSError as exc:
            return ProjectMemoryEnsureResult(
                path=path,
                seed_path=seed_path,
                created=False,
                seed_written=False,
                sha256=current_hash,
                warning=f'failed_to_upgrade_project_memory_seed: {exc}',
            )
        seed_written, seed_warning = _write_seed_metadata(
            seed_path,
            memory_path=path,
            memory_hash=template_hash,
            now=now,
        )
        return ProjectMemoryEnsureResult(
            path=path,
            seed_path=seed_path,
            created=False,
            seed_written=seed_written,
            sha256=template_hash,
            warning=seed_warning,
        )
    if current_hash == template_hash and not seed_path.is_file():
        seed_written, seed_warning = _write_seed_metadata(
            seed_path,
            memory_path=path,
            memory_hash=template_hash,
            now=now,
        )
        warning = seed_warning
    return ProjectMemoryEnsureResult(
        path=path,
        seed_path=seed_path,
        created=False,
        seed_written=seed_written,
        sha256=current_hash,
        warning=warning,
    )


def _atomic_create_text(path: Path, text: str) -> bool:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(target), flags, 0o644)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
    except Exception:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    return True


def _write_seed_metadata(seed_path: Path, *, memory_path: Path, memory_hash: str, now: str | None) -> tuple[bool, str]:
    timestamp = now or datetime.now(timezone.utc).isoformat()
    payload = {
        'schema_version': _SEED_SCHEMA_VERSION,
        'record_type': _SEED_RECORD_TYPE,
        'template_version': TEMPLATE_VERSION,
        'memory_path': str(memory_path),
        'sha256': memory_hash,
        'created_at': timestamp,
    }
    try:
        atomic_write_json(seed_path, payload)
    except OSError as exc:
        return False, f'failed_to_write_project_memory_seed: {exc}'
    return True, ''


def _file_sha256(path: Path) -> str:
    try:
        return sha256_text(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return ''


def _should_upgrade_project_memory(
    seed_record: dict[str, object],
    *,
    current_hash: str,
    template_hash: str,
) -> bool:
    if not current_hash or current_hash == template_hash:
        return False
    if _legacy_generated_template_version(current_hash) is not None:
        return True
    if not seed_record or seed_record.get('record_type') != _SEED_RECORD_TYPE:
        return False
    if str(seed_record.get('sha256') or '') != current_hash:
        return False
    try:
        seed_version = int(seed_record.get('template_version') or 0)
    except (TypeError, ValueError):
        seed_version = 0
    return seed_version < TEMPLATE_VERSION


def _legacy_generated_template_version(current_hash: str) -> int | None:
    return _LEGACY_GENERATED_TEMPLATE_HASHES.get(current_hash)


def read_seed_metadata(project_root_or_layout) -> dict[str, object]:
    path = seed_metadata_path(project_root_or_layout)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _layout(project_root_or_layout) -> PathLayout:
    if isinstance(project_root_or_layout, PathLayout):
        return project_root_or_layout
    return PathLayout(Path(project_root_or_layout))


__all__ = [
    'ensure_project_memory',
    'project_memory_path',
    'read_seed_metadata',
    'seed_metadata_path',
]
