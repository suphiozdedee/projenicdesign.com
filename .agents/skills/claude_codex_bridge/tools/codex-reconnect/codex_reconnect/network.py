from __future__ import annotations

import importlib
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

from . import __version__


DEFAULT_OPENAI_PROBE_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_PUBLIC_PROBE_URL = "https://www.google.com/generate_204"
_PROVIDER_BASE_URL_ENV_NAMES = ("OPENAI_BASE_URL", "OPENAI_API_BASE")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    url: str
    reachable: bool
    status: int | None
    elapsed_seconds: float
    error: str | None = None


class Readiness(str, Enum):
    READY = "ready"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    NETWORK_UNAVAILABLE = "network_unavailable"
    PRIMARY_UNAVAILABLE = "primary_unavailable"


def resolve_primary_probe_url(
    codex_home: str | Path | None = None,
    *,
    configured_url: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve the route used to prove that the active Codex provider is back.

    An explicit non-default URL remains authoritative. For CCB-managed sessions,
    the materialized Codex config is the provider authority; ambient API route
    variables are only used when that config has no usable route.
    """

    explicit = str(configured_url or "").strip()
    if explicit and explicit != DEFAULT_OPENAI_PROBE_URL:
        return explicit

    config_url = _configured_provider_base_url(codex_home)
    if config_url is not None:
        return config_url

    env = environment or {}
    for name in _PROVIDER_BASE_URL_ENV_NAMES:
        value = str(env.get(name) or "").strip()
        if _valid_probe_url(value):
            return value
    return explicit or DEFAULT_OPENAI_PROBE_URL


def _configured_provider_base_url(codex_home: str | Path | None) -> str | None:
    if not codex_home:
        return None
    config_path = Path(codex_home).expanduser() / "config.toml"
    try:
        reader = _toml_reader()
        if reader is None or not config_path.is_file():
            return None
        if getattr(reader, "__name__", "") == "toml":
            payload = reader.loads(config_path.read_text(encoding="utf-8"))
        else:
            with config_path.open("rb") as handle:
                payload = reader.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    provider_id = str(payload.get("model_provider") or "").strip()
    providers = payload.get("model_providers")
    if not provider_id or not isinstance(providers, dict):
        return None
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        return None
    value = str(provider.get("base_url") or "").strip()
    return value if _valid_probe_url(value) else None


def _toml_reader() -> object | None:
    for module_name in ("tomllib", "tomli", "toml"):
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
    return None


def _valid_probe_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def probe_https(
    url: str,
    *,
    timeout: float = 5.0,
    open_url: Callable[..., object] = urllib.request.urlopen,
) -> ProbeResult:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("probe URL must be an absolute https URL")
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": f"codex-reconnect/{__version__}"},
    )
    started = time.monotonic()
    try:
        response = open_url(
            request, timeout=timeout, context=ssl.create_default_context()
        )
        status = getattr(response, "status", None)
        close = getattr(response, "close", None)
        if callable(close):
            close()
        return ProbeResult(
            url,
            True,
            status if isinstance(status, int) else None,
            time.monotonic() - started,
        )
    except urllib.error.HTTPError as exc:
        # Any HTTP response proves the DNS/TCP/TLS/HTTP path is reachable.
        return ProbeResult(url, True, exc.code, time.monotonic() - started)
    except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        return ProbeResult(url, False, None, time.monotonic() - started, str(exc))


def classify_readiness(primary: ProbeResult, public: ProbeResult | None) -> Readiness:
    if primary.reachable:
        return Readiness.READY
    if public is None:
        return Readiness.PRIMARY_UNAVAILABLE
    if public.reachable:
        return Readiness.UPSTREAM_UNAVAILABLE
    return Readiness.NETWORK_UNAVAILABLE
