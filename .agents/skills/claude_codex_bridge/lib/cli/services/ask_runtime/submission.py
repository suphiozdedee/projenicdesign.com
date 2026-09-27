from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
import os
from pathlib import Path

from agents.config_loader_runtime.role_lookup import looks_like_role_id, normalize_role_id
from agents.models import AgentValidationError
from ccbd.api_models import DeliveryScope, MessageEnvelope
from mailbox_runtime.targets import NON_AGENT_ACTORS, normalize_actor_name
from project.discovery import find_nearest_project_anchor, find_workspace_binding, load_workspace_binding, project_ccb_dir
from storage.path_helpers import runtime_project_root_from_path
from storage.text_artifacts import artifact_stub, maybe_spill_text, write_text_artifact

from .models import AskSummary

_LEGACY_GUIDANCE_MARKER = 'CCB reply guidance:'
_REPLY_MODE_MARKER = 'CCB_REPLY_MODE:'
_EXPLICIT_OUTPUT_HINTS = (
    'output requirements',
    'reply format',
    'response format',
    'format:',
    'only reply',
    'reply only',
    'full report',
    'full output',
    'detailed report',
    'complete output',
    'include everything',
    'all details',
    'leave nothing out',
    'verbatim',
    'do not summarize',
    'do not abbreviate',
    '\u5b8c\u6574\u8f93\u51fa',
    '\u4e0d\u8981\u603b\u7ed3',
    '\u4e0d\u8981\u538b\u7f29',
    '\u4e0d\u8981\u7cbe\u7b80',
    '\u4e0d\u8981\u7701\u7565',
    '\u9010\u5b57\u8fd4\u56de',
    '\u9010\u5b57',
    '\u539f\u6837\u8fd4\u56de',
    '\u4fdd\u7559\u539f\u6587',
    '\u5b8c\u6574\u65e5\u5fd7',
    '\u5b8c\u6574\u62a5\u544a',
    '\u8be6\u7ec6\u62a5\u544a',
    '\u5168\u6587',
)


def submit_ask(
    context,
    command,
    *,
    load_project_config_fn: Callable,
    resolve_ask_sender_fn: Callable,
    invoke_mounted_daemon_fn: Callable,
) -> AskSummary:
    config = load_project_config_fn(context.project.project_root).config
    _validate_project_local_ask_context(context, command, configured_agents=config.agents)
    try:
        normalized_target = _resolve_target(command.target, config.agents)
        _validate_target(normalized_target, config.agents)
    except ValueError:
        reload_drain_target = _resolve_active_reload_drain_target(context, command.target, invoke_mounted_daemon_fn)
        if reload_drain_target is None:
            raise
        normalized_target = reload_drain_target
    sender = resolve_ask_sender_fn(context, command.sender)
    normalized_sender = _normalize_sender(sender)
    _validate_sender(normalized_sender, config.agents)
    message_body = message_with_reply_guidance(
        command.message,
        message_type=command.mode or 'ask',
        compact=bool(getattr(command, 'compact', False)),
        silence_on_success=command.silence,
    )
    message_body, body_artifact = _artifact_request_body(
        context.paths,
        message_body,
        owner_id=f'{normalized_sender}-to-{normalized_target}',
        force=bool(getattr(command, 'artifact_request', False)),
        inline=bool(getattr(command, 'inline_request', False)),
    )
    payload = invoke_mounted_daemon_fn(
        context,
        allow_restart_stale=True,
        request_fn=lambda client: client.submit(
            MessageEnvelope(
                project_id=context.project.project_id,
                to_agent=normalized_target,
                from_actor=normalized_sender,
                body=message_body,
                task_id=command.task_id,
                reply_to=command.reply_to,
                message_type=command.mode or 'ask',
                delivery_scope=_delivery_scope(normalized_target),
                silence_on_success=command.silence,
                route_options=_route_options(command),
                body_artifact=body_artifact,
            )
        )
    )
    return _summary_from_payload(context.project.project_id, payload)


def _route_options(command) -> dict[str, object]:
    options: dict[str, object] = {}
    if bool(getattr(command, 'callback', False)):
        options['mode'] = 'chain'
    if bool(getattr(command, 'artifact_request', False)):
        options['artifact_request'] = True
    if bool(getattr(command, 'artifact_reply', False)):
        options['artifact_reply'] = True
    allowed_chain_targets = tuple(
        str(value or '').strip().lower()
        for value in (getattr(command, 'allowed_chain_targets', ()) or ())
        if str(value or '').strip()
    )
    if allowed_chain_targets:
        options['allowed_chain_targets'] = list(dict.fromkeys(allowed_chain_targets))
    if bool(getattr(command, 'bind_chain_workspace_tree', False)):
        options['bind_chain_workspace_tree'] = True
    return options


def _artifact_request_body(layout, message_body: str, *, owner_id: str, force: bool, inline: bool = False):
    if inline:
        return message_body, None
    if force:
        artifact = write_text_artifact(
            layout,
            text=message_body,
            kind='ask-request',
            owner_id=owner_id,
        )
        return (
            artifact_stub(
                prefix='CCB ask request was stored as an artifact by --artifact-request.',
                artifact=artifact,
                include_preview=False,
            ),
            artifact,
        )
    return maybe_spill_text(
        layout,
        text=message_body,
        kind='ask-request',
        owner_id=owner_id,
        prefix='CCB ask request is larger than 4 KiB and was stored as an artifact.',
    )


def message_with_reply_guidance(
    message: str,
    *,
    message_type: str,
    compact: bool = False,
    silence_on_success: bool = False,
) -> str:
    """Preserve the legacy API while attaching only dynamic reply-mode metadata.

    Stable reply guidance lives in the managed project-memory bundle so normal
    asks do not repeat the same policy in every request body.
    """
    if str(message_type or '').strip().lower() != 'ask':
        return message
    if _has_explicit_output_guidance(message):
        return message
    if silence_on_success:
        mode = 'silent'
    elif compact:
        mode = 'compact'
    else:
        return message
    return f'{str(message).rstrip()}\n\n{_REPLY_MODE_MARKER} {mode}'


def _has_explicit_output_guidance(message: str) -> bool:
    text = str(message or '')
    lowered = text.lower()
    if _LEGACY_GUIDANCE_MARKER.lower() in lowered or _REPLY_MODE_MARKER.lower() in lowered:
        return True
    return any(hint in lowered for hint in _EXPLICIT_OUTPUT_HINTS)


def _normalize_sender(value: str | None) -> str:
    try:
        return normalize_actor_name(value)
    except AgentValidationError as exc:
        raise ValueError(str(exc)) from exc


def _normalize_target(value: str | None) -> str:
    normalized = str(value or '').strip().lower()
    if normalized == 'all':
        return normalized
    if looks_like_role_id(normalized):
        return normalize_role_id(normalized)
    return _normalize_sender(normalized)


def _validate_target(target: str, configured_agents: Collection[str]) -> None:
    if target != 'all' and target not in configured_agents:
        raise ValueError(f'unknown agent: {target}')


def _resolve_target(value: str | None, configured_agents: Collection[str]) -> str:
    normalized = _normalize_target(value)
    if normalized == 'all' or normalized in configured_agents:
        return normalized
    if looks_like_role_id(normalized):
        role_id = normalize_role_id(normalized)
        matches = sorted(
            name
            for name, spec in dict(configured_agents).items()
            if str(getattr(spec, 'role', '') or '').strip().lower() == role_id
        )
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(
                f'role {role_id} is not bound to any configured agent; '
                'target the project-local agent name or add the role to config'
            )
        raise ValueError(
            f'role {role_id} is bound to multiple agents: {", ".join(matches)}; '
            'target one agent name explicitly'
        )
    return normalized


def _resolve_active_reload_drain_target(context, value: str | None, invoke_mounted_daemon_fn: Callable) -> str | None:
    try:
        normalized = _normalize_target(value)
    except ValueError:
        return None
    if normalized == 'all' or looks_like_role_id(normalized):
        return None
    try:
        payload = invoke_mounted_daemon_fn(
            context,
            allow_restart_stale=True,
            request_fn=lambda client: client.project_view(schema_version=1),
        )
    except Exception:
        return None
    return normalized if _project_view_has_active_reload_drain_target(payload, normalized) else None


def _project_view_has_active_reload_drain_target(payload: object, target: str) -> bool:
    if not isinstance(payload, Mapping):
        return False
    view = payload.get('view')
    if not isinstance(view, Mapping):
        return False
    drains = view.get('reload_drains')
    if isinstance(drains, Mapping):
        for record in tuple(drains.get('active_records') or ()):
            if isinstance(record, Mapping) and str(record.get('agent') or '') == target:
                return True
    for agent in tuple(view.get('agents') or ()):
        if (
            isinstance(agent, Mapping)
            and str(agent.get('name') or '') == target
            and bool(agent.get('dispatch_blocked_by_reload_drain'))
        ):
            return True
    return False


def _validate_sender(sender: str, configured_agents: Collection[str]) -> None:
    if sender in NON_AGENT_ACTORS:
        if sender == 'cmd':
            raise ValueError(f'unknown sender agent: {sender}')
        return
    if sender in configured_agents:
        return
    raise ValueError(f'unknown sender agent: {sender}')


def _delivery_scope(target: str | None) -> DeliveryScope:
    return DeliveryScope.BROADCAST if str(target or '').strip().lower() == 'all' else DeliveryScope.SINGLE


def _summary_from_payload(project_id: str, payload: dict) -> AskSummary:
    if 'job_id' in payload:
        jobs = (
            {
                'job_id': payload['job_id'],
                'agent_name': payload['agent_name'],
                'target_kind': payload.get('target_kind', 'agent'),
                'target_name': payload.get('target_name', payload['agent_name']),
                'provider_instance': payload.get('provider_instance'),
                'status': payload['status'],
            },
        )
        submission_id = None
    else:
        jobs = tuple(payload.get('jobs', ()))
        submission_id = payload.get('submission_id')
    return AskSummary(project_id=project_id, submission_id=submission_id, jobs=jobs)


def _validate_project_local_ask_context(context, command, *, configured_agents: Collection[str]) -> None:
    project_root = _resolve_path(Path(context.project.project_root))
    cwd = _resolve_path(Path(context.cwd))
    local_anchor = _resolve_optional(find_nearest_project_anchor(cwd))
    source = str(getattr(context.project, 'source', '') or '')
    source_test_explicit = _is_source_test_explicit_project_ask(
        context,
        command,
        project_root=project_root,
        cwd=cwd,
    )

    if str(getattr(command, 'project', '') or '').strip():
        if local_anchor is None and not source_test_explicit:
            raise ValueError(
                'ask is project-local; --project cannot select a CCB project from outside that project'
            )
        if local_anchor != project_root and not source_test_explicit:
            raise ValueError(
                'ask is project-local; --project cannot target another .ccb project'
            )

    if (
        local_anchor is not None
        and local_anchor != project_root
        and source != 'caller-runtime'
        and not _is_internal_explicit_project_ask(context, command)
        and not source_test_explicit
    ):
        raise ValueError(
            'ask is project-local; workspace or cwd resolved to another .ccb project'
        )

    if source == 'workspace-binding':
        _validate_workspace_binding_project(context, project_root)

    _validate_caller_runtime_project(project_root, configured_agents=configured_agents)


def _is_internal_explicit_project_ask(context, command) -> bool:
    if str(getattr(context.project, 'source', '') or '') != 'explicit':
        return False
    if str(getattr(command, 'project', '') or '').strip():
        return False
    parent_kind = str(getattr(getattr(context, 'command', None), 'kind', '') or '')
    return parent_kind not in {'', 'ask'}


def _is_source_test_explicit_project_ask(context, command, *, project_root: Path, cwd: Path) -> bool:
    if os.environ.get('CCB_TEST_ENTRYPOINT') != '1':
        return False
    if str(getattr(context.project, 'source', '') or '') != 'explicit':
        return False
    if not str(getattr(command, 'project', '') or '').strip():
        return False
    allowed_roots = _source_test_allowed_roots()
    if not allowed_roots:
        return False
    return _path_under_any(cwd, allowed_roots) and _path_under_any(project_root, allowed_roots)


def _source_test_allowed_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for env_name in ('CCB_SOURCE_ALLOWED_ROOTS', 'CCB_TEST_ROOTS'):
        for item in os.environ.get(env_name, '').split(os.pathsep):
            text = item.strip()
            if text:
                roots.append(_resolve_path(Path(text)))
    return tuple(roots)


def _path_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = _resolve_path(path)
    for root in roots:
        if resolved == root or root in resolved.parents:
            return True
    return False


def _validate_workspace_binding_project(context, project_root: Path) -> None:
    binding_path = find_workspace_binding(Path(context.cwd))
    if binding_path is None:
        raise ValueError('ask is project-local; workspace binding is missing')
    binding = load_workspace_binding(binding_path)
    target_project = _resolve_path(Path(str(binding['target_project'])))
    if target_project != project_root:
        raise ValueError('ask is project-local; workspace binding targets another .ccb project')
    binding_project_id = str(binding.get('project_id') or '').strip()
    if binding_project_id and binding_project_id != str(context.project.project_id):
        raise ValueError('ask is project-local; workspace binding project id does not match')


def _validate_caller_runtime_project(project_root: Path, *, configured_agents: Collection[str]) -> None:
    runtime_project = _caller_runtime_project_root()
    if runtime_project is None or runtime_project == project_root:
        return
    caller = _normalized_actor_candidate(os.environ.get('CCB_CALLER_ACTOR'))
    if caller is not None and caller in configured_agents:
        raise ValueError(
            'ask is project-local; caller runtime belongs to another .ccb project'
        )


def _caller_runtime_project_root() -> Path | None:
    for env_name in ('CCB_CALLER_RUNTIME_DIR', 'CODEX_RUNTIME_DIR'):
        root = _project_root_from_runtime_path(os.environ.get(env_name))
        if root is not None:
            return root
    return None


def _project_root_from_runtime_path(value: str | None) -> Path | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    runtime_path = _resolve_path(Path(raw))
    marker_project_root = runtime_project_root_from_path(runtime_path)
    if marker_project_root is not None:
        marker_root = _resolve_path(marker_project_root)
        if project_ccb_dir(marker_root).is_dir():
            return marker_root
    for candidate in (runtime_path, *runtime_path.parents):
        if candidate.name != 'agents' or candidate.parent.name != '.ccb':
            continue
        project_root = _resolve_path(candidate.parent.parent)
        if project_ccb_dir(project_root).is_dir():
            return project_root
    return None


def _normalized_actor_candidate(value: str | None) -> str | None:
    try:
        return normalize_actor_name(value)
    except AgentValidationError:
        return None


def _resolve_optional(path: Path | None) -> Path | None:
    return _resolve_path(path) if path is not None else None


def _resolve_path(path: Path) -> Path:
    current = Path(path).expanduser()
    try:
        return current.resolve()
    except Exception:
        return current.absolute()


__all__ = ['message_with_reply_guidance', 'submit_ask']
