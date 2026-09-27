from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .commands_runtime import is_newer_version


CACHE_SCHEMA_VERSION = 1
CACHE_TTL_S = 12 * 60 * 60
PROMPT_DEFER_S = 12 * 60 * 60
CACHE_FILE_NAME = ".update-check.json"
LOCK_FILE_NAME = ".update-check.lock"


def load_update_check_state(install_dir: Path) -> dict[str, object] | None:
    payload = _read_cache_payload(update_check_cache_path(install_dir))
    if payload is None:
        return None
    return _normalized_cache_state(payload)


def write_update_check_state(install_dir: Path, payload: dict[str, object]) -> None:
    path = update_check_cache_path(install_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def update_check_cache_path(install_dir: Path) -> Path:
    return Path(install_dir) / CACHE_FILE_NAME


def update_check_lock_path(install_dir: Path) -> Path:
    return Path(install_dir) / LOCK_FILE_NAME


def update_check_state_is_stale(state: dict[str, object], *, now: float | None = None) -> bool:
    checked_at_epoch = safe_float(state.get("checked_at_epoch"))
    return checked_at_epoch <= 0 or checked_at_epoch + CACHE_TTL_S <= float(now or time.time())


def should_prompt_for_update(
    state: dict[str, object],
    *,
    local_info: dict[str, object],
    now: float | None = None,
) -> bool:
    latest = str(state.get("latest_version") or "").strip()
    current = str(local_info.get("version") or "").strip()
    if not _has_promptable_update(state, latest=latest, current=current, local_info=local_info, now=now):
        return False
    if str(state.get("muted_version") or "").strip() == latest:
        return False
    return not _prompt_is_deferred(state, latest=latest, now=now)


def defer_update_prompt(install_dir: Path, state: dict[str, object], *, now: float | None = None) -> None:
    payload = dict(state)
    payload["deferred_version"] = str(state.get("latest_version") or "").strip() or None
    payload["deferred_until_epoch"] = float(now or time.time()) + PROMPT_DEFER_S
    write_update_check_state(install_dir, payload)


def silence_update_version(install_dir: Path, state: dict[str, object]) -> None:
    payload = dict(state)
    payload["muted_version"] = str(state.get("latest_version") or "").strip() or None
    payload["deferred_version"] = None
    payload["deferred_until_epoch"] = None
    write_update_check_state(install_dir, payload)


def startup_release_update_supported(local_info: dict[str, object], *, platform_name: str) -> bool:
    if platform_name not in {"Linux", "Darwin"}:
        return False
    return (
        str(local_info.get("install_mode") or "").strip() == "release"
        and str(local_info.get("source_kind") or "").strip() == "release"
        and str(local_info.get("channel") or "").strip() == "stable"
    )


def refresh_cache_payload(
    *,
    local_info: dict[str, object],
    latest: str,
    existing: dict[str, object],
    now: float,
) -> dict[str, object]:
    current = str(local_info.get("version") or "").strip() or None
    muted_version = _active_muted_version(existing, latest=latest)
    deferred_version, deferred_until_epoch = _active_deferred_prompt(existing, latest=latest, now=now)
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "checked_at": utc_now_text(now),
        "checked_at_epoch": now,
        "current_version": current,
        "latest_version": latest,
        "update_available": bool(current and is_newer_version(latest, current)),
        "muted_version": muted_version,
        "deferred_version": deferred_version,
        "deferred_until_epoch": deferred_until_epoch,
    }


def safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def optional_float(value: object) -> float | None:
    try:
        resolved = float(value)
    except Exception:
        return None
    return resolved if resolved > 0 else None


def utc_now_text(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _read_cache_payload(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _normalized_cache_state(payload: dict[str, object]) -> dict[str, object] | None:
    if int(payload.get("schema_version") or 0) != CACHE_SCHEMA_VERSION:
        return None
    latest_version_text = str(payload.get("latest_version") or "").strip()
    if not latest_version_text:
        return None
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "checked_at": str(payload.get("checked_at") or "").strip() or None,
        "checked_at_epoch": safe_float(payload.get("checked_at_epoch")),
        "current_version": str(payload.get("current_version") or "").strip() or None,
        "latest_version": latest_version_text,
        "update_available": bool(payload.get("update_available")),
        "muted_version": str(payload.get("muted_version") or "").strip() or None,
        "deferred_version": str(payload.get("deferred_version") or "").strip() or None,
        "deferred_until_epoch": optional_float(payload.get("deferred_until_epoch")),
    }


def _has_promptable_update(
    state: dict[str, object],
    *,
    latest: str,
    current: str,
    local_info: dict[str, object],
    now: float | None,
) -> bool:
    if update_check_state_is_stale(state, now=now):
        return False
    if not latest or not current or not bool(state.get("update_available")):
        return False
    return is_newer_version(latest, current)


def _prompt_is_deferred(state: dict[str, object], *, latest: str, now: float | None) -> bool:
    deferred_version = str(state.get("deferred_version") or "").strip()
    deferred_until_epoch = safe_float(state.get("deferred_until_epoch"))
    return deferred_version == latest and deferred_until_epoch > float(now or time.time())


def _active_muted_version(existing: dict[str, object], *, latest: str) -> str | None:
    muted_version = str(existing.get("muted_version") or "").strip() or None
    return muted_version if muted_version == latest else None


def _active_deferred_prompt(
    existing: dict[str, object], *, latest: str, now: float
) -> tuple[str | None, float | None]:
    deferred_version = str(existing.get("deferred_version") or "").strip() or None
    deferred_until_epoch = safe_float(existing.get("deferred_until_epoch"))
    if deferred_version == latest and deferred_until_epoch > now:
        return deferred_version, deferred_until_epoch
    return None, None


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CACHE_TTL_S",
    "PROMPT_DEFER_S",
    "defer_update_prompt",
    "load_update_check_state",
    "optional_float",
    "refresh_cache_payload",
    "safe_float",
    "should_prompt_for_update",
    "silence_update_version",
    "startup_release_update_supported",
    "update_check_cache_path",
    "update_check_lock_path",
    "update_check_state_is_stale",
    "utc_now_text",
    "write_update_check_state",
]
