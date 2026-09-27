from __future__ import annotations

import fnmatch
from pathlib import Path


ALLOWED_CHANGE_PATH_PREFIXES = (
    'allowed_change_paths:',
    'allowed change paths:',
    'allowed_change_path:',
    'allowed change path:',
    'changed_files:',
    'changed files:',
)


def declared_allowed_change_paths(task_text: str) -> list[str]:
    declared: list[str] = []
    collecting_allowed_block = False
    for raw_line in task_text.splitlines():
        stripped = raw_line.strip()
        line = stripped.lstrip('-*').strip()
        lower = line.lower()
        prefix_match = False
        for prefix in ALLOWED_CHANGE_PATH_PREFIXES:
            if lower.startswith(prefix):
                prefix_match = True
                collecting_allowed_block = True
                declared.extend(split_declared_paths(line.split(':', 1)[1]))
                break
        if prefix_match:
            continue
        if is_allowed_change_paths_heading(stripped):
            collecting_allowed_block = True
            continue
        if collecting_allowed_block:
            if not stripped:
                continue
            if stripped.startswith(('-', '*')):
                declared.extend(split_declared_paths(line))
                continue
            collecting_allowed_block = False
        for marker in ('update only ', 'fix only ', 'edit only ', 'change only ', 'modify only '):
            marker_index = lower.find(marker)
            if marker_index < 0:
                continue
            tail = line[marker_index + len(marker):]
            first_sentence = tail.split('.', 1)[0]
            declared.extend(split_declared_paths(first_sentence))
    normalized: list[str] = []
    seen: set[str] = set()
    for path in declared:
        directory_scope = path.strip().strip('`"\'').endswith(('/', '\\'))
        relative = safe_relative_path(path).as_posix()
        if directory_scope:
            relative = relative.rstrip('/') + '/'
        if relative in seen:
            continue
        normalized.append(relative)
        seen.add(relative)
    return normalized


def is_allowed_change_paths_heading(line: str) -> bool:
    heading = line.strip().lstrip('#').strip().rstrip(':').lower()
    return heading in {prefix.rstrip(':') for prefix in ALLOWED_CHANGE_PATH_PREFIXES}


def split_declared_paths(value: str) -> list[str]:
    paths: list[str] = []
    for raw in value.replace(';', ',').split(','):
        token = raw.strip().strip('`"\'')
        if not token:
            continue
        if ' ' in token:
            token = token.split()[0].strip('`"\'')
        token = token.rstrip('.,')
        if '/' not in token and not token.endswith('/') and not Path(token).suffix:
            continue
        paths.append(token)
    return paths


def normalize_scope_paths(values: object, *, field_name: str) -> list[str]:
    if not isinstance(values, list):
        raise ValueError(f'{field_name} must be a list')
    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        text = str(value or '').strip()
        if not text:
            raise ValueError(f'{field_name}[{index}] must be a non-empty path')
        directory_scope = text.endswith(('/', '\\'))
        relative = safe_relative_path(text)
        if not relative.parts:
            raise ValueError(f'{field_name}[{index}] must not be the project root')
        if relative.parts[0] in {'.ccb', '.git'}:
            raise ValueError(f'{field_name}[{index}] cannot target {relative.parts[0]} authority')
        normalized_path = relative.as_posix()
        if directory_scope:
            normalized_path = normalized_path.rstrip('/') + '/'
        if normalized_path in seen:
            continue
        normalized.append(normalized_path)
        seen.add(normalized_path)
    return normalized


def path_allowed_by_scope(changed_file: str, allowed_change_paths: list[str]) -> bool:
    changed = safe_relative_path(changed_file).as_posix()
    changed_path = Path(changed)
    for allowed in allowed_change_paths:
        scope = safe_relative_path(allowed).as_posix()
        scope_path = Path(scope)
        if changed == scope:
            return True
        if scope_has_glob(scope) and fnmatch.fnmatchcase(changed, scope):
            return True
        if changed_path.suffix and not scope_path.suffix and changed_path.with_suffix('').as_posix() == scope:
            return True
        if allowed.endswith('/') and changed.startswith(scope.rstrip('/') + '/'):
            return True
    return False


def scopes_overlap(left: str, right: str) -> bool:
    left_path = safe_relative_path(left).as_posix()
    right_path = safe_relative_path(right).as_posix()
    if left_path == right_path:
        return True
    if left.endswith('/') and right_path.startswith(left_path.rstrip('/') + '/'):
        return True
    if right.endswith('/') and left_path.startswith(right_path.rstrip('/') + '/'):
        return True
    if scope_has_glob(left_path) and fnmatch.fnmatchcase(right_path, left_path):
        return True
    if scope_has_glob(right_path) and fnmatch.fnmatchcase(left_path, right_path):
        return True
    if scope_has_glob(left_path) or scope_has_glob(right_path):
        left_prefix = _scope_static_prefix(left_path)
        right_prefix = _scope_static_prefix(right_path)
        if not left_prefix or not right_prefix:
            return True
        if left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix):
            return True
    return False


def safe_relative_path(value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError(f'unsafe workspace relative path {value!r}')
    return relative


def scope_has_glob(scope: str) -> bool:
    return any(marker in scope for marker in ('*', '?', '['))


def _scope_static_prefix(scope: str) -> str:
    positions = [scope.find(marker) for marker in ('*', '?', '[') if marker in scope]
    end = min(positions) if positions else len(scope)
    return scope[:end]


__all__ = [
    'ALLOWED_CHANGE_PATH_PREFIXES',
    'declared_allowed_change_paths',
    'is_allowed_change_paths_heading',
    'normalize_scope_paths',
    'path_allowed_by_scope',
    'safe_relative_path',
    'scope_has_glob',
    'scopes_overlap',
    'split_declared_paths',
]
