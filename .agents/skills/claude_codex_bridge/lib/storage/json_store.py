from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Callable, TypeVar

from storage.atomic import atomic_write_text, atomic_write_text_if_changed

T = TypeVar('T')

_LOAD_ATTEMPTS = 3
_LOAD_RETRY_DELAY_S = 0.02


class JsonStore:
    def load(self, path: Path, loader: Callable[[dict[str, Any]], T] | None = None) -> T | dict[str, Any]:
        target = Path(path)
        payload = _load_json_object(target)
        if not isinstance(payload, dict):
            raise ValueError(f'{path}: expected JSON object')
        if loader is None:
            return payload
        return loader(payload)

    def save(
        self,
        path: Path,
        value: T | dict[str, Any],
        serializer: Callable[[T], dict[str, Any]] | None = None,
    ) -> None:
        atomic_write_text(Path(path), self._serialize(value, serializer=serializer))

    def save_if_changed(
        self,
        path: Path,
        value: T | dict[str, Any],
        serializer: Callable[[T], dict[str, Any]] | None = None,
    ) -> bool:
        return atomic_write_text_if_changed(
            Path(path),
            self._serialize(value, serializer=serializer),
        )

    @staticmethod
    def _serialize(
        value: T | dict[str, Any],
        *,
        serializer: Callable[[T], dict[str, Any]] | None,
    ) -> str:
        if serializer is None:
            if not isinstance(value, dict):
                raise ValueError('serializer is required for non-dict values')
            payload = value
        else:
            payload = serializer(value)
        return json.dumps(payload, ensure_ascii=False, indent=2) + '\n'


def _load_json_object(path: Path) -> Any:
    last_error: Exception | None = None
    for attempt in range(_LOAD_ATTEMPTS):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_error = exc
            if attempt + 1 >= _LOAD_ATTEMPTS:
                break
            time.sleep(_LOAD_RETRY_DELAY_S)
    _raise_invalid_json(path, last_error)


def _raise_invalid_json(path: Path, error: Exception | None) -> None:
    if isinstance(error, json.JSONDecodeError):
        raise json.JSONDecodeError(
            f'{path}: invalid JSON: {error.msg}',
            error.doc,
            error.pos,
        ) from error
    if isinstance(error, UnicodeDecodeError):
        raise UnicodeDecodeError(
            error.encoding,
            error.object,
            error.start,
            error.end,
            f'{path}: invalid JSON: {error.reason}',
        ) from error
    raise ValueError(f'{path}: invalid JSON: {error}') from error
