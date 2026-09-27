from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import os
import time

from .endpoint import EndpointRef, endpoint_from_record, endpoint_to_record

_ENDPOINT_FILE = 'control-plane-endpoint.json'
_TOKEN_FILE_PREFIX = 'control-plane-token-'


def endpoint_store_path(legacy_socket_path: str | Path) -> Path:
    return Path(legacy_socket_path).parent / _ENDPOINT_FILE


def token_store_path(legacy_socket_path: str | Path, generation: str) -> Path:
    clean_generation = str(generation or '').strip()
    if (
        not clean_generation
        or clean_generation != Path(clean_generation).name
        or '/' in clean_generation
        or '\\' in clean_generation
    ):
        raise ValueError('control-plane token generation must be a simple filename fragment')
    return Path(legacy_socket_path).parent / f'{_TOKEN_FILE_PREFIX}{clean_generation}.json'


def endpoint_lock_path(legacy_socket_path: str | Path) -> Path:
    return Path(legacy_socket_path).parent / 'control-plane-endpoint.lock'


def write_endpoint(endpoint: EndpointRef, *, legacy_socket_path: str | Path) -> None:
    path = endpoint_store_path(legacy_socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = endpoint_to_record(endpoint)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    with _endpoint_store_lock(legacy_socket_path):
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(tmp, path)


def read_endpoint(legacy_socket_path: str | Path) -> EndpointRef | None:
    path = endpoint_store_path(legacy_socket_path)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('ccbd endpoint descriptor must be an object')
    return endpoint_from_record(payload)


def unlink_endpoint(
    *,
    legacy_socket_path: str | Path,
    expected_generation: str | None,
) -> bool:
    path = endpoint_store_path(legacy_socket_path)
    if not path.exists() or not str(expected_generation or '').strip():
        return False
    with _endpoint_store_lock(legacy_socket_path):
        current = read_endpoint(legacy_socket_path)
        if str((current or {}).get('generation') or '') != str(expected_generation):
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


def touch_legacy_socket_marker(legacy_socket_path: str | Path) -> bool:
    path = Path(legacy_socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb'):
            pass
        return True
    except FileExistsError:
        return False


def unlink_legacy_socket_marker(legacy_socket_path: str | Path) -> None:
    try:
        Path(legacy_socket_path).unlink()
    except FileNotFoundError:
        return


def unlink_token(token_ref: str | Path) -> None:
    path = Path(token_ref)
    for attempt in range(3):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 2:
                return
            time.sleep(0.05)


@contextmanager
def _endpoint_store_lock(legacy_socket_path: str | Path):
    path = endpoint_lock_path(legacy_socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell() == 0 and path.stat().st_size == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        unlock = _lock_handle(handle)
        try:
            yield
        finally:
            unlock()


def _lock_handle(handle):
    try:
        import msvcrt  # type: ignore

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

        def _unlock() -> None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

        return _unlock
    except ModuleNotFoundError:
        pass
    try:
        import fcntl  # type: ignore

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

        def _unlock() -> None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        return _unlock
    except ModuleNotFoundError:
        return lambda: None
