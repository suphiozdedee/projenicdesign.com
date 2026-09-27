from __future__ import annotations

from pathlib import Path
import shlex


def build_hook_command(
    *,
    provider: str,
    script_path: Path,
    python_executable: str,
    completion_dir: Path,
    agent_name: str,
    workspace_path: Path,
) -> str:
    parts = [
        *_script_command_prefix(script_path, python_executable),
        '--provider',
        str(provider),
        '--completion-dir',
        str(Path(completion_dir).expanduser()),
        '--agent-name',
        str(agent_name),
        '--workspace',
        str(Path(workspace_path).expanduser()),
    ]
    return ' '.join(shlex.quote(str(part)) for part in parts)


def build_activity_hook_command(
    *,
    provider: str,
    script_path: Path,
    python_executable: str,
    project_id: str,
    agent_name: str,
    runtime_dir: Path,
    workspace_path: Path,
) -> str:
    parts = [
        *_script_command_prefix(script_path, python_executable),
        '--provider',
        str(provider),
        '--project-id',
        str(project_id),
        '--agent-name',
        str(agent_name),
        '--runtime-dir',
        str(Path(runtime_dir).expanduser()),
        '--workspace',
        str(Path(workspace_path).expanduser()),
    ]
    return ' '.join(shlex.quote(str(part)) for part in parts)


def _script_command_prefix(script_path: Path, python_executable: str) -> list[str]:
    script = Path(script_path).expanduser()
    if script.suffix.lower() == '.py':
        return [str(python_executable), str(script)]
    return [str(script)]


__all__ = ['build_activity_hook_command', 'build_hook_command']
