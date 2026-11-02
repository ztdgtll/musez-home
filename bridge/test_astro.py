"""Offline tests for the `call_astro` tool — no robot, no network.

Everything runs against FakeAstroClient (or a fake HTTP opener for
the real client) through StudioRoom and through tools.dispatch,
the same two doors the model uses. The trigger URLs in these tests
are obvious fakes: the real ones carry a Voice Monkey API token and
live only in environment variables (MUSEZ_ASTRO_URL_STUDIO /
MUSEZ_ASTRO_URL_KITCHEN), never in this repo.

Run from the repo root:  python3 -m unittest bridge.test_astro -v
"""

from __future__ import annotations

import os
import unittest
import urllib.error
from contextlib import contextmanager

from .astro_client import (
    UNAVAILABLE_ERROR,
    AstroClientUnavailable,
    FakeAstroClient,
    RealAstroClient,
)
from .studio_room import ASTRO_URL_ENVS, StudioRoom
from .tools import TOOL_SCHEMAS, DryRoom, dispatch

FAKE_STUDIO_URL = ("https://api-v3.voicemonkey.io/trigger"
                   "?token=FAKE-TOKEN&device=fake-studio-trigger")
FAKE_KITCHEN_URL = ("https://api-v3.voicemonkey.io/trigger"
                    "?token=FAKE-TOKEN&device=fake-kitchen-trigger")


@contextmanager
def astro_env(urls):
    """Set the Astro trigger-URL env vars for a test.

    `urls` maps destination -> URL (or None to leave it unset);
    destinations not mentioned are removed. Every variable is
    restored afterwards."""
    saved = {env: os.environ.get(env) for env in ASTRO_URL_ENVS.values()}
    for destination, env in ASTRO_URL_ENVS.items():
        value = (urls or {}).get(destination)
        if value is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = value
    try:
        yield
    finally:
        for env, value in saved.items():
            if value is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = value


def room_with(client) -> StudioRoom:
    return StudioRoom(turn=1, astro_client=client)


class SchemaTests(unittest.TestCase):
    def test_call_astro_is_a_registered_tool(self):
        names = {t["function"]["name"] for t in TOOL_SCHEMAS}
        self.assertIn("call_astro", names)

    def test_call_astro_requires_a_destination(self):
        schema = next(t["function"] for t in TOOL_SCHEMAS
                      if t["function"]["name"] == "call_astro")
        self.assertEqual(schema["parameters"]["required"],
                         ["destination"])


class DryRoomTests(unittest.TestCase):
    def test_dispatch_records_the_action(self):
        room = DryRoom()
        result = dispatch(room, "call_astro", {"destination": "studio"})
        self.assertEqual(result, {"ok": True, "status": "dispatched",
                                  "destination": "studio"})
        self.assertEqual(room.actions, [{"call_astro": "studio"}])


class StudioRoomCallAstroTests(unittest.TestCase):
    def test_success_fires_the_configured_studio_url(self):
        client = FakeAstroClient()
        with astro_env({"studio": FAKE_STUDIO_URL}):
            result = room_with(client).call_astro("studio")
        self.assertEqual(result, {"ok": True, "status": "dispatched",
                                  "destination": "studio"})
        self.assertEqual(client.calls, ["studio"])
        self.assertEqual(client.urls, [FAKE_STUDIO_URL])

    def test_success_through_dispatch(self):
        client = FakeAstroClient()
        with astro_env({"kitchen": FAKE_KITCHEN_URL}):
            result = dispatch(room_with(client), "call_astro",
                              {"destination": "kitchen"})
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, ["kitchen"])
        self.assertEqual(client.urls, [FAKE_KITCHEN_URL])

    def test_bare_status_dict_is_wrapped_ok(self):
        client = FakeAstroClient(result={"status": "dispatched"})
        with astro_env({"studio": FAKE_STUDIO_URL}):
            result = room_with(client).call_astro("studio")
        self.assertEqual(result, {"ok": True, "status": "dispatched"})

    def test_client_failure_result_passes_through(self):
        client = FakeAstroClient(
            result={"ok": False, "error": "routine did not run"})
        with astro_env({"studio": FAKE_STUDIO_URL}):
            result = dispatch(room_with(client), "call_astro",
                              {"destination": "studio"})
        self.assertEqual(result,
                         {"ok": False, "error": "routine did not run"})

    def test_client_exception_becomes_an_error_result(self):
        client = FakeAstroClient(error=RuntimeError("trigger timeout"))
        with astro_env({"studio": FAKE_STUDIO_URL}):
            result = room_with(client).call_astro("studio")
        self.assertFalse(result["ok"])
        self.assertIn("trigger timeout", result["error"])
        # And through dispatch the turn still cannot be killed by it.
        with astro_env({"studio": FAKE_STUDIO_URL}):
            result = dispatch(room_with(client), "call_astro",
                              {"destination": "studio"})
        self.assertFalse(result["ok"])

    def test_unconfigured_destination_never_calls_the_client(self):
        client = FakeAstroClient()
        with astro_env({"kitchen": FAKE_KITCHEN_URL}):
            result = room_with(client).call_astro("studio")
        self.assertFalse(result["ok"])
        self.assertIn(ASTRO_URL_ENVS["studio"], result["error"])
        self.assertEqual(client.calls, [])

    def test_unknown_destination_never_calls_the_client(self):
        client = FakeAstroClient()
        with astro_env({"studio": FAKE_STUDIO_URL}):
            result = room_with(client).call_astro("garage")
        self.assertFalse(result["ok"])
        self.assertIn("garage", result["error"])
        self.assertEqual(client.calls, [])


class UnavailableClientTests(unittest.TestCase):
    def test_room_turns_unavailability_into_an_error_result(self):
        client = FakeAstroClient(
            error=AstroClientUnavailable(UNAVAILABLE_ERROR))
        with astro_env({"studio": FAKE_STUDIO_URL}):
            result = room_with(client).call_astro("studio")
        self.assertEqual(result,
                         {"ok": False, "error": UNAVAILABLE_ERROR})

    def test_unavailability_through_dispatch(self):
        client = FakeAstroClient(
            error=AstroClientUnavailable(UNAVAILABLE_ERROR))
        with astro_env({"studio": FAKE_STUDIO_URL}):
            result = dispatch(room_with(client), "call_astro",
                              {"destination": "studio"})
        self.assertEqual(result,
                         {"ok": False, "error": UNAVAILABLE_ERROR})


class _FakeResponse:
    """A urlopen stand-in: context manager carrying an HTTP status."""

    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RealAstroClientTests(unittest.TestCase):
    def test_http_200_is_a_successful_dispatch(self):
        seen = []

        def opener(req):
            seen.append(req.full_url)
            return _FakeResponse(200)

        client = RealAstroClient(opener=opener)
        result = client.send_to("studio", FAKE_STUDIO_URL)
        self.assertEqual(result, {"ok": True, "status": "dispatched",
                                  "destination": "studio"})
        self.assertEqual(seen, [FAKE_STUDIO_URL])

    def test_non_200_is_a_failure_not_a_success(self):
        client = RealAstroClient(opener=lambda req: _FakeResponse(500))
        with self.assertRaises(RuntimeError) as caught:
            client.send_to("studio", FAKE_STUDIO_URL)
        self.assertIn("500", str(caught.exception))
        # The failing URL (with its token) stays out of the error.
        self.assertNotIn("FAKE-TOKEN", str(caught.exception))

    def test_transport_error_propagates_for_the_room_to_convert(self):
        def opener(req):
            raise urllib.error.URLError("no route to host")

        client = RealAstroClient(opener=opener)
        with self.assertRaises(urllib.error.URLError):
            client.send_to("studio", FAKE_STUDIO_URL)

    def test_no_url_reports_unavailable(self):
        client = RealAstroClient(opener=lambda req: _FakeResponse(200))
        with self.assertRaises(AstroClientUnavailable) as caught:
            client.send_to("studio", "")
        self.assertEqual(str(caught.exception), UNAVAILABLE_ERROR)

    def test_non_https_url_reports_unavailable(self):
        client = RealAstroClient(opener=lambda req: _FakeResponse(200))
        with self.assertRaises(AstroClientUnavailable):
            client.send_to(
                "studio", "http://example.invalid/trigger?token=FAKE")


if __name__ == "__main__":
    unittest.main()
