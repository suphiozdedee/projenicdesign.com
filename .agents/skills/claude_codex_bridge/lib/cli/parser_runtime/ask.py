from __future__ import annotations

from cli.ask_syntax import parse_ask_route
from cli.models import ParsedAskCommand, ParsedCancelCommand, ParsedPendCommand

from .constants import ASK_FLAG_OPTIONS, ASK_JOB_ACTIONS, ASK_OPTIONS_WITH_VALUES

_REMOVED_ASK_FLAGS = {
    '--sync': 'async submit is already the default',
    '--async': 'omit the flag; async submit is already the default',
}


def _parse_job_action(tokens: list[str], *, project: str | None, error_type):
    if not tokens or tokens[0] not in ASK_JOB_ACTIONS:
        return None
    action = tokens[0]
    if len(tokens) != 2:
        raise error_type(f'ask {action} requires <job_id>')
    if action == 'get':
        return ParsedPendCommand(project=project, target=tokens[1], count=None)
    return ParsedCancelCommand(project=project, job_id=tokens[1])


def _default_options() -> dict[str, str | float | None]:
    return {
        'task_id': None,
        'reply_to': None,
        'mode': None,
    }


def _set_option_value(options: dict[str, str | float | None], option: str, value: str, *, error_type) -> None:
    del error_type
    options[option[2:].replace('-', '_')] = value


def _parse_route_options(remaining: list[str], *, error_type):
    options = _default_options()
    compact = False
    silence = False
    callback = False
    artifact_request = False
    inline_request = False
    artifact_reply = False
    while remaining and remaining[0].startswith('-'):
        option = remaining.pop(0)
        if option in _REMOVED_ASK_FLAGS:
            raise error_type(f'{option} is no longer supported; {_REMOVED_ASK_FLAGS[option]}')
        if option in ASK_FLAG_OPTIONS:
            if option == '--compact':
                compact = True
                continue
            if option == '--silence':
                silence = True
                continue
            if option == '--chain':
                callback = True
                continue
            if option == '--artifact-request':
                artifact_request = True
                continue
            if option == '--inline-request':
                inline_request = True
                continue
            if option == '--artifact-reply':
                artifact_reply = True
                continue
            if option == '--artifact-io':
                artifact_request = True
                artifact_reply = True
                continue
        if option not in ASK_OPTIONS_WITH_VALUES:
            raise error_type(f'unknown ask option: {option}')
        if not remaining:
            raise error_type(f'{option} requires a value')
        _set_option_value(options, option, remaining.pop(0), error_type=error_type)
    return options, compact, silence, callback, artifact_request, inline_request, artifact_reply


def parse_ask(
    tokens: list[str],
    *,
    project: str | None,
    read_optional_stdin,
    error_type,
) -> ParsedAskCommand | ParsedPendCommand | ParsedCancelCommand:
    action_command = _parse_job_action(tokens, project=project, error_type=error_type)
    if action_command is not None:
        return action_command

    remaining = list(tokens)
    options, compact, silence, callback, artifact_request, inline_request, artifact_reply = _parse_route_options(
        remaining,
        error_type=error_type,
    )

    stdin_text = read_optional_stdin()
    if stdin_text:
        remaining.append(stdin_text)

    try:
        route = parse_ask_route(remaining, command_name='ccb ask')
    except ValueError as exc:
        raise error_type(str(exc)) from exc
    return ParsedAskCommand(
        project=project,
        target=route.target,
        sender=route.sender,
        message=route.message,
        task_id=options['task_id'],
        reply_to=options['reply_to'],
        mode=options['mode'],
        compact=compact,
        silence=silence,
        callback=callback,
        artifact_request=artifact_request,
        inline_request=inline_request,
        artifact_reply=artifact_reply,
    )


__all__ = ['parse_ask']
