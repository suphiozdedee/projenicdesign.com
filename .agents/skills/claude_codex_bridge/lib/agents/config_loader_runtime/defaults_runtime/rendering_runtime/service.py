from __future__ import annotations

import json
from pathlib import Path
import re

from agents.models import LayoutLeaf, LayoutNode, is_layout_tool_alias, normalize_agent_name, parse_layout_spec

from ..project import build_default_project_config
from .compact import can_render_compact
from .serialization import agent_spec_to_hybrid_overlay_dict, loop_capacity_to_config_dict


def render_default_project_config_text(*, provider: str | None = None, which_fn=None) -> str:
    return render_project_config_text(
        build_default_project_config(provider=provider, which_fn=which_fn)
    )


def render_project_config_text(config) -> str:
    if int(getattr(config, 'version', 2)) == 3:
        source_path = Path(str(getattr(config, 'source_path', '') or ''))
        if not source_path.is_file():
            raise ValueError('version 3 config rendering requires its original source_path')
        return source_path.read_text(encoding='utf-8')
    loop_payload = loop_capacity_to_config_dict(getattr(config, 'loop_capacity', None))
    if getattr(config, 'windows_explicit', False):
        return _render_windows_config_text(config)
    if can_render_compact(config) and not loop_payload:
        return f'{config.layout_spec}\n'
    hybrid_layout = _render_hybrid_layout(config)
    overlay_payload = _build_hybrid_overlay_payload(config)
    if not overlay_payload:
        return f'{hybrid_layout}\n'
    return f'{hybrid_layout}\n\n{render_config_document_text(overlay_payload)}'


_BARE_TOML_KEY_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')


def render_config_document_text(payload: dict[str, object]) -> str:
    """Render a validated config document as deterministic TOML text."""
    lines: list[str] = []
    _render_toml_mapping(lines, (), payload, emit_header=False)
    return '\n'.join(lines).rstrip() + '\n'


# Kept for internal callers and tests while the public renderer name is adopted.
_render_toml_document = render_config_document_text


def _render_toml_mapping(
    lines: list[str],
    path: tuple[str, ...],
    mapping: dict[str, object],
    *,
    emit_header: bool,
    is_array: bool = False,
) -> None:
    scalar_items: list[tuple[str, object]] = []
    table_items: list[tuple[str, dict[str, object]]] = []
    array_items: list[tuple[str, list[dict[str, object]]]] = []
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, dict):
            if not value:
                continue
            table_items.append((key, value))
        elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            array_items.append((key, value))
        else:
            scalar_items.append((key, value))

    if emit_header and (is_array or scalar_items or not table_items and not array_items):
        if lines:
            lines.append('')
        header = f'[[{_render_toml_path(path)}]]' if is_array else f'[{_render_toml_path(path)}]'
        lines.append(header)
    for key, value in scalar_items:
        lines.append(f'{_render_toml_key(key)} = {_render_toml_value(value)}')
    for key, value in table_items:
        _render_toml_mapping(lines, (*path, key), value, emit_header=True)
    for key, items in array_items:
        for item in items:
            _render_toml_mapping(lines, (*path, key), item, emit_header=True, is_array=True)


def _render_toml_path(path: tuple[str, ...]) -> str:
    return '.'.join(_render_toml_key(part) for part in path)


def _render_toml_key(key: str) -> str:
    return key if _BARE_TOML_KEY_PATTERN.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _render_toml_value(value: object) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return '[' + ', '.join(_render_toml_value(item) for item in value) + ']'
    if isinstance(value, dict):
        if not value:
            return '{}'
        pairs = ', '.join(
            f'{_render_toml_key(k)} = {_render_toml_value(v)}'
            for k, v in value.items()
        )
        return '{ ' + pairs + ' }'
    raise TypeError(f'unsupported TOML value type: {type(value).__name__}')


def _build_hybrid_overlay_payload(config) -> dict[str, object] | None:
    compact_agent_defaults = _compact_agent_defaults_by_name(_render_hybrid_layout(config))
    overlay_agents: dict[str, dict[str, object]] = {}
    ordered_names = list(config.default_agents) + [name for name in config.agents if name not in config.default_agents]
    for name in ordered_names:
        spec = config.agents[name]
        compact_defaults = compact_agent_defaults.get(name)
        if compact_defaults is None:
            continue
        overlay = agent_spec_to_hybrid_overlay_dict(
            spec,
            compact_provider=str(compact_defaults['provider']),
            compact_workspace_mode=str(compact_defaults['workspace_mode']),
        )
        if overlay:
            overlay_agents[name] = overlay
    loop_payload = loop_capacity_to_config_dict(getattr(config, 'loop_capacity', None))
    if not overlay_agents and not loop_payload:
        return None
    payload: dict[str, object] = {}
    if overlay_agents:
        payload['agents'] = overlay_agents
    if loop_payload:
        payload['loop'] = loop_payload
    return payload


def _render_windows_config_text(config) -> str:
    payload = _build_windows_payload(config)
    return render_config_document_text(payload)


def _build_windows_payload(config) -> dict[str, object]:
    payload: dict[str, object] = {
        'version': int(config.version),
        'entry_window': str(config.entry_window),
        'windows': {
            window.name: window.layout_spec
            for window in tuple(getattr(config, 'windows', ()) or ())
        },
    }
    tools = {
        tool.name: _tool_window_payload(tool)
        for tool in tuple(getattr(config, 'tool_windows', ()) or ())
    }
    if tools:
        payload['tool_windows'] = tools
    sidebar = _sidebar_payload(config)
    if sidebar:
        payload['ui'] = {'sidebar': sidebar}
    agent_payload = _windows_agent_overlay_payload(config)
    if agent_payload:
        payload['agents'] = agent_payload
    loop_payload = loop_capacity_to_config_dict(getattr(config, 'loop_capacity', None))
    if loop_payload:
        payload['loop'] = loop_payload
    return payload


def _tool_window_payload(tool) -> dict[str, object]:
    payload: dict[str, object] = {'command': tool.command}
    if str(tool.label or '') != str(tool.name):
        payload['label'] = tool.label
    if bool(tool.show_in_sidebar) is not True:
        payload['show_in_sidebar'] = bool(tool.show_in_sidebar)
    return payload


def _sidebar_payload(config) -> dict[str, object]:
    sidebar = getattr(config, 'sidebar', None)
    if sidebar is None:
        return {}
    payload: dict[str, object] = {}
    if sidebar.mode != 'every_window':
        payload['mode'] = sidebar.mode
    if sidebar.width != '15%':
        payload['width'] = sidebar.width
    if int(sidebar.bottom_height) != 20:
        payload['bottom_height'] = int(sidebar.bottom_height)
    if getattr(sidebar, 'position', 'left') != 'left':
        payload['position'] = sidebar.position
    sidebar_view = getattr(config, 'sidebar_view', None)
    if sidebar_view is not None:
        payload['agents_height'] = sidebar_view.agents_height
        payload['comms_height'] = sidebar_view.comms_height
        payload['tips_height'] = sidebar_view.tips_height
    return payload


def _windows_agent_overlay_payload(config) -> dict[str, dict[str, object]]:
    compact_agent_defaults: dict[str, dict[str, str]] = {}
    for window in tuple(getattr(config, 'windows', ()) or ()):
        compact_agent_defaults.update(
            _compact_agent_defaults_by_name(str(window.layout_spec))
        )
    overlay_agents: dict[str, dict[str, object]] = {}
    ordered_names = list(config.default_agents) + [name for name in config.agents if name not in config.default_agents]
    for name in ordered_names:
        spec = config.agents[name]
        compact_defaults = compact_agent_defaults.get(name)
        if compact_defaults is None:
            continue
        overlay = agent_spec_to_hybrid_overlay_dict(
            spec,
            compact_provider=str(compact_defaults['provider']),
            compact_workspace_mode=str(compact_defaults['workspace_mode']),
        )
        if overlay:
            overlay_agents[name] = overlay
    return overlay_agents


def _compact_agent_defaults_by_name(layout_spec: str) -> dict[str, dict[str, str]]:
    layout = parse_layout_spec(layout_spec)
    defaults: dict[str, dict[str, str]] = {}
    for leaf in layout.iter_leaves():
        normalized_name = normalize_agent_name(leaf.name) if leaf.name.lower() != 'cmd' else 'cmd'
        if normalized_name == 'cmd':
            continue
        defaults[normalized_name] = {
            'provider': str(leaf.provider or ''),
            'workspace_mode': 'git-worktree' if str(leaf.workspace_mode or '').strip() == 'worktree' else 'inplace',
        }
    return defaults


def _render_hybrid_layout(config) -> str:
    return _annotate_layout_with_agent_specs(parse_layout_spec(config.layout_spec), config).render()


def _annotate_layout_with_agent_specs(node, config):
    if node.kind == 'leaf':
        assert node.leaf is not None
        name = str(node.leaf.name or '').strip()
        if name.lower() == 'cmd':
            return LayoutNode(kind='leaf', leaf=LayoutLeaf(name='cmd', percent=node.leaf.percent))
        if is_layout_tool_alias(name):
            return LayoutNode(kind='leaf', leaf=LayoutLeaf(name=name, percent=node.leaf.percent))
        normalized_name = normalize_agent_name(name)
        spec = config.agents[normalized_name]
        return LayoutNode(
            kind='leaf',
            leaf=LayoutLeaf(
                name=normalized_name,
                provider=spec.provider,
                workspace_mode='worktree' if spec.workspace_mode.value == 'git-worktree' else None,
                percent=node.leaf.percent,
            ),
        )
    assert node.left is not None
    assert node.right is not None
    return LayoutNode(
        kind=node.kind,
        left=_annotate_layout_with_agent_specs(node.left, config),
        right=_annotate_layout_with_agent_specs(node.right, config),
    )


__all__ = [
    'render_config_document_text',
    'render_default_project_config_text',
    'render_project_config_text',
]
