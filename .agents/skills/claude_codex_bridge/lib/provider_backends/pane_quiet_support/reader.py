from __future__ import annotations

import re
from dataclasses import dataclass


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass
class PaneSnapshotReader:
    backend: object
    pane_id: str
    lines: int = 200

    def snapshot(self) -> str:
        getter = getattr(self.backend, "get_pane_content", None)
        if not callable(getter):
            getter = getattr(self.backend, "get_text", None)
        if not callable(getter):
            return ""
        try:
            content = getter(self.pane_id, lines=self.lines)
        except Exception:
            return ""
        if not content:
            return ""
        return _ANSI_RE.sub("", str(content))


__all__ = ["PaneSnapshotReader"]
