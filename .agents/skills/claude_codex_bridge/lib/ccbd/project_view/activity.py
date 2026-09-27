from __future__ import annotations

from dataclasses import dataclass

from ccbd.services.runtime_recovery_policy import PROVIDER_AUTH_REVOKED_RUNTIME_HEALTH
from ccbd.system import parse_utc_timestamp

ACTIVITY_ACTIVE = 'active'
ACTIVITY_PENDING = 'pending'
ACTIVITY_IDLE = 'idle'
ACTIVITY_FAILED = 'failed'
ACTIVITY_OFFLINE = 'offline'

ACTIVITY_PRESENTATION = {
    ACTIVITY_ACTIVE: ('●', 'green'),
    ACTIVITY_PENDING: ('◐', 'yellow'),
    ACTIVITY_IDLE: ('○', 'blue'),
    ACTIVITY_FAILED: ('✕', 'red'),
    ACTIVITY_OFFLINE: ('·', 'gray'),
}

_RECOVERY_STATES = frozenset({'starting', 'recovering', 'reflowing', 'mounting'})
_FAULT_HEALTH = frozenset(
    {
        'faulted',
        'failed',
        'error',
        'crashed',
        'orphaned',
        PROVIDER_AUTH_REVOKED_RUNTIME_HEALTH,
    }
)
_PROVIDER_PROMPT_MARKERS = (
    'do you trust the contents of this directory?',
    'press enter to continue',
)
_PROVIDER_ACTIVE_WORDS = frozenset({'working', 'thinking', 'running'})
_PROVIDER_ACTIVE_MARKERS = (
    'esc to interrupt',
    'running...',
    'running…',
)
_PROVIDER_BACKGROUND_ACTIVE_MARKERS = (
    'background terminal running',
    'background terminals running',
    'running scheduled task',
    'shell still running',
    'shells still running',
    'messages to be submitted after next tool call',
    'press esc to interrupt and send immediately',
)
_PROVIDER_TERMINAL_ERROR_MARKERS = (
    'stream disconnected before completion',
    'error sending request for url',
    'connection refused',
    'connection reset',
    'connection closed',
    'connection timed out',
    'request timed out',
    'model unavailable',
    'model_not_found',
    'rate limit',
    'rate_limit',
    'too many requests',
    'exceeded retry limit',
    'last status: 4',
    'last status: 5',
    'overloaded',
    'api error',
    'internal server error',
)
_PROVIDER_IDLE_PROMPTS = ('❯', '›')
_PROVIDER_WORKING_TAIL_LINES = 12
PROVIDER_INPUT_STUCK_AFTER_S = 30.0
PROVIDER_ACTIVITY_PANE_ERROR_PROBE_AFTER_S = 5.0
JOB_RUNNING_STALE_AFTER_S = 120.0
_PENDING_JOB_STATUSES = frozenset({'accepted', 'queued'})
_WAITING_CALLBACK_STATES = frozenset({'pending', 'child_completed'})


@dataclass(frozen=True)
class AgentActivityFacts:
    namespace_mounted: bool
    provider: str | None = None
    runtime_state: str | None = None
    runtime_health: str | None = None
    reconcile_state: str | None = None
    desired_state: str | None = None
    pane_id: str | None = None
    pane_state: str | None = None
    pane_text: str | None = None
    current_job_status: str | None = None
    current_job_id: str | None = None
    current_job_updated_at: str | None = None
    queue_depth: int = 0
    chain_waiting_state: str | None = None
    chain_child_job_id: str | None = None
    chain_child_agent: str | None = None
    chain_updated_at: str | None = None
    provider_activity_state: str | None = None
    provider_activity_source: str | None = None
    provider_activity_reason: str | None = None
    provider_activity_updated_at: str | None = None
    provider_runtime_state: str | None = None
    provider_runtime_source: str | None = None
    provider_runtime_reason: str | None = None


@dataclass(frozen=True)
class AgentActivity:
    state: str
    source: str
    reason: str
    last_progress_at: str | None = None
    current_job_id: str | None = None
    symbol_override: str | None = None
    color_override: str | None = None

    @property
    def symbol(self) -> str:
        if self.symbol_override:
            return self.symbol_override
        return ACTIVITY_PRESENTATION[self.state][0]

    @property
    def color(self) -> str:
        if self.color_override:
            return self.color_override
        return ACTIVITY_PRESENTATION[self.state][1]

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            'activity_state': self.state,
            'activity_symbol': self.symbol,
            'activity_color': self.color,
            'activity_source': self.source,
            'activity_reason': self.reason,
            'last_progress_at': self.last_progress_at,
        }
        if self.current_job_id:
            record['current_job_id'] = self.current_job_id
        return record


def resolve_agent_activity(
    facts: AgentActivityFacts,
    *,
    now: str,
    provider_input_stuck_after_s: float = PROVIDER_INPUT_STUCK_AFTER_S,
    job_running_stale_after_s: float = JOB_RUNNING_STALE_AFTER_S,
) -> AgentActivity:
    runtime_state = _clean(facts.runtime_state)
    runtime_health = _clean(facts.runtime_health)
    reconcile_state = _clean(facts.reconcile_state)
    desired_state = _clean(facts.desired_state)
    job_status = _clean(facts.current_job_status)
    chain_state = _clean(facts.chain_waiting_state)
    provider_activity_state = _clean(facts.provider_activity_state)

    if not facts.namespace_mounted:
        return AgentActivity(ACTIVITY_OFFLINE, 'namespace', 'namespace_unmounted')
    if runtime_state == 'stopped' or desired_state == 'stopped':
        return AgentActivity(ACTIVITY_OFFLINE, 'runtime_health', 'agent_stopped')

    if reconcile_state == 'failed':
        return AgentActivity(ACTIVITY_FAILED, 'reconcile', 'reconcile_failed')
    if runtime_health == PROVIDER_AUTH_REVOKED_RUNTIME_HEALTH:
        return AgentActivity(ACTIVITY_FAILED, 'runtime_health', 'provider_auth_revoked')
    if runtime_state == 'failed' or runtime_health in _FAULT_HEALTH:
        return AgentActivity(ACTIVITY_FAILED, 'runtime_health', 'runtime_fault')
    if _pane_missing(facts) and reconcile_state not in _RECOVERY_STATES:
        return AgentActivity(ACTIVITY_FAILED, 'pane_liveness', 'pane_missing_unowned')

    if runtime_state == 'starting' or reconcile_state in _RECOVERY_STATES:
        reason = 'pane_missing_recovering' if _pane_missing(facts) else 'reconcile_active'
        return AgentActivity(ACTIVITY_PENDING, 'reconcile', reason)

    provider_runtime_activity = _provider_runtime_activity(facts)
    if provider_runtime_activity is not None:
        return provider_runtime_activity

    provider_activity_source = _clean(facts.provider_activity_source)
    provider_is_codex = _provider_is_codex(facts)
    if (
        not provider_is_codex
        and provider_activity_state in {ACTIVITY_ACTIVE, ACTIVITY_PENDING}
        and _provider_terminal_error(facts.pane_text)
    ):
        return AgentActivity(
            ACTIVITY_FAILED,
            'provider_pane',
            'provider_terminal_error',
            last_progress_at=facts.provider_activity_updated_at,
            current_job_id=facts.current_job_id,
        )

    if not provider_is_codex and provider_activity_state in {ACTIVITY_ACTIVE, ACTIVITY_PENDING, ACTIVITY_IDLE, ACTIVITY_FAILED}:
        return AgentActivity(
            provider_activity_state,
            provider_activity_source or 'provider_activity',
            _clean(facts.provider_activity_reason) or f'provider_activity_{provider_activity_state}',
            last_progress_at=facts.provider_activity_updated_at,
            current_job_id=facts.current_job_id,
        )

    if job_status in _PENDING_JOB_STATUSES:
        return AgentActivity(
            ACTIVITY_PENDING,
            'ccb_job',
            'job_queued',
            last_progress_at=facts.current_job_updated_at,
            current_job_id=facts.current_job_id,
        )

    if job_status == 'running':
        if not provider_is_codex and _provider_working(facts.pane_text):
            return AgentActivity(
                ACTIVITY_ACTIVE,
                'provider_pane',
                'provider_working',
                last_progress_at=facts.current_job_updated_at,
                current_job_id=facts.current_job_id,
            )
        age_s = _age_seconds(now, facts.current_job_updated_at)
        if (
            not provider_is_codex
            and age_s is not None
            and age_s >= provider_input_stuck_after_s
            and provider_prompt_idle_after_request(facts.pane_text, facts.current_job_id)
        ):
            return AgentActivity(
                ACTIVITY_PENDING,
                'provider_prompt',
                'provider_prompt_idle',
                last_progress_at=facts.current_job_updated_at,
                current_job_id=facts.current_job_id,
            )
        if (
            not provider_is_codex
            and age_s is not None
            and age_s >= provider_input_stuck_after_s
            and provider_prompt_input_stuck(facts.pane_text, facts.current_job_id)
        ):
            return AgentActivity(
                ACTIVITY_PENDING,
                'provider_prompt',
                'provider_prompt_input_stuck',
                last_progress_at=facts.current_job_updated_at,
                current_job_id=facts.current_job_id,
            )
        if age_s is not None and age_s >= job_running_stale_after_s:
            return AgentActivity(
                ACTIVITY_PENDING,
                'ccb_job',
                'job_running_stale',
                last_progress_at=facts.current_job_updated_at,
                current_job_id=facts.current_job_id,
            )
        return AgentActivity(
            ACTIVITY_ACTIVE,
            'ccb_job',
            'job_running',
            last_progress_at=facts.current_job_updated_at,
            current_job_id=facts.current_job_id,
        )

    if chain_state in _WAITING_CALLBACK_STATES:
        reason = 'chain_child_completed' if chain_state == 'child_completed' else 'chain_waiting_child'
        return AgentActivity(
            ACTIVITY_PENDING,
            'chain',
            reason,
            last_progress_at=facts.chain_updated_at,
        )

    if not provider_is_codex:
        if _provider_working(facts.pane_text):
            return AgentActivity(ACTIVITY_ACTIVE, 'provider_pane', 'provider_working')
        if _provider_tail_idle_prompt(facts.pane_text):
            return AgentActivity(ACTIVITY_IDLE, 'pane_liveness', 'pane_alive')
        if _provider_waiting_for_user(facts.pane_text):
            return AgentActivity(ACTIVITY_PENDING, 'provider_prompt', 'provider_waiting_for_user')

    if runtime_state == 'degraded':
        return AgentActivity(ACTIVITY_PENDING, 'runtime_health', 'health_unknown')
    if _pane_alive(facts):
        return AgentActivity(ACTIVITY_IDLE, 'pane_liveness', 'pane_alive')
    if runtime_state == 'idle':
        return AgentActivity(ACTIVITY_IDLE, 'pane_liveness', 'pane_alive')
    if runtime_state == 'busy':
        return AgentActivity(ACTIVITY_IDLE, 'pane_liveness', 'pane_alive')

    return AgentActivity(ACTIVITY_PENDING, 'runtime_health', 'runtime_unknown')


def _age_seconds(now: str, timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        return (parse_utc_timestamp(now) - parse_utc_timestamp(timestamp)).total_seconds()
    except Exception:
        return None


def _clean(value: object) -> str:
    return str(value or '').strip().lower()


def _provider_is_codex(facts: AgentActivityFacts) -> bool:
    return _clean(facts.provider) == 'codex'


def _provider_runtime_activity(facts: AgentActivityFacts) -> AgentActivity | None:
    source = _clean(facts.provider_runtime_source)
    if source not in {'codex_runtime', 'claude_runtime'}:
        return None
    runtime_state = _clean(facts.provider_runtime_state)
    job_status = _clean(facts.current_job_status)
    if runtime_state == 'free' and job_status in {'accepted', 'queued', 'running'}:
        return None
    reason = _clean(facts.provider_runtime_reason) or f'{source}_{runtime_state or "unknown"}'
    last_progress_at = facts.current_job_updated_at
    if runtime_state == 'free':
        return AgentActivity(
            ACTIVITY_IDLE,
            source,
            reason,
            last_progress_at=last_progress_at,
            current_job_id=facts.current_job_id,
            symbol_override='◇',
            color_override='blue',
        )
    if runtime_state == 'start':
        return AgentActivity(
            ACTIVITY_PENDING,
            source,
            reason,
            last_progress_at=last_progress_at,
            current_job_id=facts.current_job_id,
            symbol_override='◌',
            color_override='yellow',
        )
    if runtime_state == 'working':
        return AgentActivity(
            ACTIVITY_ACTIVE,
            source,
            reason,
            last_progress_at=last_progress_at,
            current_job_id=facts.current_job_id,
            symbol_override='●',
            color_override='green',
        )
    if runtime_state == 'tool_running':
        return AgentActivity(
            ACTIVITY_ACTIVE,
            source,
            reason,
            last_progress_at=last_progress_at,
            current_job_id=facts.current_job_id,
            symbol_override='◆',
            color_override='green',
        )
    if runtime_state == 'interrupted':
        return AgentActivity(
            ACTIVITY_PENDING,
            source,
            reason,
            last_progress_at=last_progress_at,
            current_job_id=facts.current_job_id,
            symbol_override='!',
            color_override='yellow',
        )
    if runtime_state == 'reconnecting':
        return AgentActivity(
            ACTIVITY_PENDING,
            source,
            reason,
            last_progress_at=last_progress_at,
            current_job_id=facts.current_job_id,
            symbol_override='↻',
            color_override='yellow',
        )
    if runtime_state in {'waiting_for_user', 'auth_required'}:
        return AgentActivity(
            ACTIVITY_PENDING,
            source,
            reason,
            last_progress_at=last_progress_at,
            current_job_id=facts.current_job_id,
            symbol_override='?',
            color_override='yellow',
        )
    if runtime_state in {'auth_failed', 'api_error', 'config_error', 'failed', 'pane_dead'}:
        return AgentActivity(
            ACTIVITY_FAILED,
            source,
            reason,
            last_progress_at=last_progress_at,
            current_job_id=facts.current_job_id,
            symbol_override='✕',
            color_override='red',
        )
    if runtime_state == 'unknown':
        return AgentActivity(
            ACTIVITY_PENDING,
            source,
            reason,
            last_progress_at=last_progress_at,
            current_job_id=facts.current_job_id,
            symbol_override='?',
            color_override='gray',
        )
    return None


def _pane_missing(facts: AgentActivityFacts) -> bool:
    if _clean(facts.pane_state) in {'missing', 'dead'}:
        return True
    return bool(str(facts.pane_id or '').strip()) and _clean(facts.pane_state) == 'missing'


def _pane_alive(facts: AgentActivityFacts) -> bool:
    if not str(facts.pane_id or '').strip():
        return False
    return _clean(facts.pane_state) not in {'missing', 'dead'}


def _provider_waiting_for_user(pane_text: str | None) -> bool:
    normalized = _provider_recent_text(pane_text)
    if not normalized:
        return False
    return all(marker in normalized for marker in _PROVIDER_PROMPT_MARKERS)


def _provider_working(pane_text: str | None) -> bool:
    normalized = _provider_recent_text(pane_text)
    if not normalized:
        return False
    if any(marker in normalized for marker in _PROVIDER_BACKGROUND_ACTIVE_MARKERS):
        return True
    if 'running...' in normalized or 'running…' in normalized:
        return True
    if 'esc to interrupt' in normalized:
        return not _provider_tail_idle_prompt(pane_text)
    if _codex_ready_after_active_marker(pane_text):
        return False
    return any(word in normalized for word in _PROVIDER_ACTIVE_WORDS) and 'interrupt' in normalized


def _provider_terminal_error(pane_text: str | None) -> bool:
    normalized = _provider_recent_text(pane_text)
    if not normalized:
        return False
    return any(marker in normalized for marker in _PROVIDER_TERMINAL_ERROR_MARKERS)


def _provider_recent_text(pane_text: str | None) -> str:
    lines = [line for line in str(pane_text or '').lower().splitlines() if line.strip()]
    if not lines:
        return ''
    return ' '.join(' '.join(lines[-_PROVIDER_WORKING_TAIL_LINES:]).split())


def _provider_tail_idle_prompt(pane_text: str | None) -> bool:
    lines = [line.strip().replace('\xa0', ' ') for line in str(pane_text or '').splitlines() if line.strip()]
    if not lines:
        return False
    prompt_indexes = [
        index
        for index, line in enumerate(lines)
        if _is_provider_input_prompt_line(line)
    ]
    if not prompt_indexes:
        return False
    last_prompt = max(prompt_indexes)
    tail = ' '.join(lines[last_prompt:]).lower()
    if any(marker in tail for marker in _PROVIDER_ACTIVE_MARKERS):
        return False
    if any(word in tail for word in _PROVIDER_ACTIVE_WORDS) and 'interrupt' in tail:
        return False
    return True


def _is_provider_input_prompt_line(line: str) -> bool:
    text = str(line or '').strip()
    return any(text == marker or text.startswith(f'{marker} ') for marker in _PROVIDER_IDLE_PROMPTS)


def _codex_ready_after_active_marker(pane_text: str | None) -> bool:
    lines = [line.strip().lower() for line in str(pane_text or '').splitlines()]
    if not any('openai codex' in line for line in lines):
        return False
    active_indexes = [
        index
        for index, line in enumerate(lines)
        if any(marker in line for marker in _PROVIDER_ACTIVE_MARKERS)
    ]
    if not active_indexes:
        return False
    last_active_index = max(active_indexes)
    ready_model_indexes = [
        index
        for index, line in enumerate(lines)
        if index > last_active_index and 'model:' in line and 'loading' not in line
    ]
    prompt_indexes = [
        index
        for index, line in enumerate(lines)
        if index > last_active_index and line.startswith('›')
    ]
    return bool(ready_model_indexes and prompt_indexes)


def provider_prompt_idle_after_request(pane_text: str | None, request_id: object) -> bool:
    request = str(request_id or '').strip()
    text = str(pane_text or '').replace('\xa0', ' ')
    if not request or request not in text or _provider_working(text):
        return False
    tail = text.rsplit(request, 1)[-1]
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped in _PROVIDER_IDLE_PROMPTS:
            return True
    return False


def provider_prompt_input_stuck(pane_text: str | None, request_id: object) -> bool:
    request = str(request_id or '').strip()
    text = str(pane_text or '').replace('\xa0', ' ')
    if not request or request not in text or _provider_working(text):
        return False
    lines = text.splitlines()
    request_line_index = None
    for index, line in enumerate(lines):
        if request in line:
            request_line_index = index
    if request_line_index is None:
        return False
    request_line = lines[request_line_index].strip()
    if not any(request_line.startswith(prompt) for prompt in _PROVIDER_IDLE_PROMPTS):
        return False
    tail = '\n'.join(lines[request_line_index + 1 :])
    return not _provider_output_started(tail)


def provider_prompt_idle(pane_text: str | None) -> bool:
    text = str(pane_text or '').replace('\xa0', ' ')
    if not text or _provider_working(text):
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in _PROVIDER_IDLE_PROMPTS:
            return True
    return False


def _provider_output_started(value: str) -> bool:
    text = str(value or '')
    if not text.strip():
        return False
    markers = ('\n●', '\n✻', '\n✶', '\n·', 'Bash(', 'Web Search(', 'Read(', 'Edit(', 'Write(')
    return any(marker in text for marker in markers)


__all__ = [
    'ACTIVITY_ACTIVE',
    'ACTIVITY_FAILED',
    'ACTIVITY_IDLE',
    'ACTIVITY_OFFLINE',
    'ACTIVITY_PENDING',
    'ACTIVITY_PRESENTATION',
    'AgentActivity',
    'AgentActivityFacts',
    'JOB_RUNNING_STALE_AFTER_S',
    'PROVIDER_ACTIVITY_PANE_ERROR_PROBE_AFTER_S',
    'PROVIDER_INPUT_STUCK_AFTER_S',
    'provider_prompt_idle',
    'provider_prompt_idle_after_request',
    'provider_prompt_input_stuck',
    'resolve_agent_activity',
]
