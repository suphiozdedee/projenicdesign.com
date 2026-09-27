from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable


_NODE_KEYTAR_READ_SCRIPT = r"""
const modulePath = process.argv[1];
const service = process.argv[2];
const account = process.argv[3];
Promise.resolve()
  .then(() => {
    const loaded = require(modulePath);
    const keytar = loaded && loaded.default ? loaded.default : loaded;
    return keytar.getPassword(service, account);
  })
  .then((value) => {
    if (typeof value === "string" && value.length > 0) {
      process.stdout.write(value);
    }
  })
  .catch(() => process.exit(2));
"""


@dataclass(frozen=True)
class KeyringReadResult:
    status: str
    value: str | None = None
    detail: str = ''


def read_keyring_password(
    service: str,
    account: str,
    *,
    command_name: str,
    module_names: tuple[str, ...] = ('@github/keytar', 'keytar'),
    extra_module_paths: Iterable[Path] = (),
    timeout: float = 5.0,
) -> str | None:
    """Read one external credential without ever opening a write API."""
    result = read_keyring_password_state(
        service,
        account,
        command_name=command_name,
        module_names=module_names,
        extra_module_paths=extra_module_paths,
        timeout=timeout,
    )
    return result.value if result.status == 'present' else None


def read_keyring_password_state(
    service: str,
    account: str,
    *,
    command_name: str,
    module_names: tuple[str, ...] = ('@github/keytar', 'keytar'),
    extra_module_paths: Iterable[Path] = (),
    timeout: float = 5.0,
) -> KeyringReadResult:
    """Read one credential while preserving absence/error classification."""
    service_name = str(service or '').strip()
    account_name = str(account or '').strip()
    if not service_name or not account_name:
        return KeyringReadResult('unavailable', detail='service or account is missing')
    if platform.system() == 'Darwin':
        return _read_macos_keychain_state(
            service_name,
            account_name,
            timeout=timeout,
        )
    module_path = _find_keytar_module(
        command_name,
        module_names=module_names,
        extra_module_paths=extra_module_paths,
    )
    if module_path is not None:
        return _read_node_keytar_state(
            module_path,
            service_name,
            account_name,
            timeout=timeout,
        )
    if platform.system() == 'Linux':
        return _read_linux_secret_tool_state(
            service_name,
            account_name,
            timeout=timeout,
        )
    return KeyringReadResult('unavailable', detail='no supported credential reader is available')


def _read_macos_keychain(service: str, account: str, *, timeout: float) -> str | None:
    result = _read_macos_keychain_state(service, account, timeout=timeout)
    return result.value if result.status == 'present' else None


def _read_macos_keychain_state(
    service: str,
    account: str,
    *,
    timeout: float,
) -> KeyringReadResult:
    security = shutil.which('security') or '/usr/bin/security'
    try:
        result = subprocess.run(
            [
                security,
                'find-generic-password',
                '-s',
                service,
                '-a',
                account,
                '-w',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return KeyringReadResult('error', detail=f'{type(exc).__name__}: {exc}')
    value = result.stdout.rstrip('\r\n')
    if result.returncode == 0 and value:
        return KeyringReadResult('present', value=value)
    if result.returncode == 44:
        return KeyringReadResult('absent')
    if result.returncode == 0:
        return KeyringReadResult('absent')
    return KeyringReadResult('error', detail=f'security exited {result.returncode}')


def _read_node_keytar(
    module_path: Path,
    service: str,
    account: str,
    *,
    timeout: float,
) -> str | None:
    result = _read_node_keytar_state(
        module_path,
        service,
        account,
        timeout=timeout,
    )
    return result.value if result.status == 'present' else None


def _read_node_keytar_state(
    module_path: Path,
    service: str,
    account: str,
    *,
    timeout: float,
) -> KeyringReadResult:
    node = shutil.which('node')
    if not node:
        return KeyringReadResult('unavailable', detail='node is unavailable')
    try:
        result = subprocess.run(
            [
                node,
                '-e',
                _NODE_KEYTAR_READ_SCRIPT,
                str(module_path),
                service,
                account,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(os.environ),
        )
    except Exception as exc:
        return KeyringReadResult('error', detail=f'{type(exc).__name__}: {exc}')
    if result.returncode == 0 and result.stdout:
        return KeyringReadResult('present', value=result.stdout)
    if result.returncode == 0:
        return KeyringReadResult('absent')
    return KeyringReadResult('error', detail=f'keytar reader exited {result.returncode}')


def _read_linux_secret_tool(service: str, account: str, *, timeout: float) -> str | None:
    result = _read_linux_secret_tool_state(service, account, timeout=timeout)
    return result.value if result.status == 'present' else None


def _read_linux_secret_tool_state(
    service: str,
    account: str,
    *,
    timeout: float,
) -> KeyringReadResult:
    secret_tool = shutil.which('secret-tool')
    if not secret_tool:
        return KeyringReadResult('unavailable', detail='secret-tool is unavailable')
    try:
        result = subprocess.run(
            [
                secret_tool,
                'lookup',
                'service',
                service,
                'account',
                account,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return KeyringReadResult('error', detail=f'{type(exc).__name__}: {exc}')
    value = result.stdout.rstrip('\r\n')
    if result.returncode == 0 and value:
        return KeyringReadResult('present', value=value)
    if result.returncode in {0, 1}:
        return KeyringReadResult('absent')
    return KeyringReadResult('error', detail=f'secret-tool exited {result.returncode}')


def _find_keytar_module(
    command_name: str,
    *,
    module_names: tuple[str, ...],
    extra_module_paths: Iterable[Path],
) -> Path | None:
    for raw in extra_module_paths:
        candidate = Path(raw).expanduser()
        if candidate.is_file() or (candidate.is_dir() and (candidate / 'package.json').is_file()):
            return candidate
    command = shutil.which(str(command_name or '').strip())
    if not command:
        return None
    try:
        executable = Path(command).resolve()
    except Exception:
        executable = Path(command).expanduser()
    for root in (executable.parent, *executable.parents):
        for module_name in module_names:
            relative = Path(*module_name.split('/'))
            for candidate in (
                root / 'node_modules' / relative,
                root / relative,
            ):
                if candidate.is_file() or (
                    candidate.is_dir()
                    and (candidate / 'package.json').is_file()
                ):
                    return candidate
    return None


__all__ = ['KeyringReadResult', 'read_keyring_password', 'read_keyring_password_state']
