from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .push import PushSendResult, PushSender

_FCM_SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
_FCM_ENDPOINT_TEMPLATE = 'https://fcm.googleapis.com/v1/projects/{project_id}/messages:send'
_COMPLETION_FIELDS = (
    'id',
    'kind',
    'project_id',
    'project_short_name',
    'agent',
    'completed_at',
    'dedupe_key',
    'host_id',
    'device_id',
)
_TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504}
_TRANSIENT_FCM_STATUSES = {'ABORTED', 'DEADLINE_EXCEEDED', 'INTERNAL', 'QUOTA_EXCEEDED', 'UNAVAILABLE'}
_INVALID_TOKEN_FCM_CODES = {'INVALID_ARGUMENT', 'SENDER_ID_MISMATCH', 'UNREGISTERED'}


class _AccessTokenProvider(Protocol):
    def access_token(self, *, timeout: float) -> str:
        ...


@dataclass(frozen=True)
class FcmSenderConfig:
    project_id: str
    max_retries: int = 2
    retry_backoff_seconds: float = 0.25


class FcmHttpV1PushSender:
    def __init__(
        self,
        config: FcmSenderConfig,
        *,
        access_token_provider: _AccessTokenProvider,
        opener: Callable[[Request], object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._access_token_provider = access_token_provider
        self._opener = opener or _urlopen
        self._sleep = sleep

    def __call__(self, token: str, payload: dict[str, object], timeout: float) -> PushSendResult:
        device_token = str(token or '').strip()
        if not device_token:
            return PushSendResult(invalid_token=True, error_code='invalid_token')
        data = _validated_data_payload(payload)
        if data is None:
            return PushSendResult(error_code='invalid_payload')
        timeout_seconds = max(0.1, float(timeout))
        try:
            access_token = self._access_token_provider.access_token(timeout=timeout_seconds)
        except Exception:
            return PushSendResult(retryable=True, error_code='auth_error')
        request_body = _fcm_request_body(device_token, data)
        attempts = max(0, int(self._config.max_retries)) + 1
        last_result = PushSendResult(retryable=True, error_code='transport_error')
        for attempt in range(attempts):
            try:
                request = Request(
                    _FCM_ENDPOINT_TEMPLATE.format(project_id=self._config.project_id),
                    data=json.dumps(request_body, separators=(',', ':')).encode('utf-8'),
                    headers={
                        'Authorization': f'Bearer {access_token}',
                        'Content-Type': 'application/json; charset=utf-8',
                    },
                    method='POST',
                )
                with self._opener(request, timeout=timeout_seconds) as response:  # type: ignore[call-arg]
                    status = int(getattr(response, 'status', 200) or 200)
                    if 200 <= status < 300:
                        return PushSendResult(sent=True)
                    last_result = _classify_fcm_failure(status, {})
            except HTTPError as exc:
                error_payload = _http_error_payload(exc)
                last_result = _classify_fcm_failure(exc.code, error_payload)
            except (OSError, TimeoutError, URLError):
                last_result = PushSendResult(retryable=True, error_code='transport_error')
            if not last_result.retryable or attempt == attempts - 1:
                return last_result
            self._sleep(_retry_delay(self._config.retry_backoff_seconds, attempt))
        return last_result


class _GoogleAuthAccessTokenProvider:
    def __init__(
        self,
        credentials: object,
        *,
        request_factory: Callable[[], object] | None = None,
    ) -> None:
        self._credentials = credentials
        self._lock = threading.Lock()
        self._request_factory = (
            request_factory if request_factory is not None else _google_auth_request
        )

    def access_token(self, *, timeout: float) -> str:
        with self._lock:
            token = str(getattr(self._credentials, 'token', '') or '').strip()
            if token and bool(getattr(self._credentials, 'valid', False)):
                return token
            request = self._request_factory()

            def bounded_request(*args: object, **kwargs: object) -> object:
                kwargs.setdefault('timeout', timeout)
                return request(*args, **kwargs)

            self._credentials.refresh(bounded_request)  # type: ignore[attr-defined]
            token = str(getattr(self._credentials, 'token', '') or '').strip()
            if not token:
                raise RuntimeError('google auth did not return an access token')
            return token


def _google_auth_request() -> object:
    from google.auth.transport.requests import Request as GoogleAuthRequest

    return GoogleAuthRequest()


def build_fcm_sender_from_env(
    environ: Mapping[str, str] | None = None,
) -> tuple[PushSender | None, dict[str, object]]:
    env = dict(os.environ if environ is None else environ)
    explicit_project_id = _env_text(env, 'CCB_MOBILE_FCM_PROJECT_ID')
    adc_project_id = _env_text(env, 'GOOGLE_CLOUD_PROJECT') or _env_text(env, 'GCLOUD_PROJECT')
    credentials_file = _env_text(env, 'CCB_MOBILE_FCM_CREDENTIALS_FILE')
    google_application_credentials = _env_text(env, 'GOOGLE_APPLICATION_CREDENTIALS')
    configured = bool(explicit_project_id or credentials_file or google_application_credentials or adc_project_id)
    if not configured:
        return None, {'configured': False, 'provider': 'fcm_http_v1'}

    credential_source = 'service_account_file' if credentials_file else 'adc'
    diagnostic: dict[str, object] = {
        'configured': True,
        'provider': 'fcm_http_v1',
        'ready': False,
        'credential_source': credential_source,
        'project_id_configured': bool(explicit_project_id or adc_project_id),
    }
    try:
        import google.auth
        from google.oauth2 import service_account
    except Exception:
        diagnostic['reason'] = 'google_auth_unavailable'
        return None, diagnostic

    credentials = None
    discovered_project_id = None
    try:
        if credentials_file:
            path = Path(credentials_file)
            if not path.is_file():
                diagnostic['reason'] = 'credential_file_unreadable'
                return None, diagnostic
            credentials = service_account.Credentials.from_service_account_file(
                str(path),
                scopes=[_FCM_SCOPE],
            )
            discovered_project_id = getattr(credentials, 'project_id', None)
        else:
            credentials, discovered_project_id = google.auth.default(scopes=[_FCM_SCOPE])
    except Exception:
        diagnostic['reason'] = 'credential_load_failed'
        return None, diagnostic

    project_id = explicit_project_id or adc_project_id or str(discovered_project_id or '').strip()
    if not project_id:
        diagnostic['reason'] = 'project_id_missing'
        return None, diagnostic
    config = FcmSenderConfig(
        project_id=project_id,
        max_retries=_env_int(env, 'CCB_MOBILE_FCM_MAX_RETRIES', default=2, minimum=0, maximum=5),
        retry_backoff_seconds=_env_float(
            env,
            'CCB_MOBILE_FCM_RETRY_BACKOFF_SECONDS',
            default=0.25,
            minimum=0.0,
            maximum=5.0,
        ),
    )
    diagnostic.update(
        {
            'ready': True,
            'project_id': project_id,
            'max_retries': config.max_retries,
        }
    )
    return FcmHttpV1PushSender(config, access_token_provider=_GoogleAuthAccessTokenProvider(credentials)), diagnostic


def fcm_sender_runtime_options(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    env = dict(os.environ if environ is None else environ)
    return {
        'timeout_seconds': _env_float(
            env,
            'CCB_MOBILE_FCM_TIMEOUT_SECONDS',
            default=2.0,
            minimum=0.1,
            maximum=30.0,
        ),
        'max_workers': _env_int(
            env,
            'CCB_MOBILE_FCM_MAX_WORKERS',
            default=4,
            minimum=1,
            maximum=32,
        ),
    }


def _urlopen(request: Request, *, timeout: float) -> object:
    return urlopen(request, timeout=timeout)


def _validated_data_payload(payload: Mapping[str, object]) -> dict[str, str] | None:
    if set(payload) != set(_COMPLETION_FIELDS):
        return None
    data = {key: str(payload[key]) for key in _COMPLETION_FIELDS}
    if any(not value for value in data.values()):
        return None
    if data['kind'] != 'task_completed':
        return None
    return data


def _fcm_request_body(token: str, data: dict[str, str]) -> dict[str, object]:
    return {
        'message': {
            'token': token,
            'notification': {
                'title': 'CCB Mobile',
                'body': f"{data['project_short_name']} / {data['agent']} 任务完成",
            },
            'data': data,
            'android': {
                'priority': 'HIGH',
                'notification': {
                    'channel_id': 'ccb_task_completion',
                },
            },
        },
    }


def _http_error_payload(error: HTTPError) -> dict[str, object]:
    try:
        data = error.read()
    except Exception:
        return {}
    try:
        payload = json.loads(data.decode('utf-8'))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _classify_fcm_failure(status_code: int, payload: Mapping[str, object]) -> PushSendResult:
    fcm_code = _fcm_error_code(payload)
    if fcm_code in _INVALID_TOKEN_FCM_CODES:
        return PushSendResult(invalid_token=True, error_code=fcm_code)
    if status_code in _TRANSIENT_HTTP_CODES or fcm_code in _TRANSIENT_FCM_STATUSES:
        return PushSendResult(retryable=True, error_code=fcm_code or f'http_{status_code}')
    return PushSendResult(error_code=fcm_code or f'http_{status_code}')


def _fcm_error_code(payload: Mapping[str, object]) -> str | None:
    error = payload.get('error')
    if not isinstance(error, Mapping):
        return None
    details = error.get('details')
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, Mapping):
                code = str(detail.get('errorCode') or '').strip()
                if code:
                    return code
    status = str(error.get('status') or '').strip()
    return status or None


def _retry_delay(base_seconds: float, attempt: int) -> float:
    return max(0.0, float(base_seconds)) * (2 ** max(0, attempt))


def _env_text(env: Mapping[str, str], key: str) -> str | None:
    value = str(env.get(key) or '').strip()
    return value or None


def _env_int(
    env: Mapping[str, str],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(str(env.get(key) or '').strip() or default)
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _env_float(
    env: Mapping[str, str],
    key: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(str(env.get(key) or '').strip() or default)
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


__all__ = [
    'FcmHttpV1PushSender',
    'FcmSenderConfig',
    'build_fcm_sender_from_env',
    'fcm_sender_runtime_options',
]
