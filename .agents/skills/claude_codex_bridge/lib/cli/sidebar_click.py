from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from ccbd.socket_client import CcbdClient, CcbdClientError
from sidebar_click_targets import relative_coordinate, resolve_sidebar_click_target, sidebar_tree_targets


@dataclass(frozen=True)
class SidebarClick:
    socket_path: Path
    mouse_y: int
    pane_top: int
    pane_height: int


def maybe_handle_sidebar_click_command(tokens: list[str], *, stderr: TextIO) -> int | None:
    if not tokens or tokens[0] != '__sidebar-click':
        return None
    try:
        click = _parse_sidebar_click(tokens[1:])
        focus_sidebar_click(click)
        return 0
    except Exception as exc:
        print(f'ccb sidebar click failed: {exc}', file=stderr)
        return 1


def focus_sidebar_click(click: SidebarClick, *, client_factory=CcbdClient) -> str | None:
    relative_y = relative_coordinate(click.mouse_y, click.pane_top, click.pane_height)
    if relative_y <= 0 or relative_y >= max(1, click.pane_height - 1):
        return None
    client = client_factory(click.socket_path)
    try:
        payload = client.project_sidebar_click(
            mouse_y=click.mouse_y,
            pane_top=click.pane_top,
            pane_height=click.pane_height,
            schema_version=1,
        )
    except AttributeError:
        return _focus_sidebar_click_with_project_view(click, client)
    except CcbdClientError as exc:
        if 'unknown op' not in str(exc).lower():
            raise
        return _focus_sidebar_click_with_project_view(click, client)
    target = payload.get('target') if isinstance(payload, dict) else None
    return str(target) if target else None


def _focus_sidebar_click_with_project_view(click: SidebarClick, client) -> str | None:
    view_payload = client.project_view(schema_version=1)
    view = view_payload.get('view') if isinstance(view_payload, dict) else None
    if not isinstance(view, dict):
        return None
    target = resolve_sidebar_click_target(
        view,
        mouse_y=click.mouse_y,
        pane_top=click.pane_top,
        pane_height=click.pane_height,
    )
    if target is None:
        return None
    kind, name = target
    namespace = view.get('namespace') if isinstance(view.get('namespace'), dict) else {}
    namespace_epoch = namespace.get('epoch') if isinstance(namespace, dict) else None
    if kind == 'window':
        client.project_focus_window(name, namespace_epoch=namespace_epoch)
    else:
        client.project_focus_agent(name, namespace_epoch=namespace_epoch)
    return f'{kind}:{name}'


def _parse_sidebar_click(argv: list[str]) -> SidebarClick:
    parser = argparse.ArgumentParser(prog='ccb __sidebar-click', add_help=False)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--mouse-y', required=True, type=int)
    parser.add_argument('--pane-top', required=True, type=int)
    parser.add_argument('--pane-height', required=True, type=int)
    args = parser.parse_args(argv)
    return SidebarClick(
        socket_path=Path(args.socket),
        mouse_y=int(args.mouse_y),
        pane_top=int(args.pane_top),
        pane_height=int(args.pane_height),
    )

__all__ = [
    'SidebarClick',
    'focus_sidebar_click',
    'maybe_handle_sidebar_click_command',
    'sidebar_tree_targets',
]
