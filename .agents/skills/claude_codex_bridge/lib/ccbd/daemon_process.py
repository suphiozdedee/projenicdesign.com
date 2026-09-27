from __future__ import annotations

from pathlib import Path
import os
import signal
import subprocess
import time

from runtime_env.control_plane import control_plane_env
from process_background import background_process_kwargs, background_spawn

from ccbd.socket_client import CcbdClient, CcbdClientError
from ccbd.startup_fence import (
    EXPECTED_GENERATION_ENV,
    EXPECTED_STARTUP_ID_ENV,
    KEEPER_STARTUP_ACCEPTED_PERF_COUNTER_NS_ENV,
)
from ccbd.startup_policy import CONTROL_PLANE_RPC_TIMEOUT_S


class CcbdProcessError(RuntimeError):
    pass


def spawn_ccbd_process(
    *,
    project_root: Path,
    socket_path: Path,
    ccbd_dir: Path,
    timeout_s: float,
    keeper_pid: int | None = None,
    expected_startup_id: str | None = None,
    expected_generation: int | None = None,
    keeper_startup_accepted_perf_counter_ns: int | None = None,
) -> None:
    script = Path(__file__).resolve().parent / 'main.py'
    interpreter, venv_env = background_spawn()
    env = _ccbd_env(
        keeper_pid=keeper_pid,
        expected_startup_id=expected_startup_id,
        expected_generation=expected_generation,
        keeper_startup_accepted_perf_counter_ns=keeper_startup_accepted_perf_counter_ns,
        venv_env=venv_env,
    )
    ccbd_dir.mkdir(parents=True, exist_ok=True)
    with open(ccbd_dir / 'ccbd.stdout.log', 'ab') as stdout_log, open(
        ccbd_dir / 'ccbd.stderr.log',
        'ab',
    ) as stderr_log:
        process = subprocess.Popen(
            [interpreter, str(script), '--project', str(project_root)],
            cwd=str(project_root),
            env=env,
            stdout=stdout_log,
            stderr=stderr_log,
            **background_process_kwargs(),
        )
        try:
            _wait_for_ccbd_ready(
                process=process,
                socket_path=socket_path,
                timeout_s=timeout_s,
                expected_startup_id=expected_startup_id,
                expected_generation=expected_generation,
            )
        except Exception:
            _terminate_spawned_process(process)
            raise


def _wait_for_ccbd_ready(
    *,
    process: subprocess.Popen[bytes],
    socket_path: Path,
    timeout_s: float,
    expected_startup_id: str | None = None,
    expected_generation: int | None = None,
) -> None:
    deadline = time.time() + max(0.0, float(timeout_s))
    last_error: str | None = None
    while time.time() < deadline:
        if socket_path.exists():
            try:
                payload = CcbdClient(socket_path, timeout_s=CONTROL_PLANE_RPC_TIMEOUT_S).ping('ccbd')
                if _ready_payload_matches_expected(
                    payload,
                    expected_startup_id=expected_startup_id,
                    expected_generation=expected_generation,
                ):
                    return
                last_error = 'ccbd readiness identity does not match expected startup transaction'
            except CcbdClientError as exc:
                last_error = str(exc)
        if process.poll() is not None:
            if socket_path.exists():
                try:
                    payload = CcbdClient(socket_path, timeout_s=CONTROL_PLANE_RPC_TIMEOUT_S).ping('ccbd')
                    if _ready_payload_matches_expected(
                        payload,
                        expected_startup_id=expected_startup_id,
                        expected_generation=expected_generation,
                    ):
                        return
                    last_error = 'ccbd readiness identity does not match expected startup transaction'
                except CcbdClientError as exc:
                    last_error = str(exc)
            raise CcbdProcessError(f'ccbd exited before ready with code {process.returncode}')
        time.sleep(0.05)
    raise CcbdProcessError(last_error or 'timed out waiting for ccbd to become ready')


def _ccbd_env(
    *,
    keeper_pid: int | None,
    expected_startup_id: str | None = None,
    expected_generation: int | None = None,
    keeper_startup_accepted_perf_counter_ns: int | None = None,
    venv_env: dict[str, str] | None = None,
) -> dict[str, str]:
    startup_id = str(expected_startup_id or '').strip()
    generation = int(expected_generation or 0)
    if bool(startup_id) != bool(generation):
        raise ValueError('expected startup fence requires both startup_id and generation')
    if generation < 0:
        raise ValueError('expected startup generation cannot be negative')
    accepted_ns = _positive_diagnostics_int(
        keeper_startup_accepted_perf_counter_ns
    )
    if not startup_id or not generation:
        accepted_ns = None
    extra = {
        'PYTHONUNBUFFERED': '1',
        EXPECTED_STARTUP_ID_ENV: startup_id or None,
        EXPECTED_GENERATION_ENV: str(generation) if generation else None,
        KEEPER_STARTUP_ACCEPTED_PERF_COUNTER_NS_ENV: (
            str(accepted_ns) if accepted_ns is not None else None
        ),
    }
    if venv_env:
        for key, value in venv_env.items():
            if value:
                extra[key] = value
    env = control_plane_env(extra=extra)
    lib_root = str(Path(__file__).resolve().parents[1])
    script_root = Path(__file__).resolve().parents[2]
    _prepend_tool_paths(env, script_root)
    current_pythonpath = env.get('PYTHONPATH')
    env['PYTHONPATH'] = lib_root if not current_pythonpath else lib_root + os.pathsep + current_pythonpath
    if keeper_pid is not None and keeper_pid > 0:
        env['CCB_KEEPER_PID'] = str(int(keeper_pid))
    return env


def _positive_diagnostics_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 and str(parsed) == str(value).strip() else None


def _ready_payload_matches_expected(
    payload: dict[str, object],
    *,
    expected_startup_id: str | None,
    expected_generation: int | None,
) -> bool:
    startup_id = str(expected_startup_id or '').strip()
    generation = int(expected_generation or 0)
    if not startup_id and not generation:
        return True
    if not startup_id or generation <= 0:
        return False
    try:
        serving_pid = int(payload.get('serving_pid') or 0)
        serving_generation = int(payload.get('serving_lease_generation') or 0)
        lifecycle_generation = int(payload.get('generation') or 0)
    except (TypeError, ValueError):
        return False
    diagnostics = payload.get('diagnostics')
    if not isinstance(diagnostics, dict):
        return False
    # Do not require serving_pid == the spawner's Popen pid: on Windows the
    # venvlauncher redirector makes Popen.pid differ from the daemon's own pid.
    # Startup identity is proven by startup_id/generation/daemon_instance_id,
    # so a positive serving_pid is sufficient sanity.
    return (
        serving_pid > 0
        and serving_generation == generation
        and lifecycle_generation == generation
        and str(payload.get('accepted_startup_id') or '') == startup_id
        and bool(str(payload.get('serving_daemon_instance_id') or '').strip())
        and str(payload.get('mount_state') or '') == 'mounted'
        and str(payload.get('desired_state') or '') == 'running'
        and str(diagnostics.get('startup_id') or '') == startup_id
        and str(diagnostics.get('startup_stage') or '') == 'mounted'
    )


def _terminate_spawned_process(
    process: subprocess.Popen[bytes],
    *,
    timeout_s: float = 1.0,
) -> None:
    if process.poll() is not None:
        try:
            process.wait(timeout=0)
        except Exception:
            pass
        return
    try:
        os.killpg(int(process.pid), signal.SIGTERM)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except Exception:
            pass
    try:
        process.wait(timeout=max(0.0, float(timeout_s)))
        return
    except (subprocess.TimeoutExpired, TimeoutError):
        pass
    except Exception:
        return
    try:
        os.killpg(int(process.pid), signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=max(0.0, float(timeout_s)))
    except Exception:
        pass


def _prepend_tool_paths(env: dict[str, str], script_root: Path) -> None:
    current = env.get('PATH', '')
    parts: list[str] = []
    seen: set[str] = set()
    for candidate in (script_root / 'bin', script_root):
        text = str(candidate)
        if candidate.exists() and text not in seen:
            parts.append(text)
            seen.add(text)
    for item in current.split(os.pathsep):
        if item and item not in seen:
            parts.append(item)
            seen.add(item)
    env['PATH'] = os.pathsep.join(parts)


__all__ = ['CcbdProcessError', 'spawn_ccbd_process']
