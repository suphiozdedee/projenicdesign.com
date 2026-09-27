from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
from types import SimpleNamespace

from mobile_gateway.relay_host_credentials import (
    RELAY_MODE_OFFICIAL,
    RELAY_MODE_SELF_HOSTED,
    RelayHostCredentials,
    load_relay_host_credentials,
)

from .relay_host_activation import (
    relay_host_activate_command,
    relay_host_credential_path,
)

OFFICIAL_RELAY_CONTACT_EMAIL = "bfly123@126.com"
SELF_HOSTED_RELAY_DOC_URL = (
    "https://github.com/SeemSeam/claude_codex_bridge/blob/main/"
    "mobile/docs/relay/relay-deployment-modes.md"
)


@dataclass(frozen=True)
class MobileRouteSelection:
    route_provider: str
    relay_mode: str | None = None


@dataclass(frozen=True)
class GuidedRelayCredentialResult:
    ready: bool
    cancelled: bool = False


_ROUTE_CHOICES = {
    "1": MobileRouteSelection(route_provider="tailnet"),
    "2": MobileRouteSelection(route_provider="lan"),
    "3": MobileRouteSelection(
        route_provider="relay",
        relay_mode=RELAY_MODE_OFFICIAL,
    ),
    "4": MobileRouteSelection(
        route_provider="relay",
        relay_mode=RELAY_MODE_SELF_HOSTED,
    ),
}


def prompt_mobile_route_selection(
    *,
    read_fn: Callable[[str], str],
    print_fn: Callable[[str], None] = print,
    attempts: int = 3,
) -> MobileRouteSelection | None:
    print_fn("Choose how this computer connects to CCB Mobile:")
    print_fn("  1. Tailscale")
    print_fn("  2. Local network (LAN)")
    print_fn("  3. CCB official Relay")
    print_fn("  4. Self-hosted Relay")
    for _attempt in range(max(1, attempts)):
        try:
            answer = str(read_fn("Select [1-4] (Enter cancels): ") or "").strip()
        except (EOFError, KeyboardInterrupt):
            print_fn("")
            print_fn("Mobile setup cancelled; no gateway was changed.")
            return None
        if not answer:
            print_fn("Mobile setup cancelled; no gateway was changed.")
            return None
        selection = _ROUTE_CHOICES.get(answer)
        if selection is not None:
            return selection
        print_fn("Enter 1, 2, 3, or 4.")
    print_fn("Mobile setup stopped after three invalid selections.")
    return None


def ensure_guided_relay_credentials(
    *,
    relay_mode: str,
    read_fn: Callable[[str], str],
    print_fn: Callable[[str], None] = print,
    environ: Mapping[str, str] | None = None,
    load_credentials_fn: Callable[
        [Path], RelayHostCredentials
    ] = load_relay_host_credentials,
    activate_fn: Callable[
        [object, object], dict[str, object]
    ] = relay_host_activate_command,
) -> GuidedRelayCredentialResult:
    relay_mode = str(relay_mode or "").strip().lower().replace("-", "_")
    env = os.environ if environ is None else environ
    command = SimpleNamespace(credential_path=None)
    credential_path = relay_host_credential_path(command, environ=env)
    if credential_path.exists():
        try:
            credentials = load_credentials_fn(credential_path)
        except Exception as exc:
            print_fn(
                "Existing Relay credentials could not be loaded: "
                f"{type(exc).__name__}: {exc}"
            )
            print_fn(f"Credential file: {credential_path}")
            print_fn(
                "Back up or remove the invalid file only after confirming the "
                "old Relay host can be retired."
            )
            return GuidedRelayCredentialResult(ready=False)
        if credentials.relay_mode != relay_mode:
            print_fn(
                "Existing Relay credentials use "
                f"{_display_relay_mode(credentials.relay_mode)}; the selected "
                f"mode is {_display_relay_mode(relay_mode)}."
            )
            print_fn(f"Credential file: {credential_path}")
            print_fn(
                "Revoke or retire the old Relay host, then back up/remove this "
                "credential file before switching modes."
            )
            return GuidedRelayCredentialResult(ready=False)
        print_fn(
            "Reusing activated "
            f"{_display_relay_mode(relay_mode)} Relay host credentials "
            f"for host {credentials.host_id}."
        )
        return GuidedRelayCredentialResult(ready=True)

    if relay_mode == RELAY_MODE_OFFICIAL:
        relay_origin = None
        print_fn("CCB official Relay requires a one-time invitation on this computer.")
        print_fn(
            f"Request one by email at {OFFICIAL_RELAY_CONTACT_EMAIL}, "
            "or contact the CCB Relay administrator through WeChat."
        )
        invitation_path = _read_optional(
            read_fn,
            "One-time invitation/key file path (Enter to stop): ",
            print_fn=print_fn,
        )
        if not invitation_path:
            _print_official_relay_request_help(print_fn)
            return GuidedRelayCredentialResult(ready=False, cancelled=True)
    elif relay_mode == RELAY_MODE_SELF_HOSTED:
        print_fn("Self-hosted Relay requires a stable public wss:// endpoint.")
        print_fn(
            "Provide Android-trusted TLS on port 443, WebSocket proxying, "
            "owner-only admission state, bounded limits, and one-time invitations."
        )
        print_fn(f"Guide: {SELF_HOSTED_RELAY_DOC_URL}")
        relay_origin = _read_optional(
            read_fn,
            "Self-hosted Relay origin (wss://relay.example.com, Enter to stop): ",
            print_fn=print_fn,
        )
        if not relay_origin:
            _print_self_hosted_relay_help(print_fn)
            return GuidedRelayCredentialResult(ready=False, cancelled=True)
        invitation_path = _read_optional(
            read_fn,
            "One-time invitation/key file path (Enter to stop): ",
            print_fn=print_fn,
        )
        if not invitation_path:
            _print_self_hosted_relay_help(print_fn, relay_origin=relay_origin)
            return GuidedRelayCredentialResult(ready=False, cancelled=True)
    else:
        print_fn(f"Unsupported Relay mode: {relay_mode}")
        return GuidedRelayCredentialResult(ready=False)

    activation_command = SimpleNamespace(
        relay_mode=relay_mode,
        relay_origin=relay_origin,
        invitation=None,
        invitation_file=invitation_path,
        credential_path=str(credential_path),
    )
    try:
        summary = activate_fn(None, activation_command)
    except Exception as exc:
        print_fn(f"Relay activation failed: {type(exc).__name__}: {exc}")
        return GuidedRelayCredentialResult(ready=False)
    if not isinstance(summary, Mapping):
        print_fn("Relay activation failed: activation result is invalid")
        return GuidedRelayCredentialResult(ready=False)
    print_fn(
        f"Relay host activated: {summary.get('host_id') or 'unknown host'} "
        f"({_display_relay_mode(relay_mode)})."
    )
    print_fn(f"Credentials stored at: {credential_path}")
    return GuidedRelayCredentialResult(ready=True)


def _read_optional(
    read_fn: Callable[[str], str],
    prompt: str,
    *,
    print_fn: Callable[[str], None],
) -> str | None:
    try:
        value = str(read_fn(prompt) or "").strip()
    except (EOFError, KeyboardInterrupt):
        print_fn("")
        return None
    return value or None


def _display_relay_mode(value: object) -> str:
    return str(value or "").strip().replace("_", "-")


def _print_official_relay_request_help(
    print_fn: Callable[[str], None],
) -> None:
    print_fn("No invitation was consumed and no Relay gateway was started.")
    print_fn(f"Request a one-time key: {OFFICIAL_RELAY_CONTACT_EMAIL}")
    print_fn("Or contact the CCB Relay administrator through WeChat.")
    print_fn(
        "Then rerun `ccb update mobile`, choose option 3, and enter the key file path."
    )


def _print_self_hosted_relay_help(
    print_fn: Callable[[str], None],
    *,
    relay_origin: str = "wss://relay.example.com",
) -> None:
    print_fn("No Relay gateway was started.")
    print_fn(f"Self-hosting guide: {SELF_HOSTED_RELAY_DOC_URL}")
    print_fn("After your Relay operator issues a one-time invitation, run:")
    print_fn(
        "  ccb relay host activate --mode self-hosted "
        f"--relay-origin {relay_origin} "
        "--invitation-file /path/to/ccb-relay.key"
    )
    print_fn("  ccb update mobile --route-provider relay")


__all__ = [
    "GuidedRelayCredentialResult",
    "MobileRouteSelection",
    "OFFICIAL_RELAY_CONTACT_EMAIL",
    "SELF_HOSTED_RELAY_DOC_URL",
    "ensure_guided_relay_credentials",
    "prompt_mobile_route_selection",
]
