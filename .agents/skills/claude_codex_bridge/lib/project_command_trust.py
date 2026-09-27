from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from agents.config_loader import load_project_config, project_config_path
from agents.config_loader_runtime.io_runtime import parse_config_document_text
from storage.atomic import atomic_write_text, ensure_durable_directory


RECEIPT_SCHEMA_VERSION = 1


@dataclass(frozen=True, order=True)
class ProjectCommandField:
    path: str
    value: str

    def to_record(self) -> dict[str, str]:
        return {'path': self.path, 'value': self.value}


@dataclass(frozen=True)
class ProjectCommandApproval:
    project_root: Path
    fields: tuple[ProjectCommandField, ...]
    digest: str
    status: str
    receipt_path: Path

    @property
    def required(self) -> bool:
        return bool(self.fields) and self.status != 'approved'

    def to_record(self) -> dict[str, object]:
        return {
            'approval_status': self.status,
            'project_root': str(self.project_root),
            'command_authority_digest': self.digest,
            'command_fields': [field.to_record() for field in self.fields],
            'receipt_path': str(self.receipt_path),
        }


class ProjectCommandApprovalRequired(RuntimeError):
    def __init__(self, approval: ProjectCommandApproval) -> None:
        self.approval = approval
        fields = ', '.join(field.path for field in approval.fields)
        super().__init__(
            f'project command approval required for {fields}; '
            'review and approve with `ccb config approve-commands`'
        )


def inspect_project_command_approval(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> ProjectCommandApproval:
    root = _canonical_project_root(project_root)
    fields = load_project_command_fields(root)
    digest = project_command_authority_digest(root, fields)
    receipt = project_command_receipt_path(root, environ=environ)
    if not fields:
        status = 'not_required'
    else:
        status = _receipt_status(receipt, project_root=root, fields=fields, digest=digest)
    return ProjectCommandApproval(root, fields, digest, status, receipt)


def require_project_command_approval(
    project_root: Path,
    *,
    field_path: str | None = None,
    field_value: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProjectCommandApproval:
    approval = inspect_project_command_approval(project_root, environ=environ)
    if field_path is not None:
        expected = ProjectCommandField(str(field_path), str(field_value or ''))
        if expected not in approval.fields:
            raise RuntimeError(
                f'project command field changed before execution: {expected.path}; '
                'restart after reviewing `ccb config approve-commands`'
            )
    if approval.required:
        raise ProjectCommandApprovalRequired(approval)
    return approval


def approve_project_commands(
    project_root: Path,
    *,
    expected_digest: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProjectCommandApproval:
    approval = inspect_project_command_approval(project_root, environ=environ)
    if expected_digest is not None and approval.digest != str(expected_digest):
        raise RuntimeError(
            'project command fields changed during approval; review the current values and try again'
        )
    if not approval.fields:
        return approval
    receipt_dir = approval.receipt_path.parent
    ensure_durable_directory(receipt_dir)
    if os.name != 'nt':
        os.chmod(receipt_dir, 0o700)
    payload = {
        'schema_version': RECEIPT_SCHEMA_VERSION,
        'record_type': 'ccb-project-command-approval',
        'project_root': str(approval.project_root),
        'command_authority_digest': approval.digest,
        'command_fields': [field.to_record() for field in approval.fields],
        'approved_at': datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_text(
        approval.receipt_path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + '\n',
    )
    if os.name != 'nt':
        os.chmod(approval.receipt_path, 0o600)
    return ProjectCommandApproval(
        approval.project_root,
        approval.fields,
        approval.digest,
        'approved',
        approval.receipt_path,
    )


def require_runtime_provider_command_approval(
    project_root: Path,
    *,
    agent_name: str,
    template: str,
    environ: Mapping[str, str] | None = None,
) -> ProjectCommandApproval:
    root = _canonical_project_root(project_root)
    field_path = f'agents.{str(agent_name).strip().lower()}.provider_command_template'
    approval = inspect_project_command_approval(root, environ=environ)
    expected = ProjectCommandField(field_path, str(template))
    if expected in approval.fields:
        if approval.required:
            raise ProjectCommandApprovalRequired(approval)
        return approval

    # User defaults and generated runtime overlays are not repository-authored
    # command fields.  Preserve those existing paths only when the effective
    # config still proves that this exact template belongs to this agent.  If
    # disk authority changed after the runtime plan was built, fail closed.
    config = load_project_config(root).config
    spec = config.agents.get(str(agent_name).strip().lower())
    current = getattr(spec, 'provider_command_template', None) if spec is not None else None
    if current == template:
        return approval
    raise RuntimeError(
        f'project command field changed before execution: {field_path}; '
        'restart after reviewing `ccb config approve-commands`'
    )


def load_project_command_fields(project_root: Path) -> tuple[ProjectCommandField, ...]:
    root = _canonical_project_root(project_root)
    path = project_config_path(root)
    if not path.is_file():
        return ()
    # Do not invite the user to approve a document that cannot become effective
    # config.  This also keeps extraction aligned with the normal config loader.
    load_project_config(root, include_loop_overlays=False)
    document = parse_config_document_text(
        path.read_text(encoding='utf-8'),
        path=path,
        project_root=root,
    )
    fields: list[ProjectCommandField] = []
    _collect_command_fields(fields, document.get('tool_windows'), section='tool_windows', key='command')
    _collect_command_fields(
        fields,
        document.get('agents'),
        section='agents',
        key='provider_command_template',
    )
    return tuple(sorted(fields))


def project_command_authority_digest(
    project_root: Path,
    fields: tuple[ProjectCommandField, ...],
) -> str:
    payload = {
        'schema_version': RECEIPT_SCHEMA_VERSION,
        'project_root': str(_canonical_project_root(project_root)),
        'command_fields': [field.to_record() for field in sorted(fields)],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def project_command_receipt_path(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    root = _canonical_project_root(project_root)
    project_key = hashlib.sha256(str(root).encode('utf-8')).hexdigest()
    if _is_windows_platform():
        base_text = str(env.get('LOCALAPPDATA') or env.get('APPDATA') or '').strip()
        base = Path(base_text).expanduser() if base_text else Path.home() / 'AppData' / 'Local'
        trust_root = base / 'CCB' / 'trust' / 'project-commands'
    else:
        state_text = str(env.get('XDG_STATE_HOME') or '').strip()
        base = Path(state_text).expanduser() if state_text else Path.home() / '.local' / 'state'
        if not base.is_absolute():
            base = Path.home() / '.local' / 'state'
        trust_root = base / 'ccb' / 'trust' / 'project-commands'
    return trust_root / f'{project_key}.json'


def _collect_command_fields(
    fields: list[ProjectCommandField],
    raw_section: object,
    *,
    section: str,
    key: str,
) -> None:
    if not isinstance(raw_section, dict):
        return
    for raw_name, raw_spec in raw_section.items():
        if not isinstance(raw_spec, dict) or raw_spec.get(key) is None:
            continue
        value = str(raw_spec[key]).strip()
        if value:
            fields.append(ProjectCommandField(f'{section}.{str(raw_name).strip().lower()}.{key}', value))


def _receipt_status(
    path: Path,
    *,
    project_root: Path,
    fields: tuple[ProjectCommandField, ...],
    digest: str,
) -> str:
    if not path.is_file():
        return 'approval_required'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return 'invalid'
    expected_fields = [field.to_record() for field in fields]
    if not isinstance(payload, dict):
        return 'invalid'
    if payload.get('schema_version') != RECEIPT_SCHEMA_VERSION:
        return 'invalid'
    if payload.get('record_type') != 'ccb-project-command-approval':
        return 'invalid'
    if payload.get('project_root') != str(project_root):
        return 'invalid'
    if payload.get('command_authority_digest') != digest or payload.get('command_fields') != expected_fields:
        return 'stale'
    return 'approved'


def _canonical_project_root(project_root: Path) -> Path:
    return Path(project_root).expanduser().resolve()


def _is_windows_platform() -> bool:
    return os.name == 'nt'


__all__ = [
    'ProjectCommandApproval',
    'ProjectCommandApprovalRequired',
    'ProjectCommandField',
    'approve_project_commands',
    'inspect_project_command_approval',
    'load_project_command_fields',
    'project_command_authority_digest',
    'project_command_receipt_path',
    'require_project_command_approval',
    'require_runtime_provider_command_approval',
]
