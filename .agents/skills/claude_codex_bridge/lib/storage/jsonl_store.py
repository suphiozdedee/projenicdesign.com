from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar('T')


class JsonlStore:
    def append(
        self,
        path: Path,
        row: T | dict[str, Any],
        serializer: Callable[[T], dict[str, Any]] | None = None,
    ) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if serializer is None:
            if not isinstance(row, dict):
                raise ValueError('serializer is required for non-dict rows')
            payload = row
        else:
            payload = serializer(row)
        with target.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + '\n')

    def read_all(self, path: Path, loader: Callable[[dict[str, Any]], T] | None = None) -> list[T] | list[dict[str, Any]]:
        target = Path(path)
        if not target.exists():
            return []
        rows: list[T] | list[dict[str, Any]] = []
        with target.open('r', encoding='utf-8') as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ValueError(f'{path}: expected JSON object rows')
                rows.append(loader(payload) if loader else payload)
        return rows

    def read_since(
        self,
        path: Path,
        start_line: int = 0,
        loader: Callable[[dict[str, Any]], T] | None = None,
    ) -> tuple[int, list[T] | list[dict[str, Any]]]:
        if start_line < 0:
            raise ValueError('start_line cannot be negative')
        target = Path(path)
        if not target.exists():
            return start_line, []
        rows: list[T] | list[dict[str, Any]] = []
        current = 0
        with target.open('r', encoding='utf-8') as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                current += 1
                if current <= start_line:
                    continue
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ValueError(f'{path}: expected JSON object rows')
                rows.append(loader(payload) if loader else payload)
        return current, rows

    def read_tail(
        self,
        path: Path,
        limit: int,
        loader: Callable[[dict[str, Any]], T] | None = None,
        *,
        ignore_invalid: bool = False,
    ) -> list[T] | list[dict[str, Any]]:
        if limit < 0:
            raise ValueError('limit cannot be negative')
        if limit == 0:
            return []
        target = Path(path)
        if not target.exists():
            return []
        if _strict_jsonl_helper_required() and not ignore_invalid:
            from rust_helpers_jsonl import read_jsonl_tail_strict_required

            rows = read_jsonl_tail_strict_required(target, limit).value
            return [loader(payload) if loader else payload for payload in rows]
        rows: list[T] | list[dict[str, Any]] = []
        with target.open('rb') as handle:
            handle.seek(0, 2)
            position = handle.tell()
            buffer = b''
            while position > 0 and len(rows) < limit:
                read_size = min(8192, position)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                buffer = chunk + buffer
                lines = buffer.splitlines()
                if position > 0 and buffer and not buffer.startswith((b'\n', b'\r')):
                    buffer = lines[0] if lines else buffer
                    lines = lines[1:]
                else:
                    buffer = b''
                for raw in reversed(lines):
                    if len(rows) >= limit:
                        break
                    text = raw.decode('utf-8').strip()
                    if not text:
                        continue
                    try:
                        payload = json.loads(text)
                        if not isinstance(payload, dict):
                            raise ValueError(f'{path}: expected JSON object rows')
                        rows.append(loader(payload) if loader else payload)
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
                        if not ignore_invalid:
                            raise
        rows.reverse()
        return rows

    def find_last(
        self,
        path: Path,
        predicate: Callable[[dict[str, Any]], bool],
        loader: Callable[[dict[str, Any]], T] | None = None,
    ) -> T | dict[str, Any] | None:
        target = Path(path)
        if not target.exists():
            return None
        with target.open('rb') as handle:
            handle.seek(0, 2)
            position = handle.tell()
            buffer = b''
            while position > 0:
                read_size = min(4096, position)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                buffer = chunk + buffer
                lines = buffer.splitlines()
                if position > 0 and buffer and not buffer.startswith((b'\n', b'\r')):
                    buffer = lines[0] if lines else buffer
                    lines = lines[1:]
                else:
                    buffer = b''
                for raw in reversed(lines):
                    text = raw.decode('utf-8').strip()
                    if not text:
                        continue
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise ValueError(f'{path}: expected JSON object rows')
                    if not predicate(payload):
                        continue
                    return loader(payload) if loader else payload
            if buffer:
                text = buffer.decode('utf-8').strip()
                if text:
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise ValueError(f'{path}: expected JSON object rows')
                    if predicate(payload):
                        return loader(payload) if loader else payload
        return None


def _strict_jsonl_helper_required() -> bool:
    return str(os.environ.get('CCB_RUST_JSONL_STORE') or '').strip().lower() in {
        '1',
        'true',
        'yes',
        'on',
        'required',
    }
