from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable

from storage.atomic import atomic_write_text


def project_json_mapping_fields(
    source: Path,
    target: Path,
    *,
    fields: Iterable[str],
    enabled: bool = True,
    label: str = 'projected-settings',
    marker_path: Path | None = None,
) -> bool:
    """Project owned entries from selected JSON mapping fields."""
    source = Path(source).expanduser()
    target = Path(target).expanduser()
    marker = marker_path or Path(f'{target}.{label}.json')
    field_names = tuple(dict.fromkeys(str(field).strip() for field in fields if str(field).strip()))
    if not field_names:
        return False

    marker_exists = marker.exists()
    marker_payload = _read_json_object(marker) if marker_exists else {}
    marker_owned = _marker_matches(marker_payload, label=label)
    if marker_exists and not marker_owned:
        return False

    target_exists = target.exists()
    target_payload = _read_json_object(target) if target_exists else {}
    if target_exists and target_payload is None:
        return False
    target_payload = dict(target_payload or {})
    original_target_text = _read_text(target) if target_exists else None
    original_marker_text = _read_text(marker) if marker_exists else None
    previous = _managed_fields(marker_payload) if marker_owned else {}

    if enabled:
        source_payload = _read_json_object(source)
        if source_payload is None:
            return False
    else:
        if not marker_owned:
            return False
        source_payload = {}

    updated_payload = _clone_json_object(target_payload)
    managed: dict[str, dict[str, object]] = {}
    for field in field_names:
        current_value = updated_payload.get(field)
        if current_value is not None and not isinstance(current_value, dict):
            continue
        current = _clone_json_object(current_value if isinstance(current_value, dict) else {})
        prior = _clone_json_object(previous.get(field) if isinstance(previous.get(field), dict) else {})

        source_value = source_payload.get(field)
        if enabled and field in source_payload and not isinstance(source_value, dict):
            if prior:
                managed[field] = prior
            continue
        projected = _clone_json_object(source_value if isinstance(source_value, dict) else {})
        next_managed: dict[str, object] = {}

        for key, prior_value in prior.items():
            if key in projected:
                continue
            if key in current and current[key] == prior_value:
                current.pop(key, None)

        for key, projected_value in projected.items():
            if key in prior:
                if key in current and current[key] == prior[key]:
                    current[key] = _clone_json_value(projected_value)
                    next_managed[key] = _clone_json_value(projected_value)
                continue
            if key not in current:
                current[key] = _clone_json_value(projected_value)
                next_managed[key] = _clone_json_value(projected_value)

        if current:
            updated_payload[field] = current
        elif isinstance(current_value, dict):
            updated_payload.pop(field, None)
        if next_managed:
            managed[field] = next_managed

    target_changed = updated_payload != target_payload
    if (
        managed
        and marker_owned
        and str(marker_payload.get('source') or '') == str(source)
        and previous == managed
    ):
        next_marker = marker_payload
    else:
        next_marker = _marker_payload(label=label, source=source, managed=managed) if managed else None
    try:
        if target_changed:
            atomic_write_text(target, _json_text(updated_payload))
        if next_marker is None:
            marker.unlink(missing_ok=True)
        elif _read_text(marker) != _json_text(next_marker):
            atomic_write_text(marker, _json_text(next_marker))
    except Exception:
        if target_changed:
            _restore_file(target, original_target_text)
        _restore_file(marker, original_marker_text)
        return False
    return bool(managed)


def rebase_json_path_fields(
    paths: Iterable[Path],
    *,
    source_root: Path,
    target_root: Path,
    fields: Iterable[str],
) -> bool:
    """Atomically rebase selected absolute JSON paths into a local projection."""
    source_root = Path(source_root).expanduser()
    target_root = Path(target_root).expanduser()
    field_names = frozenset(str(field).strip() for field in fields if str(field).strip())
    if not field_names:
        return False

    pending: list[tuple[Path, str, object]] = []
    for path_value in paths:
        path = Path(path_value).expanduser()
        if not path.exists():
            continue
        original = _read_text(path)
        if original is None:
            return False
        try:
            payload = json.loads(original)
        except Exception:
            return False
        if not isinstance(payload, (dict, list)):
            return False
        rewritten, changed = _rewrite_json_path_fields(
            payload,
            source_root=source_root,
            target_root=target_root,
            fields=field_names,
        )
        if changed:
            pending.append((path, original, rewritten))

    written: list[tuple[Path, str]] = []
    try:
        for path, original, payload in pending:
            written.append((path, original))
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    except Exception:
        for path, original in reversed(written):
            _restore_file(path, original)
        return False
    return True


def _rewrite_json_path_fields(
    value: object,
    *,
    source_root: Path,
    target_root: Path,
    fields: frozenset[str],
) -> tuple[object, bool]:
    if isinstance(value, list):
        changed = False
        rewritten_items: list[object] = []
        for item in value:
            rewritten, item_changed = _rewrite_json_path_fields(
                item,
                source_root=source_root,
                target_root=target_root,
                fields=fields,
            )
            rewritten_items.append(rewritten)
            changed = changed or item_changed
        return rewritten_items, changed
    if not isinstance(value, dict):
        return value, False

    changed = False
    rewritten_payload: dict[str, object] = {}
    for key, item in value.items():
        if key in fields and isinstance(item, str):
            rebased = _rebase_projected_path(item, source_root=source_root, target_root=target_root)
            rewritten_payload[key] = rebased
            changed = changed or rebased != item
            continue
        rewritten, item_changed = _rewrite_json_path_fields(
            item,
            source_root=source_root,
            target_root=target_root,
            fields=fields,
        )
        rewritten_payload[key] = rewritten
        changed = changed or item_changed
    return rewritten_payload, changed


def _rebase_projected_path(value: str, *, source_root: Path, target_root: Path) -> str:
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return value
        relative = candidate.resolve(strict=False).relative_to(source_root.resolve(strict=False))
    except Exception:
        return value
    return str(target_root / relative)


def _marker_matches(payload: dict[str, object] | None, *, label: str) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get('record_type') == 'ccb_projected_settings'
        and str(payload.get('label') or '') == label
    )


def _managed_fields(payload: dict[str, object] | None) -> dict[str, dict[str, object]]:
    raw = payload.get('managed') if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        str(field): _clone_json_object(value)
        for field, value in raw.items()
        if isinstance(value, dict)
    }


def _marker_payload(*, label: str, source: Path, managed: dict[str, dict[str, object]]) -> dict[str, object]:
    return {
        'schema_version': 1,
        'record_type': 'ccb_projected_settings',
        'label': label,
        'source': str(source),
        'managed': managed,
        'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8')
    except Exception:
        return None


def _restore_file(path: Path, original_text: str | None) -> None:
    try:
        if original_text is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_text(path, original_text)
    except Exception:
        pass


def _clone_json_object(value: dict[str, object]) -> dict[str, object]:
    cloned = _clone_json_value(value)
    return cloned if isinstance(cloned, dict) else {}


def _clone_json_value(value: object) -> object:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return value


def _json_text(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + '\n'


__all__ = ['project_json_mapping_fields', 'rebase_json_path_fields']
