"""Windows-safe shell command construction for herdr pane respawn.

Herdr panes run PowerShell on Windows; bash-style ``export VAR=...; cmd``
payloads cannot be injected directly. When Git Bash is installed, the payload
is written to a ``.sh`` script and invoked by a generated PowerShell wrapper.
The pane receives a structured ``powershell -File`` argv rather than a shell
fragment, so paths containing spaces remain unambiguous and ``cmd.exe`` is not
spawned. Shared by the runtime-launch path
(``cli/services/runtime_launch_runtime/pane_runtime.py``) and the ccbd
namespace materialization path
(``ccbd/services/project_namespace_runtime/backend.py``) so both respawn
agent/sidebar panes with the same command shape.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def resolve_sh_executable() -> str | None:
    """Resolve Git Bash's sh.exe on Windows; None when unavailable.

    Resolution order:
    1. ``CCB_SH_EXECUTABLE`` environment variable (explicit override).
    2. ``shutil.which('sh')`` (PATH lookup).
    3. Fixed paths covering standard Git Bash install locations.
    """
    explicit = str(os.environ.get('CCB_SH_EXECUTABLE') or '').strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)
    found = shutil.which('sh')
    if found:
        return str(found)
    for candidate in (
        r'C:\Program Files\Git\bin\sh.exe',
        r'C:\Program Files\Git\usr\bin\sh.exe',
        r'C:\Program Files\Git\cmd\sh.exe',
    ):
        if Path(candidate).exists():
            return candidate
    return None


def sh_quote(value: str) -> str:
    """Quote a path for a POSIX shell single-quote literal."""
    escaped = value.replace("'", "'\\''")
    return f"'{escaped}'"


def herdr_respawn_command(command: str, cwd: Path, name: str) -> list[str]:
    """Build the herdr respawn argv for a bash-style command payload.

    With Git Bash present, writes the payload to a ``.sh`` script, wraps it in
    a ``.ps1`` helper, and returns ``['powershell', '-File', <ps1>]`` so the
    pane's native PowerShell shell invokes sh.exe via ``&`` inside the script.
    Avoids ``cmd.exe`` entirely (every ``cmd /d /c`` spawn triggers a conhost
    console host flash on Windows; 2026-08-10 diagnostic captured 64 conhost
    flashes all parented by a single cmd.exe in the launch chain).

    Without Git Bash, falls back to ``['sh', '-lc', command]`` (the historical
    tmux behavior).
    """
    sh_exe = resolve_sh_executable()
    if not sh_exe:
        import sys
        print(
            f'[shell_launch] WARNING: resolve_sh_executable() returned None '
            f'for agent={name}; fallback to bare "sh". '
            f'CCB_SH_EXECUTABLE={os.environ.get("CCB_SH_EXECUTABLE", "")!r} '
            f'PATH={os.environ.get("PATH", "")[:200]!r}',
            file=sys.stderr,
            flush=True,
        )
        return ['sh', '-lc', command]
    script_dir = Path(tempfile.gettempdir()) / 'ccb-agent-launch'
    script_dir.mkdir(parents=True, exist_ok=True)
    script_path = script_dir / f'start-{name}-{os.getpid()}.sh'
    script_path.write_text(f'cd {sh_quote(str(cwd))} && {command}\n', encoding='utf-8')
    # Wrap in a .ps1 so PowerShell (the pane shell) invokes sh.exe via the
    # `&` call operator — no cmd.exe, no conhost flash.
    # Use $PSScriptRoot to reference the .sh file by name only (relative to
    # the .ps1's directory), avoiding hardcoded Chinese/Unicode paths in the
    # script content.  utf-8-sig (BOM) is required for PowerShell 5.1.
    sh_filename = script_path.name
    ps1_path = script_dir / f'start-{name}-{os.getpid()}.ps1'
    ps1_path.write_text(
        f'$shScript = Join-Path $PSScriptRoot "{sh_filename}"\n'
        f'& "{sh_exe}" $shScript\n',
        encoding='utf-8-sig',
    )
    import sys
    print(
        f'[shell_launch] herdr_respawn: name={name} sh_exe={sh_exe!r} '
        f'script={script_path.as_posix()!r} ps1={ps1_path.as_posix()!r}',
        file=sys.stderr,
        flush=True,
    )
    return ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ps1_path)]
