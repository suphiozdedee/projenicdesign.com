from __future__ import annotations


def agent_kind(source: str) -> str:
    if source == 'loop':
        return 'loop'
    if source == 'dynamic':
        return 'dynamic'
    return 'static'


def ownership_class(*, source: str, lifetime: object | None = None) -> str:
    if source == 'loop':
        return 'loop_capacity'
    if source == 'dynamic':
        return f'dynamic_{_optional_text(lifetime) or "session"}'
    return 'static_configured'


def dispatch_state(dispatch_disabled: bool) -> str:
    return 'disabled' if dispatch_disabled else 'enabled'


def normalize_apply_payload(*payloads: dict[str, object]) -> dict[str, object]:
    for payload in payloads:
        raw = payload.get('apply')
        if isinstance(raw, dict):
            return dict(raw)
        applied = payload.get('applied')
        if isinstance(applied, dict):
            normalized = dict(applied)
            if 'apply_status' not in normalized:
                normalized['apply_status'] = normalized.get('status')
            return normalized
    return {}


def failed_apply(apply_payload: dict[str, object]) -> bool:
    failing = {'blocked', 'failed'}
    if str(apply_payload.get('apply_status') or apply_payload.get('status') or '') in failing:
        return True
    return any(
        str(apply_payload.get(key) or '') in failing
        for key in (
            'namespace_patch_status',
            'runtime_mount_status',
            'publish_status',
        )
    )


def pane_identity_source(*, observed: object | None = None, runtime: object | None = None, record: object | None = None) -> str:
    if _optional_text(observed) is not None:
        return 'observed'
    if _optional_text(runtime) is not None:
        return 'runtime'
    if _optional_text(record) is not None:
        return 'record'
    return 'missing'


def _optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    'agent_kind',
    'dispatch_state',
    'failed_apply',
    'normalize_apply_payload',
    'ownership_class',
    'pane_identity_source',
]
