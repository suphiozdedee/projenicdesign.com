from __future__ import annotations

from time import monotonic

from storage.text_artifacts import sweep_expired_text_artifacts

TEXT_ARTIFACT_SWEEP_INTERVAL_S = 5 * 60


def sweep_text_artifacts_if_due(dispatcher, *, monotonic_fn=monotonic) -> tuple[object, ...]:
    now = float(monotonic_fn())
    last = dispatcher._last_text_artifact_sweep_at
    if last is not None and now - float(last) < TEXT_ARTIFACT_SWEEP_INTERVAL_S:
        return ()
    removed = sweep_expired_text_artifacts(dispatcher._layout)
    dispatcher._last_text_artifact_sweep_at = now
    return tuple(removed)


__all__ = ['TEXT_ARTIFACT_SWEEP_INTERVAL_S', 'sweep_text_artifacts_if_due']
