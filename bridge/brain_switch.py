"""The API switch (la llave): which brain owns the room.

Javier's unification decision (2026-10-29): ONE system, with a
switch. API on — the bridge thinks with Spark/Wonder through the
Meta Model API and spends credit. API off — the bridge makes NO paid
API call at all, and the room works exactly as it did before the
bridge existed: the classic path (listener -> chat -> respond2)
owns it. After the hackathon the switch goes off and spending
returns to zero; flipping it back on is a config change, never a
rebuild.

The mode is read, in order:

1. The `MUSEZ_BRAIN` environment variable ("bridge" | "classic").
2. A small JSON file — the path from `MUSEZ_BRAIN_FILE`, else
   `~/.musez-brain.json` — shaped {"mode": "classic"}.
3. Default: "bridge" (the bridge thinks), so an unconfigured
   install behaves exactly as it did before the switch existed.

Values are case-insensitive. Anything that is not "bridge" means
OFF: the switch fails closed on spending, so a typo silences the
paid API instead of quietly billing it.

This module is the bridge-side gate. The weekend wiring (the Pi
listener reading the same variable/file to choose which path a mic
turn takes) consumes this exact contract; nothing about the room's
classic path changes.

Live entry points call `require_bridge()` before anything network
shaped happens; with the switch off it raises `ApiSwitchOff` with
SWITCH_OFF_MESSAGE, and the studio_turn runner records the refusal
and exits with SWITCH_OFF_EXIT_CODE — a clean, expected state,
distinct from a turn error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BRAIN_ENV = "MUSEZ_BRAIN"
BRAIN_FILE_ENV = "MUSEZ_BRAIN_FILE"
DEFAULT_BRAIN_FILE = Path("~/.musez-brain.json").expanduser()
BRIDGE_MODE = "bridge"
DEFAULT_MODE = BRIDGE_MODE

SWITCH_OFF_MESSAGE = "api switch off — classic path owns the room"
SWITCH_OFF_EXIT_CODE = 3


class ApiSwitchOff(RuntimeError):
    """The API switch is off: the live path must not run at all."""


def _normalize(value) -> str:
    return str(value).strip().lower()


def brain_mode(env=None) -> str:
    """The configured brain mode (normalized), defaulting to
    "bridge" when nothing anywhere configures the switch."""
    env = os.environ if env is None else env
    raw = env.get(BRAIN_ENV)
    if raw is not None and str(raw).strip():
        return _normalize(raw)
    path = Path(env.get(BRAIN_FILE_ENV) or DEFAULT_BRAIN_FILE).expanduser()
    try:
        data = json.loads(path.read_text())
        mode = data.get("mode")
        if mode is not None and str(mode).strip():
            return _normalize(mode)
    except Exception:
        pass  # no file / unreadable file = unconfigured = default
    return DEFAULT_MODE


def bridge_enabled(env=None) -> bool:
    """True only in "bridge" mode — anything else is off."""
    return brain_mode(env) == BRIDGE_MODE


def require_bridge(env=None) -> str:
    """Return the mode when the bridge may think; raise ApiSwitchOff
    (before any network call) when the switch is off."""
    mode = brain_mode(env)
    if mode != BRIDGE_MODE:
        raise ApiSwitchOff(SWITCH_OFF_MESSAGE)
    return mode
