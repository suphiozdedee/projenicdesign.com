from __future__ import annotations

import hashlib
import json


class ProjectViewSequenceCache:
    def __init__(self) -> None:
        self._last_digest: str | None = None
        self._sequence = 0

    def sequence_for(self, view: dict[str, object]) -> int:
        digest = _stable_digest(_stable_view_payload(view))
        if digest != self._last_digest:
            self._last_digest = digest
            self._sequence += 1
        return self._sequence


def _stable_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _stable_view_payload(view: dict[str, object]) -> dict[str, object]:
    stable = dict(view)
    stable.pop('generated_at', None)
    return stable


__all__ = ['ProjectViewSequenceCache']
