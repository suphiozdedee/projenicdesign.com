from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import (
    accelerator_binary,
    accelerator_socket_path,
    accelerator_startup_timeout_s,
    codex_accelerator_enabled,
)
from .ownership import (
    RuntimeAcceleratorOwnershipError,
    load_runtime_accelerator_owner,
    reclaim_runtime_accelerator,
    record_runtime_accelerator_owner,
    remove_runtime_accelerator_owner,
    runtime_accelerator_socket_is_connectable,
)


@dataclass
class RuntimeAcceleratorHandle:
    enabled: bool
    socket_path: Path | None
    process: subprocess.Popen | None = None
    error: str = ""
    project_root: Path | None = None
    reclaimed_pids: tuple[int, ...] = ()

    @property
    def started(self) -> bool:
        return self.process is not None and self.process.poll() is None


def maybe_start_runtime_accelerator(project_root: str | Path) -> RuntimeAcceleratorHandle:
    project_root = Path(project_root).expanduser().resolve()
    socket_path = accelerator_socket_path(project_root)
    if not codex_accelerator_enabled():
        return RuntimeAcceleratorHandle(enabled=False, socket_path=socket_path, project_root=project_root)
    if socket_path is None:
        return RuntimeAcceleratorHandle(
            enabled=True,
            socket_path=None,
            error="missing_socket_path",
            project_root=project_root,
        )
    binary = accelerator_binary()
    if not binary:
        return RuntimeAcceleratorHandle(
            enabled=True,
            socket_path=socket_path,
            error="missing_binary",
            project_root=project_root,
        )
    reclaimed_pids = reclaim_runtime_accelerator(project_root, socket_path=socket_path)
    try:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [binary, "serve", "--socket", str(socket_path)],
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return RuntimeAcceleratorHandle(
            enabled=True,
            socket_path=socket_path,
            error=str(exc),
            project_root=project_root,
            reclaimed_pids=reclaimed_pids,
        )
    handle = RuntimeAcceleratorHandle(
        enabled=True,
        socket_path=socket_path,
        process=process,
        project_root=project_root,
        reclaimed_pids=reclaimed_pids,
    )
    try:
        record_runtime_accelerator_owner(project_root, socket_path=socket_path, pid=process.pid)
    except RuntimeAcceleratorOwnershipError as exc:
        stop_runtime_accelerator(handle)
        if not str(exc).startswith("runtime_accelerator_identity_unavailable:"):
            raise
        try:
            socket_path.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            raise RuntimeAcceleratorOwnershipError(
                f"runtime_accelerator_identity_fallback_cleanup_failed:{socket_path}:{cleanup_exc}"
            ) from cleanup_exc
        if socket_path.exists() or runtime_accelerator_socket_is_connectable(socket_path):
            raise RuntimeAcceleratorOwnershipError(
                f"runtime_accelerator_identity_fallback_socket_active:{socket_path}"
            )
        return RuntimeAcceleratorHandle(
            enabled=True,
            socket_path=socket_path,
            error=str(exc),
            project_root=project_root,
            reclaimed_pids=reclaimed_pids,
        )
    except Exception:
        stop_runtime_accelerator(handle)
        raise
    if wait_for_socket(socket_path, process=process, timeout_s=accelerator_startup_timeout_s()):
        return handle
    error = "startup_timeout" if process.poll() is None else f"exited:{process.returncode}"
    stop_runtime_accelerator(handle)
    return RuntimeAcceleratorHandle(
        enabled=True,
        socket_path=socket_path,
        error=error,
        project_root=project_root,
        reclaimed_pids=reclaimed_pids,
    )


def wait_for_socket(socket_path: Path, *, process: subprocess.Popen, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() <= deadline:
        if socket_path.exists():
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.025)
    return socket_path.exists()


def stop_runtime_accelerator(handle: RuntimeAcceleratorHandle | None) -> None:
    if handle is None:
        return
    process = handle.process
    owns_socket = process is not None
    if process is not None:
        project_root = handle.project_root
        if project_root is not None:
            owner = load_runtime_accelerator_owner(project_root)
            if owner is not None and owner.pid != process.pid:
                raise RuntimeAcceleratorOwnershipError(
                    f"runtime_accelerator_handle_owner_mismatch:handle={process.pid}:owner={owner.pid}"
                )
        _stop_process(process)
        if project_root is not None:
            remove_runtime_accelerator_owner(project_root, pid=process.pid)
    socket_path = handle.socket_path
    if owns_socket and socket_path is not None:
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.0)


__all__ = [
    "RuntimeAcceleratorHandle",
    "maybe_start_runtime_accelerator",
    "stop_runtime_accelerator",
    "wait_for_socket",
]
