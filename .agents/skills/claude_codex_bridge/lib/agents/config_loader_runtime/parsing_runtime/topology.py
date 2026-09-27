from __future__ import annotations

from typing import Any

from agents.config_loader_runtime.parsing_runtime.agent_specs import build_agent_spec
from agents.config_loader_runtime.parsing_runtime.expectations import expect_bool, expect_mapping, expect_string, expect_string_list
from agents.models import (
    AgentValidationError,
    SidebarSpec,
    SidebarViewSpec,
    ToolWindowSpec,
    WindowSpec,
    is_layout_tool_alias,
    is_native_windows_only_layout_tool_alias,
    normalize_agent_name,
    normalize_layout_tool_alias,
    parse_layout_spec,
)
from platforms.windows.os_platform import is_native_windows

from ..common import ConfigValidationError

_UNSUPPORTED_LEGACY_NEOVIM_TOOL_NAMES = {'neovim', 'nvim'}


_SIDEBAR_TOPOLOGY_FIELDS = {'mode', 'width', 'bottom_height', 'position'}
_SIDEBAR_VIEW_FIELDS = {'agents_height', 'comms_height', 'tips_height', 'comms_limit', 'comms_compact', 'tips_enabled', 'tips'}


def parse_sidebar(raw_ui: Any) -> SidebarSpec | None:
    if raw_ui is None:
        return None
    ui = expect_mapping(raw_ui, field_name='ui')
    unknown_ui = sorted(set(ui) - {'sidebar'})
    if unknown_ui:
        raise ConfigValidationError(f'ui contains unknown fields: {", ".join(unknown_ui)}')
    if ui.get('sidebar') is None:
        return None
    sidebar = expect_mapping(ui['sidebar'], field_name='ui.sidebar')
    unknown_sidebar = sorted(set(sidebar) - (_SIDEBAR_TOPOLOGY_FIELDS | _SIDEBAR_VIEW_FIELDS | {'view'}))
    if unknown_sidebar:
        raise ConfigValidationError(
            f'ui.sidebar contains unknown fields: {", ".join(unknown_sidebar)}'
        )
    try:
        return SidebarSpec(
            mode=sidebar.get('mode', 'every_window'),
            width=sidebar.get('width', '15%'),
            bottom_height=sidebar.get('bottom_height', 20),
            position=sidebar.get('position', 'left'),
        )
    except AgentValidationError as exc:
        raise ConfigValidationError(str(exc)) from exc


def parse_sidebar_view(raw_ui: Any) -> SidebarViewSpec | None:
    if raw_ui is None:
        return None
    ui = expect_mapping(raw_ui, field_name='ui')
    if ui.get('sidebar') is None:
        return None
    sidebar = expect_mapping(ui['sidebar'], field_name='ui.sidebar')
    inline_view = {key: sidebar[key] for key in _SIDEBAR_VIEW_FIELDS if key in sidebar}
    legacy_view = sidebar.get('view')
    if legacy_view is None and not inline_view:
        return None
    view: dict[str, Any] = {}
    if legacy_view is not None:
        legacy_map = expect_mapping(legacy_view, field_name='ui.sidebar.view')
        unknown_view = sorted(set(legacy_map) - _SIDEBAR_VIEW_FIELDS)
        if unknown_view:
            raise ConfigValidationError(
                f'ui.sidebar.view contains unknown fields: {", ".join(unknown_view)}'
            )
        view.update(legacy_map)
    view.update(inline_view)
    field_prefix = 'ui.sidebar' if inline_view else 'ui.sidebar.view'
    try:
        return SidebarViewSpec(
            agents_height=view.get('agents_height', '50%'),
            comms_height=view.get('comms_height', '15%'),
            tips_height=view.get('tips_height', '35%'),
            comms_limit=view.get('comms_limit', 5),
            comms_compact=expect_bool(view.get('comms_compact', True), field_name=f'{field_prefix}.comms_compact'),
            tips_enabled=expect_bool(view.get('tips_enabled', True), field_name=f'{field_prefix}.tips_enabled'),
            tips=expect_string_list(view.get('tips', list(SidebarViewSpec().tips)), field_name=f'{field_prefix}.tips'),
            field_prefix=field_prefix,
        )
    except AgentValidationError as exc:
        raise ConfigValidationError(str(exc)) from exc


def parse_topology_windows(raw_windows: Any) -> tuple[WindowSpec, ...] | None:
    if raw_windows is None:
        return None
    windows_map = expect_mapping(raw_windows, field_name='windows')
    if not windows_map:
        raise ConfigValidationError('windows cannot be empty')
    windows: list[WindowSpec] = []
    seen_agents: set[str] = set()
    for index, (raw_name, raw_layout) in enumerate(windows_map.items()):
        if not isinstance(raw_name, str):
            raise ConfigValidationError('windows keys must be strings')
        layout_text = expect_string(raw_layout, field_name=f'windows.{raw_name}')
        try:
            layout = parse_layout_spec(layout_text)
            leaves = layout.iter_leaves()
            agent_names: list[str] = []
            tool_names: list[str] = []
            for leaf in leaves:
                if leaf.name.strip().lower() == 'cmd':
                    raise ConfigValidationError('cmd is not supported in windows topology')
                native_shell_alias = is_native_windows_only_layout_tool_alias(leaf.name)
                # A provider suffix unambiguously denotes an Agent. Preserve
                # existing Agent names such as ``bash:codex`` while keeping the
                # provider-less ``bash`` spelling as a native-Windows tool pane.
                if _leaf_is_tool_alias(leaf):
                    if leaf.provider is not None:
                        raise ConfigValidationError(
                            f'windows.{raw_name}: tool alias {leaf.name!r} must not declare a provider'
                        )
                    if native_shell_alias and not is_native_windows():
                        raise ConfigValidationError(
                            f'windows.{raw_name}: tool alias {leaf.name!r} is only available on native Windows'
                        )
                    try:
                        normalized_tool = normalize_layout_tool_alias(leaf.name)
                    except AgentValidationError as exc:
                        raise ConfigValidationError(str(exc)) from exc
                    if normalized_tool in tool_names:
                        raise ConfigValidationError(
                            f'duplicate tool alias in windows.{raw_name}: {normalized_tool}'
                        )
                    tool_names.append(normalized_tool)
                    continue
                if leaf.provider is None:
                    raise ConfigValidationError(
                        f'windows.{raw_name}: agent leaf {leaf.name!r} must declare a provider'
                    )
                try:
                    normalized_name = normalize_agent_name(leaf.name)
                except AgentValidationError as exc:
                    raise ConfigValidationError(str(exc)) from exc
                if normalized_name in seen_agents:
                    raise ConfigValidationError(
                        f'duplicate agent across windows: {normalized_name}'
                    )
                seen_agents.add(normalized_name)
                agent_names.append(normalized_name)
            windows.append(
                WindowSpec(
                    name=raw_name,
                    order=index,
                    layout_spec=layout.render(),
                    agent_names=tuple(agent_names),
                    tool_names=tuple(tool_names),
                )
            )
        except ConfigValidationError:
            raise
        except AgentValidationError as exc:
            raise ConfigValidationError(str(exc)) from exc
        except Exception as exc:
            raise ConfigValidationError(f'windows.{raw_name}: invalid layout: {exc}') from exc
    return tuple(windows)


def parse_tool_windows(raw_tool_windows: Any) -> tuple[ToolWindowSpec, ...]:
    if raw_tool_windows is None:
        return ()
    tool_map = expect_mapping(raw_tool_windows, field_name='tool_windows')
    tools: list[ToolWindowSpec] = []
    for index, (raw_name, raw_spec) in enumerate(tool_map.items()):
        if not isinstance(raw_name, str):
            raise ConfigValidationError('tool_windows keys must be strings')
        spec = expect_mapping(raw_spec, field_name=f'tool_windows.{raw_name}')
        unknown = sorted(set(spec) - {'command', 'label', 'show_in_sidebar'})
        if unknown:
            raise ConfigValidationError(
                f'tool_windows.{raw_name} contains unknown fields: {", ".join(unknown)}'
            )
        command = expect_string(spec.get('command'), field_name=f'tool_windows.{raw_name}.command')
        _reject_legacy_neovim_tool_window(raw_name, command=command)
        label = None
        if spec.get('label') is not None:
            label = expect_string(spec.get('label'), field_name=f'tool_windows.{raw_name}.label')
        try:
            tools.append(
                ToolWindowSpec(
                    name=raw_name,
                    order=index,
                    command=command,
                    label=label,
                    show_in_sidebar=expect_bool(
                        spec.get('show_in_sidebar', True),
                        field_name=f'tool_windows.{raw_name}.show_in_sidebar',
                    ),
                )
            )
        except AgentValidationError as exc:
            raise ConfigValidationError(str(exc)) from exc
    return tuple(tools)


def _reject_legacy_neovim_tool_window(raw_name: str, *, command: str) -> None:
    name = str(raw_name or '').strip().lower()
    command_words = str(command or '').replace('"', ' ').replace("'", ' ').split()
    if name in _UNSUPPORTED_LEGACY_NEOVIM_TOOL_NAMES or 'ccb-nvim' in command_words:
        raise ConfigValidationError(
            'managed Neovim tool windows are no longer supported; use the rich layout alias instead'
        )


def agents_from_topology_windows(
    windows: tuple[WindowSpec, ...] | None,
    *,
    raw_agents: Any,
) -> dict[str, object]:
    if windows is None:
        return {}
    window_agent_names = {
        agent_name
        for window in windows
        for agent_name in tuple(getattr(window, 'agent_names', ()) or ())
    }
    overlays = _topology_agent_overlays(raw_agents, referenced_agent_names=window_agent_names)
    agents: dict[str, object] = {}
    for window in windows:
        layout = parse_layout_spec(window.layout_spec)
        for leaf in layout.iter_leaves():
            if _leaf_is_tool_alias(leaf):
                continue
            name = normalize_agent_name(leaf.name)
            raw_spec = _merge_topology_agent_overlay(
                _topology_leaf_agent_defaults(leaf),
                overlays.get(name, {}),
            )
            _validate_topology_overlay_provider(name, raw_spec=raw_spec, leaf_provider=leaf.provider)
            agents[name] = build_agent_spec(name, raw_spec)
    return agents


def _leaf_is_tool_alias(leaf: object) -> bool:
    name = getattr(leaf, 'name', '')
    provider = getattr(leaf, 'provider', None)
    return is_layout_tool_alias(name) and not (
        is_native_windows_only_layout_tool_alias(name) and provider is not None
    )


def _topology_agent_overlays(
    raw_agents: Any,
    *,
    referenced_agent_names: set[str],
) -> dict[str, dict[str, Any]]:
    raw_agents_map = expect_mapping({} if raw_agents is None else raw_agents, field_name='agents')
    overlays: dict[str, dict[str, Any]] = {}
    for raw_name, raw_spec in raw_agents_map.items():
        if not isinstance(raw_name, str):
            raise ConfigValidationError('agents table keys must be strings')
        try:
            normalized_name = normalize_agent_name(raw_name)
        except AgentValidationError as exc:
            raise ConfigValidationError(str(exc)) from exc
        if normalized_name not in referenced_agent_names:
            continue
        if normalized_name in overlays:
            raise ConfigValidationError(f'duplicate agent name after normalization: {normalized_name}')
        overlays[normalized_name] = expect_mapping(raw_spec, field_name=f'agents.{raw_name}')
    return overlays


def _topology_leaf_agent_defaults(leaf) -> dict[str, object]:
    return {
        'provider': leaf.provider,
        'target': '.',
        'workspace_mode': 'git-worktree'
        if str(leaf.workspace_mode or '').strip() == 'worktree'
        else 'inplace',
        'restore': 'auto',
        'permission': 'manual',
    }


def _merge_topology_agent_overlay(
    defaults: dict[str, object],
    overlay: dict[str, Any],
) -> dict[str, object]:
    merged = dict(defaults)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_topology_agent_overlay(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _validate_topology_overlay_provider(
    agent_name: str,
    *,
    raw_spec: dict[str, object],
    leaf_provider: object,
) -> None:
    provider = expect_string(raw_spec.get('provider'), field_name=f'agents.{agent_name}.provider')
    if provider.strip().lower() != str(leaf_provider or '').strip().lower():
        raise ConfigValidationError(
            f'agent {agent_name!r} provider conflicts between windows and agents table'
        )


__all__ = [
    'agents_from_topology_windows',
    'parse_sidebar',
    'parse_sidebar_view',
    'parse_tool_windows',
    'parse_topology_windows',
]
