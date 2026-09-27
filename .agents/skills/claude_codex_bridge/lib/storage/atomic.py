from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from runtime_observability import (
    in_startup_operation_scope,
    record_startup_operation,
    record_startup_operations,
    startup_operation_collection_active,
)


def _directory_fsync_supported() -> bool:
    return hasattr(os, 'O_DIRECTORY')


def _open_directory(path: Path) -> int:
    if not _directory_fsync_supported():
        raise NotImplementedError('durable atomic writes require directory fsync support')
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_CLOEXEC', 0)
    return os.open(path, flags)


def _fsync_directory(path: Path) -> None:
    if not _directory_fsync_supported():
        return
    fd = _open_directory(path)
    try:
        os.fsync(fd)
    except BaseException:
        _close_directory_after_error(fd)
        raise
    else:
        os.close(fd)


def _close_directory_after_error(fd: int) -> None:
    try:
        os.close(fd)
    except BaseException:
        pass


def _verify_directory_path(fd: int, path: Path) -> None:
    opened = os.fstat(fd)
    current = os.stat(path)
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise RuntimeError(f'atomic write parent directory was replaced: {path}')


def ensure_durable_directory(path: Path) -> None:
    path = Path(path).absolute()
    missing: list[Path] = []
    candidate = path
    while candidate != candidate.parent:
        if candidate.is_symlink():
            raise ValueError(f'durable directory path cannot contain symlinks: {candidate}')
        if candidate.exists():
            if not candidate.is_dir():
                raise NotADirectoryError(candidate)
            break
        missing.append(candidate)
        candidate = candidate.parent

    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if directory.is_symlink() or not directory.is_dir():
                raise
        else:
            _fsync_directory(directory.parent)

    candidate = path
    while candidate != candidate.parent:
        if candidate.is_symlink() or not candidate.is_dir():
            raise RuntimeError(f'durable directory path was replaced: {candidate}')
        candidate = candidate.parent


def atomic_write_text(path: Path, text: str, *, encoding: str = 'utf-8') -> None:
    target = Path(path)
    collect_metrics = startup_operation_collection_active()
    if collect_metrics:
        record_startup_operation('atomic_durable_write_attempt_count')
    ensure_durable_directory(target.parent)
    if not _directory_fsync_supported():
        _atomic_write_text_path_replace(target, text, encoding=encoding)
        written_bytes = target.stat().st_size if target.exists() else None
        _record_atomic_write_metrics(collect_metrics, written_bytes=written_bytes)
        return
    directory_fd = _open_directory(target.parent)
    _verify_directory_path(directory_fd, target.parent)
    tmp_name = f'.{target.name}.{secrets.token_hex(8)}.tmp'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(tmp_name, flags, 0o600, dir_fd=directory_fd)
    except BaseException:
        _close_directory_after_error(directory_fd)
        raise
    handle_opened = False
    written_bytes: int | None = None
    try:
        with os.fdopen(fd, 'w', encoding=encoding) as handle:
            handle_opened = True
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            if collect_metrics:
                try:
                    written_bytes = max(0, int(os.fstat(handle.fileno()).st_size))
                except Exception:
                    written_bytes = None
        _verify_directory_path(directory_fd, target.parent)
        os.replace(tmp_name, target.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
        _verify_directory_path(directory_fd, target.parent)
    except BaseException:
        if not handle_opened:
            try:
                os.close(fd)
            except BaseException:
                pass
        try:
            os.unlink(tmp_name, dir_fd=directory_fd)
        except BaseException:
            pass
        _close_directory_after_error(directory_fd)
        raise
    else:
        os.close(directory_fd)
    _record_atomic_write_metrics(collect_metrics, written_bytes=written_bytes)


def _atomic_write_text_path_replace(target: Path, text: str, *, encoding: str) -> None:
    tmp_path = target.parent / f'.{target.name}.{secrets.token_hex(8)}.tmp'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    fd = os.open(tmp_path, flags, 0o600)
    handle_opened = False
    try:
        with os.fdopen(fd, 'w', encoding=encoding) as handle:
            handle_opened = True
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_path_with_windows_retry(tmp_path, target)
    except BaseException:
        if not handle_opened:
            try:
                os.close(fd)
            except BaseException:
                pass
        try:
            tmp_path.unlink()
        except BaseException:
            pass
        raise


def _replace_path_with_windows_retry(source: Path, target: Path) -> None:
    for attempt in range(4):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not _is_transient_windows_replace_error(exc) or attempt == 3:
                raise
            time.sleep(0.025 * (attempt + 1))


def _is_transient_windows_replace_error(exc: OSError) -> bool:
    if os.name != 'nt':
        return False
    return int(getattr(exc, 'winerror', 0) or 0) in {5, 32}


def _record_atomic_write_metrics(collect_metrics: bool, *, written_bytes: int | None) -> None:
    if not collect_metrics:
        return
    counts = {'atomic_durable_write_count': 1}
    if written_bytes is not None:
        counts['atomic_durable_write_byte_count'] = written_bytes
    if in_startup_operation_scope('provider_prepare'):
        counts['provider_prepare_atomic_write_count'] = 1
        if written_bytes is not None:
            counts['provider_prepare_atomic_write_byte_count'] = written_bytes
    record_startup_operations(counts)


def atomic_write_json(path: Path, payload: Any, *, encoding: str = 'utf-8') -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding=encoding)


def atomic_write_text_if_changed(path: Path, text: str, *, encoding: str = 'utf-8') -> bool:
    target = Path(path)
    collect_metrics = startup_operation_collection_active()
    try:
        if target.read_text(encoding=encoding) == text:
            if collect_metrics:
                record_startup_operation('atomic_durable_write_skip_count')
                if in_startup_operation_scope('provider_prepare'):
                    record_startup_operation('provider_prepare_atomic_write_skip_count')
            return False
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        pass
    atomic_write_text(target, text, encoding=encoding)
    return True


def atomic_write_json_if_changed(path: Path, payload: Any, *, encoding: str = 'utf-8') -> bool:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    return atomic_write_text_if_changed(path, text, encoding=encoding)
