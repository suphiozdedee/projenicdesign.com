from __future__ import annotations

from typing import TextIO


def write_ask_usage(
    out: TextIO,
    *,
    command_name: str,
    error: str | None = None,
    alias_note: str | None = None,
) -> None:
    if error:
        print(f"error: {error}", file=out)
        print("", file=out)
    print("Usage:", file=out)
    print(
        f"  {command_name} [--compact] [--silence] [--chain] [--artifact-request] [--inline-request] [--artifact-reply] <target> [--] <message...>",
        file=out,
    )
    print("      --compact request a distilled reply that preserves key information", file=out)
    print("      --silence request silent-on-success delivery; failures/blockers still surface", file=out)
    print("      --chain mark this ask as part of the current active task chain", file=out)
    print("      --artifact-request force the request body into a CCB text artifact", file=out)
    print("      --inline-request keep the request body inline and disable automatic artifact spill", file=out)
    print("      --artifact-reply force the final reply into a CCB text artifact", file=out)
    print("      --artifact-io enable both --artifact-request and --artifact-reply", file=out)
    print("      nested asks from active tasks must use --chain or --silence", file=out)
    print("      sender is inferred from the current workspace agent and falls back to user", file=out)
    print("      message text may be supplied on stdin", file=out)
    print("      examples:", file=out)
    print(f"        {command_name} --compact agent1 review latest diff", file=out)
    print(f"        {command_name} --silence agent1 run smoke check", file=out)
    print(f"        {command_name} --chain agent2 collect evidence for this task", file=out)
    print(f"        {command_name} --chain --artifact-reply agent2 collect long evidence", file=out)
    print(f"  {command_name} get <job_id>    diagnostics-only: inspect one submitted job", file=out)
    print(f"  {command_name} cancel <job_id>", file=out)
    if alias_note:
        print("", file=out)
        print(alias_note, file=out)


__all__ = [
    "write_ask_usage",
]
