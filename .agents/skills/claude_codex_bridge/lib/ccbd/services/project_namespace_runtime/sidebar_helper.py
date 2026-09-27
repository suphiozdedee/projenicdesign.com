from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
from typing import Mapping


SIDEBAR_BINARY_NAME = 'ccb-agent-sidebar'
SIDEBAR_ENV_PATH = 'CCB_AGENT_SIDEBAR_BIN'
SIDEBAR_HELPER_ID_OPTION = '@ccb_sidebar_helper_id'
_SIDEBAR_WRAPPER_MARKER = b'CCB_AGENT_SIDEBAR_WRAPPER'


@dataclass(frozen=True)
class SidebarHelperResolution:
    path: str | None
    source: str
    reason: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.path)


def resolve_sidebar_helper(
    *,
    env: Mapping[str, str] | None = None,
    which=shutil.which,
    script_root: Path | None = None,
) -> SidebarHelperResolution:
    env_map = env if env is not None else os.environ
    override = _clean_text(env_map.get(SIDEBAR_ENV_PATH))
    if override is not None:
        return _resolve_explicit(Path(override).expanduser(), source=SIDEBAR_ENV_PATH)

    root = script_root or _default_script_root()
    root_candidate = root / 'bin' / SIDEBAR_BINARY_NAME
    if _is_executable_file(root_candidate):
        return SidebarHelperResolution(path=str(root_candidate), source='script_root_bin')

    prefix = _clean_text(env_map.get('CODEX_INSTALL_PREFIX'))
    if prefix is not None:
        prefix_candidate = Path(prefix).expanduser() / 'bin' / SIDEBAR_BINARY_NAME
        if _is_executable_file(prefix_candidate):
            return SidebarHelperResolution(path=str(prefix_candidate), source='CODEX_INSTALL_PREFIX')

    path_candidate = _clean_text(which(SIDEBAR_BINARY_NAME) if callable(which) else None)
    if path_candidate is not None:
        return SidebarHelperResolution(path=path_candidate, source='PATH')

    return SidebarHelperResolution(
        path=None,
        source='missing',
        reason=f'{SIDEBAR_BINARY_NAME} not found in {SIDEBAR_ENV_PATH}, repository bin, install prefix bin, or PATH',
    )


def sidebar_respawn_args(
    launch_args: tuple[str, ...],
    *,
    env: Mapping[str, str] | None = None,
    which=shutil.which,
    script_root: Path | None = None,
) -> tuple[str, ...]:
    if not launch_args or launch_args[0] != SIDEBAR_BINARY_NAME:
        return launch_args
    resolution = resolve_sidebar_helper(env=env, which=which, script_root=script_root)
    if resolution.available and resolution.path is not None:
        return (resolution.path, *launch_args[1:])
    return missing_sidebar_respawn_args(resolution.reason)


def sidebar_helper_fingerprint(
    *,
    env: Mapping[str, str] | None = None,
    which=shutil.which,
    script_root: Path | None = None,
) -> str | None:
    root = script_root or _default_script_root()
    resolution = resolve_sidebar_helper(env=env, which=which, script_root=root)
    if not resolution.available or resolution.path is None:
        return None
    runtime_binary = _sidebar_runtime_binary(Path(resolution.path), script_root=root)
    try:
        digest = hashlib.sha256(runtime_binary.read_bytes()).hexdigest()
    except OSError:
        return None
    return f'sha256:{digest}'


def missing_sidebar_respawn_args(reason: str | None = None) -> tuple[str, ...]:
    message = 'CCB sidebar helper unavailable'
    detail = _clean_text(reason) or f'{SIDEBAR_BINARY_NAME} not found'
    body = (
        f"printf '%s\\n' '{_shell_single_quote_text(message)}'; "
        f"printf '%s\\n' '{_shell_single_quote_text(detail)}'; "
        "printf '%s\\n' 'Build or install bin/ccb-agent-sidebar, or set CCB_AGENT_SIDEBAR_BIN.'; "
        'while :; do sleep 3600; done'
    )
    return ('sh', '-lc', body)


def _resolve_explicit(path: Path, *, source: str) -> SidebarHelperResolution:
    if _is_executable_file(path):
        return SidebarHelperResolution(path=str(path), source=source)
    return SidebarHelperResolution(
        path=None,
        source=source,
        reason=f'{source} points to a missing or non-executable file: {path}',
    )


def _sidebar_runtime_binary(path: Path, *, script_root: Path) -> Path:
    try:
        prefix = path.read_bytes()[:4096]
    except OSError:
        return path
    if _SIDEBAR_WRAPPER_MARKER not in prefix:
        return path
    candidate = script_root / 'tools' / 'ccb-agent-sidebar' / 'target' / 'release' / SIDEBAR_BINARY_NAME
    return candidate if candidate.is_file() else path


def _default_script_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _clean_text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def _shell_single_quote_text(value: object) -> str:
    return str(value).replace("'", "'\"'\"'")


__all__ = [
    'SIDEBAR_BINARY_NAME',
    'SIDEBAR_ENV_PATH',
    'SIDEBAR_HELPER_ID_OPTION',
    'SidebarHelperResolution',
    'missing_sidebar_respawn_args',
    'resolve_sidebar_helper',
    'sidebar_helper_fingerprint',
    'sidebar_respawn_args',
]
