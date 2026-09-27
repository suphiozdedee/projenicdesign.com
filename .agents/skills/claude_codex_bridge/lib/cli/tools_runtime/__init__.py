from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from . import workbench


def cmd_tools(
    argv: list[str],
    *,
    script_root: Path | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if not argv or argv[0] in {'-h', '--help', 'help'}:
        _print_help(stdout)
        return 0
    if len(argv) < 2:
        _print_help(stdout)
        return 2
    tool = argv[1]
    if tool == 'neovim':
        print('ERROR: standalone Neovim tools are no longer supported; use `ccb update rich`.', file=stderr)
        return 2
    if tool == 'workbench':
        return workbench.cmd_tools(argv, script_root=script_root, stdout=stdout, stderr=stderr)
    print(f'ERROR: unsupported tool: {tool}', file=stderr)
    return 2


def _print_help(stdout: TextIO) -> None:
    print('usage: ccb tools <doctor|install|update|enable|disable|launch|uninstall> workbench [--profile rich]', file=stdout)
    print('       ccb update rich', file=stdout)


__all__ = ['cmd_tools']
