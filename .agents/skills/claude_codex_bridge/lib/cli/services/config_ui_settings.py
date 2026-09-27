from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from agents.config_loader import ConfigValidationError
from agents.config_loader_runtime.io_runtime import parse_config_document_text
from agents.config_loader_runtime.parsing_runtime.validation import validate_config_ui_settings

_ENV_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


@dataclass(frozen=True)
class ResolvedConfigUiSettings:
    port: int
    token: str | None
    token_source: str


def resolve_config_ui_settings(
    *,
    project_root: Path,
    cli_port: int | None,
    environ: dict[str, str] | None = None,
) -> ResolvedConfigUiSettings:
    config_path = project_root / '.ccb' / 'ccb.config'
    raw: dict[str, object] = {}
    if config_path.is_file():
        document = parse_config_document_text(
            config_path.read_text(encoding='utf-8'),
            path=config_path,
            project_root=project_root,
        )
        raw = validate_config_ui_settings(document.get('config_ui'))

    configured_port = int(raw.get('port', 0))
    port = configured_port if cli_port is None else int(cli_port)
    if 'token_env' in raw:
        name = str(raw['token_env']).strip()
        if not _ENV_NAME.fullmatch(name):
            raise ConfigValidationError('config_ui.token_env must be a valid environment variable name')
        value = (os.environ if environ is None else environ).get(name)
        if value is None or not value:
            raise ConfigValidationError('the configured Config UI token environment variable is unset or empty')
        return ResolvedConfigUiSettings(port=port, token=value, token_source='environment')
    if 'token_file' in raw:
        token_path = _secure_token_path(project_root, str(raw['token_file']))
        value = token_path.read_text(encoding='utf-8').rstrip('\r\n')
        if not value:
            raise ConfigValidationError('the configured Config UI token file is empty')
        return ResolvedConfigUiSettings(port=port, token=value, token_source='file')
    return ResolvedConfigUiSettings(port=port, token=None, token_source='ephemeral')


def _secure_token_path(project_root: Path, configured_path: str) -> Path:
    relative = Path(configured_path).expanduser()
    if relative.is_absolute():
        raise ConfigValidationError('config_ui.token_file must be project-relative')
    root = project_root.resolve()
    candidate = root / relative
    if candidate.is_symlink():
        raise ConfigValidationError('config_ui.token_file must not be a symbolic link')
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise ConfigValidationError('the configured Config UI token file is unavailable') from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigValidationError('config_ui.token_file must stay within the project') from exc
    file_stat = resolved.stat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise ConfigValidationError('config_ui.token_file must be a regular file')
    if os.name == 'posix' and file_stat.st_mode & 0o077:
        raise ConfigValidationError('config_ui.token_file must have owner-only permissions (0600)')
    return resolved


__all__ = ['ResolvedConfigUiSettings', 'resolve_config_ui_settings']
