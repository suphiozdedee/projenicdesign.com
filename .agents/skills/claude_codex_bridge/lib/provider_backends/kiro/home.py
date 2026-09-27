"""Kiro provider isolated ``KIRO_HOME`` projection.

CCB launches each kiro agent under an isolated ``KIRO_HOME`` (see
``lib/provider_backends/native_cli_support/launcher.py``) so that multiple
agents on the same provider do not fight over ``~/.kiro/sessions/`` or share
the same conversation history.

Unlike claude/codex, kiro-cli **has a first-class knob for this**: the
``KIRO_HOME`` environment variable overrides the ``~/.kiro`` directory used
for global agents, prompts, skills, steering, settings, and sessions
(introduced in kiro-cli via the same-named CLI change; see the kiro-cli
changelog). Because it only redirects the ``.kiro`` tree — **not** the whole
``$HOME`` — kiro-cli still finds:

* its bun runtime and ``tui.js`` under
  ``~/Library/Application Support/kiro-cli`` (macOS locates that path via
  ``$HOME``);
* the login token under ``~/Library/Keychains``;
* the user's ``~/.bashrc`` / ``~/.zshrc`` for its shell-integration checks.

So this projection only has to seed the isolated ``.kiro`` directory itself
— no HOME override, no Keychain symlink, no Application Support redirect.

Steps performed by :func:`materialize_kiro_home_config`:

1. create ``<KIRO_HOME>/{sessions,settings,agents}`` so kiro can start
   writing state without racing on ``mkdir``;
2. copy the user's ``~/.kiro/settings/*.json`` (CLI preferences) into
   the isolated ``settings/`` directory so per-user options survive;
3. **do not** copy ``~/.kiro/sessions/`` — session isolation is the
   whole point.

The function is idempotent and best-effort: missing sources are skipped,
unexpected errors are swallowed, so a bad seed does not abort the agent
launch (the user can still log in manually inside the pane if projection
fails).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from provider_core.source_home import current_provider_source_home


_KIRO_INHERITED_SETTINGS = ("cli.json", "survey_state.json")


def managed_kiro_home_for_runtime(runtime_dir: Path) -> Path:
    """Return the isolated ``KIRO_HOME`` directory kiro should run under.

    Mirrors ``managed_droid_home_for_runtime``. The CCB provider-state layout
    stores per-agent provider data under
    ``.ccb/agents/<agent>/provider-state/kiro/home``; the value that ends up
    exported as ``KIRO_HOME`` is exactly this directory (kiro-cli treats it
    like ``~/.kiro``).
    """

    runtime_dir = Path(runtime_dir).expanduser()
    if runtime_dir.parent.name == "provider-runtime":
        return runtime_dir.parent.parent / "provider-state" / "kiro" / "home"
    return runtime_dir / "kiro-home"


def materialize_kiro_home_config(
    target_home: Path,
    *,
    profile=None,
    source_home: Path | None = None,
) -> Path:
    """Populate the isolated ``KIRO_HOME`` directory from the user's ``~/.kiro``.

    ``target_home`` is the value CCB exports as ``KIRO_HOME`` — kiro-cli
    treats it as its ``~/.kiro`` directory, so we create ``sessions/``,
    ``settings/``, ``agents/`` directly inside it (no extra ``.kiro/`` layer).

    ``source_home`` defaults to the user's real ``$HOME``; the user's actual
    ``~/.kiro/settings/*.json`` (CLI preferences) is copied so per-user
    options survive per-agent isolation. Sessions are intentionally not
    copied: each agent must keep its own conversation history.
    """

    del profile  # reserved for future per-profile knobs (inherit_settings etc.)
    target_home = Path(target_home).expanduser()
    source_root = (
        Path(source_home).expanduser() if source_home is not None else _system_home_root()
    )

    target_home.mkdir(parents=True, exist_ok=True)
    (target_home / "sessions").mkdir(parents=True, exist_ok=True)
    (target_home / "settings").mkdir(parents=True, exist_ok=True)
    (target_home / "agents").mkdir(parents=True, exist_ok=True)

    source_kiro_dir = source_root / ".kiro"
    if target_home.resolve() == source_kiro_dir.resolve():
        # Running against the real ~/.kiro: nothing to project.
        return target_home

    _materialize_settings(source_kiro_dir, target_home)
    return target_home


def _system_home_root() -> Path:
    if os.environ.get("CCB_SOURCE_HOME"):
        return current_provider_source_home()
    return Path.home().expanduser()


def _materialize_settings(source_kiro_dir: Path, target_home: Path) -> None:
    source_settings = source_kiro_dir / "settings"
    if not source_settings.is_dir():
        return
    target_settings = target_home / "settings"
    for name in _KIRO_INHERITED_SETTINGS:
        src = source_settings / name
        if not src.is_file():
            continue
        dst = target_settings / name
        try:
            shutil.copy2(src, dst)
        except Exception:
            # Best-effort; kiro-cli can re-create defaults if seeding fails.
            pass


__all__ = [
    "managed_kiro_home_for_runtime",
    "materialize_kiro_home_config",
]
