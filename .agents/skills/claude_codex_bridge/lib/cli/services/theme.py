from __future__ import annotations

from typing import TextIO
import os
import shutil
import subprocess

from cli.services.tmux_ui_runtime.helpers import script_path
from terminal_runtime.ui_theme import (
    ThemePreference,
    available_themes,
    effective_theme_preference,
    load_or_default_theme_preference,
    resolve_theme_request,
    save_theme_preference,
    theme_config_path,
)


def cmd_theme(argv: list[str], *, stdout: TextIO, stderr: TextIO) -> int:
    tokens = list(argv or [])
    if tokens and tokens[0] in {'-h', '--help', 'help'}:
        print_theme_usage(stdout)
        return 0
    if len(tokens) > 1:
        print('ERROR: theme accepts at most one value', file=stderr)
        print_theme_usage(stderr)
        return 2
    if not tokens:
        preference = load_or_default_theme_preference()
        payload = theme_preference_payload(preference)
        _print_theme_status(
            stdout,
            preference=preference,
            action='status',
            tmux_refresh='skipped',
            tmux_refresh_reason='status_only',
            payload=payload,
        )
        return 0

    preference = resolve_theme_request(tokens[0])
    if preference is None:
        print(f'ERROR: unsupported theme: {tokens[0]}', file=stderr)
        print(f'Available themes: {", ".join(available_themes())}', file=stderr)
        return 2
    payload = set_theme_preference(preference.theme)
    _print_theme_status(
        stdout,
        preference=preference,
        action='set',
        tmux_refresh=str(payload['tmux_refresh']),
        tmux_refresh_reason=str(payload['tmux_refresh_reason']),
        payload=payload,
    )
    return 0


def print_theme_usage(stdout: TextIO) -> None:
    print('usage: ccb theme [system|dark|light|+|-|solarized|tokyo|gruvbox|rose-pine]', file=stdout)
    print('       ccb theme +', file=stdout)
    print('       ccb theme system', file=stdout)
    print('       ccb theme light', file=stdout)
    print('       ccb theme dark', file=stdout)


def theme_preference_payload(
    preference: ThemePreference | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, object]:
    env = dict(os.environ if environ is None else environ)
    selected = preference or load_or_default_theme_preference(env)
    effective = effective_theme_preference(selected, env)
    return {
        'schema_version': 1,
        'theme': selected.theme,
        'palette': selected.palette,
        'tmux_profile': selected.tmux_profile,
        'effective_theme': effective.theme,
        'effective_palette': effective.palette,
        'effective_tmux_profile': effective.tmux_profile,
        'available_themes': list(available_themes()),
        'config_path': str(theme_config_path(env)),
    }


def set_theme_preference(
    request: str,
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, object]:
    env = dict(os.environ if environ is None else environ)
    preference = resolve_theme_request(request, environ=env)
    if preference is None:
        raise ValueError(f'unsupported theme: {request}')
    save_theme_preference(preference, environ=env)
    tmux_refresh, tmux_refresh_reason = _refresh_current_tmux(
        preference,
        environ=env,
    )
    payload = theme_preference_payload(preference, environ=env)
    payload.update(
        {
            'theme_action': 'set',
            'tmux_refresh': tmux_refresh,
            'tmux_refresh_reason': tmux_refresh_reason,
            'rich_context': _rich_context_status(env),
        }
    )
    payload['wezterm_refresh'] = _wezterm_refresh_status(
        str(payload['rich_context'])
    )
    return payload


def _refresh_current_tmux(
    preference: ThemePreference,
    *,
    environ: dict[str, str] | None = None,
) -> tuple[str, str]:
    source_env = os.environ if environ is None else environ
    if not ((source_env.get('TMUX') or source_env.get('TMUX_PANE') or '').strip()):
        return ('skipped', 'not_inside_tmux')
    tmux = shutil.which('tmux', path=source_env.get('PATH'))
    if not tmux:
        return ('skipped', 'tmux_not_found')
    env = dict(source_env)
    effective = effective_theme_preference(preference, env)
    env['CCB_TMUX_THEME_PROFILE'] = effective.tmux_profile
    env['CCB_SIDEBAR_THEME_PROFILE'] = effective.tmux_profile
    for key, value in (
        ('CCB_TMUX_THEME_PROFILE', effective.tmux_profile),
        ('CCB_SIDEBAR_THEME_PROFILE', effective.tmux_profile),
    ):
        try:
            subprocess.run([tmux, 'set-environment', '-g', key, value], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        except Exception:
            pass
    on_script = script_path('ccb-tmux-on.sh')
    if not on_script:
        return ('partial', 'ccb_tmux_on_not_found')
    try:
        result = subprocess.run([on_script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except Exception:
        return ('failed', 'ccb_tmux_on_failed')
    if result.returncode != 0:
        return ('failed', 'ccb_tmux_on_failed')
    return ('applied', 'ok')


def _rich_context_status(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    if str(env.get('CCB_WORKBENCH_PROFILE') or '').strip().lower() != 'rich':
        return 'not_rich'
    if (env.get('WEZTERM_PANE') or env.get('WEZTERM_UNIX_SOCKET') or env.get('WEZTERM_EXECUTABLE')):
        return 'rich_wezterm'
    return 'rich'


def _wezterm_refresh_status(rich_context: str) -> str:
    if rich_context == 'rich_wezterm':
        return 'watched_config_reload'
    if rich_context == 'rich':
        return 'rich_context_without_wezterm'
    return 'next_rich_start'


def _print_theme_status(
    stdout: TextIO,
    *,
    preference: ThemePreference,
    action: str,
    tmux_refresh: str,
    tmux_refresh_reason: str,
    payload: dict[str, object] | None = None,
) -> None:
    rich_context = _rich_context_status()
    wezterm_refresh = _wezterm_refresh_status(rich_context)
    details = payload or theme_preference_payload(preference)
    print('theme_status: ok', file=stdout)
    print(f'theme_action: {action}', file=stdout)
    print(f'theme: {preference.theme}', file=stdout)
    print(f'palette: {preference.palette}', file=stdout)
    print(f'tmux_profile: {preference.tmux_profile}', file=stdout)
    print(f"effective_theme: {details['effective_theme']}", file=stdout)
    print(f"effective_palette: {details['effective_palette']}", file=stdout)
    print(f"effective_tmux_profile: {details['effective_tmux_profile']}", file=stdout)
    print(f'config_path: {theme_config_path()}', file=stdout)
    print(f'tmux_refresh: {tmux_refresh}', file=stdout)
    print(f'tmux_refresh_reason: {tmux_refresh_reason}', file=stdout)
    print(f'rich_context: {rich_context}', file=stdout)
    print(f'wezterm_refresh: {wezterm_refresh}', file=stdout)


__all__ = [
    'cmd_theme',
    'print_theme_usage',
    'set_theme_preference',
    'theme_preference_payload',
]
