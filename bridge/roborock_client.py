"""Roborock clients for the `clean_studio` room tool.

The client contract is one method:

    start_room_clean(room_id) -> dict

returning a status dict ({"ok": True, "status": ...} on success).
Clients may also raise; StudioRoom.clean_studio converts every
failure into the room's {"ok": False, "error": ...} tool result —
a vacuum problem never kills a turn.

Two implementations ship:

- `FakeRoborockClient` — in-memory, for tests and mock runs. It
  records the room ids it was asked to clean and can be armed to
  return a failure result or to raise.

- `RealRoborockClient` — a clearly-marked STUB. The feasibility
  note (roborock-factibilidad-2026-10-29) chose the python-roborock
  route (one account login, then LAN commands), with the wiring
  landing in the Saturday integration box alongside the physical
  mapping run. Until then this client imports `roborock` LAZILY —
  the package is deliberately NOT a dependency of the bridge — and
  reports itself unavailable. No credentials live here, ever: the
  account login is Javier's, done in person, outside this repo.
"""

from __future__ import annotations

UNAVAILABLE_ERROR = "roborock client unavailable"


class RoborockClientUnavailable(RuntimeError):
    """No working Roborock client in this environment (yet)."""


class FakeRoborockClient:
    """Records clean requests; returns a canned status or fails on cue."""

    def __init__(self, result: dict = None, error: Exception = None) -> None:
        self.calls: list = []
        self._result = result
        self._error = error

    def start_room_clean(self, room_id) -> dict:
        self.calls.append(room_id)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        return {"ok": True, "status": "cleaning", "room": room_id}


class RealRoborockClient:
    """STUB for the real vacuum — see the module docstring.

    The lazy import happens per call, never at module or
    construction time, so the bridge runs fine on machines without
    the package (this dev VM included)."""

    def start_room_clean(self, room_id) -> dict:
        try:
            import roborock  # noqa: F401  (python-roborock)
        except ImportError as e:
            raise RoborockClientUnavailable(UNAVAILABLE_ERROR) from e
        # The package exists but the account session / device wiring
        # is Saturday-integration work, not something to improvise
        # from the bridge: report unavailable rather than guessing.
        raise RoborockClientUnavailable(
            f"{UNAVAILABLE_ERROR}: account login not wired yet")


def default_client() -> RealRoborockClient:
    return RealRoborockClient()
