from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Callable
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from ccbd.system import utc_now
from cli.kill_runtime.processes import is_pid_alive, terminate_pid_tree
from cli.services.mobile import prepare_server_mobile_gateway
from mobile_gateway import (
    MobileGatewayPairingStore,
    mobile_host_state_dir,
    parse_listen_address,
)
from mobile_gateway.relay_host_credentials import (
    build_relay_pairing_payload,
    load_relay_host_credentials,
)
from mobile_gateway.relay_host_runtime import RelayHostConnectorRuntime
from storage.atomic import atomic_write_json


MOBILE_HOST_SERVE_COMMAND = '__mobile-host-serve'
MOBILE_HOST_SERVICE_RECORD_TYPE = 'ccb_mobile_host_service'
MOBILE_HOST_SERVICE_SCHEMA_VERSION = 1
MOBILE_HOST_LOCK_TTL_S = 120.0
MOBILE_HOST_LOCK_WAIT_TIMEOUT_S = 10.0
MOBILE_HOST_LOCK_RETRY_INTERVAL_S = 0.05
MOBILE_HOST_STOP_TIMEOUT_S = 2.0
MOBILE_HOST_PORT_RELEASE_TIMEOUT_S = 3.0
MOBILE_HOST_HEALTH_TIMEOUT_S = 10.0
MOBILE_HOST_HEALTH_REQUEST_TIMEOUT_S = 2.0
MOBILE_HOST_STATE_READY_TIMEOUT_S = MOBILE_HOST_HEALTH_TIMEOUT_S


class MobileHostServiceError(RuntimeError):
    pass


class _MobileHostShutdown(RuntimeError):
    pass


@dataclass(frozen=True)
class PortOwner:
    pid: int
    command: str = ''


@dataclass(frozen=True)
class MobileHostServicePaths:
    state_dir: Path
    state_path: Path
    lock_path: Path
    log_path: Path


@dataclass(frozen=True)
class MobileHostServiceResult:
    status: str
    pid: int
    generation: int
    listen: str
    gateway_url: str | None
    local_gateway_url: str
    route_provider: str
    state_dir: Path
    state_path: Path
    log_path: Path
    replaced_pid: int | None = None
    pairing: Mapping[str, object] | None = None
    pairing_diagnostic: str | None = None

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            'mobile_status': 'serving',
            'service_status': self.status,
            'pid': self.pid,
            'generation': self.generation,
            'listen': self.listen,
            'local_gateway_url': self.local_gateway_url,
            'gateway_url': self.gateway_url or self.local_gateway_url,
            'route_provider': self.route_provider,
            'mobile_state_dir': str(self.state_dir),
            'service_state_path': str(self.state_path),
            'service_log_path': str(self.log_path),
        }
        if self.replaced_pid is not None:
            record['replaced_pid'] = self.replaced_pid
        if self.pairing is not None:
            record['pairing'] = dict(self.pairing)
        if self.pairing_diagnostic:
            record['pairing_diagnostic'] = self.pairing_diagnostic
        return record


def start_or_replace_mobile_host_service(
    *,
    script_root: Path,
    listen: str,
    public_url: str | None,
    route_provider: str,
    state_dir: Path | None = None,
    host_id: str | None = None,
    process_exists_fn: Callable[[int], bool] = is_pid_alive,
    process_cmdline_fn: Callable[[int], str] | None = None,
    terminate_pid_tree_fn: Callable[..., bool] = terminate_pid_tree,
    port_owner_fn: Callable[[str], PortOwner | None] | None = None,
    spawn_fn: Callable[..., object] | None = None,
    health_check_fn: Callable[[str], bool] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    lock_wait_timeout_s: float = MOBILE_HOST_LOCK_WAIT_TIMEOUT_S,
    rotate_pairing: bool = False,
    force_restart: bool = False,
) -> MobileHostServiceResult:
    try:
        parsed_listen = parse_listen_address(
            listen,
            allow_lan=str(route_provider or '').strip().lower() == 'lan',
        )
    except ValueError as exc:
        raise MobileHostServiceError(str(exc)) from exc
    if parsed_listen.port <= 0:
        raise MobileHostServiceError(
            'mobile gateway listen port must be between 1 and 65535'
        )
    listen = parsed_listen.text
    paths = mobile_host_service_paths(state_dir)
    lock_fd = _acquire_mobile_host_lock(
        paths.lock_path,
        process_exists_fn=process_exists_fn,
        timeout_s=lock_wait_timeout_s,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )
    if lock_fd is None:
        raise MobileHostServiceError(f'mobile host service update already in progress: {paths.lock_path}')
    replaced_pid: int | None = None
    try:
        state = read_mobile_host_service_state(paths.state_path)
        old_pid = _state_pid(state)
        if old_pid and not process_exists_fn(old_pid):
            remove_mobile_host_service_state(paths.state_path)
            state = None
            old_pid = None
        if old_pid:
            if process_exists_fn(old_pid) and _managed_mobile_host_process(
                old_pid,
                state,
                script_root=script_root,
                listen=listen,
                state_dir=paths.state_dir,
                process_cmdline_fn=process_cmdline_fn,
            ):
                if not force_restart and _mobile_host_state_matches_request(
                    state,
                    listen=listen,
                    public_url=public_url,
                    route_provider=route_provider,
                ) and _mobile_host_state_is_healthy(
                    state,
                    listen=listen,
                    health_check_fn=health_check_fn,
                ) and _mobile_host_listen_owned_by_process(
                    listen,
                    old_pid,
                    port_owner_fn=port_owner_fn,
                ):
                    if not rotate_pairing:
                        state = _mobile_host_state_with_pairing_diagnostic(state, paths=paths)
                    state = (
                        _mobile_host_state_with_rotated_pairing(state, paths=paths)
                        if rotate_pairing
                        else state
                    )
                    if state is None:
                        replaced_pid = old_pid
                        _terminate_managed_mobile_host(
                            old_pid,
                            terminate_pid_tree_fn=terminate_pid_tree_fn,
                        )
                        if not _wait_until(
                            lambda: not process_exists_fn(old_pid),
                            timeout_s=MOBILE_HOST_STOP_TIMEOUT_S,
                            sleep_fn=sleep_fn,
                            monotonic_fn=monotonic_fn,
                        ):
                            raise MobileHostServiceError(f'mobile host service did not stop: pid={old_pid}')
                        state = read_mobile_host_service_state(paths.state_path)
                        old_pid = None
                    else:
                        return _mobile_host_result_from_state(
                            state,
                            paths=paths,
                            status='running',
                        )
                if old_pid is None:
                    pass
                else:
                    replaced_pid = old_pid
                    _terminate_managed_mobile_host(
                        old_pid,
                        terminate_pid_tree_fn=terminate_pid_tree_fn,
                    )
                    if not _wait_until(
                        lambda: not process_exists_fn(old_pid),
                        timeout_s=MOBILE_HOST_STOP_TIMEOUT_S,
                        sleep_fn=sleep_fn,
                        monotonic_fn=monotonic_fn,
                    ):
                        raise MobileHostServiceError(f'mobile host service did not stop: pid={old_pid}')
            else:
                remove_mobile_host_service_state(paths.state_path)
                state = None
                old_pid = None
        generation = _next_generation(state)
        owner = (port_owner_fn or detect_loopback_port_owner)(listen)
        if owner is not None:
            if not _managed_mobile_host_process(
                owner.pid,
                state,
                script_root=script_root,
                listen=listen,
                state_dir=paths.state_dir,
                process_cmdline_fn=process_cmdline_fn,
            ):
                detail = f'pid={owner.pid}'
                if owner.command:
                    detail += f' command={owner.command}'
                raise MobileHostServiceError(f'mobile gateway listen port is already owned by a non-CCB process: {detail}')
            replaced_pid = owner.pid
            _terminate_managed_mobile_host(owner.pid, terminate_pid_tree_fn=terminate_pid_tree_fn)
        _wait_until(
            lambda: (port_owner_fn or detect_loopback_port_owner)(listen) is None,
            timeout_s=MOBILE_HOST_PORT_RELEASE_TIMEOUT_S,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
        if (port_owner_fn or detect_loopback_port_owner)(listen) is not None:
            raise MobileHostServiceError(f'mobile gateway listen port did not release: {listen}')

        process = _spawn_mobile_host_service(
            script_root=script_root,
            paths=paths,
            listen=listen,
            public_url=public_url,
            route_provider=route_provider,
            generation=generation,
            host_id=host_id,
            spawn_fn=spawn_fn,
            rotate_pairing=rotate_pairing,
        )
        pid = int(getattr(process, 'pid', 0) or 0)
        local_gateway_url = _local_gateway_url(listen)
        try:
            _wait_for_mobile_host_health(
                local_gateway_url,
                process=process,
                log_path=paths.log_path,
                health_check_fn=health_check_fn,
                timeout_s=MOBILE_HOST_HEALTH_TIMEOUT_S,
                sleep_fn=sleep_fn,
                monotonic_fn=monotonic_fn,
            )
        except Exception:
            if pid > 0:
                _terminate_managed_mobile_host(pid, terminate_pid_tree_fn=terminate_pid_tree_fn)
            raise
        state = _wait_for_mobile_host_state_ready(
            paths,
            process=process,
            pid=pid,
            generation=generation,
            timeout_s=MOBILE_HOST_STATE_READY_TIMEOUT_S,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
        return MobileHostServiceResult(
            status='replaced' if replaced_pid is not None else 'started',
            pid=_state_pid(state) or pid,
            generation=_state_generation(state, generation),
            listen=_state_listen(state, listen=listen),
            gateway_url=_state_gateway_url(state, fallback=public_url or local_gateway_url),
            local_gateway_url=_state_local_gateway_url(state, listen=listen),
            route_provider=_state_route_provider(state, fallback=route_provider),
            state_dir=paths.state_dir,
            state_path=paths.state_path,
            log_path=paths.log_path,
            replaced_pid=replaced_pid,
            pairing=_state_pairing(state),
        )
    finally:
        _release_mobile_host_lock(paths.lock_path, lock_fd)


def restart_running_mobile_host_service(
    *,
    script_root: Path,
    state_dir: Path | None = None,
    process_exists_fn: Callable[[int], bool] = is_pid_alive,
    process_cmdline_fn: Callable[[int], str] | None = None,
    terminate_pid_tree_fn: Callable[..., bool] = terminate_pid_tree,
    port_owner_fn: Callable[[str], PortOwner | None] | None = None,
    spawn_fn: Callable[..., object] | None = None,
    health_check_fn: Callable[[str], bool] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> MobileHostServiceResult | None:
    """Restart an active Host owned by this installation without rotating access."""
    paths = mobile_host_service_paths(state_dir)
    state = read_mobile_host_service_state(paths.state_path)
    pid = _state_pid(state)
    if pid is None or not process_exists_fn(pid):
        return None
    if not _mobile_host_process_uses_script_root(
        pid,
        state,
        script_root=script_root,
        process_cmdline_fn=process_cmdline_fn,
    ):
        return None

    listen = _state_listen(state, listen='')
    route_provider = _state_route_provider(state, fallback='')
    if not listen or not route_provider:
        raise MobileHostServiceError(
            f'active mobile host service state is incomplete: {paths.state_path}'
        )
    public_url = _state_restart_public_url(state, route_provider=route_provider)
    host_id = str((state or {}).get('host_id') or '').strip() or None
    return start_or_replace_mobile_host_service(
        script_root=script_root,
        listen=listen,
        public_url=public_url,
        route_provider=route_provider,
        state_dir=paths.state_dir,
        host_id=host_id,
        process_exists_fn=process_exists_fn,
        process_cmdline_fn=process_cmdline_fn,
        terminate_pid_tree_fn=terminate_pid_tree_fn,
        port_owner_fn=port_owner_fn,
        spawn_fn=spawn_fn,
        health_check_fn=health_check_fn,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
        rotate_pairing=False,
        force_restart=True,
    )


def maybe_handle_mobile_host_serve_command(tokens: list[str], *, script_root: Path) -> int | None:
    if list(tokens[:1]) != [MOBILE_HOST_SERVE_COMMAND]:
        return None
    parser = argparse.ArgumentParser(prog=f'ccb {MOBILE_HOST_SERVE_COMMAND}', add_help=False)
    parser.add_argument('--listen', required=True)
    parser.add_argument('--public-url', default=None)
    parser.add_argument('--route-provider', default='tailnet')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--generation', type=int, required=True)
    parser.add_argument('--host-id', default=None)
    parser.add_argument('--rotate-pairing', action='store_true')
    namespace = parser.parse_args(tokens[1:])
    return run_mobile_host_serve_command(namespace, script_root=script_root)


def run_mobile_host_serve_command(args, *, script_root: Path) -> int:
    del script_root
    state_dir = Path(args.state_dir).expanduser()
    os.environ['CCB_MOBILE_HOST_STATE_HOME'] = str(state_dir)
    route_provider = str(args.route_provider or 'tailnet')
    relay_credentials = None
    effective_host_id = str(args.host_id or '').strip() or None
    if route_provider == 'relay':
        relay_credentials = load_relay_host_credentials(
            Path(
                str(os.environ.get('CCB_RELAY_HOST_CREDENTIALS') or '').strip()
                or state_dir / 'relay-host-credentials.json'
            )
        )
        if effective_host_id and effective_host_id != relay_credentials.host_id:
            raise MobileHostServiceError(
                'mobile relay host id does not match activated relay credentials'
            )
        effective_host_id = relay_credentials.host_id
    handle = prepare_server_mobile_gateway(
        SimpleNamespace(
            listen=str(args.listen),
            public_url=str(args.public_url).strip() if args.public_url else None,
            route_provider=route_provider,
        ),
        host_id=effective_host_id,
        rotate_pairing=bool(getattr(args, 'rotate_pairing', False)),
    )
    summary = dict(handle.summary)
    paths = mobile_host_service_paths(state_dir)
    generation = int(args.generation)
    relay_runtime = None
    if relay_credentials is not None:
        relay_runtime = RelayHostConnectorRuntime(
            credentials=relay_credentials,
            gateway_origin=str(
                summary.get('local_gateway_url')
                or _local_gateway_url(str(args.listen))
            ),
        )
        relay_runtime.start()
    try:
        pairing = summary.get('pairing') if isinstance(summary.get('pairing'), dict) else None
        write_mobile_host_service_state(
            paths.state_path,
            {
                'schema_version': MOBILE_HOST_SERVICE_SCHEMA_VERSION,
                'record_type': MOBILE_HOST_SERVICE_RECORD_TYPE,
                'pid': os.getpid(),
                'process_group_id': os.getpgrp() if hasattr(os, 'getpgrp') else os.getpid(),
                'generation': generation,
                'host_id': str(summary.get('host_id') or args.host_id or ''),
                'listen': str(summary.get('listen') or args.listen),
                'local_gateway_url': str(summary.get('local_gateway_url') or _local_gateway_url(str(args.listen))),
                'gateway_url': str(summary.get('gateway_url') or summary.get('local_gateway_url') or ''),
                'route_provider': str(summary.get('route_provider') or args.route_provider or 'tailnet'),
                **(
                    {'public_url': str(args.public_url).strip()}
                    if args.public_url
                    else {}
                ),
                **(
                    {'relay_mode': relay_credentials.relay_mode}
                    if relay_credentials is not None
                    else {}
                ),
                **(
                    {'relay_outbound': relay_runtime.diagnostics()}
                    if relay_runtime is not None
                    else {}
                ),
                **({'pairing': dict(pairing)} if pairing is not None else {}),
                'state_dir': str(paths.state_dir),
                'started_at': utc_now(),
                'command_kind': 'ccb_mobile_host_serve',
                'entrypoint': sys.argv[0] if sys.argv else '',
            },
        )
    except Exception as exc:
        print(f'mobile host service could not write state: {type(exc).__name__}: {exc}', file=sys.stderr)
        if relay_runtime is not None:
            relay_runtime.stop()
        handle.close()
        return 1
    previous_sigterm_handler = _install_mobile_host_sigterm_handler()
    exit_code = 0
    try:
        try:
            handle.serve_forever()
        except _MobileHostShutdown:
            pass
        except KeyboardInterrupt:
            exit_code = 130
    finally:
        _restore_mobile_host_sigterm_handler(previous_sigterm_handler)
        _remove_mobile_host_service_state_if_current(
            paths.state_path,
            pid=os.getpid(),
            generation=generation,
        )
        if relay_runtime is not None:
            relay_runtime.stop()
        handle.close()
    return exit_code


def _install_mobile_host_sigterm_handler():
    try:
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _raise_mobile_host_shutdown)
    except (AttributeError, ValueError):
        return None
    return previous


def _restore_mobile_host_sigterm_handler(previous) -> None:
    if previous is None:
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except (AttributeError, ValueError):
        pass


def _raise_mobile_host_shutdown(_signum, _frame) -> None:
    raise _MobileHostShutdown()


def mobile_host_service_paths(state_dir: Path | None = None) -> MobileHostServicePaths:
    root = Path(state_dir or mobile_host_state_dir()).expanduser()
    return MobileHostServicePaths(
        state_dir=root,
        state_path=root / 'service.json',
        lock_path=root / 'service.lock',
        log_path=root / 'service.log',
    )


def read_mobile_host_service_state(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise MobileHostServiceError(f'invalid mobile host service state: {path}') from exc
    if not isinstance(payload, dict):
        raise MobileHostServiceError(f'invalid mobile host service state: {path}')
    if payload.get('record_type') != MOBILE_HOST_SERVICE_RECORD_TYPE:
        raise MobileHostServiceError(f'unexpected mobile host service record: {path}')
    return payload


def write_mobile_host_service_state(path: Path, payload: dict[str, object]) -> None:
    _ensure_private_mobile_host_state_dir(Path(path).parent)
    atomic_write_json(Path(path), payload)


def remove_mobile_host_service_state(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def detect_loopback_port_owner(listen: str) -> PortOwner | None:
    try:
        parsed = parse_listen_address(listen, allow_lan=True)
    except ValueError as exc:
        raise MobileHostServiceError(str(exc)) from exc
    host, port = parsed.host, parsed.port
    if port <= 0:
        raise MobileHostServiceError(
            'mobile gateway listen port must be between 1 and 65535'
        )
    owner = _detect_loopback_port_owner_netstat(host=host, port=port)
    if owner is not None:
        return owner
    owner = _detect_loopback_port_owner_ss(host=host, port=port)
    if owner is not None:
        return owner
    owner = _detect_loopback_port_owner_lsof(host=host, port=port)
    if owner is not None:
        return owner
    if _loopback_port_accepts_connection(host=host, port=port):
        return PortOwner(pid=0, command='unknown loopback listener')
    return None


def _detect_loopback_port_owner_netstat(*, host: str, port: int) -> PortOwner | None:
    if os.name != 'nt':
        return None
    try:
        result = subprocess.run(
            ['netstat', '-ano', '-p', 'tcp'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or '').splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != 'TCP':
            continue
        local_address = fields[1]
        state = fields[3].upper()
        if state != 'LISTENING':
            continue
        if not _listen_address_matches(local_address, host=host, port=port):
            continue
        try:
            pid = int(fields[4])
        except ValueError:
            return PortOwner(pid=0, command='unknown loopback listener')
        return PortOwner(pid=pid, command=_process_cmdline(pid))
    return None


def _detect_loopback_port_owner_ss(*, host: str, port: int) -> PortOwner | None:
    if shutil.which('ss') is None:
        return None
    try:
        result = subprocess.run(
            ['ss', '-ltnp', f'sport = :{port}'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or '').splitlines():
        if f':{port}' not in line:
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        local_address = fields[3]
        if not _listen_address_matches(local_address, host=host, port=port):
            continue
        pid = _pid_from_ss_line(line)
        if pid is None:
            return None
        return PortOwner(pid=pid, command=_process_cmdline(pid))
    return None


def _detect_loopback_port_owner_lsof(*, host: str, port: int) -> PortOwner | None:
    if shutil.which('lsof') is None:
        return None
    try:
        result = subprocess.run(
            ['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or '').splitlines()[1:]:
        if f':{port}' not in line or 'TCP ' not in line:
            continue
        tcp_name = line.split('TCP ', 1)[1]
        if not _lsof_tcp_name_matches(tcp_name, host=host, port=port):
            continue
        fields = line.split(None, 2)
        if len(fields) < 2:
            return PortOwner(pid=0, command='unknown loopback listener')
        try:
            pid = int(fields[1])
        except ValueError:
            return PortOwner(pid=0, command='unknown loopback listener')
        return PortOwner(pid=pid, command=_process_cmdline(pid) or fields[0])
    return None


def _loopback_port_accepts_connection(*, host: str, port: int) -> bool:
    connect_host = '127.0.0.1' if host == 'localhost' else host
    try:
        with socket.create_connection((connect_host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _spawn_mobile_host_service(
    *,
    script_root: Path,
    paths: MobileHostServicePaths,
    listen: str,
    public_url: str | None,
    route_provider: str,
    generation: int,
    host_id: str | None,
    spawn_fn: Callable[..., object] | None,
    rotate_pairing: bool = False,
) -> object:
    command = [
        sys.executable,
        str(Path(script_root) / 'ccb.py'),
        MOBILE_HOST_SERVE_COMMAND,
        '--listen',
        listen,
        '--route-provider',
        route_provider,
        '--state-dir',
        str(paths.state_dir),
        '--generation',
        str(generation),
    ]
    if public_url:
        command.extend(['--public-url', public_url])
    if host_id:
        command.extend(['--host-id', host_id])
    if rotate_pairing:
        command.append('--rotate-pairing')
    _ensure_private_mobile_host_state_dir(paths.state_dir)
    env = dict(os.environ)
    env['CCB_MOBILE_HOST_STATE_HOME'] = str(paths.state_dir)
    env['CCB_SKIP_STARTUP_UPDATE_CHECK'] = '1'
    env['CCB_SOURCE_RUNTIME_OK'] = '1'
    log = paths.log_path.open('ab')
    try:
        spawner = spawn_fn or subprocess.Popen
        process = spawner(
            command,
            cwd=str(Path.cwd()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        return process
    except Exception:
        raise
    finally:
        log.close()


def _wait_for_mobile_host_health(
    local_gateway_url: str,
    *,
    process: object,
    log_path: Path,
    health_check_fn: Callable[[str], bool] | None,
    timeout_s: float,
    sleep_fn: Callable[[float], None],
    monotonic_fn: Callable[[], float],
) -> None:
    deadline = monotonic_fn() + max(0.0, timeout_s)
    checker = health_check_fn or _http_health_check
    while monotonic_fn() < deadline:
        poll = getattr(process, 'poll', None)
        if callable(poll):
            exit_code = poll()
            if exit_code is not None:
                detail = f'mobile host service exited before becoming healthy: exit_code={exit_code}'
                log_tail = _mobile_host_log_tail(log_path)
                if log_tail:
                    detail += f'; log_tail={log_tail}'
                raise MobileHostServiceError(detail)
        if checker(local_gateway_url):
            return
        sleep_fn(0.1)
    raise MobileHostServiceError(f'mobile host service did not become healthy: {local_gateway_url}/v1/health')


def _wait_for_mobile_host_state_ready(
    paths: MobileHostServicePaths,
    *,
    process: object,
    pid: int,
    generation: int,
    timeout_s: float,
    sleep_fn: Callable[[float], None],
    monotonic_fn: Callable[[], float],
) -> dict[str, object]:
    deadline = monotonic_fn() + max(0.0, timeout_s)
    last_state: dict[str, object] | None = None
    while monotonic_fn() < deadline:
        poll = getattr(process, 'poll', None)
        if callable(poll):
            exit_code = poll()
            if exit_code is not None:
                detail = (
                    'mobile host service exited before publishing pairing state: '
                    f'exit_code={exit_code}'
                )
                log_tail = _mobile_host_log_tail(paths.log_path)
                if log_tail:
                    detail += f'; log_tail={log_tail}'
                raise MobileHostServiceError(detail)
        state = _matching_spawned_state(
            read_mobile_host_service_state(paths.state_path),
            pid=pid,
            generation=generation,
        )
        if state is not None:
            last_state = state
            if _state_pairing(state) is not None:
                return state
        sleep_fn(0.05)
    if last_state is not None:
        raise MobileHostServiceError(
            f'mobile host service did not publish pairing state: {paths.state_path}'
        )
    raise MobileHostServiceError(
        f'mobile host service did not publish matching state: {paths.state_path}'
    )


def _mobile_host_log_tail(path: Path, *, max_chars: int = 1200) -> str:
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    text = text.strip()
    if not text:
        return ''
    if len(text) > max_chars:
        text = text[-max_chars:]
    return ' '.join(text.split())


def _http_health_check(local_gateway_url: str) -> bool:
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(
            f'{local_gateway_url}/v1/health',
            timeout=MOBILE_HOST_HEALTH_REQUEST_TIMEOUT_S,
        ) as response:
            return 200 <= int(response.status) < 300
    except (OSError, URLError):
        return False


def _terminate_managed_mobile_host(pid: int, *, terminate_pid_tree_fn: Callable[..., bool]) -> None:
    terminate_pid_tree_fn(pid, timeout_s=MOBILE_HOST_STOP_TIMEOUT_S)


def _managed_mobile_host_process(
    pid: int,
    state: dict[str, object] | None,
    *,
    script_root: Path,
    listen: str,
    state_dir: Path,
    process_cmdline_fn: Callable[[int], str] | None,
) -> bool:
    cmdline = (process_cmdline_fn or _process_cmdline)(pid)
    if MOBILE_HOST_SERVE_COMMAND in cmdline:
        if str(state_dir) in cmdline:
            return True
        return bool(
            state is not None
            and str(state.get('command_kind') or '') == 'ccb_mobile_host_serve'
            and _state_matches_mobile_host_state_dir(state, state_dir=state_dir)
        )
    return _legacy_mobile_gateway_process(
        cmdline,
        script_root=script_root,
        listen=listen,
    )


def _legacy_mobile_gateway_process(
    cmdline: str,
    *,
    script_root: Path,
    listen: str,
) -> bool:
    """Recognize the pre-service foreground mobile gateway for safe takeover."""
    try:
        tokens = shlex.split(cmdline, posix=os.name != 'nt')
    except ValueError:
        return False
    expected_script = str((Path(script_root) / 'ccb.py').expanduser().resolve())
    script_index = next(
        (
            index
            for index, token in enumerate(tokens)
            if token.endswith('ccb.py') and str(Path(token).expanduser().resolve()) == expected_script
        ),
        None,
    )
    if script_index is None or tokens[script_index + 1 : script_index + 3] not in (
        ['install', 'mobile'],
        ['update', 'mobile'],
    ):
        return False
    for index, token in enumerate(tokens[script_index + 3 :], start=script_index + 3):
        if token == '--listen':
            return index + 1 < len(tokens) and tokens[index + 1] == listen
        if token.startswith('--listen='):
            return token.split('=', 1)[1] == listen
    return False


def _process_cmdline(pid: int) -> str:
    if pid <= 0:
        return ''
    if os.name == 'nt':
        try:
            command = [
                'powershell',
                '-NoProfile',
                '-Command',
                (
                    'Get-CimInstance Win32_Process -Filter '
                    f'"ProcessId = {pid}" | Select-Object -ExpandProperty CommandLine'
                ),
            ]
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
            )
        except Exception:
            return ''
        if result.returncode != 0:
            return ''
        return (result.stdout or '').strip()
    proc_cmdline = Path('/proc') / str(pid) / 'cmdline'
    try:
        text = proc_cmdline.read_bytes().replace(b'\x00', b' ').decode('utf-8', errors='replace').strip()
        if text:
            return text
    except Exception:
        pass
    try:
        result = subprocess.run(
            ['ps', '-p', str(pid), '-o', 'command='],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        )
    except Exception:
        return ''
    return (result.stdout or '').strip()


def _mobile_host_process_uses_script_root(
    pid: int,
    state: dict[str, object] | None,
    *,
    script_root: Path,
    process_cmdline_fn: Callable[[int], str] | None,
) -> bool:
    expected_entrypoint = (Path(script_root).expanduser() / 'ccb.py').resolve()
    recorded_entrypoint = str((state or {}).get('entrypoint') or '').strip()
    if recorded_entrypoint:
        try:
            return Path(recorded_entrypoint).expanduser().resolve() == expected_entrypoint
        except OSError:
            return False

    cmdline = (process_cmdline_fn or _process_cmdline)(pid)
    try:
        tokens = shlex.split(cmdline, posix=os.name != 'nt')
    except ValueError:
        return False
    for token in tokens:
        if not token.endswith('ccb.py'):
            continue
        try:
            return Path(token).expanduser().resolve() == expected_entrypoint
        except OSError:
            return False
    return False


def _acquire_mobile_host_lock(
    lock_path: Path,
    *,
    process_exists_fn: Callable[[int], bool],
    timeout_s: float,
    sleep_fn: Callable[[float], None],
    monotonic_fn: Callable[[], float],
) -> int | None:
    _ensure_private_mobile_host_state_dir(lock_path.parent)
    deadline = monotonic_fn() + max(0.0, float(timeout_s))
    while True:
        _clear_stale_lock(lock_path, process_exists_fn=process_exists_fn)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            now = monotonic_fn()
            if now >= deadline:
                return None
            sleep_for = min(MOBILE_HOST_LOCK_RETRY_INTERVAL_S, max(0.0, deadline - now))
            if sleep_for <= 0:
                return None
            sleep_fn(sleep_for)
            continue
        break
    payload = {'pid': os.getpid(), 'created_at': utc_now()}
    os.write(fd, (json.dumps(payload) + '\n').encode('utf-8'))
    return fd


def _release_mobile_host_lock(lock_path: Path, fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _ensure_private_mobile_host_state_dir(path: Path) -> None:
    Path(path).mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != 'nt':
        try:
            Path(path).chmod(0o700)
        except OSError:
            pass


def _clear_stale_lock(lock_path: Path, *, process_exists_fn: Callable[[int], bool]) -> None:
    try:
        payload = json.loads(lock_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return
    except Exception:
        return
    pid = _coerce_positive_int(payload.get('pid') if isinstance(payload, dict) else None)
    try:
        age_s = max(0.0, time.time() - lock_path.stat().st_mtime)
    except OSError:
        return
    if (pid is not None and not process_exists_fn(pid)) or age_s > MOBILE_HOST_LOCK_TTL_S:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
    sleep_fn: Callable[[float], None],
    monotonic_fn: Callable[[], float],
) -> bool:
    deadline = monotonic_fn() + max(0.0, timeout_s)
    while monotonic_fn() < deadline:
        if predicate():
            return True
        sleep_fn(0.05)
    return predicate()


def _next_generation(state: dict[str, object] | None) -> int:
    if state is None:
        return 1
    return max(0, int(state.get('generation') or 0)) + 1


def _state_pid(state: dict[str, object] | None) -> int | None:
    if state is None:
        return None
    return _coerce_positive_int(state.get('pid'))


def _state_generation(state: dict[str, object] | None, fallback: int) -> int:
    if state is None:
        return int(fallback)
    try:
        return int(state.get('generation') or fallback)
    except (TypeError, ValueError):
        return int(fallback)


def _coerce_positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _state_matches_mobile_host_state_dir(state: dict[str, object], *, state_dir: Path) -> bool:
    recorded = str(state.get('state_dir') or '').strip()
    if not recorded:
        return False
    try:
        return str(Path(recorded).expanduser()) == str(state_dir)
    except Exception:
        return False


def _mobile_host_state_matches_request(
    state: dict[str, object] | None,
    *,
    listen: str,
    public_url: str | None,
    route_provider: str,
) -> bool:
    if state is None:
        return False
    if _state_listen(state, listen='') != str(listen):
        return False
    expected_gateway_url = str(public_url or _local_gateway_url(listen))
    if _state_gateway_url(state, fallback='') != expected_gateway_url:
        return False
    if _state_route_provider(state, fallback='') != str(route_provider or 'tailnet'):
        return False
    return _state_pairing(state) is not None


def _mobile_host_state_is_healthy(
    state: dict[str, object] | None,
    *,
    listen: str,
    health_check_fn: Callable[[str], bool] | None,
) -> bool:
    checker = health_check_fn or _http_health_check
    try:
        return bool(checker(_state_local_gateway_url(state, listen=listen)))
    except Exception:
        return False


def _mobile_host_listen_owned_by_process(
    listen: str,
    pid: int,
    *,
    port_owner_fn: Callable[[str], PortOwner | None] | None,
) -> bool:
    owner = (port_owner_fn or detect_loopback_port_owner)(listen)
    return owner is not None and owner.pid == pid


def _mobile_host_state_with_rotated_pairing(
    state: dict[str, object] | None,
    *,
    paths: MobileHostServicePaths,
) -> dict[str, object] | None:
    pairing = _state_pairing(state)
    if pairing is None:
        return None
    store = MobileGatewayPairingStore(paths.state_dir)
    refreshed = _rotate_mobile_host_pairing(state, pairing=pairing, store=store)
    if refreshed is None:
        return None
    if _state_route_provider(state, fallback='') == 'relay':
        credentials_path = Path(
            str(os.environ.get('CCB_RELAY_HOST_CREDENTIALS') or '').strip()
            or paths.state_dir / 'relay-host-credentials.json'
        )
        refreshed = build_relay_pairing_payload(
            refreshed,
            credentials=load_relay_host_credentials(credentials_path),
        )
    updated = dict(state or {})
    updated['pairing'] = refreshed
    updated.pop('pairing_diagnostic', None)
    write_mobile_host_service_state(paths.state_path, updated)
    return updated


def _mobile_host_state_with_pairing_diagnostic(
    state: dict[str, object] | None,
    *,
    paths: MobileHostServicePaths,
) -> dict[str, object] | None:
    pairing = _state_pairing(state)
    if pairing is None:
        return state
    claimable = MobileGatewayPairingStore(paths.state_dir).pairing_code_is_claimable(
        str(pairing.get('pairing_code') or '')
    )
    updated = dict(state or {})
    if claimable:
        updated.pop('pairing_diagnostic', None)
    else:
        updated['pairing_diagnostic'] = (
            'Pairing handoff is no longer claimable; run `ccb update mobile` to rotate it.'
        )
    if updated != state:
        write_mobile_host_service_state(paths.state_path, updated)
    return updated


def _rotate_mobile_host_pairing(
    state: dict[str, object] | None,
    *,
    pairing: Mapping[str, object],
    store: MobileGatewayPairingStore,
) -> dict[str, object] | None:
    project_id = _state_project_id(state, pairing=pairing)
    gateway_url = _state_gateway_url(state, fallback=str(pairing.get('gateway_url') or ''))
    route_provider = _state_route_provider(state, fallback=str(pairing.get('route_provider') or 'tailnet'))
    scopes = _pairing_scopes(pairing)
    if not project_id or not gateway_url or not route_provider or not scopes:
        return None
    old_pairing_id = str(pairing.get('pairing_id') or '').strip()
    if old_pairing_id:
        # Covers pre-handoff-state gateways during the one-way migration.
        store.revoke_pairing(old_pairing_id, reason='manual_handoff_rotation')
    return store.rotate_reusable_pairing_payload(
        project_id=project_id,
        gateway_url=gateway_url,
        route_provider=route_provider,
        scopes=scopes,
    )


def _state_project_id(state: dict[str, object] | None, *, pairing: Mapping[str, object]) -> str:
    for value in (
        (state or {}).get('project_id'),
        (state or {}).get('host_id'),
        pairing.get('project_id'),
    ):
        text = str(value or '').strip()
        if text:
            return text
    return ''


def _pairing_scopes(pairing: Mapping[str, object]) -> tuple[str, ...]:
    raw = pairing.get('scopes')
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Iterable):
        values = [str(item) for item in raw]
    else:
        values = []
    return tuple(sorted({value.strip() for value in values if value.strip()}))


def _matching_spawned_state(
    state: dict[str, object] | None,
    *,
    pid: int,
    generation: int,
) -> dict[str, object] | None:
    if _state_pid(state) != pid:
        return None
    if _state_generation(state, generation) != int(generation):
        return None
    return state


def _mobile_host_result_from_state(
    state: dict[str, object] | None,
    *,
    paths: MobileHostServicePaths,
    status: str,
) -> MobileHostServiceResult:
    if state is None:
        raise MobileHostServiceError(f'missing mobile host service state: {paths.state_path}')
    listen = _state_listen(state, listen='')
    pairing_diagnostic = str((state or {}).get('pairing_diagnostic') or '').strip() or None
    return MobileHostServiceResult(
        status=status,
        pid=_state_pid(state) or 0,
        generation=_state_generation(state, 0),
        listen=listen,
        gateway_url=_state_gateway_url(state, fallback=_state_local_gateway_url(state, listen=listen)),
        local_gateway_url=_state_local_gateway_url(state, listen=listen),
        route_provider=_state_route_provider(state, fallback='tailnet'),
        state_dir=paths.state_dir,
        state_path=paths.state_path,
        log_path=paths.log_path,
        pairing=None if pairing_diagnostic else _state_pairing(state),
        pairing_diagnostic=pairing_diagnostic,
    )


def _state_listen(state: dict[str, object] | None, *, listen: str) -> str:
    value = str((state or {}).get('listen') or '').strip()
    return value or str(listen)


def _state_gateway_url(state: dict[str, object] | None, *, fallback: str) -> str:
    value = str((state or {}).get('gateway_url') or '').strip()
    return value or str(fallback)


def _state_local_gateway_url(state: dict[str, object] | None, *, listen: str) -> str:
    value = str((state or {}).get('local_gateway_url') or '').strip()
    return value or _local_gateway_url(listen)


def _state_route_provider(state: dict[str, object] | None, *, fallback: str) -> str:
    value = str((state or {}).get('route_provider') or '').strip()
    return value or str(fallback)


def _state_restart_public_url(
    state: dict[str, object] | None,
    *,
    route_provider: str,
) -> str | None:
    if str(route_provider).strip().lower() == 'relay':
        return None
    recorded = str((state or {}).get('public_url') or '').strip()
    if recorded:
        return recorded
    gateway_url = _state_gateway_url(state, fallback='')
    local_gateway_url = _state_local_gateway_url(
        state,
        listen=_state_listen(state, listen=''),
    )
    if gateway_url and gateway_url.rstrip('/') != local_gateway_url.rstrip('/'):
        return gateway_url
    return None


def _state_pairing(state: dict[str, object] | None) -> dict[str, object] | None:
    pairing = (state or {}).get('pairing')
    if not isinstance(pairing, dict):
        return None
    required = ('pairing_code', 'claim_endpoint', 'gateway_url', 'route_provider')
    if any(not str(pairing.get(key) or '').strip() for key in required):
        return None
    return dict(pairing)


def _remove_mobile_host_service_state_if_current(path: Path, *, pid: int, generation: int) -> None:
    try:
        state = read_mobile_host_service_state(path)
    except MobileHostServiceError:
        return
    if _state_pid(state) != pid:
        return
    try:
        state_generation = int((state or {}).get('generation') or 0)
    except (TypeError, ValueError):
        return
    if state_generation == generation:
        remove_mobile_host_service_state(path)


def _split_listen(listen: str) -> tuple[str, int]:
    text = str(listen or '').strip()
    if ':' not in text:
        raise MobileHostServiceError('mobile gateway listen must be host:port')
    host, port_text = text.rsplit(':', 1)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise MobileHostServiceError('mobile gateway listen port must be an integer') from exc
    if port <= 0 or port > 65535:
        raise MobileHostServiceError('mobile gateway listen port must be between 1 and 65535')
    return host, port


def _local_gateway_url(listen: str) -> str:
    host, port = _split_listen(listen)
    return f'http://{host}:{port}'


def _listen_address_matches(value: str, *, host: str, port: int) -> bool:
    normalized = value.strip()
    if not normalized.endswith(f':{port}'):
        return False
    bind_host = normalized.rsplit(':', 1)[0]
    if bind_host in {host, f'[{host}]'}:
        return True
    if bind_host in {'*', '0.0.0.0', '[::]', '::'}:
        return True
    if host == 'localhost':
        return bind_host in {'127.0.0.1', '[::1]', '::1'}
    return False


def _lsof_tcp_name_matches(value: str, *, host: str, port: int) -> bool:
    local_name = value.split(None, 1)[0].strip()
    return _listen_address_matches(local_name, host=host, port=port)


def _pid_from_ss_line(line: str) -> int | None:
    marker = 'pid='
    if marker not in line:
        return None
    suffix = line.split(marker, 1)[1]
    digits = []
    for ch in suffix:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    if not digits:
        return None
    return int(''.join(digits))


__all__ = [
    'MOBILE_HOST_SERVE_COMMAND',
    'MobileHostServiceError',
    'MobileHostServiceResult',
    'PortOwner',
    'detect_loopback_port_owner',
    'maybe_handle_mobile_host_serve_command',
    'mobile_host_service_paths',
    'read_mobile_host_service_state',
    'remove_mobile_host_service_state',
    'restart_running_mobile_host_service',
    'run_mobile_host_serve_command',
    'start_or_replace_mobile_host_service',
    'write_mobile_host_service_state',
]
