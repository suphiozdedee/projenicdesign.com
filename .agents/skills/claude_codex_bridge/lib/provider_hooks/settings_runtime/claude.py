from __future__ import annotations

import json
from pathlib import Path
import re
import shlex

from provider_backends.claude.home_layout import ClaudeHomeLayout, claude_layout_for_home

from .common import load_json, save_json, workspace_key

_CLAUDE_ACTIVITY_EVENTS = (
    'SessionStart',
    'UserPromptSubmit',
    'PreToolUse',
    'PermissionRequest',
    'Notification',
    'PostToolUse',
    'Stop',
)
_CCB_FINISH_HOOK_NAME = 'ccb-provider-finish-hook'
_CCB_ACTIVITY_HOOK_NAME = 'ccb-provider-activity-hook'
_LEGACY_CCB_HOOK_NAMES = {_CCB_FINISH_HOOK_NAME, _CCB_ACTIVITY_HOOK_NAME}
_PYTHON_EXECUTABLE_RE = re.compile(r'^python(?:\d+(?:\.\d+)*)?$', re.IGNORECASE)


def migrate_legacy_project_ccb_hooks(*, workspace_root: Path) -> tuple[Path, ...]:
    migrated: list[Path] = []
    settings_dir = Path(workspace_root).expanduser() / '.claude'
    for settings_path in (
        settings_dir / 'settings.json',
        settings_dir / 'settings.local.json',
    ):
        if _migrate_legacy_project_ccb_hook_file(settings_path):
            migrated.append(settings_path)
    return tuple(migrated)


def install_claude_hooks(*, home_root: Path, command: str) -> Path:
    settings_path = claude_layout_for_home(Path(home_root).expanduser()).settings_path
    data = _load_settings(settings_path)
    hooks = _hooks_payload(data)
    groups = _event_groups(hooks, event_name='Stop')
    groups = _prune_ccb_managed_hook_groups(
        groups,
        current_command=command,
        managed_hook_name=_CCB_FINISH_HOOK_NAME,
    )
    if not claude_event_has_command(groups, command):
        groups.append(_command_hook_group(command))
    hooks['Stop'] = groups
    return save_json(settings_path, data)


def install_claude_activity_hooks(*, home_root: Path, command: str) -> Path:
    settings_path = claude_layout_for_home(Path(home_root).expanduser()).settings_path
    data = _load_settings(settings_path)
    hooks = _hooks_payload(data)
    for event_name in _CLAUDE_ACTIVITY_EVENTS:
        groups = _event_groups(hooks, event_name=event_name)
        groups = _prune_ccb_managed_hook_groups(
            groups,
            current_command=command,
            managed_hook_name=_CCB_ACTIVITY_HOOK_NAME,
        )
        if not claude_event_has_command(groups, command):
            groups.append(_command_hook_group(command))
        hooks[event_name] = groups
    return save_json(settings_path, data)


def trust_claude_workspace(*, home_root: Path, workspace_path: Path) -> Path:
    layout = claude_layout_for_home(Path(home_root).expanduser())
    data = _load_settings(layout.trust_path)
    key = workspace_key(workspace_path)
    record = data.get(key)
    if not isinstance(record, dict):
        record = {}
    record['hasTrustDialogAccepted'] = True
    data[key] = record
    projects = data.get('projects')
    if not isinstance(projects, dict):
        projects = {}
    project_record = projects.get(key)
    if not isinstance(project_record, dict):
        project_record = {}
    project_record['hasTrustDialogAccepted'] = True
    projects[key] = project_record
    data['projects'] = projects
    save_json(layout.trust_path, data)
    return layout.trust_path


def claude_hook_home_layout(home_root: Path) -> ClaudeHomeLayout:
    return claude_layout_for_home(Path(home_root).expanduser())


def claude_event_has_command(groups: list[object], command: str) -> bool:
    for group in groups:
        if not isinstance(group, dict):
            continue
        hooks = group.get('hooks')
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            if str(hook.get('type') or '').strip().lower() != 'command':
                continue
            if str(hook.get('command') or '').strip() == command:
                return True
    return False


def _prune_ccb_managed_hook_groups(
    groups: list[object],
    *,
    current_command: str,
    managed_hook_name: str,
) -> list[object]:
    pruned: list[object] = []
    for group in groups:
        if not isinstance(group, dict):
            pruned.append(group)
            continue
        hooks = group.get('hooks')
        if not isinstance(hooks, list):
            pruned.append(group)
            continue

        kept_hooks: list[object] = []
        for hook in hooks:
            if not _is_stale_ccb_managed_hook(
                hook,
                current_command=current_command,
                managed_hook_name=managed_hook_name,
            ):
                kept_hooks.append(hook)
        if kept_hooks:
            next_group = dict(group)
            next_group['hooks'] = kept_hooks
            pruned.append(next_group)
    return pruned


def _is_stale_ccb_managed_hook(
    hook: object,
    *,
    current_command: str,
    managed_hook_name: str,
) -> bool:
    if not isinstance(hook, dict):
        return False
    if str(hook.get('type') or '').strip().lower() != 'command':
        return False
    command = str(hook.get('command') or '').strip()
    return managed_hook_name in command and command != current_command


def _migrate_legacy_project_ccb_hook_file(settings_path: Path) -> bool:
    try:
        payload = json.loads(settings_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get('hooks'), dict):
        return False
    hooks = payload['hooks']
    changed = False
    for event_name, raw_groups in tuple(hooks.items()):
        if not isinstance(raw_groups, list):
            continue
        groups, event_changed = _remove_legacy_python_wrapped_ccb_hooks(raw_groups)
        if event_changed:
            hooks[event_name] = groups
            changed = True
    if changed:
        save_json(settings_path, payload)
    return changed


def _remove_legacy_python_wrapped_ccb_hooks(groups: list[object]) -> tuple[list[object], bool]:
    migrated: list[object] = []
    changed = False
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
            migrated.append(group)
            continue
        kept = [hook for hook in group['hooks'] if not _is_legacy_python_wrapped_ccb_hook(hook)]
        if len(kept) == len(group['hooks']):
            migrated.append(group)
            continue
        changed = True
        if kept:
            next_group = dict(group)
            next_group['hooks'] = kept
            migrated.append(next_group)
    return migrated, changed


def _is_legacy_python_wrapped_ccb_hook(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    if str(hook.get('type') or '').strip().lower() != 'command':
        return False
    try:
        parts = shlex.split(str(hook.get('command') or '').strip())
    except ValueError:
        return False
    if len(parts) < 2 or not _PYTHON_EXECUTABLE_RE.fullmatch(Path(parts[0]).name):
        return False
    launcher = Path(parts[1])
    return launcher.name in _LEGACY_CCB_HOOK_NAMES and not launcher.suffix


def _load_settings(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    return load_json(path)


def _hooks_payload(data: dict[str, object]) -> dict[str, object]:
    hooks = data.get('hooks')
    if not isinstance(hooks, dict):
        hooks = {}
    data['hooks'] = hooks
    return hooks


def _event_groups(hooks: dict[str, object], *, event_name: str) -> list[object]:
    groups = hooks.get(event_name)
    if not isinstance(groups, list):
        return []
    return groups


def _command_hook_group(command: str) -> dict[str, list[dict[str, str]]]:
    return {
        'hooks': [
            {
                'type': 'command',
                'command': command,
            }
        ]
    }


__all__ = [
    'claude_event_has_command',
    'claude_hook_home_layout',
    'install_claude_activity_hooks',
    'install_claude_hooks',
    'migrate_legacy_project_ccb_hooks',
    'trust_claude_workspace',
]
