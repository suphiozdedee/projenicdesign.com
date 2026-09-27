from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

from cli.models import ParsedAskCommand
from storage.atomic import atomic_write_json, atomic_write_text

from .auto_runner_lock import active_auto_runner
from .ask import submit_ask
from .role_output_import import (
    frontdesk_intake_missing_fields,
    planner_contract_for_frontdesk_text,
    planner_expected_task_ids_for_frontdesk_text,
    planner_from_frontdesk_intake_message,
    planner_required_output_for_contract,
    planner_script_write_rules_for_contract,
)


_SEGMENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')


def frontdesk_intake(context, command, services=None) -> dict[str, object]:
    deps = _deps(services)
    if str(getattr(command, 'action', '') or '') != 'forward-planner':
        return _blocked_payload(
            context,
            reason='unsupported_frontdesk_action',
            evidence={'action': getattr(command, 'action', None), 'supported_actions': ['forward-planner']},
        )
    plan_result = _resolve_plan_slug(context, command)
    if plan_result.get('status') != 'ok':
        return _blocked_payload(context, reason=str(plan_result.get('reason')), evidence=plan_result)
    plan_slug = str(plan_result['plan_slug'])
    intake_result = _read_intake_text(command)
    if intake_result.get('status') != 'ok':
        return _blocked_payload(context, reason=str(intake_result.get('reason')), evidence=intake_result)
    intake_text = str(intake_result.get('intake_text') or '')
    missing = frontdesk_intake_missing_fields(intake_text)
    if missing:
        return _blocked_payload(
            context,
            reason='frontdesk_intake_missing_required_anchors',
            evidence={'missing_fields': missing},
        )
    request_id_result = _resolve_request_id(command, intake_text)
    if request_id_result.get('status') != 'ok':
        return _blocked_payload(context, reason=str(request_id_result.get('reason')), evidence=request_id_result)
    request_id = str(request_id_result['request_id'])
    source_request_result = _resolve_source_request(context, command, deps)
    if source_request_result.get('status') == 'blocked':
        return _blocked_payload(
            context,
            reason=str(source_request_result.get('reason') or 'frontdesk_source_request_invalid'),
            evidence=source_request_result,
        )
    source_request_text = str(source_request_result.get('text') or '')
    digest = hashlib.sha256(intake_text.encode('utf-8')).hexdigest()
    activation_id = f'act-frontdesk-{request_id}'
    activation_path = _activation_path(context, activation_id)
    existing = _load_existing_activation(activation_path)
    if existing is not None:
        return _handle_existing_activation(
        context,
        activation_path=activation_path,
        activation=existing,
        intake_sha256=digest,
        source_request_sha256=_optional_digest(source_request_result.get('sha256')),
        plan_slug=plan_slug,
    )
    activation = _new_activation(
        context,
        activation_id=activation_id,
        plan_slug=plan_slug,
        request_id=request_id,
        intake_text=intake_text,
        intake_sha256=digest,
        source_request=source_request_result,
    )
    atomic_write_json(activation_path, activation)
    summary = deps.submit_ask(
        context,
        ParsedAskCommand(
            project=None,
            target='planner',
            sender='frontdesk',
            message=planner_from_frontdesk_intake_message(
                activation,
                intake_text,
                original_request=source_request_text,
            ),
            task_id=activation_id,
            compact=True,
            silence=True,
            inline_request=False,
        ),
    )
    job = _single_job(summary.jobs, target='planner')
    activation['ask'] = {
        'target': 'planner',
        'job_id': str(job['job_id']),
        'status': job.get('status'),
        'sender': 'frontdesk',
        'silence': True,
    }
    activation['auto_runner'] = deps.start_auto_runner(
        context,
        activation_id=activation_id,
        wait_job_id=str(job['job_id']),
    )
    activation['status'] = 'planner_submitted'
    activation['submitted_at'] = _utc_now()
    atomic_write_json(activation_path, activation)
    return _ok_payload(
        context,
        action='forwarded_to_planner',
        activation_path=activation_path,
        activation=activation,
        idempotent=False,
    )


def _resolve_plan_slug(context, command) -> dict[str, object]:
    raw = str(getattr(command, 'plan_slug', None) or '').strip()
    source = 'command'
    existing = _existing_plan_slugs(context)
    if not raw:
        if len(existing) == 1:
            raw = existing[0]
            source = 'single_plan_root'
        elif len(existing) > 1:
            env_slug = _env_plan_slug()
            if env_slug and env_slug[0] in existing:
                raw, env_name = env_slug
                source = f'env:{env_name}'
            else:
                evidence: dict[str, object] = {
                    'status': 'blocked',
                    'reason': 'frontdesk_intake_requires_plan_slug',
                    'existing_plan_slugs': existing,
                    'hint': 'pass --plan <plan_slug> when multiple plan roots exist',
                }
                if env_slug:
                    evidence['ignored_env_plan_slug'] = env_slug[0]
                    evidence['ignored_env_name'] = env_slug[1]
                return evidence
    if not raw:
        env_slug = _env_plan_slug()
        if env_slug:
            raw, env_name = env_slug
            source = f'env:{env_name}'
    if not raw:
        raw = _default_plan_slug()
        source = 'script_bootstrap_default_plan'
    if not _SEGMENT_RE.fullmatch(raw):
        return {'status': 'blocked', 'reason': 'invalid_plan_slug', 'plan_slug': raw, 'source': source}
    if not existing:
        _bootstrap_plan_root(context, plan_slug=raw)
    return {'status': 'ok', 'plan_slug': raw, 'source': source}


def _env_plan_slug() -> tuple[str, str] | None:
    for env_name in ('CCB_ACTIVE_PLAN', 'CCB_PLAN_SLUG', 'CCB_REAL_PLAN'):
        env_value = str(os.environ.get(env_name) or '').strip()
        if env_value:
            return env_value, env_name
    return None


def _default_plan_slug() -> str:
    return 'frontdesk-intake'


def _bootstrap_plan_root(context, *, plan_slug: str) -> None:
    plan_root = Path(context.project.project_root) / 'docs' / 'plantree' / 'plans' / plan_slug
    plan_root.mkdir(parents=True, exist_ok=True)
    readme = plan_root / 'README.md'
    brief = plan_root / 'brief.md'
    if not readme.exists():
        atomic_write_text(readme, f'# {plan_slug}\n\nScript-owned plan root created by frontdesk intake.\n')
    if not brief.exists():
        atomic_write_text(brief, f'# {plan_slug} Brief\n\nCreated for frontdesk-to-planner intake handoff.\n')


def _existing_plan_slugs(context) -> tuple[str, ...]:
    plans_root = Path(context.project.project_root) / 'docs' / 'plantree' / 'plans'
    if not plans_root.is_dir():
        return ()
    return tuple(sorted(path.name for path in plans_root.iterdir() if path.is_dir() and _SEGMENT_RE.fullmatch(path.name)))


def _read_intake_text(command) -> dict[str, object]:
    file_path = str(getattr(command, 'file_path', None) or '').strip()
    intake_base64 = str(getattr(command, 'intake_base64', None) or '').strip()
    stdin_text = str(getattr(command, 'intake_text', '') or '')
    source_count = sum(1 for value in (file_path, intake_base64, stdin_text.strip()) if value)
    if source_count > 1:
        return {'status': 'blocked', 'reason': 'frontdesk_intake_cannot_combine_input_sources'}
    if intake_base64:
        try:
            raw = base64.b64decode(intake_base64.encode('ascii'), validate=True)
            text = raw.decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError, ValueError) as exc:
            return {
                'status': 'blocked',
                'reason': 'frontdesk_intake_base64_invalid',
                'error': str(exc),
            }
        return {'status': 'ok', 'intake_text': text, 'source': 'intake_base64'}
    if file_path:
        try:
            text = Path(file_path).read_text(encoding='utf-8')
        except FileNotFoundError:
            return {'status': 'blocked', 'reason': 'frontdesk_intake_file_missing', 'file_path': file_path}
        return {'status': 'ok', 'intake_text': text, 'source': 'file', 'file_path': file_path}
    if stdin_text.strip():
        return {'status': 'ok', 'intake_text': stdin_text, 'source': 'stdin'}
    return {'status': 'blocked', 'reason': 'frontdesk_intake_requires_file_or_stdin'}


def _resolve_request_id(command, intake_text: str) -> dict[str, object]:
    raw = str(getattr(command, 'request_id', None) or '').strip()
    if not raw:
        match = re.search(r'(?mi)^\s*CCB_REQ_ID\s*:\s*`?([^`\n]+?)`?\s*$', intake_text)
        if match:
            raw = match.group(1).strip()
    if not raw:
        raw = f'intake-{hashlib.sha256(intake_text.encode("utf-8")).hexdigest()[:12]}'
    if not _SEGMENT_RE.fullmatch(raw):
        return {'status': 'blocked', 'reason': 'invalid_request_id', 'request_id': raw}
    return {'status': 'ok', 'request_id': raw}


def _handle_existing_activation(
    context,
    *,
    activation_path: Path,
    activation: dict[str, object],
    intake_sha256: str,
    source_request_sha256: str | None,
    plan_slug: str,
) -> dict[str, object]:
    if str(activation.get('plan_slug') or '') != plan_slug:
        return _blocked_payload(
            context,
            reason='frontdesk_activation_plan_conflict',
            evidence={
                'activation_path': str(activation_path),
                'existing_plan_slug': activation.get('plan_slug'),
                'incoming_plan_slug': plan_slug,
            },
        )
    if str(activation.get('intake_sha256') or '') != intake_sha256:
        return _blocked_payload(
            context,
            reason='frontdesk_activation_request_id_conflict',
            evidence={
                'activation_path': str(activation_path),
                'existing_intake_sha256': activation.get('intake_sha256'),
                'incoming_intake_sha256': intake_sha256,
            },
        )
    existing_source_request = activation.get('source_request')
    existing_source_sha256 = (
        str(existing_source_request.get('sha256') or '').strip()
        if isinstance(existing_source_request, dict)
        else ''
    )
    if source_request_sha256 and existing_source_sha256 != source_request_sha256:
        return _blocked_payload(
            context,
            reason='frontdesk_activation_source_request_conflict',
            evidence={
                'activation_path': str(activation_path),
                'existing_source_request_sha256': existing_source_sha256 or None,
                'incoming_source_request_sha256': source_request_sha256,
            },
        )
    ask = activation.get('ask') if isinstance(activation.get('ask'), dict) else {}
    if str(ask.get('target') or '') != 'planner' or not str(ask.get('job_id') or '').strip():
        return _blocked_payload(
            context,
            reason='frontdesk_activation_incomplete',
            evidence={'activation_path': str(activation_path), 'ask': ask},
        )
    return _ok_payload(
        context,
        action='already_forwarded_to_planner',
        activation_path=activation_path,
        activation=activation,
        idempotent=True,
    )


def _new_activation(
    context,
    *,
    activation_id: str,
    plan_slug: str,
    request_id: str,
    intake_text: str,
    intake_sha256: str,
    source_request: dict[str, object],
) -> dict[str, object]:
    original_request = str(source_request.get('text') or '')
    semantic_input = '\n\n'.join(part for part in (original_request, intake_text) if part)
    planner_contract = planner_contract_for_frontdesk_text(semantic_input)
    expected_task_ids = planner_expected_task_ids_for_frontdesk_text(semantic_input)
    script_write_rules = planner_script_write_rules_for_contract(
        planner_contract,
        expected_task_ids=expected_task_ids,
    )
    required_next_output = planner_required_output_for_contract(
        planner_contract,
        expected_task_ids=expected_task_ids,
    )
    return {
        'schema_version': 1,
        'record_type': 'ccb_loop_frontdesk_planner_activation',
        'activation_id': activation_id,
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'activate_planner_from_frontdesk',
        'source': 'frontdesk_forward_planner',
        'plan_slug': plan_slug,
        'request_id': request_id,
        'intake_sha256': intake_sha256,
        'source_job': {
            'job_id': str(source_request.get('source_job_id') or request_id),
            'agent_name': 'frontdesk',
            'terminal_status': 'forwarded',
            'finished_at': None,
            'reply_sha256': intake_sha256,
        },
        'source_intake': {
            'sha256': intake_sha256,
            'bytes': len(intake_text.encode('utf-8')),
            'preview': intake_text.strip()[:400],
        },
        'source_request': _source_request_evidence(source_request),
        'planner_contract': planner_contract,
        'required_next_output': required_next_output,
        'script_write_rules': script_write_rules,
        'expected_task_ids': list(expected_task_ids),
        'status': 'pending_planner_submit',
        'created_at': _utc_now(),
    }


def _load_existing_activation(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f'frontdesk activation is invalid JSON: {path}') from exc
    if not isinstance(payload, dict):
        raise ValueError(f'frontdesk activation is invalid: {path}')
    return payload


def _activation_path(context, activation_id: str) -> Path:
    path = Path(context.project.project_root) / '.ccb' / 'runtime' / 'loops' / 'activations' / f'{activation_id}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _single_job(jobs, *, target: str) -> dict[str, object]:
    matches = [job for job in tuple(jobs or ()) if str(job.get('agent_name') or job.get('target_name') or '') == target]
    if len(matches) != 1:
        raise RuntimeError(f'expected one ask job for {target}; got {len(matches)}')
    job = dict(matches[0])
    if not str(job.get('job_id') or '').strip():
        raise RuntimeError(f'ask job for {target} did not return job_id')
    return job


def _ok_payload(
    context,
    *,
    action: str,
    activation_path: Path,
    activation: dict[str, object],
    idempotent: bool,
) -> dict[str, object]:
    ask = activation.get('ask') if isinstance(activation.get('ask'), dict) else {}
    auto_runner = activation.get('auto_runner') if isinstance(activation.get('auto_runner'), dict) else {}
    return {
        'schema_version': 1,
        'record_type': 'ccb_frontdesk_intake',
        'frontdesk_intake_status': 'ok',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': action,
        'plan_slug': activation.get('plan_slug'),
        'request_id': activation.get('request_id'),
        'activation_id': activation.get('activation_id'),
        'activation_path': str(activation_path),
        'ask': ask,
        'planner_job_id': ask.get('job_id'),
        'silence': True,
        'auto_runner': auto_runner,
        'idempotent': idempotent,
        'next_activation': 'auto_runner',
    }


def _blocked_payload(context, *, reason: str, evidence: dict[str, object] | None = None) -> dict[str, object]:
    return {
        'schema_version': 1,
        'record_type': 'ccb_frontdesk_intake',
        'frontdesk_intake_status': 'blocked',
        'project_id': context.project.project_id,
        'project_root': str(context.project.project_root),
        'action': 'rejected',
        'reason': reason,
        'evidence': evidence or {},
        'next_activation': 'inspect',
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _resolve_source_request(context, command, deps) -> dict[str, object]:
    source_job_id = str(getattr(command, 'source_job_id', None) or '').strip()
    if not source_job_id:
        return {'status': 'not_requested', 'text': ''}
    result = deps.resolve_source_request(context, source_job_id)
    if not isinstance(result, dict):
        return {
            'status': 'blocked',
            'reason': 'frontdesk_source_request_resolver_invalid',
            'source_job_id': source_job_id,
        }
    result = dict(result)
    if str(result.get('status') or '') != 'ok':
        result['status'] = 'blocked'
        result.setdefault('reason', 'frontdesk_source_request_unavailable')
        result.setdefault('source_job_id', source_job_id)
        return result
    text = str(result.get('text') or '')
    if not text.strip():
        return {
            'status': 'blocked',
            'reason': 'frontdesk_source_request_empty',
            'source_job_id': source_job_id,
        }
    data = text.encode('utf-8')
    result['source_job_id'] = source_job_id
    result['text'] = text
    result['bytes'] = len(data)
    result['sha256'] = hashlib.sha256(data).hexdigest()
    result['preview'] = text.strip()[:400]
    return result


def _source_request_evidence(source_request: dict[str, object]) -> dict[str, object] | None:
    if str(source_request.get('status') or '') != 'ok':
        return None
    return {
        key: source_request.get(key)
        for key in (
            'source_job_id',
            'agent_name',
            'project_id',
            'to_agent',
            'from_actor',
            'message_type',
            'bytes',
            'sha256',
            'preview',
            'body_artifact',
        )
    }


def _optional_digest(value) -> str | None:
    text = str(value or '').strip()
    return text or None


def _deps(services):
    services = services or SimpleNamespace()
    return SimpleNamespace(
        submit_ask=getattr(services, 'submit_ask', submit_ask),
        start_auto_runner=getattr(services, 'start_auto_runner', _start_auto_runner),
        resolve_source_request=getattr(services, 'resolve_source_request', _missing_source_request),
    )


def _missing_source_request(_context, source_job_id: str) -> dict[str, object]:
    return {
        'status': 'blocked',
        'reason': 'frontdesk_source_request_resolver_unavailable',
        'source_job_id': source_job_id,
    }


def _start_auto_runner(context, *, activation_id: str, wait_job_id: str) -> dict[str, object]:
    project_root = Path(context.project.project_root)
    active = active_auto_runner(project_root)
    if active is not None:
        return {
            **active,
            'wait_job_id': wait_job_id,
            'activation_id': activation_id,
            'next_activation': 'existing_auto_runner',
            'drain_source': 'activation_records',
        }
    log_dir = project_root / '.ccb' / 'runtime' / 'loops' / 'auto-runner'
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f'{activation_id}.stdout.log'
    stderr_path = log_dir / f'{activation_id}.stderr.log'
    script = Path(__file__).resolve().parents[3] / 'ccb.py'
    command = [
        sys.executable,
        str(script),
        '--project',
        str(project_root),
        'loop',
        'runner',
        '--auto',
        '--wait-job',
        wait_job_id,
        '--json',
    ]
    env = dict(os.environ)
    env['PYTHONUNBUFFERED'] = '1'
    with open(stdout_path, 'ab') as stdout, open(stderr_path, 'ab') as stderr:
        process = subprocess.Popen(
            command,
            cwd=str(project_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    return {
        'status': 'started',
        'pid': process.pid,
        'wait_job_id': wait_job_id,
        'command': command,
        'stdout_path': str(stdout_path),
        'stderr_path': str(stderr_path),
    }


__all__ = ['frontdesk_intake']
