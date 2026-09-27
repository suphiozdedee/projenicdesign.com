from __future__ import annotations

import json
import os
import importlib.util
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Any


class AgentRolesManagerError(ValueError):
    pass


def install(role_id: str | None, *, source_path: Path | None = None) -> dict[str, object]:
    args = ['install']
    if role_id:
        args.append(role_id)
    if source_path is not None:
        args.extend(['--path', str(Path(source_path).expanduser())])
    return _run_json(args)


def update(role_id: str | None) -> dict[str, object]:
    if not role_id:
        raise AgentRolesManagerError('role id is required for update')
    return _run_json(['update', role_id])


def sync(path: Path) -> dict[str, object]:
    return _run_json(['sync', str(Path(path).expanduser())])


def _run_json(args: list[str]) -> dict[str, object]:
    command, cwd, env = _command_context()
    try:
        completed = subprocess.run(
            command + args + ['--json'],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_timeout_seconds(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        timeout = exc.timeout if exc.timeout is not None else _timeout_seconds()
        raise AgentRolesManagerError(
            f'agent-roles timed out after {timeout:g}s for {" ".join(args)}'
        ) from exc
    except OSError as exc:
        raise AgentRolesManagerError(f'agent-roles could not run: {exc}') from exc
    stdout_text = completed.stdout.strip()
    stderr_text = completed.stderr.strip()
    if completed.returncode != 0:
        for source in (stdout_text, stderr_text):
            if not source:
                continue
            try:
                payload = json.loads(source)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                raise AgentRolesManagerError(
                    str(payload.get('error') or payload.get('status') or 'agent-roles failed')
                )
        detail = stderr_text or stdout_text or 'no output'
        raise AgentRolesManagerError(
            f'agent-roles {" ".join(args)} failed with exit code {completed.returncode}: {detail}'
        )
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise AgentRolesManagerError(
            f'agent-roles returned invalid JSON for {" ".join(args)}: {stdout_text or "no output"}'
        ) from exc
    if not isinstance(payload, dict):
        raise AgentRolesManagerError(f'agent-roles returned non-object JSON for {" ".join(args)}')
    return dict(payload)


def _command_context() -> tuple[list[str], Path | None, dict[str, str]]:
    env = dict(os.environ)
    raw = str(os.environ.get('AGENT_ROLES_CLI') or '').strip()
    if raw:
        try:
            return shlex.split(raw), None, env
        except ValueError as exc:
            raise AgentRolesManagerError(f'invalid AGENT_ROLES_CLI: {exc}') from exc
    executable = shutil.which('agent-roles')
    if executable:
        return [executable], None, env
    if importlib.util.find_spec('agent_roles') is not None:
        return [sys.executable, '-m', 'agent_roles'], None, env
    source_root = _agent_roles_source_root()
    if source_root is not None:
        pythonpath = str(source_root)
        existing = str(env.get('PYTHONPATH') or '').strip()
        env['PYTHONPATH'] = pythonpath if not existing else pythonpath + os.pathsep + existing
        return [sys.executable, '-m', 'agent_roles'], source_root, env
    raise AgentRolesManagerError(
        'agent-roles manager is enabled but no agent-roles command was found; '
        'set AGENT_ROLES_CLI or AGENT_ROLES_SPEC_HOME'
    )


def _agent_roles_source_root() -> Path | None:
    candidates: list[Path] = []
    for env_name in ('AGENT_ROLES_SPEC_HOME', 'CCB_AGENT_ROLES_SPEC_HOME'):
        value = str(os.environ.get(env_name) or '').strip()
        if value:
            candidates.append(Path(value).expanduser())
    candidates.append(Path.home() / 'yunwei' / 'agent-roles-spec')
    try:
        from .sources import default_agent_roles_source

        default_source = default_agent_roles_source()
    except Exception:
        default_source = None
    if default_source is not None:
        candidates.append(default_source)
    for candidate in candidates:
        if (candidate / 'agent_roles' / 'cli.py').is_file():
            return candidate.resolve()
    return None


def _timeout_seconds() -> float:
    raw = str(os.environ.get('CCB_AGENT_ROLES_TIMEOUT_SECONDS') or '120').strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 120.0


__all__ = [
    'AgentRolesManagerError',
    'install',
    'sync',
    'update',
]
