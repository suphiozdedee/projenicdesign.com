from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .managed import ManagedCodexSession, ManagedSessionError
from .network import (
    DEFAULT_OPENAI_PROBE_URL,
    DEFAULT_PUBLIC_PROBE_URL,
    classify_readiness,
    probe_https,
)
from .paths import default_state_dir
from .protocol import JsonlAppServer
from .tmux_watch import (
    TmuxWatchError,
    disable_current,
    enable_current,
    status_current,
    watcher_main,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-reconnect",
        description="Disconnect-only reconnection supervision for Codex CLI",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    toggle_help = {
        "on": "enable tmux-bound reconnect for the current Codex thread",
        "off": "disable tmux-bound reconnect for the current Codex thread",
        "status": "show tmux-bound reconnect status for the current Codex thread",
    }
    for toggle in ("on", "off", "status"):
        command = subparsers.add_parser(
            toggle,
            help=toggle_help[toggle],
        )
        command.add_argument("--state-dir", type=Path, default=default_state_dir())
        if toggle == "on":
            command.add_argument("--openai-probe-url", default=None)
            command.add_argument("--public-probe-url", default=DEFAULT_PUBLIC_PROBE_URL)
            command.add_argument("--no-public-probe", action="store_true")
            command.add_argument("--probe-timeout", type=float, default=5.0)

    probe = subparsers.add_parser("probe", help="classify HTTPS recovery readiness")
    probe.add_argument("--openai-url", default=DEFAULT_OPENAI_PROBE_URL)
    probe.add_argument("--public-url", default=DEFAULT_PUBLIC_PROBE_URL)
    probe.add_argument("--no-public-probe", action="store_true")
    probe.add_argument("--timeout", type=float, default=5.0)

    handshake = subparsers.add_parser(
        "handshake", help="initialize app-server without a thread"
    )
    handshake.add_argument("--codex", default=shutil.which("codex") or "codex")
    handshake.add_argument("--timeout", type=float, default=10.0)

    managed_open = subparsers.add_parser(
        "open",
        help="open a codex-reconnect-managed interactive Codex CLI",
        description=(
            "Open Codex through a local transparent App Server bridge. Pass Codex arguments "
            "after `--`, then use `$reconnect on` or `$reconnect off` inside that CLI."
        ),
    )
    managed_open.add_argument("--codex", default=shutil.which("codex") or "codex")
    managed_open.add_argument("--state-dir", type=Path, default=default_state_dir())
    managed_open.add_argument("--openai-probe-url", default=None)
    managed_open.add_argument("--public-probe-url", default=DEFAULT_PUBLIC_PROBE_URL)
    managed_open.add_argument("--no-public-probe", action="store_true")
    managed_open.add_argument("--probe-timeout", type=float, default=5.0)
    managed_open.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="arguments forwarded to Codex; prefix them with --",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["_watch"]:
        watcher_parser = argparse.ArgumentParser(add_help=False)
        watcher_parser.add_argument("--state-file", type=Path, required=True)
        watcher_parser.add_argument("--instance-id", required=True)
        watcher_parser.add_argument("--log-file", type=Path)
        watcher_parser.add_argument("--log-cursor", type=int, default=0)
        watcher_args = watcher_parser.parse_args(arguments[1:])
        return watcher_main(
            watcher_args.state_file,
            watcher_args.instance_id,
            log_path=watcher_args.log_file,
            log_cursor=watcher_args.log_cursor,
        )
    args = build_parser().parse_args(arguments)
    if args.command in {"on", "off", "status"}:
        try:
            if args.command == "on":
                state = enable_current(
                    state_dir=args.state_dir,
                    openai_probe_url=args.openai_probe_url,
                    public_probe_url=None
                    if args.no_public_probe
                    else args.public_probe_url,
                    probe_timeout=args.probe_timeout,
                )
            elif args.command == "off":
                state = disable_current(state_dir=args.state_dir)
            else:
                state = status_current(state_dir=args.state_dir)
        except (OSError, ValueError, TmuxWatchError) as exc:
            print(f"reconnect {args.command} failed: {exc}", file=sys.stderr)
            return 3
        payload: dict[str, object] = {
            "reconnect": "off" if state is None or not state.enabled else "on",
        }
        if state is not None:
            payload.update(
                {
                    "status": state.status,
                    "threadId": state.thread_id,
                    "paneId": state.pane_id,
                    "watcherPid": state.watcher_pid,
                }
            )
            if state.last_error is not None:
                payload["error"] = state.last_error
        print(json.dumps(payload, sort_keys=True))
        return 0
    if args.command == "probe":
        primary = probe_https(args.openai_url, timeout=args.timeout)
        public = None
        if not args.no_public_probe:
            public = probe_https(args.public_url, timeout=args.timeout)
        payload = {
            "classification": classify_readiness(primary, public).value,
            "primary": _probe_payload(primary),
            "public": _probe_payload(public) if public is not None else None,
        }
        print(json.dumps(payload, sort_keys=True))
        return 0 if primary.reachable else 2
    if args.command == "handshake":
        with JsonlAppServer([args.codex, "app-server", "--stdio"]) as transport:
            result = transport.initialize(
                client_name="codex-reconnect",
                client_version=__version__,
                timeout=args.timeout,
                experimental_api=True,
            )
        print(json.dumps({"status": "ok", "server": result}, sort_keys=True))
        return 0
    if args.command == "open":
        codex_args = list(args.codex_args)
        if codex_args[:1] == ["--"]:
            codex_args.pop(0)
        try:
            session = ManagedCodexSession(
                codex_command=args.codex,
                codex_args=codex_args,
                state_dir=args.state_dir,
                openai_probe_url=args.openai_probe_url,
                public_probe_url=None
                if args.no_public_probe
                else args.public_probe_url,
                probe_timeout=args.probe_timeout,
            )
            return session.run()
        except (ManagedSessionError, OSError, ValueError) as exc:
            print(f"codex-reconnect open failed: {exc}", file=sys.stderr)
            return 3
    raise AssertionError(f"unhandled command: {args.command}")


def _probe_payload(result: object) -> dict[str, object]:
    return {
        "url": getattr(result, "url"),
        "reachable": getattr(result, "reachable"),
        "status": getattr(result, "status"),
        "elapsedSeconds": round(getattr(result, "elapsed_seconds"), 3),
        "error": getattr(result, "error"),
    }
