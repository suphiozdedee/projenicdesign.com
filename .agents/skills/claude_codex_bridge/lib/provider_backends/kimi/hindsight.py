from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_API_URL = "http://127.0.0.1:18888"
DEFAULT_BANK_ID = "codex"
CONFIG_CANDIDATES = (
    Path(".hindsight") / "kimi.json",
    Path(".hindsight") / "codex.json",
)


@dataclass(frozen=True)
class HindsightRecall:
    context: str = ""
    diagnostics: dict[str, object] | None = None


@dataclass(frozen=True)
class HindsightRetain:
    retained: bool = False
    diagnostics: dict[str, object] | None = None


@dataclass(frozen=True)
class _HindsightConfig:
    enabled: bool
    api_url: str
    bank_id: str
    auto_recall: bool
    auto_retain: bool
    recall_budget: str
    recall_max_tokens: int
    recall_timeout: int
    retain_context: str
    recall_preamble: str
    show_hook_context: bool
    api_token: str = ""
    config_path: str = ""


def recall_hindsight_memories(
    prompt: str,
    *,
    session_id: str,
    agent_name: str,
    workspace_path: str,
) -> HindsightRecall:
    config = _load_config()
    if not config.enabled or not config.auto_recall:
        return HindsightRecall(diagnostics={"enabled": config.enabled, "auto_recall": config.auto_recall})
    query = str(prompt or "").strip()
    if len(query) < 5:
        return HindsightRecall(diagnostics={"enabled": True, "reason": "prompt_too_short"})
    try:
        response = _request(
            "POST",
            f"/v1/default/banks/{_quote(config.bank_id)}/memories/recall",
            {
                "query": query[:800],
                "thinking_budget": _thinking_budget(config.recall_budget),
                "max_tokens": config.recall_max_tokens,
            },
            config=config,
            timeout=config.recall_timeout,
        )
    except Exception as exc:
        return HindsightRecall(diagnostics={"enabled": True, "status": "failed", "reason": str(exc)[:240]})

    results = _response_results(response)
    if not results:
        return HindsightRecall(diagnostics={"enabled": True, "status": "empty", "bank_id": config.bank_id})

    context = _format_context(results, preamble=config.recall_preamble)
    if config.show_hook_context:
        context = _format_visible_hook_context(context)
    return HindsightRecall(
        context=context,
        diagnostics={
            "enabled": True,
            "status": "ok",
            "bank_id": config.bank_id,
            "result_count": len(results),
            "session_id": session_id,
            "agent_name": agent_name,
            "workspace_path": workspace_path,
            "show_hook_context": config.show_hook_context,
        },
    )


def retain_hindsight_turn(
    *,
    prompt: str,
    reply: str,
    session_id: str,
    job_id: str,
    agent_name: str,
    workspace_path: str,
) -> HindsightRetain:
    config = _load_config()
    if not config.enabled or not config.auto_retain:
        return HindsightRetain(diagnostics={"enabled": config.enabled, "auto_retain": config.auto_retain})
    content = _turn_transcript(prompt=prompt, reply=reply, agent_name=agent_name, workspace_path=workspace_path)
    if not content.strip():
        return HindsightRetain(diagnostics={"enabled": True, "reason": "empty_turn"})
    document_id = f"kimi:{session_id or agent_name}:{job_id}"
    try:
        _request(
            "POST",
            f"/v1/default/banks/{_quote(config.bank_id)}/memories",
            {
                "items": [
                    {
                        "content": content,
                        "context": config.retain_context,
                        "document_id": document_id,
                    }
                ]
            },
            config=config,
            timeout=15,
        )
    except Exception as exc:
        return HindsightRetain(diagnostics={"enabled": True, "status": "failed", "reason": str(exc)[:240]})
    return HindsightRetain(
        retained=True,
        diagnostics={
            "enabled": True,
            "status": "ok",
            "bank_id": config.bank_id,
            "document_id": document_id,
            "content_chars": len(content),
        },
    )


def _load_config() -> _HindsightConfig:
    payload, path = _load_config_payload()
    env_api_url = str(os.environ.get("HINDSIGHT_API_URL") or "").strip()
    env_bank_id = str(os.environ.get("HINDSIGHT_BANK_ID") or "").strip()
    api_url = (env_api_url or str(payload.get("hindsightApiUrl") or "") or DEFAULT_API_URL).rstrip("/")
    bank_id = env_bank_id or str(payload.get("bankId") or DEFAULT_BANK_ID).strip()
    enabled = bool(env_api_url or env_bank_id or path)
    return _HindsightConfig(
        enabled=enabled,
        api_url=api_url,
        bank_id=bank_id,
        auto_recall=bool(payload.get("autoRecall", True)),
        auto_retain=bool(payload.get("autoRetain", True)),
        recall_budget=str(payload.get("recallBudget") or "mid"),
        recall_max_tokens=_int(payload.get("recallMaxTokens"), 1024),
        recall_timeout=_int(payload.get("recallTimeout"), 10),
        retain_context=_retain_context(payload, path),
        recall_preamble=str(payload.get("recallPromptPreamble") or "Relevant memories from past conversations:"),
        show_hook_context=_bool_env("CCB_KIMI_HINDSIGHT_SHOW_HOOK_CONTEXT")
        or _bool(payload.get("showHookContext"), False),
        api_token=str(_api_token_from_env_or_payload(payload)),
        config_path=str(path or ""),
    )


def _load_config_payload() -> tuple[dict[str, object], Path | None]:
    home = Path.home().expanduser()
    for relative in CONFIG_CANDIDATES:
        path = home / relative
        try:
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload, path
    return {}, None


def _retain_context(payload: dict[str, object], path: Path | None) -> str:
    configured = str(payload.get("retainContext") or "").strip()
    if path is not None and path.name == "kimi.json" and configured:
        return configured
    return "kimi"


def _request(method: str, path: str, payload: object, *, config: _HindsightConfig, timeout: int) -> object:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    request = urllib.request.Request(
        f"{config.api_url}{path}",
        data=body,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            content_type = response.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"unreachable: {exc.reason}") from exc
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type:
        return json.loads(text)
    try:
        return json.loads(text)
    except Exception:
        return {"text": text}


def _response_results(response: object) -> list[object]:
    if not isinstance(response, dict):
        return []
    raw = response.get("results") or response.get("memories") or []
    return list(raw) if isinstance(raw, list) else []


def _format_context(results: list[object], *, preamble: str) -> str:
    lines = [
        "<hindsight_memories>",
        preamble,
        f"Current time - {time.strftime('%Y-%m-%d %H:%M', time.localtime())}",
        "",
    ]
    for item in results[:8]:
        text = _memory_text(item)
        if text:
            lines.append(f"- {text}")
    lines.append("</hindsight_memories>")
    return "\n".join(lines).strip()


def _format_visible_hook_context(context: str) -> str:
    indented = "\n".join(f"  {line}" if line else "" for line in context.splitlines())
    return "\n".join(
        [
            "UserPromptSubmit hook (completed)",
            f"hook context: {indented}",
        ]
    ).strip()


def _memory_text(item: object) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for key in ("content", "text", "memory", "summary"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = item.get("memory")
    if isinstance(value, dict):
        return _memory_text(value)
    return ""


def _turn_transcript(*, prompt: str, reply: str, agent_name: str, workspace_path: str) -> str:
    return "\n".join(
        [
            f"Provider: kimi",
            f"Agent: {agent_name}",
            f"Workspace: {workspace_path}",
            "",
            f"User: {prompt.strip()}",
            "",
            f"Assistant: {reply.strip()}",
        ]
    )


def _thinking_budget(value: str) -> int:
    return {"low": 20, "mid": 50, "high": 100}.get(str(value or "").strip().lower(), 50)


def _int(value: object, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _bool_env(name: str) -> bool:
    return _bool(os.environ.get(name), False)


def _api_token_from_env_or_payload(payload: dict[str, object]) -> object:
    return (
        os.environ.get("HINDSIGHT_API_TOKEN")
        or os.environ.get("HINDSIGHT_API_KEY")
        or payload.get("hindsightApiToken")
        or payload.get("hindsightApiKey")
        or ""
    )


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


__all__ = ["HindsightRecall", "HindsightRetain", "recall_hindsight_memories", "retain_hindsight_turn"]
