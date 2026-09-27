#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dynamic_layout_smoke  # noqa: E402


DEFAULT_FLOWS = (
    "same-window-continuous",
    "batch-release",
    "move-agent",
    "move-shared-source",
    "window-class-continuous",
    "arrange-window",
    "mixed-move-add",
    "batch-move-window-class",
    "resolve-preflight",
)
DEFAULT_TEST_ROOT = dynamic_layout_smoke.DEFAULT_TEST_ROOT
DEFAULT_CCB_TEST = dynamic_layout_smoke.DEFAULT_CCB_TEST
DEFAULT_PROJECT_PREFIX = "core-dynamic-layout"
DEFAULT_COMMAND_TIMEOUT_S = int(os.environ.get("CCB_GUARDED_CORE_DYNAMIC_LAYOUT_TIMEOUT_S", "240"))

REQUIRED_TOP_CHECKS = {
    "same_window_continuous_1_to_6_to_1",
    "batch_release_multi_window",
    "move_agent_to_new_window",
    "move_agent_shared_source",
    "window_class_continuous_1_to_8_to_1",
    "arrange_window_disturb_restore",
    "mixed_move_add_explicit_windows",
    "batch_move_window_class",
    "resolve_preflight_chain",
}

REQUIRED_FLOW_CHECKS = {
    "same_window_continuous_1_to_6_to_1": {
        "grew_to_six_order",
        "observed_grew_to_six_panes",
        "observed_grow_geometry",
        "observed_grow_indexes_contiguous",
        "observed_grow_min_width",
        "observed_grow_fixed_columns",
        "release_remove_agent_plans",
        "release_reflowed_main",
        "main_pane_preserved",
        "shrunk_to_one_order",
        "observed_shrunk_to_one_pane",
        "observed_shrink_geometry",
        "dynamic_agents_cleaned",
        "ask_main_accepted",
    },
    "move_agent_to_new_window": {
        "add_agent_plan",
        "pre_move_ask_accepted",
        "pre_move_ask_terminal",
        "move_plan_class",
        "move_preserved_helper_pane",
        "move_window_evidence",
        "post_move_ask_accepted",
        "post_move_ask_terminal",
        "return_preserved_helper_pane",
        "return_removed_review_window",
        "return_ask_accepted",
        "return_ask_terminal",
        "release_kept_main_window",
        "dynamic_agents_cleaned",
    },
    "move_agent_shared_source": {
        "first_add_window_plan",
        "second_add_agent_plan",
        "move_plan_class",
        "move_source_window_retained",
        "move_preserved_moved_pane",
        "move_preserved_stay_pane",
        "post_move_moved_ask_accepted",
        "post_move_stay_ask_accepted",
        "return_move_plan_class",
        "return_preserved_moved_pane",
        "return_preserved_stay_pane",
        "final_release_removed_review_window",
        "dynamic_agents_cleaned",
    },
    "batch_release_multi_window": {
        "batch_remove_agent_plan",
        "batch_removed_agents",
        "batch_removed_agent_panes_match",
        "batch_removed_windows",
        "survivor_panes_preserved",
        "after_windows",
        "survivor_ask_accepted",
        "main_ask_accepted",
    },
    "window_class_continuous_1_to_8_to_1": {
        "add_plan_sequence",
        "page1_order",
        "page2_order",
        "page1_observed_fixed_columns",
        "page2_observed_fixed_columns",
        "helper7_ask_accepted",
        "helper7_ask_terminal",
        "release_remove_agent_plans",
        "page2_removed_when_empty",
        "after_page2_removed",
        "dynamic_agents_cleaned",
    },
    "arrange_window_disturb_restore": {
        "disturb_made_non_fixed",
        "arrange_status_ok",
        "arrange_reflowed_plan",
        "arrange_fixed_columns",
        "pane_ids_preserved",
        "helper_ask_accepted",
        "helper_ask_terminal",
        "dynamic_agents_cleaned",
    },
    "mixed_move_add_explicit_windows": {
        "reload_published",
        "reload_move_plan",
        "reload_namespace_planned_mixed_steps",
        "moved_panes_preserved",
        "new_beta_pane_created",
        "review_window_removed",
        "asks_terminal",
    },
    "batch_move_window_class": {
        "move_plan_class",
        "move_target_windows",
        "moved_agent_panes_match",
        "removed_review_window",
        "zeta_ask_accepted",
        "alpha_ask_accepted",
    },
    "resolve_preflight_chain": {
        "class_resolve_overflow",
        "class_add_matches_resolve",
        "class_add_window_plan",
        "class_release_removed_window",
        "class_after_clean",
        "node_resolve_execution_window",
        "capacity_add_window_plan",
        "node_window_visible",
        "capacity_release_clean",
    },
}


def validate_core_dynamic_layout_payload(payload: dict[str, Any], *, expected_flows: tuple[str, ...] = DEFAULT_FLOWS) -> None:
    if payload.get("dynamic_layout_smoke_status") != "ok":
        raise AssertionError(f"dynamic layout smoke status is not ok: {payload.get('dynamic_layout_smoke_status')!r}")
    if list(payload.get("flows") or ()) != list(expected_flows):
        raise AssertionError(f"unexpected flows: {payload.get('flows')!r}")

    top_checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    missing_top = sorted(name for name in REQUIRED_TOP_CHECKS if name not in top_checks)
    failed_top = sorted(name for name in REQUIRED_TOP_CHECKS if top_checks.get(name) is not True)
    if missing_top or failed_top:
        raise AssertionError(f"top-level dynamic layout checks failed; missing={missing_top}, failed={failed_top}")

    results = {str(item.get("flow") or ""): item for item in payload.get("results", []) if isinstance(item, dict)}
    missing_flows = sorted(name for name in REQUIRED_FLOW_CHECKS if name not in results)
    if missing_flows:
        raise AssertionError(f"missing flow results: {missing_flows}")
    for flow, required_checks in REQUIRED_FLOW_CHECKS.items():
        item = results[flow]
        if item.get("flow_status") != "ok":
            raise AssertionError(f"{flow} status is not ok: {item.get('flow_status')!r}")
        checks = item.get("checks") if isinstance(item.get("checks"), dict) else {}
        missing = sorted(name for name in required_checks if name not in checks)
        failed = sorted(name for name in required_checks if checks.get(name) is not True)
        if missing or failed:
            raise AssertionError(f"{flow} checks failed; missing={missing}, failed={failed}")


def run_guarded_core_dynamic_layout_smoke(
    *,
    test_root: Path,
    project_prefix: str,
    ccb_test: Path,
    provider: str = "fake",
    flows: tuple[str, ...] = DEFAULT_FLOWS,
    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
    reset: bool = False,
    full_output: bool = False,
) -> dict[str, Any]:
    if provider != "fake" and os.environ.get(dynamic_layout_smoke.REAL_RUN_ENV) != "1":
        raise RuntimeError(f"non-fake core dynamic layout smoke requires {dynamic_layout_smoke.REAL_RUN_ENV}=1")
    payload = dynamic_layout_smoke.run_dynamic_layout_smoke(
        test_root=test_root,
        project_prefix=project_prefix,
        ccb_test=ccb_test,
        provider=provider,
        flows=flows,
        provider_home_mode="source-home" if provider == "fake" else "real-home",
        command_timeout_s=command_timeout_s,
        prepare_only=False,
        reset=reset,
        keep_running=False,
    )
    validate_core_dynamic_layout_payload(payload, expected_flows=flows)
    return payload if full_output else dynamic_layout_smoke.compact_smoke_payload(payload)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guarded fake-provider smoke for core dynamic [windows] layout operations.")
    parser.add_argument("--test-root", type=Path, default=DEFAULT_TEST_ROOT)
    parser.add_argument("--project-prefix", default=DEFAULT_PROJECT_PREFIX)
    parser.add_argument("--ccb-test", type=Path, default=DEFAULT_CCB_TEST)
    parser.add_argument("--provider", default="fake")
    parser.add_argument(
        "--flow",
        action="append",
        choices=dynamic_layout_smoke.FLOW_NAMES,
        help="Flow to run. Defaults to the guarded core dynamic layout bundle.",
    )
    parser.add_argument("--command-timeout", type=int, default=DEFAULT_COMMAND_TIMEOUT_S)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--full-output", action="store_true")
    parser.add_argument("--json", action="store_true", help="Accepted for consistency; output is always JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    try:
        payload = run_guarded_core_dynamic_layout_smoke(
            test_root=args.test_root,
            project_prefix=args.project_prefix,
            ccb_test=args.ccb_test,
            provider=str(args.provider),
            flows=tuple(args.flow or DEFAULT_FLOWS),
            command_timeout_s=int(args.command_timeout),
            reset=bool(args.reset),
            full_output=bool(args.full_output),
        )
    except AssertionError as exc:
        print(f"guard_status: failed\nerror: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
