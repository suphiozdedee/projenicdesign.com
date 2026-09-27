from __future__ import annotations

from types import SimpleNamespace

from ccbd.api_models import DeliveryScope, MessageEnvelope
from cli.models import ParsedFrontdeskCommand
from cli.services.ask_runtime import AskSummary
from cli.services.ask_runtime.submission import (
    _artifact_request_body,
    message_with_reply_guidance,
)
from cli.services.frontdesk_intake import frontdesk_intake
from cli.services.frontdesk_source_request import resolve_frontdesk_source_request


def build_frontdesk_forward_planner_handler(dispatcher, *, start_auto_runner=None):
    def handle(payload: dict) -> dict:
        command = ParsedFrontdeskCommand(
            project=None,
            action='forward-planner',
            plan_slug=_optional_text(payload.get('plan_slug')),
            request_id=_optional_text(payload.get('request_id')),
            source_job_id=_optional_text(payload.get('source_job_id')),
            file_path=_optional_text(payload.get('file_path')),
            intake_base64=_optional_text(payload.get('intake_base64')),
            intake_text=str(payload.get('intake_text') or ''),
            json_output=bool(payload.get('json_output', False)),
        )
        context = _frontdesk_context(dispatcher, command)
        services = SimpleNamespace(
            submit_ask=_submit_ask_via_dispatcher(dispatcher),
            resolve_source_request=_resolve_source_request_via_dispatcher(dispatcher),
        )
        if start_auto_runner is not None:
            services.start_auto_runner = start_auto_runner
        return frontdesk_intake(context, command, services=services)

    return handle


def _frontdesk_context(dispatcher, command):
    layout = dispatcher._layout
    return SimpleNamespace(
        command=command,
        cwd=layout.project_root,
        paths=layout,
        project=SimpleNamespace(
            cwd=layout.project_root,
            project_root=layout.project_root,
            config_dir=layout.ccb_dir,
            project_id=layout.project_id,
            source='ccbd-frontdesk-forward',
        ),
    )


def _submit_ask_via_dispatcher(dispatcher):
    def submit_ask(context, ask_command):
        message_body = message_with_reply_guidance(
            ask_command.message,
            message_type=getattr(ask_command, 'mode', None) or 'ask',
            compact=bool(getattr(ask_command, 'compact', False)),
            silence_on_success=bool(getattr(ask_command, 'silence', False)),
        )
        message_body, body_artifact = _artifact_request_body(
            context.paths,
            message_body,
            owner_id=f'{ask_command.sender}-to-{ask_command.target}',
            force=bool(getattr(ask_command, 'artifact_request', False)),
            inline=bool(getattr(ask_command, 'inline_request', False)),
        )
        receipt = dispatcher.submit(
            MessageEnvelope(
                project_id=context.project.project_id,
                to_agent=str(getattr(ask_command, 'target', '') or ''),
                from_actor=str(getattr(ask_command, 'sender', '') or 'frontdesk'),
                body=message_body,
                task_id=getattr(ask_command, 'task_id', None),
                reply_to=getattr(ask_command, 'reply_to', None),
                message_type=getattr(ask_command, 'mode', None) or 'ask',
                delivery_scope=DeliveryScope.SINGLE,
                silence_on_success=bool(getattr(ask_command, 'silence', False)),
                route_options=_route_options(ask_command),
                body_artifact=body_artifact,
            )
        )
        return AskSummary(
            project_id=context.project.project_id,
            submission_id=receipt.submission_id,
            jobs=tuple(job.to_record() for job in receipt.jobs),
        )

    return submit_ask


def _resolve_source_request_via_dispatcher(dispatcher):
    def resolve_source_request(context, source_job_id: str) -> dict[str, object]:
        get_job = getattr(dispatcher, 'get', None)
        if not callable(get_job):
            return {
                'status': 'blocked',
                'reason': 'frontdesk_source_job_lookup_unavailable',
                'source_job_id': source_job_id,
            }
        job = get_job(source_job_id)
        if job is None:
            return {
                'status': 'blocked',
                'reason': 'frontdesk_source_job_missing',
                'source_job_id': source_job_id,
            }
        return resolve_frontdesk_source_request(context, source_job_id=source_job_id, job=job)

    return resolve_source_request


def _route_options(command) -> dict[str, object]:
    options: dict[str, object] = {}
    if bool(getattr(command, 'callback', False)):
        options['mode'] = 'chain'
    if bool(getattr(command, 'artifact_request', False)):
        options['artifact_request'] = True
    if bool(getattr(command, 'artifact_reply', False)):
        options['artifact_reply'] = True
    return options


def _optional_text(value) -> str | None:
    text = str(value or '').strip()
    return text or None


__all__ = ['build_frontdesk_forward_planner_handler']
