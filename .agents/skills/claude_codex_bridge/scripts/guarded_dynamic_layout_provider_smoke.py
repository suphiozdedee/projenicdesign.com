#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[0]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dynamic_layout_smoke  # noqa: E402


DEFAULT_PROVIDERS = ("codex", "claude")
DEFAULT_FLOWS = ("window-class", "move-agent", "resolve-preflight")
DEFAULT_TEST_ROOT = dynamic_layout_smoke.DEFAULT_TEST_ROOT
DEFAULT_CCB_TEST = dynamic_layout_smoke.DEFAULT_CCB_TEST
DEFAULT_PROJECT_PREFIX = "dynamic-layout-provider-matrix"
DEFAULT_COMMAND_TIMEOUT_S = int(os.environ.get("CCB_GUARDED_DYNAMIC_LAYOUT_TIMEOUT_S", "300"))
DEFAULT_RESOLVE_PREFLIGHT_STATIC_PROVIDER = "fake"


def run_guarded_provider_matrix_smoke(
    *,
    test_root: Path,
    project_prefix: str,
    ccb_test: Path,
    providers: tuple[str, ...] = DEFAULT_PROVIDERS,
    flows: tuple[str, ...] = DEFAULT_FLOWS,
    provider_home_mode: str = "real-home",
    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
    resolve_preflight_static_provider: str | None = DEFAULT_RESOLVE_PREFLIGHT_STATIC_PROVIDER,
    run: bool = False,
    reset: bool = False,
    full_output: bool = False,
) -> dict[str, Any]:
    if run and os.environ.get(dynamic_layout_smoke.REAL_RUN_ENV) != "1":
        raise RuntimeError(f"real provider matrix smoke requires {dynamic_layout_smoke.REAL_RUN_ENV}=1")
    payload = dynamic_layout_smoke.run_dynamic_layout_provider_matrix(
        test_root=test_root,
        project_prefix=project_prefix,
        ccb_test=ccb_test,
        providers=providers,
        flows=flows,
        provider_home_mode=provider_home_mode,
        command_timeout_s=command_timeout_s,
        resolve_preflight_static_provider=resolve_preflight_static_provider,
        prepare_only=not run,
        reset=reset,
        keep_running=False,
    )
    return payload if full_output else dynamic_layout_smoke.compact_smoke_payload(payload)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Guarded standard smoke for dynamic [windows] provider matrix hot-load/unload/reflow."
    )
    parser.add_argument("--test-root", type=Path, default=DEFAULT_TEST_ROOT)
    parser.add_argument("--project-prefix", default=DEFAULT_PROJECT_PREFIX)
    parser.add_argument("--ccb-test", type=Path, default=DEFAULT_CCB_TEST)
    parser.add_argument("--provider", action="append", dest="providers", help="Provider to include. Defaults to codex and claude.")
    parser.add_argument(
        "--flow",
        action="append",
        choices=dynamic_layout_smoke.FLOW_NAMES,
        help="Flow to run. Defaults to the guarded provider matrix flows.",
    )
    parser.add_argument("--provider-home-mode", choices=("source-home", "real-home"), default="real-home")
    parser.add_argument(
        "--resolve-preflight-static-provider",
        default=DEFAULT_RESOLVE_PREFLIGHT_STATIC_PROVIDER,
        help="Provider used only for static resolve-preflight filler panes; defaults to fake for the guarded matrix.",
    )
    parser.add_argument("--command-timeout", type=int, default=DEFAULT_COMMAND_TIMEOUT_S)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--run", action="store_true", help=f"Actually run providers; also requires {dynamic_layout_smoke.REAL_RUN_ENV}=1.")
    parser.add_argument("--full-output", action="store_true")
    parser.add_argument("--json", action="store_true", help="Accepted for consistency; output is always JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    payload = run_guarded_provider_matrix_smoke(
        test_root=args.test_root,
        project_prefix=args.project_prefix,
        ccb_test=args.ccb_test,
        providers=tuple(args.providers or DEFAULT_PROVIDERS),
        flows=tuple(args.flow or DEFAULT_FLOWS),
        provider_home_mode=args.provider_home_mode,
        command_timeout_s=int(args.command_timeout),
        resolve_preflight_static_provider=args.resolve_preflight_static_provider,
        run=bool(args.run),
        reset=bool(args.reset),
        full_output=bool(args.full_output),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("dynamic_layout_smoke_status") in {"ok", "prepared"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
