from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import platform
import shutil
import subprocess
from typing import Mapping


SCHEMA_VERSION = 1
SYSTEM_THEME_ENV = 'CCB_SYSTEM_THEME'


@dataclass(frozen=True)
class ThemePreference:
    theme: str
    palette: str
    tmux_profile: str


_THEMES: dict[str, ThemePreference] = {
    'system': ThemePreference(theme='system', palette='system', tmux_profile='system'),
    'dark': ThemePreference(theme='dark', palette='dark', tmux_profile='default'),
    'light': ThemePreference(theme='light', palette='latte', tmux_profile='light'),
    'solarized': ThemePreference(theme='solarized', palette='solarized_light', tmux_profile='light'),
    'tokyo': ThemePreference(theme='tokyo', palette='tokyo_night_light', tmux_profile='light'),
    'gruvbox': ThemePreference(theme='gruvbox', palette='gruvbox_light', tmux_profile='light'),
    'rose-pine': ThemePreference(theme='rose-pine', palette='rose_pine_dawn', tmux_profile='light'),
}

THEME_CYCLE: tuple[str, ...] = ('system', 'dark', 'light', 'solarized', 'tokyo', 'gruvbox', 'rose-pine')

_ALIASES = {
    '': 'dark',
    'default': 'dark',
    'auto': 'system',
    'os': 'system',
    'system': 'system',
    'system-default': 'system',
    'nord': 'dark',
    'contrast': 'dark',
    'dark': 'dark',
    'light': 'light',
    'latte': 'light',
    'catppuccin-latte': 'light',
    'catppuccin_latte': 'light',
    'solarized': 'solarized',
    'solarized-light': 'solarized',
    'solarized_light': 'solarized',
    'tokyo': 'tokyo',
    'tokyo-light': 'tokyo',
    'tokyo_light': 'tokyo',
    'tokyo-night-light': 'tokyo',
    'tokyo_night_light': 'tokyo',
    'tokyo-night-day': 'tokyo',
    'gruvbox': 'gruvbox',
    'gruvbox-light': 'gruvbox',
    'gruvbox_light': 'gruvbox',
    'rose-pine': 'rose-pine',
    'rose-pine-dawn': 'rose-pine',
    'rose_pine_dawn': 'rose-pine',
}


def theme_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    config_home = str(env.get('XDG_CONFIG_HOME') or '').strip()
    home = Path(str(env.get('HOME') or Path.home())).expanduser()
    root = Path(config_home).expanduser() if config_home else home / '.config'
    return root / 'ccb' / 'theme.json'


def normalize_theme_name(value: str | None) -> str | None:
    key = str(value or '').strip().lower().replace('_', '-').replace(' ', '-')
    if key in _ALIASES:
        return _ALIASES[key]
    return None


def preference_for_theme(theme: str | None) -> ThemePreference | None:
    name = normalize_theme_name(theme)
    if name is None:
        return None
    return _THEMES[name]


def default_theme_preference() -> ThemePreference:
    return _THEMES['dark']


def detect_system_theme(environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    explicit = _dark_or_light(env.get(SYSTEM_THEME_ENV))
    if explicit is not None:
        return explicit

    for key in ('GTK_THEME', 'QT_STYLE_OVERRIDE'):
        value = str(env.get(key) or '').strip().lower()
        if value:
            return 'dark' if 'dark' in value else 'light'

    system = platform.system().lower()
    if system == 'darwin':
        value = _command_output(('defaults', 'read', '-g', 'AppleInterfaceStyle'), environ=env)
        return 'dark' if 'dark' in value.lower() else 'light'

    if system == 'windows' or _is_wsl(env):
        value = _command_output(
            (
                'powershell.exe',
                '-NoProfile',
                '-NonInteractive',
                '-Command',
                '(Get-ItemProperty '
                "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize' "
                '-Name AppsUseLightTheme).AppsUseLightTheme',
            ),
            environ=env,
        )
        if value.strip() == '0':
            return 'dark'
        if value.strip() == '1':
            return 'light'

    if system == 'linux':
        color_scheme = _command_output(
            ('gsettings', 'get', 'org.gnome.desktop.interface', 'color-scheme'),
            environ=env,
        ).lower()
        if 'dark' in color_scheme:
            return 'dark'
        if color_scheme and 'default' not in color_scheme:
            return 'light'
        gtk_theme = _command_output(
            ('gsettings', 'get', 'org.gnome.desktop.interface', 'gtk-theme'),
            environ=env,
        ).lower()
        if gtk_theme:
            return 'dark' if 'dark' in gtk_theme else 'light'

    colorfgbg = str(env.get('COLORFGBG') or '').strip()
    if colorfgbg:
        try:
            background = int(colorfgbg.rsplit(';', 1)[-1])
        except ValueError:
            pass
        else:
            return 'dark' if background < 7 else 'light'
    return 'dark'


def effective_theme_preference(
    preference: ThemePreference,
    environ: Mapping[str, str] | None = None,
) -> ThemePreference:
    if (
        preference.theme != 'system'
        and preference.palette != 'system'
        and preference.tmux_profile != 'system'
    ):
        return preference
    return _THEMES[detect_system_theme(environ)]


def available_themes() -> tuple[str, ...]:
    return THEME_CYCLE


def load_theme_preference(environ: Mapping[str, str] | None = None) -> ThemePreference | None:
    path = theme_config_path(environ)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ('theme', 'palette'):
        preference = preference_for_theme(str(payload.get(key) or ''))
        if preference is not None:
            return preference
    tmux_profile = str(payload.get('tmux_profile') or '').strip().lower()
    if tmux_profile == 'light':
        return _THEMES['light']
    if tmux_profile in {'default', 'contrast'}:
        return _THEMES['dark']
    return None


def load_or_default_theme_preference(environ: Mapping[str, str] | None = None) -> ThemePreference:
    return load_theme_preference(environ) or default_theme_preference()


def resolve_theme_request(request: str | None, *, environ: Mapping[str, str] | None = None) -> ThemePreference | None:
    token = str(request or '').strip()
    current = load_or_default_theme_preference(environ)
    if token == '+':
        return _cycle_from(current, step=1)
    if token == '-':
        return _cycle_from(current, step=-1)
    return preference_for_theme(token)


def save_theme_preference(preference: ThemePreference, environ: Mapping[str, str] | None = None) -> Path:
    path = theme_config_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema_version': SCHEMA_VERSION,
        'theme': preference.theme,
        'palette': preference.palette,
        'tmux_profile': preference.tmux_profile,
    }
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temporary.replace(path)
    return path


def _cycle_from(current: ThemePreference, *, step: int) -> ThemePreference:
    try:
        index = THEME_CYCLE.index(current.theme)
    except ValueError:
        index = 0
    return _THEMES[THEME_CYCLE[(index + step) % len(THEME_CYCLE)]]


def _dark_or_light(value: object) -> str | None:
    normalized = str(value or '').strip().lower().replace('_', '-')
    if normalized in {'dark', 'prefer-dark', 'dark-mode'}:
        return 'dark'
    if normalized in {'light', 'prefer-light', 'light-mode'}:
        return 'light'
    return None


def _is_wsl(environ: Mapping[str, str]) -> bool:
    if environ.get('WSL_DISTRO_NAME') or environ.get('WSL_INTEROP'):
        return True
    try:
        return 'microsoft' in Path('/proc/version').read_text(
            encoding='utf-8',
            errors='ignore',
        ).lower()
    except Exception:
        return False


def _command_output(
    command: tuple[str, ...],
    *,
    environ: Mapping[str, str],
) -> str:
    if shutil.which(command[0], path=str(environ.get('PATH') or os.defpath)) is None:
        return ''
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
            env=dict(environ),
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    return completed.stdout.strip() if completed.returncode == 0 else ''


__all__ = [
    'ThemePreference',
    'SYSTEM_THEME_ENV',
    'available_themes',
    'default_theme_preference',
    'detect_system_theme',
    'effective_theme_preference',
    'load_or_default_theme_preference',
    'load_theme_preference',
    'normalize_theme_name',
    'preference_for_theme',
    'resolve_theme_request',
    'save_theme_preference',
    'theme_config_path',
]
