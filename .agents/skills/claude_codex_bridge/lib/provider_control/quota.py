from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from storage.paths import PathLayout


# Contract and normalization adapted from Paseo at
# b599d38a772f621e0001abfb90a769de11c8cd8b:
# packages/server/src/services/quota-fetcher/providers/{codex,claude}.ts and
# packages/server/src/services/quota-fetcher/{service,usage}.ts.
_CODEX_USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'
_CLAUDE_USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
_CLAUDE_OAUTH_BETA = 'oauth-2025-04-20'
_DEFAULT_TIMEOUT_SECONDS = 3.0
_DEFAULT_TTL_SECONDS = 5 * 60
_DEFAULT_MAX_ENTRIES = 32
_MAX_RESPONSE_BYTES = 512 * 1024

QuotaFetch = Callable[[str, Mapping[str, str], float], tuple[int, object]]


@dataclass(frozen=True)
class ProviderQuotaWindow:
    id: str
    label: str
    used_pct: float | None
    resets_at: str | None

    def to_record(self) -> dict[str, object]:
        used = _percentage(self.used_pct)
        return {
            'id': self.id,
            'label': self.label,
            'used_pct': used,
            'remaining_pct': max(0.0, 100.0 - used) if used is not None else None,
            'resets_at': self.resets_at,
            'tone': _tone(used),
        }


@dataclass(frozen=True)
class ProviderQuotaBalance:
    id: str
    label: str
    remaining: float | None
    unit: str

    def to_record(self) -> dict[str, object]:
        return {
            'id': self.id,
            'label': self.label,
            'remaining': self.remaining,
            'unit': self.unit,
            'tone': 'danger' if self.remaining is not None and self.remaining <= 0 else 'ok',
        }


@dataclass(frozen=True)
class ProviderAccountQuota:
    provider_id: str
    display_name: str
    status: str
    fetched_at: str
    next_refresh_at: str
    plan_label: str | None = None
    windows: tuple[ProviderQuotaWindow, ...] = ()
    balances: tuple[ProviderQuotaBalance, ...] = ()
    details: tuple[dict[str, object], ...] = ()
    diagnostic_code: str | None = None

    def to_record(self) -> dict[str, object]:
        return {
            'provider_id': self.provider_id,
            'display_name': self.display_name,
            'status': self.status,
            'plan_label': self.plan_label,
            'source_label': 'provider_account_api',
            'fetched_at': self.fetched_at,
            'next_refresh_at': self.next_refresh_at,
            'windows': [item.to_record() for item in self.windows],
            'balances': [item.to_record() for item in self.balances],
            'details': [dict(item) for item in self.details],
            'error': self.diagnostic_code,
        }


@dataclass(frozen=True)
class _QuotaCacheEntry:
    credential_revision: str
    expires_at_monotonic: float
    quota: ProviderAccountQuota


@dataclass
class _QuotaInflight:
    credential_revision: str
    completed: threading.Event = field(default_factory=threading.Event)


class ProviderQuotaService:
    def __init__(
        self,
        *,
        fetch: QuotaFetch | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._fetch = fetch or _fetch_json
        self._timeout_seconds = max(0.1, float(timeout_seconds))
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[str, str, str], _QuotaCacheEntry] = OrderedDict()
        self._inflight: dict[tuple[str, str, str], _QuotaInflight] = {}

    def account_quota(
        self,
        *,
        project_root: Path,
        agent: str,
        provider: str,
        force_refresh: bool = False,
    ) -> ProviderAccountQuota:
        provider_id = str(provider or '').strip().lower()
        agent_name = str(agent or '').strip()
        now = self._wall_clock()
        next_refresh = now + timedelta(seconds=self._ttl_seconds)
        home = PathLayout(project_root).agent_provider_state_dir(agent_name, provider_id) / 'home'
        credential_path = _credential_path(home, provider_id)
        credential_revision = _file_revision(credential_path)
        cache_key = (str(Path(project_root).resolve()), agent_name, provider_id)
        owner = False
        with self._lock:
            cached = self._cache.get(cache_key)
            if (
                not force_refresh
                and cached is not None
                and cached.credential_revision == credential_revision
                and cached.expires_at_monotonic > self._monotonic_clock()
            ):
                self._cache.move_to_end(cache_key)
                return cached.quota
            inflight = self._inflight.get(cache_key)
            if inflight is None:
                if len(self._inflight) >= self._max_entries:
                    return ProviderAccountQuota(
                        provider_id=provider_id,
                        display_name=_provider_label(provider_id),
                        status='error',
                        fetched_at=_iso(now),
                        next_refresh_at=_iso(next_refresh),
                        diagnostic_code='quota_fetch_busy',
                    )
                inflight = _QuotaInflight(credential_revision=credential_revision)
                self._inflight[cache_key] = inflight
                owner = True
        if not owner:
            if not inflight.completed.wait(timeout=self._timeout_seconds + 0.5):
                return ProviderAccountQuota(
                    provider_id=provider_id,
                    display_name=_provider_label(provider_id),
                    status='error',
                    fetched_at=_iso(now),
                    next_refresh_at=_iso(next_refresh),
                    diagnostic_code='upstream_timeout',
                )
            with self._lock:
                cached = self._cache.get(cache_key)
                if cached is not None and cached.credential_revision == credential_revision:
                    self._cache.move_to_end(cache_key)
                    return cached.quota
            return self.account_quota(
                project_root=project_root,
                agent=agent,
                provider=provider,
            )

        quota: ProviderAccountQuota | None = None
        try:
            quota = self._fetch_quota(
                provider=provider_id,
                credential_path=credential_path,
                now=now,
                next_refresh=next_refresh,
            )
            return quota
        finally:
            with self._lock:
                current = self._inflight.get(cache_key)
                if quota is not None and _file_revision(credential_path) == credential_revision:
                    self._cache[cache_key] = _QuotaCacheEntry(
                        credential_revision=credential_revision,
                        expires_at_monotonic=self._monotonic_clock() + self._ttl_seconds,
                        quota=quota,
                    )
                    self._cache.move_to_end(cache_key)
                    while len(self._cache) > self._max_entries:
                        self._cache.popitem(last=False)
                if current is inflight:
                    self._inflight.pop(cache_key, None)
                    inflight.completed.set()

    def _fetch_quota(
        self,
        *,
        provider: str,
        credential_path: Path | None,
        now: datetime,
        next_refresh: datetime,
    ) -> ProviderAccountQuota:
        base = {
            'provider_id': provider,
            'display_name': _provider_label(provider),
            'fetched_at': _iso(now),
            'next_refresh_at': _iso(next_refresh),
        }
        credentials = _read_json(credential_path)
        if provider == 'codex':
            tokens = _mapping(credentials.get('tokens'))
            access_token = _text(tokens.get('access_token'))
            account_id = _text(tokens.get('account_id'))
            if access_token is None:
                return ProviderAccountQuota(status='unavailable', **base)
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json',
                'User-Agent': 'CCB-Mobile/Provider-Quota',
            }
            if account_id is not None:
                headers['ChatGPT-Account-Id'] = account_id
            return self._call_codex(headers=headers, base=base)
        if provider == 'claude':
            oauth = _mapping(credentials.get('claudeAiOauth'))
            access_token = _text(oauth.get('accessToken'))
            if access_token is None:
                return ProviderAccountQuota(status='unavailable', **base)
            plan = _claude_plan(oauth)
            return self._call_claude(
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Accept': 'application/json',
                    'anthropic-beta': _CLAUDE_OAUTH_BETA,
                },
                plan=plan,
                base=base,
            )
        return ProviderAccountQuota(status='unavailable', **base)

    def _call_codex(
        self,
        *,
        headers: Mapping[str, str],
        base: Mapping[str, str],
    ) -> ProviderAccountQuota:
        status, body = self._safe_fetch(_CODEX_USAGE_URL, headers)
        if status in {401, 403}:
            return ProviderAccountQuota(
                status='unavailable', diagnostic_code='authentication_required', **base
            )
        if status != 200 or not isinstance(body, Mapping):
            return ProviderAccountQuota(
                status='error', diagnostic_code=_upstream_diagnostic(status, body), **base
            )
        rate_limit = _mapping(body.get('rate_limit'))
        code_review = _mapping(body.get('code_review_rate_limit'))
        windows = tuple(
            item
            for item in (
                _codex_window('session', 'Session', rate_limit.get('primary_window')),
                _codex_window('weekly', 'Weekly', rate_limit.get('secondary_window')),
                _codex_window('code_review', 'Code review', code_review.get('primary_window')),
            )
            if item is not None
        )
        credits = _mapping(body.get('credits'))
        balance = _number(credits.get('balance'))
        balances = (
            (ProviderQuotaBalance('credits', 'Credits', balance, 'usd'),)
            if balance is not None
            else ()
        )
        return ProviderAccountQuota(
            status='available',
            plan_label=_text(body.get('plan_type')),
            windows=windows,
            balances=balances,
            **base,
        )

    def _call_claude(
        self,
        *,
        headers: Mapping[str, str],
        plan: str | None,
        base: Mapping[str, str],
    ) -> ProviderAccountQuota:
        status, body = self._safe_fetch(_CLAUDE_USAGE_URL, headers)
        if status in {401, 403}:
            return ProviderAccountQuota(
                status='unavailable', diagnostic_code='authentication_required', **base
            )
        if status != 200 or not isinstance(body, Mapping):
            return ProviderAccountQuota(
                status='error', diagnostic_code=_upstream_diagnostic(status, body), **base
            )
        windows = [
            item
            for item in (
                _claude_window('five_hour', 'Session', body.get('five_hour')),
                _claude_window('weekly', 'Weekly', body.get('seven_day')),
                _claude_window('weekly_model_opus', 'Weekly · Opus', body.get('seven_day_opus')),
                _claude_window(
                    'weekly_model_omelette',
                    'Weekly · Omelette',
                    body.get('seven_day_omelette'),
                ),
            )
            if item is not None
        ]
        windows.extend(_claude_scoped_windows(body.get('limits'), existing={item.id for item in windows}))
        details = ()
        extra = _mapping(body.get('extra_usage'))
        if isinstance(extra.get('is_enabled'), bool):
            details = (
                {
                    'id': 'extra_usage',
                    'label': 'Extra usage',
                    'value': 'Enabled' if extra['is_enabled'] else 'Disabled',
                },
            )
        return ProviderAccountQuota(
            status='available',
            plan_label=plan,
            windows=tuple(windows),
            details=details,
            **base,
        )

    def _safe_fetch(self, url: str, headers: Mapping[str, str]) -> tuple[int, object]:
        try:
            return self._fetch(url, headers, self._timeout_seconds)
        except TimeoutError:
            return 599, {'diagnostic_code': 'upstream_timeout'}
        except Exception:
            return 598, {'diagnostic_code': 'upstream_error'}


def _fetch_json(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, object]:
    request = Request(url, headers=dict(headers), method='GET')
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        return int(exc.code), {}
    except (TimeoutError, URLError) as exc:
        if isinstance(exc, URLError) and not isinstance(exc.reason, TimeoutError):
            return 598, {}
        return 599, {}
    with response:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            return 413, {}
        try:
            return int(response.status), json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return int(response.status), {}


def _credential_path(home: Path, provider: str) -> Path | None:
    if provider == 'codex':
        return home / 'auth.json'
    if provider == 'claude':
        return home / '.claude' / '.credentials.json'
    return None


def _file_revision(path: Path | None) -> str:
    if path is None:
        return 'none'
    try:
        stat = path.stat()
    except OSError:
        return 'missing'
    return f'{stat.st_mtime_ns}:{stat.st_size}'


def _read_json(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _codex_window(identifier: str, label: str, value: object) -> ProviderQuotaWindow | None:
    row = _mapping(value)
    if not row:
        return None
    return ProviderQuotaWindow(
        identifier,
        label,
        _number(row.get('used_percent')),
        _epoch_seconds_iso(row.get('reset_at')),
    )


def _claude_window(identifier: str, label: str, value: object) -> ProviderQuotaWindow | None:
    row = _mapping(value)
    if not row:
        return None
    return ProviderQuotaWindow(
        identifier,
        label,
        _number(row.get('utilization')),
        _text(row.get('resets_at')),
    )


def _claude_scoped_windows(value: object, *, existing: set[str]) -> list[ProviderQuotaWindow]:
    if not isinstance(value, list):
        return []
    windows: list[ProviderQuotaWindow] = []
    for item in value:
        row = _mapping(item)
        if _text(row.get('kind')) != 'weekly_scoped':
            continue
        scope = _mapping(row.get('scope'))
        dimension = None
        scope_row: dict[str, object] = {}
        for candidate in ('model', 'surface'):
            parsed = _mapping(scope.get(candidate))
            if _text(parsed.get('display_name')) or _text(parsed.get('id')):
                dimension = candidate
                scope_row = parsed
                break
        if dimension is None:
            continue
        raw_id = _text(scope_row.get('id'))
        name = _text(scope_row.get('display_name')) or raw_id
        if name is None:
            continue
        suffix = raw_id or _normalized_name(name)
        identifier = f'weekly_{dimension}_{suffix}'
        counter = 2
        while identifier in existing:
            identifier = f'weekly_{dimension}_{suffix}_{counter}'
            counter += 1
        existing.add(identifier)
        windows.append(
            ProviderQuotaWindow(
                identifier,
                f'Weekly · {name}',
                _number(row.get('percent')),
                _text(row.get('resets_at')),
            )
        )
    return windows


def _claude_plan(oauth: Mapping[str, object]) -> str | None:
    subscription = _text(oauth.get('subscriptionType'))
    if subscription is None:
        return None
    tier = _text(oauth.get('rateLimitTier'))
    suffix = tier.split('_')[-1] if tier else None
    label = subscription[:1].upper() + subscription[1:]
    return f'{label} {suffix}' if suffix else label


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    text = str(value or '').strip()
    return text or None


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float('inf') else None


def _percentage(value: float | None) -> float | None:
    return min(100.0, max(0.0, value)) if value is not None else None


def _epoch_seconds_iso(value: object) -> str | None:
    seconds = _number(value)
    if seconds is None:
        return None
    try:
        return _iso(datetime.fromtimestamp(seconds, timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _tone(used_pct: float | None) -> str:
    if used_pct is None:
        return 'default'
    if used_pct > 90:
        return 'danger'
    if used_pct >= 70:
        return 'warning'
    return 'ok'


def _normalized_name(value: str) -> str:
    chars = [character.lower() if character.isalnum() else '_' for character in value]
    return '_'.join(filter(None, ''.join(chars).strip('_').split('_'))) or 'unknown'


def _provider_label(provider: str) -> str:
    return {'codex': 'Codex', 'claude': 'Claude'}.get(provider, provider.title())


def _upstream_diagnostic(status: int, body: object) -> str:
    code = _text(_mapping(body).get('diagnostic_code'))
    if code in {'upstream_timeout', 'upstream_error'}:
        return code
    return 'upstream_timeout' if status == 599 else 'upstream_error'


__all__ = ['ProviderAccountQuota', 'ProviderQuotaService']
