"""``ccb-herdr-bridge.json`` v1 schema — CCB × Herdr integration config.

Defines the bridge configuration that documents the relationship between a CCB
project and its Herdr session/pane bindings.  This config is informational and
diagnostic; it does NOT replace ``.ccb/ccb.config`` or Herdr workspace state.
"""

from __future__ import annotations

from typing import Literal, TypedDict

SCHEMA_VERSION = 1
BRIDGE_CONFIG_FILENAME = "ccb-herdr-bridge.json"

# -- typed schema -----------------------------------------------------------

class HerdrPaneBinding(TypedDict):
    """A single CCB pane bound to a Herdr pane."""
    ccb_agent_label: str
    ccb_provider: str
    ccb_role: str
    ccb_slot: str
    herdr_pane_id: str
    herdr_session_name: str
    herdr_workspace_id: str
    ccb_pane_ref: str  # "herdr:<pane_id>" format for runtime_ref


class HerdrBridgeConfigV1(TypedDict):
    """v1 schema for ``ccb-herdr-bridge.json``."""
    schema_version: Literal[1]
    project_id: str
    herdr_session: str
    herdr_version: str
    config_revision: str
    pane_bindings: list[HerdrPaneBinding]
    managed_mode: Literal["managed", "attached", "import"]
    lifecycle_owner: Literal["ccb"]
    recovery_owner: Literal["ccb"]


# -- validation -------------------------------------------------------------

_REQUIRED_KEYS = frozenset(
    {
        "schema_version", "project_id", "herdr_session", "herdr_version",
        "config_revision", "pane_bindings", "managed_mode",
        "lifecycle_owner", "recovery_owner",
    }
)
_ALLOWED_MODES = frozenset({"managed", "attached", "import"})


def validate_bridge_config(raw: object) -> HerdrBridgeConfigV1:
    """Validate and return a typed bridge config dict.

    Raises ``ValueError`` for schema violations.
    """
    if not isinstance(raw, dict):
        raise ValueError("bridge config must be a JSON object")

    missing = _REQUIRED_KEYS - set(raw.keys())
    if missing:
        raise ValueError(f"bridge config missing required keys: {sorted(missing)}")

    version = raw.get("schema_version")
    if version != 1:
        raise ValueError(f"bridge config schema_version must be 1, got {version!r}")

    mode = str(raw.get("managed_mode") or "")
    if mode not in _ALLOWED_MODES:
        raise ValueError(
            f"bridge config managed_mode must be one of {sorted(_ALLOWED_MODES)}, got {mode!r}"
        )

    lifecycle = str(raw.get("lifecycle_owner") or "")
    if lifecycle != "ccb":
        raise ValueError(f"bridge config lifecycle_owner must be 'ccb', got {lifecycle!r}")

    recovery = str(raw.get("recovery_owner") or "")
    if recovery != "ccb":
        raise ValueError(f"bridge config recovery_owner must be 'ccb', got {recovery!r}")

    project_id = str(raw.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("bridge config project_id is required")

    pane_bindings = raw.get("pane_bindings")
    if not isinstance(pane_bindings, list):
        raise ValueError("bridge config pane_bindings must be an array")

    validated_bindings: list[HerdrPaneBinding] = []
    for i, binding in enumerate(pane_bindings):
        if not isinstance(binding, dict):
            raise ValueError(f"bridge config pane_bindings[{i}] must be an object")
        validated_bindings.append(_validate_pane_binding(binding, i))

    return HerdrBridgeConfigV1(
        schema_version=1,
        project_id=project_id,
        herdr_session=str(raw.get("herdr_session") or ""),
        herdr_version=str(raw.get("herdr_version") or ""),
        config_revision=str(raw.get("config_revision") or ""),
        pane_bindings=validated_bindings,
        managed_mode=mode,  # type: ignore[arg-type]
        lifecycle_owner="ccb",
        recovery_owner="ccb",
    )


def _validate_pane_binding(raw: dict, index: int) -> HerdrPaneBinding:
    required = frozenset(
        {
            "ccb_agent_label", "ccb_provider", "ccb_role", "ccb_slot",
            "herdr_pane_id", "herdr_session_name", "herdr_workspace_id",
            "ccb_pane_ref",
        }
    )
    missing = required - set(raw.keys())
    if missing:
        raise ValueError(f"bridge config pane_bindings[{index}] missing keys: {sorted(missing)}")

    return HerdrPaneBinding(
        ccb_agent_label=str(raw["ccb_agent_label"]),
        ccb_provider=str(raw["ccb_provider"]),
        ccb_role=str(raw["ccb_role"]),
        ccb_slot=str(raw["ccb_slot"]),
        herdr_pane_id=str(raw["herdr_pane_id"]),
        herdr_session_name=str(raw["herdr_session_name"]),
        herdr_workspace_id=str(raw["herdr_workspace_id"]),
        ccb_pane_ref=str(raw["ccb_pane_ref"]),
    )


def bridge_config_is_sensitive_free(config: HerdrBridgeConfigV1) -> bool:
    """Verify no sensitive fields leaked into the bridge config."""
    for binding in config.get("pane_bindings", []):
        for key in binding:
            key_lower = key.lower()
            if any(
                sensitive in key_lower
                for sensitive in ("api_key", "token", "auth", "secret", "credential", "password")
            ):
                return False
    return True


__all__ = [
    "BRIDGE_CONFIG_FILENAME",
    "SCHEMA_VERSION",
    "HerdrBridgeConfigV1",
    "HerdrPaneBinding",
    "validate_bridge_config",
    "bridge_config_is_sensitive_free",
]
