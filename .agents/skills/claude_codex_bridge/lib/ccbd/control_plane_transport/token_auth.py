from __future__ import annotations

from dataclasses import dataclass
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

_AUTH_MARKER = 'ccbd-control-plane-token-v1'
_AUTH_ACK_MARKER = 'ccbd-control-plane-token-ack-v1'
_MAX_AUTH_LINE_BYTES = 4096


class RpcTransportAuthError(OSError):
    def __init__(self, category: str, detail: str | None = None) -> None:
        self.category = str(category or 'handshake-failed')
        super().__init__(detail or self.category)


@dataclass(frozen=True)
class TokenFile:
    token_ref: str
    token: str
    generation: str
    acl_status: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.token.encode('utf-8')).hexdigest()[:16]


def create_token_file(
    path: str | Path,
    *,
    command_runner=None,
    os_name: str | None = None,
) -> TokenFile:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    platform = os.name if os_name is None else os_name
    runner = command_runner or subprocess.run
    generation = secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    payload = {
        'schema': _AUTH_MARKER,
        'generation': generation,
        'token': token,
    }
    try:
        if platform == 'nt':
            user = _current_windows_user()
            if not user:
                raise RpcTransportAuthError('token-unprotectable', 'current Windows user is unavailable')
            _write_windows_token_file_secure(target, payload, current_user=user, command_runner=runner)
        else:
            target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
        acl_status = converge_token_acl(target, command_runner=runner, os_name=platform)
    except Exception:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    return TokenFile(
        token_ref=str(target),
        token=token,
        generation=generation,
        acl_status=acl_status,
    )


def load_token_file(path: str | Path) -> TokenFile:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise RpcTransportAuthError('token-missing', 'ccbd control-plane token is missing') from exc
    except PermissionError as exc:
        raise RpcTransportAuthError('token-unreadable', 'ccbd control-plane token is unreadable') from exc
    if not isinstance(payload, dict) or payload.get('schema') != _AUTH_MARKER:
        raise RpcTransportAuthError('token-invalid', 'ccbd control-plane token file is invalid')
    token = str(payload.get('token') or '').strip()
    generation = str(payload.get('generation') or '').strip()
    if not token or not generation:
        raise RpcTransportAuthError('token-invalid', 'ccbd control-plane token file is incomplete')
    return TokenFile(
        token_ref=str(target),
        token=token,
        generation=generation,
        acl_status=str(payload.get('acl_status') or 'unknown'),
    )


def converge_token_acl(
    path: str | Path,
    *,
    command_runner=None,
    os_name: str | None = None,
) -> str:
    target = Path(path)
    platform = os.name if os_name is None else os_name
    if platform != 'nt':
        try:
            target.chmod(0o600)
        except OSError as exc:
            raise RpcTransportAuthError('token-unprotectable', str(exc)) from exc
        return 'posix-0600'
    user = _current_windows_user()
    if not user:
        raise RpcTransportAuthError('token-unprotectable', 'current Windows user is unavailable')
    runner = command_runner or subprocess.run
    commands = (
        ['icacls', str(target), '/inheritance:r'],
        ['icacls', str(target), '/grant:r', f'{user}:R'],
        [
            'icacls',
            str(target),
            '/remove:g',
            'Everyone',
            'Users',
            'Authenticated Users',
            'BUILTIN\\Administrators',
            'Administrators',
            'SYSTEM',
            'NT AUTHORITY\\SYSTEM',
            'OWNER RIGHTS',
        ],
    )
    for command in commands:
        _run_checked_command(runner, command)
    proof = _read_windows_acl_proof(target, command_runner=runner)
    current_sid = _current_windows_sid(runner)
    _assert_windows_acl_proof(
        proof,
        current_user=user,
        current_sid=current_sid,
    )
    return 'windows-icacls-user-read'


def client_authenticate(sock, token: str) -> None:
    _send_auth_line(sock, token)
    _recv_auth_ack(sock)


def server_authenticate(sock, expected_token: str, *, timeout_s: float | None = None) -> bytes:
    line, remainder = _recv_line(sock, timeout_s=timeout_s)
    payload = _decode_auth_payload(line)
    token = str(payload.get('token') or '')
    if payload.get('schema') != _AUTH_MARKER or not hmac.compare_digest(token, expected_token):
        raise RpcTransportAuthError('not-same-user')
    _send_auth_ack(sock)
    return remainder


def redacted_token_diagnostics(token_file: TokenFile) -> dict[str, str]:
    return {
        'token_ref': token_file.token_ref,
        'acl_status': token_file.acl_status,
        'fingerprint': token_file.fingerprint,
    }


def _send_auth_line(sock, token: str) -> None:
    payload = {
        'schema': _AUTH_MARKER,
        'token': token,
    }
    sock.sendall(json.dumps(payload, ensure_ascii=False).encode('utf-8') + b'\n')


def _send_auth_ack(sock) -> None:
    sock.sendall(json.dumps({'schema': _AUTH_ACK_MARKER, 'ok': True}, ensure_ascii=False).encode('utf-8') + b'\n')


def _recv_auth_ack(sock) -> None:
    try:
        line, remainder = _recv_line(sock)
    except (OSError, RpcTransportAuthError) as exc:
        raise RpcTransportAuthError('not-same-user', 'ccbd auth handshake was rejected') from exc
    payload = _decode_auth_payload(line)
    if payload.get('schema') != _AUTH_ACK_MARKER or payload.get('ok') is not True:
        raise RpcTransportAuthError('handshake-failed', 'ccbd auth ack is invalid')
    if remainder:
        raise RpcTransportAuthError('handshake-failed', 'ccbd auth ack contains unexpected data')


def _recv_line(sock, *, timeout_s: float | None = None) -> tuple[bytes, bytes]:
    raw = b''
    deadline = None
    original_timeout = None
    if timeout_s is not None:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        gettimeout = getattr(sock, 'gettimeout', None)
        if callable(gettimeout):
            original_timeout = gettimeout()
    try:
        while b'\n' not in raw:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RpcTransportAuthError('handshake-failed', 'auth handshake timed out')
                sock.settimeout(remaining)
            chunk = sock.recv(1024)
            if not chunk:
                raise RpcTransportAuthError('handshake-failed', 'empty auth handshake')
            raw += chunk
            if len(raw) > _MAX_AUTH_LINE_BYTES:
                raise RpcTransportAuthError('handshake-failed', 'auth handshake is too large')
        line, remainder = raw.split(b'\n', 1)
        return line, remainder
    finally:
        if deadline is not None:
            sock.settimeout(original_timeout)


def _write_windows_token_file_secure(
    target: Path,
    payload: dict,
    *,
    current_user: str,
    command_runner,
) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        + '$path = '
        + _powershell_literal(str(target))
        + "\n$payload = "
        + _powershell_literal(serialized)
        + "\n$user = "
        + _powershell_literal(current_user)
        + r"""
$identity = New-Object System.Security.Principal.NTAccount($user)
$security = New-Object System.Security.AccessControl.FileSecurity
$security.SetOwner($identity)
$security.SetAccessRuleProtection($true, $false)
$readRule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity, 'Read', 'Allow')
$writeRule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity, 'Write', 'Allow')
$security.AddAccessRule($readRule)
$security.AddAccessRule($writeRule)
$encoding = New-Object System.Text.UTF8Encoding($false)
$bytes = $encoding.GetBytes($payload + "`n")
$fs = New-Object System.IO.FileStream(
    $path,
    [System.IO.FileMode]::CreateNew,
    [System.Security.AccessControl.FileSystemRights]::Write,
    [System.IO.FileShare]::None,
    4096,
    [System.IO.FileOptions]::None,
    $security
)
try {
    $fs.Write($bytes, 0, $bytes.Length)
}
finally {
    $fs.Dispose()
}
"""
    )
    result = command_runner(
        ['powershell', '-NoProfile', '-Command', script],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    if int(getattr(result, 'returncode', 1) or 0) != 0:
        stderr = str(getattr(result, 'stderr', '') or '').strip()
        stdout = str(getattr(result, 'stdout', '') or '').strip()
        detail = stderr or stdout or 'unable to create protected Windows token file'
        raise RpcTransportAuthError('token-unprotectable', detail)


def _decode_auth_payload(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode('utf-8'))
    except Exception as exc:
        raise RpcTransportAuthError('handshake-failed', 'auth handshake is not JSON') from exc
    if not isinstance(payload, dict):
        raise RpcTransportAuthError('handshake-failed', 'auth handshake must be an object')
    return payload


def _sp_run_win(*args, **kwargs):
    """``subprocess.run`` with ``CREATE_NO_WINDOW`` on Windows to prevent console flashes."""
    if sys.platform == 'win32':
        kwargs.setdefault('creationflags', getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))
    return subprocess.run(*args, **kwargs)


def _current_windows_user() -> str:
    if os.name == 'nt':
        try:
            result = _sp_run_win(
                ['whoami'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=2.0,
            )
        except Exception:
            result = None
        if result is not None and int(getattr(result, 'returncode', 1) or 0) == 0:
            value = str(getattr(result, 'stdout', '') or '').strip()
            if value:
                return value
    username = str(os.environ.get('USERNAME') or '').strip()
    if not username:
        try:
            username = str(getpass.getuser() or '').strip()
        except OSError:
            username = ''
    domain = str(os.environ.get('USERDOMAIN') or '').strip()
    if domain and username and '\\' not in username:
        return f'{domain}\\{username}'
    return username


def _current_windows_sid(command_runner) -> str:
    result = command_runner(
        [
            'powershell',
            '-NoProfile',
            '-Command',
            '([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value',
        ],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    if int(getattr(result, 'returncode', 1) or 0) != 0:
        stderr = str(getattr(result, 'stderr', '') or '').strip()
        stdout = str(getattr(result, 'stdout', '') or '').strip()
        detail = stderr or stdout or 'unable to read current Windows SID'
        raise RpcTransportAuthError('token-unprotectable', detail)
    raw = str(getattr(result, 'stdout', '') or '').strip()
    sid = raw.splitlines()[-1].strip() if raw else ''
    if not sid:
        raise RpcTransportAuthError('token-unprotectable', 'current Windows SID is unavailable')
    return sid


def _read_windows_acl_proof(path: Path, *, command_runner) -> dict:
    script = (
        "$ErrorActionPreference = 'Stop'; "
        + '$acl = [System.IO.File]::GetAccessControl('
        + _powershell_literal(str(path))
        + '); '
        + '$payload = [pscustomobject]@{'
        + 'owner = $acl.GetOwner([System.Security.Principal.NTAccount]).Value; '
        + 'sddl = $acl.GetSecurityDescriptorSddlForm([System.Security.AccessControl.AccessControlSections]::All); '
        + 'access = @($acl.GetAccessRules($true, $true, [System.Security.Principal.NTAccount]) | ForEach-Object { [pscustomobject]@{ '
        + 'identity = $_.IdentityReference.Value; '
        + 'rights = $_.FileSystemRights.ToString(); '
        + 'access_type = $_.AccessControlType.ToString(); '
        + 'inherited = [bool]$_.IsInherited '
        + '} })'
        + '}; '
        + '$payload | ConvertTo-Json -Compress -Depth 4'
    )
    result = command_runner(
        ['powershell', '-NoProfile', '-Command', script],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    if int(getattr(result, 'returncode', 1) or 0) != 0:
        stderr = str(getattr(result, 'stderr', '') or '').strip()
        stdout = str(getattr(result, 'stdout', '') or '').strip()
        detail = stderr or stdout or 'unable to verify Windows ACL proof'
        raise RpcTransportAuthError('token-unprotectable', detail)
    raw = str(getattr(result, 'stdout', '') or '').strip()
    if not raw:
        stderr = str(getattr(result, 'stderr', '') or '').strip()
        detail = stderr or 'Windows ACL proof is empty'
        raise RpcTransportAuthError('token-unprotectable', detail)
    try:
        proof = json.loads(raw)
    except Exception as exc:
        raise RpcTransportAuthError('token-unprotectable', 'Windows ACL proof is not JSON') from exc
    if not isinstance(proof, dict):
        raise RpcTransportAuthError('token-unprotectable', 'Windows ACL proof is invalid')
    return proof


def _assert_windows_acl_proof(proof: dict, *, current_user: str, current_sid: str) -> None:
    if not _windows_acl_owner_matches(proof, current_user=current_user, current_sid=current_sid):
        raise RpcTransportAuthError(
            'token-owner-mismatch',
            f'token file owner {proof.get("owner")!r} did not converge to current user {current_user!r} (SID {current_sid!r})'
        )
    access = proof.get('access') or []
    if isinstance(access, dict):
        access = [access]
    if not isinstance(access, list) or not access:
        raise RpcTransportAuthError('token-unprotectable', 'Windows token ACL is empty')
    allowed_identities = {current_user.casefold(), current_sid.casefold()}
    seen_identity = False
    for entry in access:
        if not isinstance(entry, dict):
            raise RpcTransportAuthError('token-unprotectable', 'Windows token ACL proof is invalid')
        identity = str(entry.get('identity') or '').strip()
        rights = str(entry.get('rights') or '').strip()
        access_type = str(entry.get('access_type') or '').strip()
        inherited = bool(entry.get('inherited'))
        if not identity:
            raise RpcTransportAuthError('token-unprotectable', 'Windows token ACL proof is incomplete')
        if inherited or access_type.casefold() != 'allow' or not _windows_acl_rights_prove_read(rights):
            raise RpcTransportAuthError('token-unprotectable', 'Windows token ACL did not converge to a read-only allow entry')
        if identity.casefold() not in allowed_identities:
            raise RpcTransportAuthError('token-unprotectable', 'Windows token ACL contains an unexpected principal')
        seen_identity = True
    if not seen_identity:
        raise RpcTransportAuthError('token-unprotectable', 'Windows token ACL proof is incomplete')


def _windows_acl_owner_matches(proof: dict, *, current_user: str, current_sid: str) -> bool:
    owner = str(proof.get('owner') or '').strip().casefold()
    user = str(current_user or '').strip().casefold()
    sid = str(current_sid or '').strip().casefold()
    if owner and owner in {user, sid}:
        return True
    sddl = str(proof.get('sddl') or '').strip().casefold()
    if bool(sid and f'o:{sid}' in sddl):
        return True
    # Windows icacls /grant:r owner may be reported as BUILTIN\\Administrators
    # when the creating user belongs to the Administrators group. Accept this as
    # long as the ACL access rule lists only the current user or its SID.
    if owner == 'builtin\\administrators':
        return True
    return False


def _windows_acl_rights_prove_read(rights: str) -> bool:
    normalized = rights.casefold()
    return 'read' in normalized or normalized in {'r', 'rx', 'read, synchronize', 'read and execute'}


def _run_checked_command(command_runner, command) -> None:
    runner = command_runner if command_runner is not None else _sp_run_win
    result = runner(
        command,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    if int(getattr(result, 'returncode', 1) or 0) != 0:
        stderr = str(getattr(result, 'stderr', '') or '').strip()
        stdout = str(getattr(result, 'stdout', '') or '').strip()
        detail = stderr or stdout or 'icacls failed'
        raise RpcTransportAuthError('token-unprotectable', detail)


def _powershell_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"
