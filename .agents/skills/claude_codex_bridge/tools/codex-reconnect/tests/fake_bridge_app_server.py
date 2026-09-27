from __future__ import annotations

import json
import sys
from typing import Any


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


MODE = sys.argv[1] if len(sys.argv) > 1 else "default"
OVERLOAD_ERROR = {"codexErrorInfo": "serverOverloaded"}
FAILED_OVERLOAD_TURN = {
    "id": "turn-overload",
    "status": "failed",
    "startedAt": 1,
    "error": OVERLOAD_ERROR,
}
thread_reads = 0
recovery_starts: list[dict[str, Any]] = []
interrupts: list[dict[str, Any]] = []


def input_text(params: dict[str, Any]) -> str:
    turn_input = params.get("input")
    if not isinstance(turn_input, list):
        return ""
    return " ".join(
        item.get("text", "")
        for item in turn_input
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    )


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if request_id is None:
        continue
    if method == "initialize":
        emit({"id": request_id, "result": {"userAgent": "fake-bridge/1"}})
    elif method == "skills/extraRoots/set":
        emit({"id": request_id, "result": {}})
    elif method == "thread/start":
        emit(
            {
                "id": request_id,
                "result": {
                    "thread": {"id": "thread-1", "turns": []},
                    "model": params.get("model") or "gpt-test",
                },
            }
        )
    elif MODE == "server-overloaded" and method == "thread/read":
        thread_reads += 1
        emit(
            {
                "id": request_id,
                "result": {
                    "thread": {"id": "thread-1", "turns": [FAILED_OVERLOAD_TURN]}
                },
            }
        )
    elif MODE == "server-overloaded" and method == "test/state":
        emit(
            {
                "id": request_id,
                "result": {
                    "threadReads": thread_reads,
                    "recoveryStarts": recovery_starts,
                    "interrupts": interrupts,
                },
            }
        )
    elif MODE == "server-overloaded" and method == "test/completeOverload":
        emit({"id": request_id, "result": {}})
        emit(
            {
                "method": "error",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-overload",
                    "error": OVERLOAD_ERROR,
                    "willRetry": False,
                },
            }
        )
        emit(
            {
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": FAILED_OVERLOAD_TURN},
            }
        )
    elif MODE == "server-overloaded" and method == "test/rerouteRecovery":
        emit({"id": request_id, "result": {}})
        emit(
            {
                "method": "model/rerouted",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-recovery",
                    "fromModel": "gpt-overload-test",
                    "toModel": "gpt-fallback-test",
                },
            }
        )
    elif MODE == "server-overloaded" and method == "test/failRecovery":
        emit({"id": request_id, "result": {}})
        emit(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-recovery",
                        "status": "failed",
                        "startedAt": 2,
                        "error": OVERLOAD_ERROR,
                    },
                },
            }
        )
    elif MODE == "server-overloaded" and method == "turn/interrupt":
        interrupts.append(params)
        emit({"id": request_id, "result": {}})
        emit(
            {
                "method": "test/recoveryInterrupted",
                "params": {"count": len(interrupts), **params},
            }
        )
    elif method == "turn/start":
        if (
            MODE == "server-overloaded"
            and isinstance(request_id, str)
            and request_id.startswith("reconnect:")
        ):
            recovery_starts.append(params)
            emit(
                {
                    "id": request_id,
                    "result": {
                        "turn": {
                            "id": "turn-recovery",
                            "status": "inProgress",
                            "items": [],
                        }
                    },
                }
            )
            emit(
                {
                    "method": "test/recoveryTurnStarted",
                    "params": {
                        "count": len(recovery_starts),
                        "request": params,
                    },
                }
            )
        elif MODE == "server-overloaded" and input_text(params) == "trigger overload":
            emit(
                {
                    "id": request_id,
                    "result": {
                        "turn": {
                            "id": "turn-overload",
                            "status": "inProgress",
                            "items": [],
                        }
                    },
                }
            )
            emit(
                {
                    "method": "error",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-overload",
                        "error": OVERLOAD_ERROR,
                        "willRetry": True,
                    },
                }
            )
        else:
            emit(
                {
                    "id": request_id,
                    "result": {
                        "turn": {
                            "id": "turn-control",
                            "status": "inProgress",
                            "items": [],
                        }
                    },
                }
            )
    else:
        emit({"id": request_id, "result": {}})
