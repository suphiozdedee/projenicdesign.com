from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import threading
import time

from agents.config_loader import project_config_path, validate_project_config
from agents.config_loader_runtime.io_runtime import parse_config_document_text
from cli.output import atomic_write_text
from cli.services.config_restart_intent import (
    load_config_restart_intent,
    record_config_restart_intent,
)
from storage.paths import PathLayout


# Confirmed-versus-pending mutation semantics align with Paseo at pinned
# commit b599d38. CCB applies them through validated config and restart intent.
_TABLE_HEADER = re.compile(r'^\s*\[([^]]+)]\s*(?:#.*)?$')
_FIELD_LINE = re.compile(r'^(\s*)(model|thinking)\s*=.*$')


class ProviderSettingsError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


@dataclass(frozen=True)
class ProviderSettingsResult:
    agent: str
    provider: str
    model: str
    thinking: str | None
    prior_revision: str
    config_revision: str
    backup_path: str | None
    changed: bool

    def to_record(self) -> dict[str, object]:
        return {
            'schema_version': 1,
            'status': 'pending_restart' if self.changed else 'unchanged',
            'agent': self.agent,
            'provider': self.provider,
            'configured_model': self.model,
            'configured_thinking': self.thinking,
            'prior_revision': self.prior_revision,
            'config_revision': self.config_revision,
            'changed': self.changed,
            'restart_required': self.changed,
            'mutation_mode': 'restart_required',
        }


class ProviderSettingsStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def apply(
        self,
        *,
        project_root: Path,
        agent: str,
        model: str,
        thinking: str | None,
        expected_revision: str,
        allowed_models: set[str],
        allowed_thinking: set[str],
    ) -> ProviderSettingsResult:
        agent_name = str(agent or '').strip()
        model_id = str(model or '').strip()
        thinking_id = str(thinking or '').strip().lower() or None
        if not agent_name or not model_id:
            raise ProviderSettingsError('agent and model are required')
        if model_id not in allowed_models:
            raise ProviderSettingsError('model is not selectable for this provider', status_code=422)
        if thinking_id is not None and thinking_id not in allowed_thinking:
            raise ProviderSettingsError('thinking option is not valid for this model', status_code=422)
        config_path = project_config_path(project_root)
        with self._lock:
            try:
                current_text = config_path.read_text(encoding='utf-8')
            except OSError as exc:
                raise ProviderSettingsError('project config is unavailable', status_code=409) from exc
            current_revision = _digest(current_text)
            if not expected_revision or expected_revision != current_revision:
                raise ProviderSettingsError('project config changed; refresh before saving', status_code=409)
            document = parse_config_document_text(
                current_text,
                path=config_path,
                project_root=project_root,
            )
            config = validate_project_config(
                document,
                source_path=config_path,
                project_root=project_root,
            )
            spec = config.agents.get(agent_name)
            if spec is None:
                raise ProviderSettingsError('unknown agent', status_code=404)
            provider = str(getattr(spec, 'provider', '') or '').strip().lower()
            table_path = _agent_table_path(document, agent_name)
            updated_text = _patch_table_fields(
                current_text,
                table_path=table_path,
                model=model_id,
                thinking=thinking_id,
            )
            # Validate before touching the active file. This also compiles the
            # provider startup flags and catches unsupported model shortcuts.
            updated_document = parse_config_document_text(
                updated_text,
                path=config_path,
                project_root=project_root,
            )
            validate_project_config(
                updated_document,
                source_path=config_path,
                project_root=project_root,
            )
            changed = updated_text != current_text
            backup_path = None
            if changed:
                backup = config_path.with_name(f'{config_path.name}.bak.mobile.{time.time_ns()}')
                shutil.copy2(config_path, backup)
                atomic_write_text(config_path, updated_text)
                backup_path = str(backup)
            revision = _digest(updated_text)
            if changed:
                try:
                    record_config_restart_intent(
                        project_root,
                        target_config_digest=revision,
                        affected_agents=(agent_name,),
                        reason='mobile_provider_model_changed',
                    )
                except Exception as exc:
                    try:
                        atomic_write_text(config_path, current_text)
                    except Exception as rollback_exc:
                        raise ProviderSettingsError(
                            'provider settings failed and the config rollback failed',
                            status_code=500,
                        ) from rollback_exc
                    raise ProviderSettingsError(
                        'provider settings were not saved because restart state is unavailable',
                        status_code=503,
                    ) from exc
            return ProviderSettingsResult(
                agent=agent_name,
                provider=provider,
                model=model_id,
                thinking=thinking_id,
                prior_revision=current_revision,
                config_revision=revision,
                backup_path=backup_path,
                changed=changed,
            )


def project_config_revision(project_root: Path) -> str | None:
    try:
        return _digest(project_config_path(project_root).read_text(encoding='utf-8'))
    except OSError:
        return None


def provider_restart_pending_agents(project_root: Path) -> frozenset[str]:
    revision = project_config_revision(project_root)
    if revision is None:
        return frozenset()
    intent = load_config_restart_intent(PathLayout(project_root))
    if intent is None or intent.target_config_digest != revision:
        return frozenset()
    return frozenset(intent.affected_agents)


def _agent_table_path(document: dict[str, object], agent: str) -> tuple[str, ...]:
    if int(document.get('version') or 2) == 3:
        workflow = document.get('workflow')
        if isinstance(workflow, dict):
            resident = workflow.get('resident')
            if isinstance(resident, dict) and agent in resident:
                return ('workflow', 'resident', agent)
        raise ProviderSettingsError(
            'dynamic workflow agents inherit their model from a role profile',
            status_code=409,
        )
    return ('agents', agent)


def _patch_table_fields(
    text: str,
    *,
    table_path: tuple[str, ...],
    model: str,
    thinking: str | None,
) -> str:
    lines = text.splitlines()
    target = '.'.join(_toml_key(part) for part in table_path)
    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        match = _TABLE_HEADER.match(line)
        if match is None:
            continue
        if start is not None:
            end = index
            break
        if match.group(1).strip() == target:
            start = index
    if start is None:
        if lines and lines[-1].strip():
            lines.append('')
        lines.extend([f'[{target}]', f'model = {json.dumps(model)}'])
        if thinking is not None:
            lines.append(f'thinking = {json.dumps(thinking)}')
        return '\n'.join(lines).rstrip() + '\n'

    replacements = {'model': model, 'thinking': thinking}
    found: set[str] = set()
    for index in range(start + 1, end):
        match = _FIELD_LINE.match(lines[index])
        if match is None:
            continue
        key = match.group(2)
        value = replacements[key]
        found.add(key)
        lines[index] = (
            f'{match.group(1)}{key} = {json.dumps(value)}'
            if value is not None
            else ''
        )
    insert_at = end
    additions = []
    if 'model' not in found:
        additions.append(f'model = {json.dumps(model)}')
    if thinking is not None and 'thinking' not in found:
        additions.append(f'thinking = {json.dumps(thinking)}')
    if additions:
        lines[insert_at:insert_at] = additions
    return '\n'.join(lines).rstrip() + '\n'


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r'[A-Za-z0-9_-]+', value) else json.dumps(value)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


__all__ = [
    'ProviderSettingsError',
    'ProviderSettingsResult',
    'ProviderSettingsStore',
    'project_config_revision',
    'provider_restart_pending_agents',
]
