"""Offline tests for the `clean_studio` tool — no vacuum, no network.

The python-roborock package is intentionally absent here, which is
itself one of the cases under test: the real client reports itself
unavailable (lazily, per call) and the room turns that into the
standard {"ok": False} tool result. Everything else runs against
FakeRoborockClient through StudioRoom and through tools.dispatch,
the same two doors the model uses.

Run from the repo root:  python3 -m unittest bridge.test_robrock -v
"""

from __future__ import annotations

import os
import unittest
from contextlib import contextmanager

from .roborock_client import (
    UNAVAILABLE_ERROR,
    FakeRoborockClient,
    RealRoborockClient,
    RoborockClientUnavailable,
)
from .studio_room import STUDIO_SEGMENT_ENV, StudioRoom
from .tools import TOOL_SCHEMAS, DryRoom, dispatch


@contextmanager
def segment_env(value):
    """Set MUSEZ_STUDIO_SEGMENT for a test (None removes it)."""
    saved = os.environ.get(STUDIO_SEGMENT_ENV)
    if value is None:
        os.environ.pop(STUDIO_SEGMENT_ENV, None)
    else:
        os.environ[STUDIO_SEGMENT_ENV] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(STUDIO_SEGMENT_ENV, None)
        else:
            os.environ[STUDIO_SEGMENT_ENV] = saved


def room_with(client) -> StudioRoom:
    return StudioRoom(turn=1, roborock_client=client)


class SchemaTests(unittest.TestCase):
    def test_clean_studio_is_a_registered_tool(self):
        names = {t["function"]["name"] for t in TOOL_SCHEMAS}
        self.assertIn("clean_studio", names)


class DryRoomTests(unittest.TestCase):
    def test_dispatch_records_the_action(self):
        room = DryRoom()
        result = dispatch(room, "clean_studio", {})
        self.assertEqual(result, {"ok": True, "status": "cleaning"})
        self.assertEqual(room.actions, [{"clean_studio": True}])


class StudioRoomCleanTests(unittest.TestCase):
    def test_success_passes_the_configured_segment_as_an_int(self):
        client = FakeRoborockClient()
        with segment_env("16"):
            result = room_with(client).clean_studio()
        self.assertEqual(result,
                         {"ok": True, "status": "cleaning", "room": 16})
        self.assertEqual(client.calls, [16])

    def test_success_through_dispatch(self):
        client = FakeRoborockClient()
        with segment_env("17"):
            result = dispatch(room_with(client), "clean_studio", {})
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, [17])

    def test_bare_status_dict_is_wrapped_ok(self):
        client = FakeRoborockClient(result={"status": "cleaning"})
        with segment_env("16"):
            result = room_with(client).clean_studio()
        self.assertEqual(result, {"ok": True, "status": "cleaning"})

    def test_client_failure_result_passes_through(self):
        client = FakeRoborockClient(
            result={"ok": False, "error": "dock blocked"})
        with segment_env("16"):
            result = dispatch(room_with(client), "clean_studio", {})
        self.assertEqual(result, {"ok": False, "error": "dock blocked"})

    def test_client_exception_becomes_an_error_result(self):
        client = FakeRoborockClient(error=RuntimeError("vacuum asleep"))
        with segment_env("16"):
            result = room_with(client).clean_studio()
        self.assertFalse(result["ok"])
        self.assertIn("vacuum asleep", result["error"])
        # And through dispatch the turn still cannot be killed by it.
        with segment_env("16"):
            result = dispatch(room_with(client), "clean_studio", {})
        self.assertFalse(result["ok"])

    def test_no_segment_configured_never_calls_the_client(self):
        client = FakeRoborockClient()
        with segment_env(None):
            result = room_with(client).clean_studio()
        self.assertFalse(result["ok"])
        self.assertIn(STUDIO_SEGMENT_ENV, result["error"])
        self.assertEqual(client.calls, [])


class UnavailableClientTests(unittest.TestCase):
    def test_real_client_reports_unavailable_without_the_package(self):
        client = RealRoborockClient()
        with self.assertRaises(RoborockClientUnavailable) as caught:
            client.start_room_clean(16)
        self.assertEqual(str(caught.exception), UNAVAILABLE_ERROR)

    def test_room_turns_unavailability_into_an_error_result(self):
        room = StudioRoom(turn=1)  # default client: the real stub
        with segment_env("16"):
            result = room.clean_studio()
        self.assertEqual(result,
                         {"ok": False, "error": UNAVAILABLE_ERROR})

    def test_unavailability_through_dispatch(self):
        room = StudioRoom(turn=1)
        with segment_env("16"):
            result = dispatch(room, "clean_studio", {})
        self.assertEqual(result,
                         {"ok": False, "error": UNAVAILABLE_ERROR})


if __name__ == "__main__":
    unittest.main()
