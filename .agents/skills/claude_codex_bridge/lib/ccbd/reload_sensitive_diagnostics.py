from __future__ import annotations

from collections.abc import Mapping

_REDACTED = '<redacted>'


def redact_sensitive_diagnostics(diagnostics: Mapping[str, object]) -> dict[str, object]:
    return {
        key: _REDACTED if _is_restore_token_key(key) else redact_sensitive_diagnostic_value(value)
        for key, value in diagnostics.items()
    }


def redact_sensitive_diagnostic_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _REDACTED if _is_restore_token_key(key) else redact_sensitive_diagnostic_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_diagnostic_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_diagnostic_value(item) for item in value)
    return value


def _is_restore_token_key(key: object) -> bool:
    normalized = str(key or '').strip().lower().replace('_', '').replace('-', '')
    return normalized.endswith('restoretoken')


__all__ = ['redact_sensitive_diagnostic_value', 'redact_sensitive_diagnostics']
