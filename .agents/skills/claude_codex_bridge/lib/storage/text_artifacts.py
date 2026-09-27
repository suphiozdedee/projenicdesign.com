from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from .atomic import atomic_write_text

TEXT_ARTIFACT_SPILL_BYTES = 4 * 1024
TEXT_ARTIFACT_PREVIEW_CHARS = 1200
TEXT_ARTIFACT_TTL_S = 24 * 60 * 60


def utf8_size(text: str) -> int:
    return len(str(text or '').encode('utf-8'))


def should_spill_text(text: str, *, threshold_bytes: int = TEXT_ARTIFACT_SPILL_BYTES) -> bool:
    return utf8_size(text) > int(threshold_bytes)


def maybe_spill_text(
    layout,
    *,
    text: str,
    kind: str,
    owner_id: str,
    prefix: str,
    threshold_bytes: int = TEXT_ARTIFACT_SPILL_BYTES,
    ttl_seconds: int = TEXT_ARTIFACT_TTL_S,
    now: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    body = str(text or '')
    if not should_spill_text(body, threshold_bytes=threshold_bytes):
        return body, None
    artifact = write_text_artifact(
        layout,
        text=body,
        kind=kind,
        owner_id=owner_id,
        ttl_seconds=ttl_seconds,
        now=now,
    )
    return artifact_stub(prefix=prefix, artifact=artifact), artifact


def write_text_artifact(
    layout,
    *,
    text: str,
    kind: str,
    owner_id: str,
    ttl_seconds: int = TEXT_ARTIFACT_TTL_S,
    now: str | None = None,
) -> dict[str, Any]:
    body = str(text or '')
    data = body.encode('utf-8')
    digest = hashlib.sha256(data).hexdigest()
    timestamp = now or _utc_now()
    ensure_runtime_root = getattr(layout, 'ensure_runtime_state_root', None)
    if callable(ensure_runtime_root):
        ensure_runtime_root(created_at=timestamp)
    artifact_id = f'art_{uuid4().hex[:16]}'
    safe_kind = _safe_segment(kind, fallback='text')
    safe_owner = _safe_segment(owner_id, fallback='unknown')
    directory = Path(layout.ccbd_text_artifacts_dir) / safe_kind
    path = directory / f'{safe_owner}-{artifact_id}.txt'
    atomic_write_text(path, body)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {
        'schema_version': 1,
        'kind': safe_kind,
        'artifact_id': artifact_id,
        'path': str(path),
        'bytes': len(data),
        'sha256': digest,
        'encoding': 'utf-8',
        'preview': preview_text(body),
        'created_at': timestamp,
        'expires_at': _expires_at(timestamp, ttl_seconds),
    }


def artifact_stub(
    *,
    prefix: str,
    artifact: dict[str, Any],
    include_preview: bool = True,
    instruction: str | None = None,
) -> str:
    preview = str(artifact.get('preview') or '').rstrip()
    show_preview = bool(include_preview and preview)
    lines = [
        prefix.rstrip() or 'CCB large text artifact.',
        f"Full text: {artifact.get('path')}",
        f"Bytes: {artifact.get('bytes')}",
        f"SHA256: {artifact.get('sha256')}",
    ]
    if show_preview:
        lines.extend(['', 'Preview:', preview])
    if instruction is None:
        if show_preview:
            instruction = 'Instruction: read the full text file above before acting when the preview is insufficient.'
        else:
            instruction = 'Instruction: read the full text file above before acting.'
    lines.extend(['', instruction])
    return '\n'.join(lines).rstrip()


def preview_text(text: str, *, max_chars: int = TEXT_ARTIFACT_PREVIEW_CHARS) -> str:
    body = str(text or '').strip()
    if len(body) <= max_chars:
        return body
    return f'{body[:max_chars].rstrip()}\n...[truncated]'


def validate_text_artifact_ref(layout, artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    if not artifact:
        return None
    ref = dict(artifact)
    path = _validated_artifact_path(layout, ref.get('path'))
    data = path.read_bytes()
    expected_size = ref.get('bytes')
    if expected_size is not None and int(expected_size) != len(data):
        raise ValueError('text artifact byte size mismatch')
    expected_sha = str(ref.get('sha256') or '').strip()
    actual_sha = hashlib.sha256(data).hexdigest()
    if expected_sha and expected_sha != actual_sha:
        raise ValueError('text artifact sha256 mismatch')
    ref['path'] = str(path)
    ref['bytes'] = len(data)
    ref['sha256'] = actual_sha
    ref.setdefault('encoding', 'utf-8')
    ref.setdefault('preview', preview_text(data.decode('utf-8', errors='replace')))
    return ref


def read_text_artifact(layout, artifact: dict[str, Any]) -> str:
    ref = validate_text_artifact_ref(layout, artifact)
    if not ref:
        return ''
    return Path(ref['path']).read_text(encoding=str(ref.get('encoding') or 'utf-8'))


def sweep_expired_text_artifacts(layout, *, now: str | None = None) -> tuple[Path, ...]:
    root = Path(layout.ccbd_text_artifacts_dir)
    if not root.exists():
        return ()
    current = _parse_utc(now or _utc_now())
    removed: list[Path] = []
    for path in root.rglob('*.txt'):
        try:
            age_s = max(0.0, current.timestamp() - path.stat().st_mtime)
        except OSError:
            continue
        if age_s < TEXT_ARTIFACT_TTL_S:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        else:
            removed.append(path)
    return tuple(removed)


def _validated_artifact_path(layout, value: object) -> Path:
    root = Path(layout.ccbd_text_artifacts_dir).resolve()
    path = Path(str(value or '')).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError('text artifact path does not exist') from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError('text artifact path escapes CCB artifact directory') from exc
    return resolved


def _safe_segment(value: object, *, fallback: str) -> str:
    text = ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '-' for ch in str(value or '').strip())
    text = text.strip('-_').lower()
    return text or fallback


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _expires_at(created_at: str, ttl_seconds: int) -> str:
    return (
        _parse_utc(created_at) + timedelta(seconds=max(0, int(ttl_seconds)))
    ).isoformat().replace('+00:00', 'Z')


def _parse_utc(value: str) -> datetime:
    text = str(value or '').strip()
    if text.endswith('Z'):
        text = f'{text[:-1]}+00:00'
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    'TEXT_ARTIFACT_PREVIEW_CHARS',
    'TEXT_ARTIFACT_SPILL_BYTES',
    'artifact_stub',
    'maybe_spill_text',
    'preview_text',
    'read_text_artifact',
    'should_spill_text',
    'sweep_expired_text_artifacts',
    'utf8_size',
    'validate_text_artifact_ref',
    'write_text_artifact',
]
