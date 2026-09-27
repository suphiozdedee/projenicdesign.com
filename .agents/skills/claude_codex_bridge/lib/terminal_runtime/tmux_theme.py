from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import shlex
from typing import Mapping

from terminal_runtime.ui_theme import effective_theme_preference, load_theme_preference


@dataclass(frozen=True)
class TmuxPaneVisual:
    label_style: str
    border_style: str
    active_border_style: str


@dataclass(frozen=True)
class TmuxStatusPalette:
    background: str
    foreground: str
    muted: str
    segment_fg: str
    mode_accent: str
    git_bg: str
    focus_bg: str
    version_bg: str
    indicator_bg: str
    indicator_fg: str
    time_bg: str
    time_fg: str


@dataclass(frozen=True)
class TmuxThemeProfile:
    name: str
    fallback_label_style: str
    pane_border_style: str
    pane_active_border_style: str
    status: TmuxStatusPalette
    pane_title_style: str = '#[fg=#565f89]'
    window_style: str | None = None
    window_active_style: str | None = None


@dataclass(frozen=True)
class RenderedTmuxSessionTheme:
    profile_name: str
    session_options: dict[str, str]
    window_options: dict[str, str]


_DEFAULT_FALLBACK_LABEL_STYLE = '#[fg=#1e1e2e]#[bg=#7aa2f7]#[bold]'
_LIGHT_FALLBACK_LABEL_STYLE = '#[fg=#eff1f5]#[bg=#1e66f5]#[bold]'

_DARK_STATUS = TmuxStatusPalette(
    background='#1e1e2e',
    foreground='#cdd6f4',
    muted='#6c7086',
    segment_fg='#1e1e2e',
    mode_accent='#{?client_prefix,#f38ba8,#{?pane_in_mode,#fab387,#f5c2e7}}',
    git_bg='#cba6f7',
    focus_bg='#f38ba8',
    version_bg='#cba6f7',
    indicator_bg='#89b4fa',
    indicator_fg='#cdd6f4',
    time_bg='#fab387',
    time_fg='#1e1e2e',
)

_LIGHT_STATUS = TmuxStatusPalette(
    background='#eff1f5',
    foreground='#4c4f69',
    muted='#6c6f85',
    segment_fg='#eff1f5',
    mode_accent='#{?client_prefix,#d20f39,#{?pane_in_mode,#df8e1d,#1e66f5}}',
    git_bg='#179299',
    focus_bg='#d20f39',
    version_bg='#8839ef',
    indicator_bg='#1e66f5',
    indicator_fg='#eff1f5',
    time_bg='#4c4f69',
    time_fg='#eff1f5',
)

_THEME_PROFILES: dict[str, TmuxThemeProfile] = {
    'default': TmuxThemeProfile(
        name='default',
        fallback_label_style=_DEFAULT_FALLBACK_LABEL_STYLE,
        pane_border_style='fg=#3b4261,bold',
        pane_active_border_style='fg=#7aa2f7,bold',
        status=_DARK_STATUS,
    ),
    'contrast': TmuxThemeProfile(
        name='contrast',
        fallback_label_style=_DEFAULT_FALLBACK_LABEL_STYLE,
        pane_border_style='fg=#565f89,bold',
        pane_active_border_style='fg=#89b4fa,bold',
        status=_DARK_STATUS,
        window_style='bg=#181825',
        window_active_style='bg=#1e1e2e',
    ),
    'light': TmuxThemeProfile(
        name='light',
        fallback_label_style=_LIGHT_FALLBACK_LABEL_STYLE,
        pane_border_style='fg=#bcc0cc,bold',
        pane_active_border_style='fg=#1e66f5,bold',
        status=_LIGHT_STATUS,
        pane_title_style='#[fg=#6c6f85]',
    ),
}

_CONTRAST_TERMINAL_FAMILIES = {'apple_terminal'}

_WINDOW_STATUS_FORMAT = ''
_WINDOW_STATUS_CURRENT_FORMAT = ''
_WINDOW_STATUS_SEPARATOR = ''
_PANE_BORDER_STATUS = 'top'
_PANE_BORDER_LINES = 'heavy'


def _visual(*, bg: str, border: str | None = None, active: str | None = None, fg: str = '#16161e') -> TmuxPaneVisual:
    border_color = str(border or bg).strip()
    active_color = str(active or border_color).strip()
    return TmuxPaneVisual(
        label_style=f'#[fg={fg}]#[bg={bg}]#[bold]',
        border_style=f'fg={border_color}',
        active_border_style=f'fg={active_color},bold',
    )


_CMD_VISUALS_DEFAULT: tuple[TmuxPaneVisual, ...] = (
    _visual(bg='#7dcfff', border='#5fb3d6', active='#7dcfff'),
    _visual(bg='#73daca', border='#4fb7a9', active='#73daca'),
    _visual(bg='#89b4fa', border='#6b8fd6', active='#89b4fa'),
    _visual(bg='#2ac3de', border='#1b9fb8', active='#2ac3de'),
)

_AGENT_VISUALS_DEFAULT: tuple[TmuxPaneVisual, ...] = (
    _visual(bg='#ff9e64', border='#d9824f', active='#ff9e64'),
    _visual(bg='#9ece6a', border='#7ca952', active='#9ece6a'),
    _visual(bg='#f7768e', border='#d85f78', active='#f7768e'),
    _visual(bg='#e0af68', border='#bd8d4f', active='#e0af68'),
    _visual(bg='#bb9af7', border='#9d7fda', active='#bb9af7'),
    _visual(bg='#73daca', border='#54bda7', active='#73daca'),
    _visual(bg='#7aa2f7', border='#5d82d6', active='#7aa2f7'),
    _visual(bg='#f6bd60', border='#d69f46', active='#f6bd60'),
    _visual(bg='#ff757f', border='#da5a66', active='#ff757f'),
    _visual(bg='#8bd5ca', border='#68b6aa', active='#8bd5ca'),
    _visual(bg='#c6a0f6', border='#a885d8', active='#c6a0f6'),
    _visual(bg='#a6da95', border='#84b777', active='#a6da95'),
    TmuxPaneVisual(
        label_style='#[fg=#16161e]#[bg=#f5bde6]#[bold]',
        border_style='fg=#d49ac5',
        active_border_style='fg=#f5bde6,bold',
    ),
)

_CMD_VISUALS_CONTRAST: tuple[TmuxPaneVisual, ...] = (
    _visual(bg='#7dcfff'),
    _visual(bg='#73daca'),
    _visual(bg='#89b4fa'),
    _visual(bg='#2ac3de'),
)

_AGENT_VISUALS_CONTRAST: tuple[TmuxPaneVisual, ...] = (
    _visual(bg='#ff9e64'),
    _visual(bg='#9ece6a'),
    _visual(bg='#f7768e'),
    _visual(bg='#e0af68'),
    _visual(bg='#bb9af7'),
    _visual(bg='#73daca'),
    _visual(bg='#7aa2f7'),
    _visual(bg='#f6bd60'),
    _visual(bg='#ff757f'),
    _visual(bg='#8bd5ca'),
    _visual(bg='#c6a0f6'),
    _visual(bg='#a6da95'),
    _visual(bg='#f5bde6'),
)

_CMD_VISUALS_LIGHT: tuple[TmuxPaneVisual, ...] = (
    _visual(bg='#1e66f5', fg='#eff1f5'),
    _visual(bg='#179299', fg='#eff1f5'),
    _visual(bg='#209fb5', fg='#eff1f5'),
    _visual(bg='#8839ef', fg='#eff1f5'),
)

_AGENT_VISUALS_LIGHT: tuple[TmuxPaneVisual, ...] = (
    _visual(bg='#fe640b', fg='#eff1f5'),
    _visual(bg='#40a02b', fg='#eff1f5'),
    _visual(bg='#d20f39', fg='#eff1f5'),
    _visual(bg='#8839ef', fg='#eff1f5'),
    _visual(bg='#1e66f5', fg='#eff1f5'),
    _visual(bg='#179299', fg='#eff1f5'),
    _visual(bg='#df8e1d', fg='#eff1f5'),
    _visual(bg='#7287fd', fg='#eff1f5'),
    _visual(bg='#209fb5', fg='#eff1f5'),
    _visual(bg='#ea76cb', fg='#eff1f5'),
    _visual(bg='#e64553', fg='#eff1f5'),
    _visual(bg='#04a5e5', fg='#eff1f5'),
)

_SIDEBAR_VISUAL_DEFAULT = TmuxPaneVisual(
    label_style='#[fg=#cdd6f4]#[bg=#45475a]#[bold]',
    border_style='fg=#6c7086',
    active_border_style='fg=#6c7086',
)

_SIDEBAR_VISUAL_LIGHT = TmuxPaneVisual(
    label_style='#[fg=#eff1f5]#[bg=#6c6f85]#[bold]',
    border_style='fg=#bcc0cc',
    active_border_style='fg=#9ca0b0',
)


def _env(environ: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return environ if environ is not None else os.environ


def detect_terminal_family(environ: Mapping[str, str] | None = None) -> str:
    env = _env(environ)
    for key in ('TERM_PROGRAM', 'LC_TERMINAL'):
        value = str(env.get(key, '') or '').strip().lower()
        if value:
            return value
    return str(env.get('TERM', '') or '').strip().lower()


def _normalize_profile_name(value: str | None) -> str | None:
    name = str(value or '').strip().lower()
    if not name:
        return None
    return name if name in _THEME_PROFILES else None


def tmux_theme_profile(environ: Mapping[str, str] | None = None) -> str:
    env = _env(environ)
    preference = load_theme_preference(env)
    if preference is not None:
        normalized = _normalize_profile_name(
            effective_theme_preference(preference, env).tmux_profile
        )
        if normalized is not None:
            return normalized
    bootstrap = _normalize_profile_name(env.get('CCB_TMUX_THEME_PROFILE'))
    if bootstrap is not None:
        return bootstrap
    family = detect_terminal_family(env)
    return 'contrast' if family in _CONTRAST_TERMINAL_FAMILIES else 'default'


def tmux_status_interval(environ: Mapping[str, str] | None = None) -> str:
    raw = str(_env(environ).get('CCB_TMUX_STATUS_INTERVAL', '') or '').strip()
    if raw.isdigit() and int(raw) > 0:
        return str(int(raw))
    return '5'


def theme_profile_definition(profile_name: str | None = None, *, environ: Mapping[str, str] | None = None) -> TmuxThemeProfile:
    resolved = _normalize_profile_name(profile_name) or tmux_theme_profile(environ)
    return _THEME_PROFILES.get(resolved, _THEME_PROFILES['default'])


def pane_border_format(profile_name: str | None = None, *, environ: Mapping[str, str] | None = None) -> str:
    profile = theme_profile_definition(profile_name, environ=environ)
    return (
        '#{?#{@ccb_agent},'
        f'#{{?#{{@ccb_label_style}},#{{@ccb_label_style}},{profile.fallback_label_style}}} '
        '#{@ccb_agent} #[default],'
        f'{profile.pane_title_style} #{{pane_title}} #[default]}}'
    )


def render_tmux_session_theme(
    *,
    ccb_version: str,
    status_script: str | None,
    git_script: str | None,
    environ: Mapping[str, str] | None = None,
    profile_name: str | None = None,
) -> RenderedTmuxSessionTheme:
    profile = theme_profile_definition(profile_name, environ=environ)
    normalized_version = _normalized_ccb_version(ccb_version)
    focus_agent = '#{?#{@ccb_agent},#{@ccb_agent},-}'
    palette = profile.status
    accent = palette.mode_accent
    label = '#{?client_prefix,KEY,#{?pane_in_mode,COPY,INPUT}}'
    git_info = f'#({git_script} "#{{pane_current_path}}")' if git_script else '-'
    status_indicator = f'#({status_script} modern "#{{pane_current_path}}")' if status_script else '-'

    session_options = {
        '@ccb_active': '1',
        '@ccb_version': normalized_version,
        '@ccb_theme_profile': profile.name,
        'status-position': 'bottom',
        'status-interval': tmux_status_interval(environ),
        'status-style': _status_style(palette),
        'status': 'on',
        'status-left-length': '80',
        'status-right-length': '120',
        'status-format[0]': _status_format_0(palette),
        'status-left': (
            f'#[fg={palette.segment_fg},bg={accent},bold] {label} '
            f'#[fg={accent},bg={palette.git_bg}]'
            f'#[fg={palette.segment_fg},bg={palette.git_bg}] {git_info} '
            f'#[fg={palette.git_bg},bg={palette.background}]'
        ),
        'status-right': (
            f'#[fg={palette.focus_bg},bg={palette.background}]'
            f'#[fg={palette.segment_fg},bg={palette.focus_bg},bold] {focus_agent} '
            f'#[fg={palette.version_bg},bg={palette.focus_bg}]'
            f'#[fg={palette.segment_fg},bg={palette.version_bg},bold] CCB:{normalized_version} '
            f'#[fg={palette.indicator_bg},bg={palette.version_bg}]'
            f'#[fg={palette.indicator_fg},bg={palette.indicator_bg}] {status_indicator} '
            f'#[fg={palette.time_bg},bg={palette.indicator_bg}]'
            f'#[fg={palette.time_fg},bg={palette.time_bg},bold] %m/%d %a %H:%M #[default]'
        ),
        'window-status-format': _WINDOW_STATUS_FORMAT,
        'window-status-current-format': _WINDOW_STATUS_CURRENT_FORMAT,
        'window-status-separator': _WINDOW_STATUS_SEPARATOR,
    }
    window_options = {
        'pane-border-status': _PANE_BORDER_STATUS,
        'pane-border-lines': _PANE_BORDER_LINES,
        'pane-border-style': profile.pane_border_style,
        'pane-active-border-style': profile.pane_active_border_style,
        'pane-border-format': pane_border_format(profile.name),
    }
    if profile.window_style:
        window_options['window-style'] = profile.window_style
    if profile.window_active_style:
        window_options['window-active-style'] = profile.window_active_style
    return RenderedTmuxSessionTheme(
        profile_name=profile.name,
        session_options=session_options,
        window_options=window_options,
    )


def _pane_palette(*, profile_name: str, is_cmd: bool) -> tuple[TmuxPaneVisual, ...]:
    if profile_name == 'contrast':
        return _CMD_VISUALS_CONTRAST if is_cmd else _AGENT_VISUALS_CONTRAST
    if profile_name == 'light':
        return _CMD_VISUALS_LIGHT if is_cmd else _AGENT_VISUALS_LIGHT
    return _CMD_VISUALS_DEFAULT if is_cmd else _AGENT_VISUALS_DEFAULT


def pane_visual(
    *,
    project_id: str | None = None,
    slot_key: str | None = None,
    order_index: int | None = None,
    is_cmd: bool = False,
    role: str | None = None,
    profile_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> TmuxPaneVisual:
    resolved_profile = theme_profile_definition(profile_name, environ=environ).name
    if str(role or '').strip().lower() == 'sidebar':
        return _SIDEBAR_VISUAL_LIGHT if resolved_profile == 'light' else _SIDEBAR_VISUAL_DEFAULT
    visuals = _pane_palette(profile_name=resolved_profile, is_cmd=is_cmd)
    return _select_visual(visuals, project_id=project_id, slot_key=slot_key, fallback_index=order_index)


def _select_visual(
    visuals: tuple[TmuxPaneVisual, ...],
    *,
    project_id: str | None,
    slot_key: str | None,
    fallback_index: int | None,
) -> TmuxPaneVisual:
    if project_id and slot_key:
        key = f'{project_id}:{slot_key}'
        return visuals[_stable_index(key, len(visuals))]
    index = max(0, int(fallback_index or 0))
    return visuals[index % len(visuals)]


def _stable_index(key: str, size: int) -> int:
    if size <= 0:
        return 0
    digest = hashlib.sha256(str(key or '').encode('utf-8')).hexdigest()
    return int(digest[:8], 16) % size


def _status_style(palette: TmuxStatusPalette) -> str:
    return f'bg={palette.background} fg={palette.foreground}'


def _status_format_0(palette: TmuxStatusPalette) -> str:
    return (
        f'#[align=left,bg={palette.background}]#{{T:status-left}}'
        f'#[align=centre,fg={palette.muted}]#{{b:pane_current_path}}'
        '#[align=right]#{T:status-right}'
    )


def shell_exports(
    *,
    ccb_version: str,
    status_script: str | None,
    git_script: str | None,
    environ: Mapping[str, str] | None = None,
    profile_name: str | None = None,
) -> str:
    rendered = render_tmux_session_theme(
        ccb_version=ccb_version,
        status_script=status_script,
        git_script=git_script,
        environ=environ,
        profile_name=profile_name,
    )
    items = {
        'CCB_TMUX_RENDERED_THEME_PROFILE': rendered.profile_name,
        'CCB_TMUX_RENDERED_STATUS_POSITION': rendered.session_options['status-position'],
        'CCB_TMUX_RENDERED_STATUS_INTERVAL': rendered.session_options['status-interval'],
        'CCB_TMUX_RENDERED_STATUS_STYLE': rendered.session_options['status-style'],
        'CCB_TMUX_RENDERED_STATUS_LINES': rendered.session_options['status'],
        'CCB_TMUX_RENDERED_STATUS_LEFT_LENGTH': rendered.session_options['status-left-length'],
        'CCB_TMUX_RENDERED_STATUS_RIGHT_LENGTH': rendered.session_options['status-right-length'],
        'CCB_TMUX_RENDERED_STATUS_FORMAT_0': rendered.session_options['status-format[0]'],
        'CCB_TMUX_RENDERED_STATUS_LEFT': rendered.session_options['status-left'],
        'CCB_TMUX_RENDERED_STATUS_RIGHT': rendered.session_options['status-right'],
        'CCB_TMUX_RENDERED_WINDOW_STATUS_FORMAT': rendered.session_options['window-status-format'],
        'CCB_TMUX_RENDERED_WINDOW_STATUS_CURRENT_FORMAT': rendered.session_options['window-status-current-format'],
        'CCB_TMUX_RENDERED_WINDOW_STATUS_SEPARATOR': rendered.session_options['window-status-separator'],
        'CCB_TMUX_RENDERED_PANE_BORDER_STATUS': rendered.window_options['pane-border-status'],
        'CCB_TMUX_RENDERED_PANE_BORDER_STYLE': rendered.window_options['pane-border-style'],
        'CCB_TMUX_RENDERED_PANE_ACTIVE_BORDER_STYLE': rendered.window_options['pane-active-border-style'],
        'CCB_TMUX_RENDERED_PANE_BORDER_FORMAT': rendered.window_options['pane-border-format'],
        'CCB_TMUX_RENDERED_WINDOW_STYLE': rendered.window_options.get('window-style', ''),
        'CCB_TMUX_RENDERED_WINDOW_ACTIVE_STYLE': rendered.window_options.get('window-active-style', ''),
    }
    return '\n'.join(f'{key}={shlex.quote(value)}' for key, value in items.items())


def _normalized_ccb_version(value: str) -> str:
    return str(value or '?').strip() or '?'


__all__ = [
    'RenderedTmuxSessionTheme',
    'TmuxPaneVisual',
    'TmuxStatusPalette',
    'TmuxThemeProfile',
    'detect_terminal_family',
    'pane_border_format',
    'pane_visual',
    'render_tmux_session_theme',
    'shell_exports',
    'theme_profile_definition',
    'tmux_status_interval',
    'tmux_theme_profile',
]
