from __future__ import annotations

import hashlib


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or '').encode('utf-8')).hexdigest()


__all__ = ['sha256_text']
