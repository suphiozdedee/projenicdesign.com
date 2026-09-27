from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


CONTROL_SCHEMA_VERSION = 1
MAX_CONTROL_BYTES = 16 * 1024
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class ControlError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SessionControl:
    schema_version: int
    instance_id: str
    enabled: bool
    session_id: str | None
    updated_at: float

    @classmethod
    def disabled(cls, instance_id: str) -> "SessionControl":
        return cls(
            CONTROL_SCHEMA_VERSION,
            _validate_instance_id(instance_id),
            False,
            None,
            time.time(),
        )

    @classmethod
    def from_dict(cls, payload: object) -> "SessionControl":
        if not isinstance(payload, dict):
            raise ControlError("reconnect control must be a JSON object")
        expected = {"schemaVersion", "instanceId", "enabled", "sessionId", "updatedAt"}
        if set(payload) != expected:
            raise ControlError("reconnect control schema mismatch")
        state = cls(
            schema_version=payload["schemaVersion"],
            instance_id=payload["instanceId"],
            enabled=payload["enabled"],
            session_id=payload["sessionId"],
            updated_at=payload["updatedAt"],
        )
        state.validate()
        return state

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "schemaVersion": self.schema_version,
            "instanceId": self.instance_id,
            "enabled": self.enabled,
            "sessionId": self.session_id,
            "updatedAt": self.updated_at,
        }

    def validate(self) -> None:
        if self.schema_version != CONTROL_SCHEMA_VERSION:
            raise ControlError(f"unsupported control schema: {self.schema_version!r}")
        _validate_instance_id(self.instance_id)
        if not isinstance(self.enabled, bool):
            raise ControlError("enabled must be a boolean")
        if self.session_id is not None:
            _validate_session_id(self.session_id)
        if self.enabled and self.session_id is None:
            raise ControlError("an enabled control requires a session id")
        if not isinstance(self.updated_at, (int, float)) or not math.isfinite(
            self.updated_at
        ):
            raise ControlError("updatedAt must be a finite number")


def load_control(
    path: Path, *, expected_instance_id: str | None = None
) -> SessionControl:
    path = Path(path)
    if path.is_symlink():
        raise ControlError(f"control file must not be a symlink: {path}")
    try:
        file_stat = path.stat()
    except FileNotFoundError as exc:
        raise ControlError(f"control file does not exist: {path}") from exc
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise ControlError(f"control file is not owned by the current user: {path}")
    if file_stat.st_size > MAX_CONTROL_BYTES:
        raise ControlError(f"control file exceeds {MAX_CONTROL_BYTES} bytes")
    try:
        state = SessionControl.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControlError(f"failed to read reconnect control: {exc}") from exc
    if expected_instance_id is not None and state.instance_id != expected_instance_id:
        raise ControlError("reconnect instance does not match the managed CLI")
    return state


def save_control(path: Path, state: SessionControl) -> None:
    state.validate()
    path = Path(path)
    _validate_control_parent(path.parent)
    if path.exists() and path.is_symlink():
        raise ControlError(f"control file must not be a symlink: {path}")
    encoded = (
        json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".control.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def set_session_control(
    path: Path,
    *,
    instance_id: str,
    session_id: str,
    enabled: bool,
) -> SessionControl:
    state = SessionControl(
        schema_version=CONTROL_SCHEMA_VERSION,
        instance_id=_validate_instance_id(instance_id),
        enabled=bool(enabled),
        session_id=_validate_session_id(session_id),
        updated_at=time.time(),
    )
    save_control(path, state)
    return state


def control_main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or arguments[0] not in {"on", "off"}:
        print("usage: control.py on|off", file=sys.stderr)
        return 2
    if os.environ.get("CODEX_RECONNECT_MANAGED") != "1":
        print(
            "reconnect is unavailable: start this CLI with `codex-reconnect open`",
            file=sys.stderr,
        )
        return 3
    raw_path = os.environ.get("CODEX_RECONNECT_CONTROL_FILE", "")
    instance_id = os.environ.get("CODEX_RECONNECT_INSTANCE_ID", "")
    session_id = os.environ.get("CODEX_THREAD_ID", "")
    if not raw_path or not Path(raw_path).is_absolute():
        print("reconnect control path is missing or not absolute", file=sys.stderr)
        return 3
    try:
        current = load_control(Path(raw_path), expected_instance_id=instance_id)
        if current.instance_id != instance_id:
            raise ControlError("reconnect instance mismatch")
        state = set_session_control(
            Path(raw_path),
            instance_id=instance_id,
            session_id=session_id,
            enabled=arguments[0] == "on",
        )
    except ControlError as exc:
        print(f"reconnect control failed: {exc}", file=sys.stderr)
        return 3
    print(
        json.dumps(
            {
                "reconnect": "on" if state.enabled else "off",
                "sessionId": state.session_id,
            },
            sort_keys=True,
        )
    )
    return 0


def _validate_control_parent(parent: Path) -> None:
    if parent.is_symlink():
        raise ControlError(f"control directory must not be a symlink: {parent}")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_stat = parent.stat()
    if hasattr(os, "getuid") and directory_stat.st_uid != os.getuid():
        raise ControlError(
            f"control directory is not owned by the current user: {parent}"
        )
    os.chmod(parent, 0o700)


def _validate_instance_id(instance_id: object) -> str:
    if not isinstance(instance_id, str) or not SESSION_ID_RE.fullmatch(instance_id):
        raise ControlError("instance id is invalid")
    return instance_id


def _validate_session_id(session_id: object) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise ControlError("CODEX_THREAD_ID is missing or invalid")
    return session_id


if __name__ == "__main__":
    raise SystemExit(control_main())
