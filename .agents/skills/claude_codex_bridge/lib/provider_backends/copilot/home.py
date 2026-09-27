from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from cli.services.role_command_policy import role_command_policy_disables_inherited_assets
from provider_core.projected_assets import (
    projected_path_is_owned,
    tree_content_fingerprint,
    write_projected_marker,
)
from provider_core.one_way_inheritance import (
    copy_regular_file,
    copy_regular_tree,
    ensure_private_inheritance_directory,
)
from provider_core.source_home import current_provider_source_home
from storage.atomic import atomic_write_text


_CONFIG_HEADER = '// User settings belong in settings.json.\n// This file is managed automatically.\n'
_PROJECTION_LABEL = 'copilot-inherited-plugins'
_PROJECTION_RECORD_TYPE = 'ccb_copilot_plugin_projection'
_PROJECTION_MARKER_NAME = '.ccb-installed-plugins-projection.json'
_PLUGIN_MANIFEST_PATHS = (
    Path('.plugin/plugin.json'),
    Path('plugin.json'),
    Path('.github/plugin/plugin.json'),
    Path('.claude-plugin/plugin.json'),
)
_AUTH_CONFIG_KEYS = (
    'loggedInUsers',
    'copilotTokens',
    'githubToken',
    'oauth',
    'authentication',
    'credentials',
)


@dataclass(frozen=True)
class _PluginCandidate:
    identity: str
    name: str
    marketplace: str
    source_dir: Path
    relative_path: Path
    target_dir: Path
    tree_label: str
    content_fingerprint: str
    entry: dict[str, object]


@dataclass(frozen=True)
class _TreeOperation:
    kind: str
    target: Path
    label: str
    source: Path | None = None
    expected_fingerprint: str | None = None


@dataclass
class _AppliedTreeOperation:
    operation: _TreeOperation
    backup_target: Path | None
    backup_marker: Path | None


def materialize_copilot_home_config(
    target_home: Path,
    *,
    profile=None,
    source_home: Path | None = None,
    command_policy=None,
) -> Path:
    target_home = Path(target_home).expanduser()
    source_home = Path(source_home).expanduser() if source_home is not None else _system_copilot_home()
    target_home = ensure_private_inheritance_directory(target_home, source_home)
    _project_installed_plugins(
        source_home,
        target_home,
        enabled=(
            _inherits_config(profile)
            and not role_command_policy_disables_inherited_assets(command_policy)
        ),
    )
    _project_auth_state(
        source_home,
        target_home,
        enabled=_inherits_auth(profile),
    )
    return target_home


def _project_auth_state(source_home: Path, target_home: Path, *, enabled: bool) -> None:
    if not enabled:
        return
    source_document = _read_copilot_config(source_home / 'config.json')
    if source_document is not None:
        source_payload, _ = source_document
        target_document = _read_copilot_config(target_home / 'config.json')
        if target_document is None:
            target_payload, target_prefix = {}, _CONFIG_HEADER
        else:
            target_payload, target_prefix = target_document
        updated = _clone_json_object(target_payload)
        changed = False
        for key in _AUTH_CONFIG_KEYS:
            if key not in source_payload:
                continue
            value = _clone_json_value(source_payload[key])
            if updated.get(key) != value:
                updated[key] = value
                changed = True
        if changed:
            atomic_write_text(
                target_home / 'config.json',
                _copilot_config_text(updated, prefix=target_prefix),
            )
    for filename in ('auth.json', 'credentials.json'):
        copy_regular_file(source_home / filename, target_home / filename)
    for dirname in ('mcp-secrets', 'mcp-oauth-config'):
        copy_regular_tree(source_home / dirname, target_home / dirname)


def _project_installed_plugins(source_home: Path, target_home: Path, *, enabled: bool) -> bool:
    source_home = Path(source_home).expanduser()
    target_home = Path(target_home).expanduser()
    target_config = target_home / 'config.json'
    aggregate_marker = target_home / _PROJECTION_MARKER_NAME

    marker_present = aggregate_marker.exists() or aggregate_marker.is_symlink()
    marker_payload = _read_json_object(aggregate_marker) if marker_present else {}
    marker_owned = _aggregate_marker_matches(marker_payload)
    if marker_present and not marker_owned:
        return False

    if target_config.is_symlink():
        return False
    if target_config.exists():
        target_document = _read_copilot_config(target_config)
        if target_document is None:
            return False
        target_payload, target_prefix = target_document
    else:
        target_payload, target_prefix = {}, _CONFIG_HEADER

    target_entries = _installed_plugin_entries(target_payload)
    if target_entries is None:
        return False
    target_index = _entry_index(target_entries)
    if target_index is None:
        return False

    previous = _managed_entries(marker_payload, target_home=target_home) if marker_owned else {}
    if marker_owned and previous is None:
        return False

    if enabled:
        source_config = source_home / 'config.json'
        if source_config.is_symlink():
            return False
        source_document = _read_copilot_config(source_config)
        if source_document is None:
            return False
        source_payload, _source_prefix = source_document
        candidates = _source_candidates(source_payload, source_home=source_home, target_home=target_home)
        if candidates is None:
            return False
    else:
        if not marker_owned:
            return False
        candidates = ()

    desired = {candidate.identity: candidate for candidate in candidates}
    updated_entries = [_clone_json_value(entry) for entry in target_entries]
    replacements: dict[int, dict[str, object]] = {}
    removals: set[int] = set()
    appends: list[dict[str, object]] = []
    operations: list[_TreeOperation] = []
    next_managed: dict[str, dict[str, object]] = {}
    handled: set[str] = set()

    target_path_owners = _target_cache_path_owners(target_entries, target_home=target_home)
    if target_path_owners is None:
        return False

    for identity, record in previous.items():
        handled.add(identity)
        prior_entry = record['entry']
        relative_path = Path(str(record['relative_path']))
        prior_target = target_home / 'installed-plugins' / relative_path
        tree_label = str(record['tree_label'])
        current_index = target_index.get(identity)
        current_entry = target_entries[current_index] if current_index is not None else None
        tree_owned = projected_path_is_owned(prior_target, label=tree_label)
        tree_unchanged = (
            tree_owned
            and prior_target.is_dir()
            and not prior_target.is_symlink()
            and tree_content_fingerprint(prior_target) == record['content_fingerprint']
        )
        candidate = desired.get(identity)

        if bool(record.get('suppressed')):
            if candidate is not None and current_entry is None:
                next_managed[identity] = _suppressed_record(candidate)
            continue

        if current_entry == prior_entry and tree_unchanged:
            if candidate is None:
                removals.add(current_index)
                operations.append(_TreeOperation('remove', prior_target, tree_label))
                continue
            if candidate.relative_path == relative_path:
                replacements[current_index] = candidate.entry
                if not (
                    candidate.content_fingerprint == record['content_fingerprint']
                    and projected_path_is_owned(
                        prior_target,
                        label=tree_label,
                        source=candidate.source_dir,
                    )
                ):
                    operations.append(
                        _TreeOperation(
                            'install',
                            candidate.target_dir,
                            candidate.tree_label,
                            candidate.source_dir,
                            candidate.content_fingerprint,
                        )
                    )
                next_managed[identity] = _managed_record(candidate)
                continue
            if _candidate_target_available(
                candidate,
                target_index=target_index,
                target_path_owners=target_path_owners,
                allow_owned=False,
            ):
                replacements[current_index] = candidate.entry
                operations.extend(
                    (
                        _TreeOperation('remove', prior_target, tree_label),
                        _TreeOperation(
                            'install',
                            candidate.target_dir,
                            candidate.tree_label,
                            candidate.source_dir,
                            candidate.content_fingerprint,
                        ),
                    )
                )
                next_managed[identity] = _managed_record(candidate)
            else:
                next_managed[identity] = record
            continue

        if current_entry is None:
            if tree_unchanged:
                operations.append(_TreeOperation('remove', prior_target, tree_label))
            elif tree_owned:
                operations.append(_TreeOperation('abandon', prior_target, tree_label))
            if candidate is not None:
                next_managed[identity] = _suppressed_record(candidate)
            continue

        if tree_owned:
            operations.append(_TreeOperation('abandon', prior_target, tree_label))

    for candidate in candidates:
        if candidate.identity in handled:
            continue
        if candidate.identity in target_index:
            continue
        if not _candidate_target_available(
            candidate,
            target_index=target_index,
            target_path_owners=target_path_owners,
            allow_owned=False,
        ):
            continue
        appends.append(candidate.entry)
        operations.append(
            _TreeOperation(
                'install',
                candidate.target_dir,
                candidate.tree_label,
                candidate.source_dir,
                candidate.content_fingerprint,
            )
        )
        next_managed[candidate.identity] = _managed_record(candidate)

    for index, entry in replacements.items():
        updated_entries[index] = _clone_json_value(entry)
    updated_entries = [entry for index, entry in enumerate(updated_entries) if index not in removals]
    updated_entries.extend(_clone_json_value(entry) for entry in appends)

    updated_payload = _clone_json_object(target_payload)
    if updated_entries:
        updated_payload['installedPlugins'] = updated_entries
    else:
        updated_payload.pop('installedPlugins', None)

    target_changed = updated_payload != target_payload
    original_config_text = _read_text(target_config) if target_config.exists() else None
    original_marker_text = _read_text(aggregate_marker) if marker_present else None
    next_marker = _next_aggregate_marker(
        marker_payload,
        marker_owned=marker_owned,
        source_home=source_home,
        managed=next_managed,
    )
    marker_changed = (
        (next_marker is None and marker_present)
        or (next_marker is not None and _read_text(aggregate_marker) != _json_text(next_marker))
    )

    if not operations and not target_changed and not marker_changed:
        return bool(next_managed)

    return _commit_projection_transaction(
        target_home=target_home,
        target_config=target_config,
        target_prefix=target_prefix,
        updated_payload=updated_payload,
        target_changed=target_changed,
        aggregate_marker=aggregate_marker,
        next_marker=next_marker,
        marker_changed=marker_changed,
        operations=operations,
        original_config_text=original_config_text,
        original_marker_text=original_marker_text,
    )


def _source_candidates(
    source_payload: dict[str, object],
    *,
    source_home: Path,
    target_home: Path,
) -> tuple[_PluginCandidate, ...] | None:
    if 'installedPlugins' not in source_payload:
        return None
    raw_entries = source_payload.get('installedPlugins')
    if not isinstance(raw_entries, list):
        return None
    source_root = source_home / 'installed-plugins'
    target_root = target_home / 'installed-plugins'
    candidates: list[_PluginCandidate] = []
    identities: set[str] = set()
    relative_paths: set[str] = set()

    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            return None
        name = raw_entry.get('name')
        marketplace = raw_entry.get('marketplace')
        installed_at = raw_entry.get('installed_at')
        enabled = raw_entry.get('enabled')
        version = raw_entry.get('version')
        if not isinstance(name, str) or not _safe_component(name):
            return None
        if not isinstance(marketplace, str) or (marketplace and not _safe_component(marketplace)):
            return None
        if not isinstance(installed_at, str) or not installed_at.strip():
            return None
        if not isinstance(enabled, bool):
            return None
        if version is not None and (not isinstance(version, str) or not version.strip()):
            return None

        source_dir = _source_plugin_dir(
            raw_entry,
            source_root=source_root,
            name=name,
            marketplace=marketplace,
        )
        if source_dir is None or not _valid_plugin_tree(source_dir):
            return None
        content_fingerprint = tree_content_fingerprint(source_dir)
        if not content_fingerprint:
            return None
        try:
            relative_path = source_dir.resolve(strict=True).relative_to(source_root.resolve(strict=True))
        except Exception:
            return None
        relative_text = relative_path.as_posix()
        identity = _entry_identity(marketplace, name)
        if identity in identities or relative_text in relative_paths:
            return None
        identities.add(identity)
        relative_paths.add(relative_text)

        target_dir = target_root / relative_path
        entry: dict[str, object] = {
            'name': name,
            'marketplace': marketplace,
        }
        if version is not None:
            entry['version'] = version
        entry.update(
            {
                'installed_at': installed_at,
                'enabled': enabled,
                'cache_path': str(target_dir),
            }
        )
        candidates.append(
            _PluginCandidate(
                identity=identity,
                name=name,
                marketplace=marketplace,
                source_dir=source_dir,
                relative_path=relative_path,
                target_dir=target_dir,
                tree_label=_tree_label(identity),
                content_fingerprint=content_fingerprint,
                entry=entry,
            )
        )
    return tuple(candidates)


def _source_plugin_dir(
    raw_entry: dict[str, object],
    *,
    source_root: Path,
    name: str,
    marketplace: str,
) -> Path | None:
    if source_root.is_symlink():
        return None
    raw_cache_path = raw_entry.get('cache_path')
    if raw_cache_path is None:
        candidate = source_root / (Path(marketplace) / name if marketplace else Path('_direct') / name)
    elif isinstance(raw_cache_path, str) and raw_cache_path.strip():
        candidate = Path(raw_cache_path).expanduser()
    else:
        return None
    if candidate.is_symlink():
        return None
    try:
        resolved_root = source_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(resolved_root)
    except Exception:
        return None
    if len(relative.parts) != 2:
        return None
    if marketplace:
        if relative.parts != (marketplace, name):
            return None
    elif relative.parts[0] != '_direct' or not _safe_component(relative.parts[1]):
        return None
    return resolved


def _valid_plugin_tree(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        for entry in path.rglob('*'):
            if entry.is_symlink():
                return False
        for relative in _PLUGIN_MANIFEST_PATHS:
            manifest = path / relative
            if manifest.is_symlink() or not manifest.is_file():
                continue
            payload = json.loads(manifest.read_text(encoding='utf-8'))
            if isinstance(payload, dict):
                return True
    except Exception:
        return False
    return False


def _candidate_target_available(
    candidate: _PluginCandidate,
    *,
    target_index: dict[str, int],
    target_path_owners: dict[str, str],
    allow_owned: bool,
) -> bool:
    del target_index
    path_key = str(candidate.target_dir.resolve(strict=False))
    path_owner = target_path_owners.get(path_key)
    if path_owner is not None and path_owner != candidate.identity:
        return False
    marker = Path(f'{candidate.target_dir}.ccb-projection.json')
    target_present = candidate.target_dir.exists() or candidate.target_dir.is_symlink()
    marker_present = marker.exists() or marker.is_symlink()
    if allow_owned and projected_path_is_owned(candidate.target_dir, label=candidate.tree_label):
        return True
    return not target_present and not marker_present


def _commit_projection_transaction(
    *,
    target_home: Path,
    target_config: Path,
    target_prefix: str,
    updated_payload: dict[str, object],
    target_changed: bool,
    aggregate_marker: Path,
    next_marker: dict[str, object] | None,
    marker_changed: bool,
    operations: list[_TreeOperation],
    original_config_text: str | None,
    original_marker_text: str | None,
) -> bool:
    transaction_root = Path(tempfile.mkdtemp(prefix='.ccb-copilot-plugin-txn-', dir=target_home))
    candidates_root = transaction_root / 'candidates'
    backups_root = transaction_root / 'backups'
    applied: list[_AppliedTreeOperation] = []
    try:
        staged: dict[int, Path] = {}
        for index, operation in enumerate(operations):
            if operation.kind != 'install':
                continue
            assert operation.source is not None
            candidate = candidates_root / str(index)
            candidate.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(operation.source, candidate)
            if tree_content_fingerprint(candidate) != operation.expected_fingerprint:
                raise OSError(f'Copilot plugin changed while staging: {operation.source}')
            staged[index] = candidate

        for index, operation in enumerate(operations):
            marker = Path(f'{operation.target}.ccb-projection.json')
            backup_target = None
            backup_marker = None
            if operation.kind != 'abandon' and (
                operation.target.exists() or operation.target.is_symlink()
            ):
                backup_target = backups_root / f'{index}-target'
                backup_target.parent.mkdir(parents=True, exist_ok=True)
                operation.target.rename(backup_target)
            if marker.exists() or marker.is_symlink():
                backup_marker = backups_root / f'{index}-marker'
                backup_marker.parent.mkdir(parents=True, exist_ok=True)
                marker.rename(backup_marker)
            applied.append(_AppliedTreeOperation(operation, backup_target, backup_marker))

            if operation.kind == 'install':
                operation.target.parent.mkdir(parents=True, exist_ok=True)
                staged[index].rename(operation.target)
                assert operation.source is not None
                if not write_projected_marker(
                    operation.target,
                    label=operation.label,
                    mode='copy-seed',
                    source=operation.source,
                ):
                    raise OSError(f'failed to write Copilot plugin marker: {marker}')
            elif operation.kind not in {'remove', 'abandon'}:
                raise ValueError(f'unknown Copilot tree operation: {operation.kind}')

        if target_changed:
            atomic_write_text(target_config, _copilot_config_text(updated_payload, prefix=target_prefix))
        if marker_changed:
            if next_marker is None:
                aggregate_marker.unlink(missing_ok=True)
            else:
                atomic_write_text(aggregate_marker, _json_text(next_marker))
        return bool(next_marker and next_marker.get('managed'))
    except Exception:
        _restore_file(target_config, original_config_text)
        _restore_file(aggregate_marker, original_marker_text)
        _rollback_tree_operations(applied)
        return False
    finally:
        shutil.rmtree(transaction_root, ignore_errors=True)


def _rollback_tree_operations(applied: list[_AppliedTreeOperation]) -> None:
    for record in reversed(applied):
        operation = record.operation
        marker = Path(f'{operation.target}.ccb-projection.json')
        if operation.kind != 'abandon':
            _remove_path(operation.target)
        _remove_path(marker)
        if record.backup_target is not None and (
            record.backup_target.exists() or record.backup_target.is_symlink()
        ):
            operation.target.parent.mkdir(parents=True, exist_ok=True)
            record.backup_target.rename(operation.target)
        if record.backup_marker is not None and (
            record.backup_marker.exists() or record.backup_marker.is_symlink()
        ):
            marker.parent.mkdir(parents=True, exist_ok=True)
            record.backup_marker.rename(marker)


def _aggregate_marker_matches(payload: dict[str, object] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get('schema_version') == 1
        and payload.get('record_type') == _PROJECTION_RECORD_TYPE
        and payload.get('label') == _PROJECTION_LABEL
        and bool(str(payload.get('source') or '').strip())
        and isinstance(payload.get('managed'), dict)
    )


def _managed_entries(
    marker_payload: dict[str, object],
    *,
    target_home: Path,
) -> dict[str, dict[str, object]] | None:
    raw = marker_payload.get('managed')
    if not isinstance(raw, dict):
        return None
    managed: dict[str, dict[str, object]] = {}
    relative_paths: set[str] = set()
    target_root = target_home / 'installed-plugins'
    for identity, record in raw.items():
        if not isinstance(identity, str) or not isinstance(record, dict):
            return None
        entry = record.get('entry')
        relative_text = record.get('relative_path')
        tree_label = record.get('tree_label')
        content_fingerprint = record.get('content_fingerprint')
        suppressed = record.get('suppressed', False)
        if not isinstance(entry, dict) or not isinstance(relative_text, str) or not relative_text:
            return None
        if not isinstance(suppressed, bool):
            return None
        if not isinstance(content_fingerprint, str) or not content_fingerprint:
            return None
        entry_identity = _identity_from_entry(entry)
        if entry_identity != identity or tree_label != _tree_label(identity):
            return None
        relative = Path(relative_text)
        if relative.is_absolute() or '..' in relative.parts or len(relative.parts) != 2:
            return None
        if relative.as_posix() in relative_paths:
            return None
        relative_paths.add(relative.as_posix())
        target_dir = target_root / relative
        if str(entry.get('cache_path') or '') != str(target_dir):
            return None
        managed[identity] = {
            'entry': _clone_json_object(entry),
            'relative_path': relative.as_posix(),
            'tree_label': tree_label,
            'content_fingerprint': content_fingerprint,
        }
        if suppressed:
            managed[identity]['suppressed'] = True
    return managed


def _managed_record(candidate: _PluginCandidate) -> dict[str, object]:
    return {
        'entry': _clone_json_object(candidate.entry),
        'relative_path': candidate.relative_path.as_posix(),
        'tree_label': candidate.tree_label,
        'content_fingerprint': candidate.content_fingerprint,
    }


def _suppressed_record(candidate: _PluginCandidate) -> dict[str, object]:
    record = _managed_record(candidate)
    record['suppressed'] = True
    return record


def _next_aggregate_marker(
    marker_payload: dict[str, object],
    *,
    marker_owned: bool,
    source_home: Path,
    managed: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    if not managed:
        return None
    if (
        marker_owned
        and marker_payload.get('source') == str(source_home)
        and marker_payload.get('managed') == managed
    ):
        return marker_payload
    return {
        'schema_version': 1,
        'record_type': _PROJECTION_RECORD_TYPE,
        'label': _PROJECTION_LABEL,
        'source': str(source_home),
        'managed': managed,
        'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }


def _installed_plugin_entries(payload: dict[str, object]) -> list[object] | None:
    raw = payload.get('installedPlugins', [])
    return raw if isinstance(raw, list) else None


def _entry_index(entries: list[object]) -> dict[str, int] | None:
    index: dict[str, int] = {}
    for position, entry in enumerate(entries):
        identity = _identity_from_entry(entry)
        if identity is None:
            return None
        if identity in index:
            return None
        index[identity] = position
    return index


def _target_cache_path_owners(
    entries: list[object],
    *,
    target_home: Path,
) -> dict[str, str] | None:
    owners: dict[str, str] = {}
    target_root = (target_home / 'installed-plugins').resolve(strict=False)
    for entry in entries:
        identity = _identity_from_entry(entry)
        if identity is None or not isinstance(entry, dict):
            continue
        raw = entry.get('cache_path')
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            candidate = Path(raw).expanduser().resolve(strict=False)
            candidate.relative_to(target_root)
        except Exception:
            continue
        key = str(candidate)
        if key in owners and owners[key] != identity:
            return None
        owners[key] = identity
    return owners


def _identity_from_entry(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get('name')
    marketplace = entry.get('marketplace')
    if not isinstance(name, str) or not _safe_component(name):
        return None
    if not isinstance(marketplace, str) or (marketplace and not _safe_component(marketplace)):
        return None
    return _entry_identity(marketplace, name)


def _entry_identity(marketplace: str, name: str) -> str:
    return json.dumps([marketplace, name], ensure_ascii=True, separators=(',', ':'))


def _tree_label(identity: str) -> str:
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]
    return f'copilot-inherited-plugin:{digest}'


def _safe_component(value: str) -> bool:
    if not value or value in {'.', '..'} or value.strip() != value:
        return False
    if any(character in value for character in '/\\\x00<>:"|?*'):
        return False
    return not any(ord(character) < 32 for character in value)


def _read_copilot_config(path: Path) -> tuple[dict[str, object], str] | None:
    text = _read_text(path)
    if text is None:
        return None
    offset = text.find('{')
    if offset < 0:
        return None
    prefix = text[:offset]
    if any(line.strip() and not line.lstrip().startswith('//') for line in prefix.splitlines()):
        return None
    try:
        payload = json.loads(text[offset:])
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload, prefix


def _read_json_object(path: Path) -> dict[str, object] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8')
    except Exception:
        return None


def _copilot_config_text(payload: dict[str, object], *, prefix: str) -> str:
    normalized_prefix = prefix
    if normalized_prefix and not normalized_prefix.endswith('\n'):
        normalized_prefix += '\n'
    return normalized_prefix + _json_text(payload)


def _json_text(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + '\n'


def _clone_json_object(value: dict[str, object]) -> dict[str, object]:
    cloned = _clone_json_value(value)
    return cloned if isinstance(cloned, dict) else {}


def _clone_json_value(value: object) -> object:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return value


def _restore_file(path: Path, original_text: str | None) -> None:
    try:
        if original_text is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_text(path, original_text)
    except Exception:
        pass


def _remove_path(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
    except Exception:
        pass


def _system_copilot_home() -> Path:
    if os.environ.get('CCB_SOURCE_HOME'):
        return current_provider_source_home() / '.copilot'
    raw = str(os.environ.get('COPILOT_HOME') or '').strip()
    if raw:
        candidate = Path(raw).expanduser()
        if not _looks_like_ccb_provider_home(candidate):
            return candidate
    return current_provider_source_home() / '.copilot'


def _inherits_config(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_config', True))


def _inherits_auth(profile) -> bool:
    return True if profile is None else bool(getattr(profile, 'inherit_auth', True))


def _looks_like_ccb_provider_home(path: Path) -> bool:
    parts = Path(path).expanduser().parts
    for index in range(0, max(len(parts) - 4, 0)):
        if parts[index] != 'agents':
            continue
        if parts[index + 2] == 'provider-state' and parts[index + 4] == 'home':
            return True
    return False


__all__ = ['materialize_copilot_home_config']
