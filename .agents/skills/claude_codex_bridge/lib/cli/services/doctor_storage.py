from __future__ import annotations

from storage_classification import summarize_storage, summarize_storage_compact


def doctor_storage_summary(context, *, compact: bool = False) -> dict[str, object]:
    if compact:
        return summarize_storage_compact(context)
    return summarize_storage(context)


__all__ = ['doctor_storage_summary']
