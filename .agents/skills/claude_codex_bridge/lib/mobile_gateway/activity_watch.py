from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable, Mapping

from agents.models import normalize_agent_name
from provider_hooks.activity import read_activity_evidence
from provider_pane_status.claude_pane import parse_claude_pane_status
from provider_pane_status.claude_session import (
    claude_activity_status,
    compose_claude_runtime_status,
)
from provider_pane_status.codex_pane import parse_codex_pane_status
from provider_pane_status.codex_session import (
    compose_codex_runtime_status,
    read_codex_session_status,
)
from storage.paths import PathLayout

from .terminal import TerminalHistoryTarget, capture_tmux_pane_text


_ACTIVE_RUNTIME_STATES = frozenset({'working', 'tool_running'})
_PENDING_RUNTIME_STATES = frozenset(
    {'start', 'interrupted', 'waiting_for_user', 'reconnecting'}
)
_FAILED_RUNTIME_STATES = frozenset(
    {
        'api_error',
        'auth_failed',
        'auth_required',
        'config_error',
        'failed',
        'pane_dead',
    }
)


@dataclass(frozen=True)
class MobileAgentActivityProbe:
    activity_state: str
    reason: str
    source: str


PaneCapture = Callable[[TerminalHistoryTarget], str]


def probe_mobile_agent_activity(
    *,
    project_root: Path,
    project_id: str,
    agent: str,
    provider: str | None,
    namespace_epoch: int | None,
    now: str,
    pane_capture: PaneCapture = capture_tmux_pane_text,
) -> MobileAgentActivityProbe:
    """Read bounded provider evidence for one actively watched mobile agent.

    The probe does not call ccbd and does not parse a full conversation. It
    validates the CCB-owned runtime binding, captures a small pane tail, and
    feeds that evidence through the same provider status parsers used by
    ProjectView. Unknown or stale evidence remains unknown so a transient
    capture failure cannot manufacture an idle or failed transition.
    """

    layout = PathLayout(Path(project_root))
    normalized_agent = normalize_agent_name(agent)
    runtime = _read_json(layout.agent_runtime_path(normalized_agent))
    provider_name = str(provider or runtime.get('provider') or '').strip().lower()
    if provider_name not in {'claude', 'codex'}:
        return MobileAgentActivityProbe(
            'unknown',
            'provider_activity_probe_unsupported',
            'provider_probe',
        )
    if not _runtime_identity_matches(
        runtime,
        project_id=project_id,
        agent=normalized_agent,
        provider=provider_name,
        namespace_epoch=namespace_epoch,
    ):
        return MobileAgentActivityProbe(
            'unknown',
            'provider_activity_probe_stale_binding',
            'provider_probe',
        )

    session = _provider_session_record(
        layout=layout,
        runtime=runtime,
        provider=provider_name,
        agent=normalized_agent,
    )
    binding = {**session, **runtime}
    target = _terminal_target(
        project_id=project_id,
        agent=normalized_agent,
        namespace_epoch=namespace_epoch,
        binding=binding,
    )
    if target is None:
        return MobileAgentActivityProbe(
            'unknown',
            'provider_activity_probe_missing_pane_binding',
            'provider_probe',
        )

    try:
        pane_text = pane_capture(target)
    except Exception:
        return MobileAgentActivityProbe(
            'unknown',
            'provider_activity_probe_capture_failed',
            'provider_probe',
        )

    if provider_name == 'claude':
        runtime_state, reason = _claude_runtime_state(
            layout=layout,
            project_id=project_id,
            agent=normalized_agent,
            binding=binding,
            pane_text=pane_text,
            now=now,
        )
    else:
        runtime_state, reason = _codex_runtime_state(
            layout=layout,
            binding=binding,
            pane_text=pane_text,
        )
    return MobileAgentActivityProbe(
        _activity_state_for_runtime(runtime_state),
        reason,
        f'{provider_name}_runtime',
    )


def _claude_runtime_state(
    *,
    layout: PathLayout,
    project_id: str,
    agent: str,
    binding: Mapping[str, object],
    pane_text: str,
    now: str,
) -> tuple[str, str]:
    pane_status = parse_claude_pane_status(pane_text)
    runtime_dir = layout.agent_provider_runtime_dir(agent, 'claude')
    activity = read_activity_evidence(
        runtime_dir,
        project_id=project_id,
        agent_name=agent,
        provider='claude',
        ccb_session_id=_text(binding.get('ccb_session_id')),
        provider_session_id=_text(binding.get('claude_session_id')),
        pane_id=_pane_id(binding),
        workspace_path=_text(binding.get('workspace_path') or binding.get('work_dir')),
        now=now,
    )
    activity_status = claude_activity_status(activity, now=now)
    if pane_status.state == 'unknown' and activity_status is None:
        return 'unknown', pane_status.reason
    status = compose_claude_runtime_status(
        activity_status,
        None,
        job_running=False,
        pane_status=pane_status,
    )
    return status.state, status.reason


def _codex_runtime_state(
    *,
    layout: PathLayout,
    binding: Mapping[str, object],
    pane_text: str,
) -> tuple[str, str]:
    pane_status = parse_codex_pane_status(pane_text)
    home_text = _text(binding.get('codex_home'))
    session_root = (
        Path(home_text).expanduser() / 'sessions'
        if home_text
        else layout.agent_provider_state_dir(
            str(binding.get('agent_name') or ''),
            'codex',
        )
        / 'home'
        / 'sessions'
    )
    session_status = read_codex_session_status(
        session_root,
        work_dir=_text(binding.get('workspace_path') or binding.get('work_dir')),
    )
    if (
        pane_status.state == 'unknown'
        and session_status.state == 'free'
        and session_status.reason == 'no_codex_session_files'
    ):
        return 'unknown', pane_status.reason
    status = compose_codex_runtime_status(pane_status, session_status)
    return status.state, status.reason


def _activity_state_for_runtime(runtime_state: str) -> str:
    state = str(runtime_state or '').strip().lower()
    if state == 'free':
        return 'idle'
    if state in _ACTIVE_RUNTIME_STATES:
        return 'active'
    if state in _PENDING_RUNTIME_STATES:
        return 'pending'
    if state in _FAILED_RUNTIME_STATES:
        return 'failed'
    return 'unknown'


def _runtime_identity_matches(
    runtime: Mapping[str, object],
    *,
    project_id: str,
    agent: str,
    provider: str,
    namespace_epoch: int | None,
) -> bool:
    if not runtime:
        return False
    recorded_project = _text(runtime.get('project_id'))
    recorded_agent = _text(runtime.get('agent_name'))
    recorded_provider = _text(runtime.get('provider')).lower()
    if recorded_project and recorded_project != str(project_id).strip():
        return False
    if recorded_agent and recorded_agent != agent:
        return False
    if recorded_provider and recorded_provider != provider:
        return False
    recorded_epoch = _int(runtime.get('workspace_epoch'))
    if (
        namespace_epoch is not None
        and recorded_epoch is not None
        and namespace_epoch != recorded_epoch
    ):
        return False
    desired_state = _text(runtime.get('desired_state')).lower()
    if desired_state == 'stopped':
        return False
    return True


def _provider_session_record(
    *,
    layout: PathLayout,
    runtime: Mapping[str, object],
    provider: str,
    agent: str,
) -> dict[str, object]:
    candidates: list[Path] = []
    session_ref = _text(runtime.get('session_ref') or runtime.get('session_file'))
    if session_ref:
        candidates.append(Path(session_ref).expanduser())
    candidates.append(layout.runtime_state_root / f'.{provider}-{agent}-session')
    allowed_roots = {
        layout.ccb_dir.resolve(strict=False),
        layout.runtime_state_root.resolve(strict=False),
    }
    for path in candidates:
        resolved = path.resolve(strict=False)
        if not any(_is_relative_to(resolved, root) for root in allowed_roots):
            continue
        payload = _read_json(resolved)
        if payload:
            return payload
    return {}


def _terminal_target(
    *,
    project_id: str,
    agent: str,
    namespace_epoch: int | None,
    binding: Mapping[str, object],
) -> TerminalHistoryTarget | None:
    pane_id = _pane_id(binding)
    socket_path = _text(binding.get('tmux_socket_path'))
    if pane_id is None or not socket_path:
        return None
    session_name = _text(binding.get('tmux_session')) or pane_id
    return TerminalHistoryTarget(
        project_id=project_id,
        namespace_epoch=namespace_epoch or _int(binding.get('workspace_epoch')) or 0,
        agent=agent,
        window=_text(binding.get('tmux_window_name')),
        pane_id=pane_id,
        socket_path=socket_path,
        session_name=session_name,
        max_lines=80,
    )


def _pane_id(binding: Mapping[str, object]) -> str | None:
    for key in ('active_pane_id', 'pane_id', 'tmux_session'):
        value = _text(binding.get(key))
        if value.startswith('%'):
            return value
    return None


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _text(value: object) -> str:
    return str(value or '').strip()


def _int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = ['MobileAgentActivityProbe', 'probe_mobile_agent_activity']
