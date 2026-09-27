#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys


TOOL_ROOT = Path(__file__).resolve().parents[1] / "tools" / "codex-reconnect"
sys.path.insert(0, str(TOOL_ROOT))

from codex_reconnect.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
