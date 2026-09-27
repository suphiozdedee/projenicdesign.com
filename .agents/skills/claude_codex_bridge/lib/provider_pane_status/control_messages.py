from __future__ import annotations

from dataclasses import dataclass
import re


_LOCAL_COMMAND_CAVEAT_RE = re.compile(
    r"^\s*<local-command-caveat>.*?</local-command-caveat>\s*$",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_COMMAND_RE = re.compile(
    r"^\s*<command-name>(?P<name>.*?)</command-name>\s*"
    r"<command-message>.*?</command-message>\s*"
    r"<command-args>(?P<args>.*?)</command-args>\s*$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ProviderLocalCommand:
    name: str
    args: str


def provider_local_command(text: object) -> ProviderLocalCommand | None:
    match = _LOCAL_COMMAND_RE.fullmatch(str(text or "").strip())
    if match is None:
        return None
    name = match.group("name").strip()
    if not name:
        return None
    return ProviderLocalCommand(name=name, args=match.group("args").strip())


def is_provider_local_control_message(text: object) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(
        _LOCAL_COMMAND_CAVEAT_RE.fullmatch(value)
        or provider_local_command(value) is not None
    )


def clean_provider_local_control_message(
    text: object,
    *,
    hidden_commands: frozenset[str] = frozenset({"/clear"}),
) -> str:
    value = str(text or "").strip()
    if not value or _LOCAL_COMMAND_CAVEAT_RE.fullmatch(value):
        return ""
    command = provider_local_command(value)
    if command is None:
        return value
    if command.name.casefold() in hidden_commands:
        return ""
    return f"{command.name} {command.args}".strip()


__all__ = [
    "ProviderLocalCommand",
    "clean_provider_local_control_message",
    "is_provider_local_control_message",
    "provider_local_command",
]
