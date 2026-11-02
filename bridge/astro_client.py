"""Astro clients for the `call_astro` room tool.

The client contract is one method:

    send_to(destination, url) -> dict

returning a status dict ({"ok": True, "status": ...} on success).
Clients may also raise; StudioRoom.call_astro converts every
failure into the room's {"ok": False, "error": ...} tool result —
a robot problem never kills a turn.

The `url` is the destination's Voice Monkey Routine Trigger URL
(configuration — see docs/astro-setup.md). It carries a secret API
token, so it lives ONLY in an environment variable
(MUSEZ_ASTRO_URL_STUDIO / MUSEZ_ASTRO_URL_KITCHEN), is read by
StudioRoom at call time and handed to the client per call. It is
never hard-coded, printed, logged or committed — this module only
ever sees it as an argument, and no error message includes it.

Two implementations ship:

- `FakeAstroClient` — in-memory, for tests and mock runs. It
  records the destinations (and URLs) it was asked to dispatch and
  can be armed to return a failure result or to raise.

- `RealAstroClient` — an HTTPS GET on the trigger URL via urllib
  (no extra dependency, same transport shape as the room's other
  runners). Firing the URL presses the Voice Monkey virtual
  trigger, which runs the Alexa routine that sends Astro to the
  destination's viewpoint. Success is reported ONLY on HTTP 200;
  anything else raises. With no URL at all the client reports
  itself unavailable, mirroring the Roborock real client.
"""

from __future__ import annotations

import urllib.request

UNAVAILABLE_ERROR = "astro client unavailable"


class AstroClientUnavailable(RuntimeError):
    """No working Astro dispatch for this destination (yet)."""


class FakeAstroClient:
    """Records dispatch requests; returns a canned status or fails on cue."""

    def __init__(self, result: dict = None, error: Exception = None) -> None:
        self.calls: list = []
        self.urls: list = []
        self._result = result
        self._error = error

    def send_to(self, destination, url) -> dict:
        self.calls.append(destination)
        self.urls.append(url)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        return {"ok": True, "status": "dispatched",
                "destination": destination}


class RealAstroClient:
    """Fires a Voice Monkey trigger URL (GET, ~10 s timeout).

    The opener is injectable for tests, like the room's other
    urllib runners; the URL is deliberately kept out of every
    error this client raises — it carries a secret token."""

    def __init__(self, opener=None, timeout: int = 10) -> None:
        self._open = opener or (
            lambda req: urllib.request.urlopen(req, timeout=timeout))

    def send_to(self, destination, url) -> dict:
        if not url:
            raise AstroClientUnavailable(UNAVAILABLE_ERROR)
        if not url.startswith("https://"):
            # The token rides in the URL: never send it in cleartext.
            raise AstroClientUnavailable(
                f"{UNAVAILABLE_ERROR}: trigger URL is not HTTPS")
        req = urllib.request.Request(url, method="GET")
        with self._open(req) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
        if status != 200:
            raise RuntimeError(
                f"astro trigger endpoint answered HTTP {status}")
        return {"ok": True, "status": "dispatched",
                "destination": destination}


def default_client() -> RealAstroClient:
    return RealAstroClient()
