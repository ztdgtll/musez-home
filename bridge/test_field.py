"""Offline tests for the `get_hackathon_field` tool — no network.

The count comes from GitHub's public search API at call time, so
every test runs against a fake opener (runner level), a fake runner
(room level) or a fake model transport (the full turn loop): the
three doors the live path uses. What is pinned here:

- the runner sends a User-Agent (GitHub refuses API calls without
  one), parses `total_count`, and raises on any failure or unusable
  payload — never returns a number it did not read;
- the room turns a failure into {"ok": False} and keeps NO count
  (`room.field` stays None), so a failed lookup can never leak an
  invented number into an answer or a wall card;
- scene composition builds the field card ("THE FIELD") from the
  turn's own result, in the turn's language, and the model's own
  scene still wins over it;
- a scripted model that decides the question is about the field
  gets the tool offered, its call is dispatched to the real room
  method, and the live count is what flows back into the turn.

Run from the repo root:  python3 -m unittest bridge.test_field -v
"""

from __future__ import annotations

import copy
import io
import json
import unittest
import urllib.error

from .meta_source import MetaSource
from .studio_room import GitHubFieldRunner, StudioRoom
from .studio_sink import StudioSink
from .studio_turn import compose_scene
from .tools import TOOL_SCHEMAS, DryRoom, dispatch
from .turn import PrintSink, run_turn

DONE = "data: [DONE]"


def sse(delta: dict, finish=None) -> str:
    return "data: " + json.dumps(
        {"choices": [{"delta": delta, "finish_reason": finish}]})


def content(text: str) -> str:
    return sse({"content": text})


def tool_delta(index: int, call_id: str = "", name: str = "",
               arguments: str = "") -> str:
    tc: dict = {"index": index}
    if call_id:
        tc["id"] = call_id
    fn: dict = {}
    if name:
        fn["name"] = name
    if arguments:
        fn["arguments"] = arguments
    if fn:
        tc["function"] = fn
    return sse({"tool_calls": [tc]})


class FakeTransport:
    """Returns the queued SSE responses, one per request; records bodies."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, url: str, body: dict, headers: dict):
        self.requests.append(copy.deepcopy(body))
        if not self.responses:
            raise AssertionError("the source made more requests than queued")
        return iter(self.responses.pop(0))


class FakeResponse:
    """A urlopen-shaped reply: context manager with read()."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(payload: dict, seen: list = None):
    def opener(req):
        if seen is not None:
            seen.append(req)
        return FakeResponse(json.dumps(payload).encode())
    return opener


class SchemaTests(unittest.TestCase):
    def test_get_hackathon_field_is_a_registered_tool(self):
        names = {t["function"]["name"] for t in TOOL_SCHEMAS}
        self.assertIn("get_hackathon_field", names)


class RunnerTests(unittest.TestCase):
    def test_parses_total_count_and_sends_a_user_agent(self):
        seen = []
        runner = GitHubFieldRunner(opener=opener_returning(
            {"total_count": 611, "incomplete_results": False,
             "items": []}, seen))
        self.assertEqual(runner(), 611)
        req = seen[0]
        self.assertIn("meta-ai-developer-hackathon", req.full_url)
        self.assertTrue(req.get_header("User-agent"))

    def test_http_error_raises(self):
        def opener(req):
            raise urllib.error.HTTPError(
                req.full_url, 403, "rate limited", {}, io.BytesIO(b""))
        runner = GitHubFieldRunner(opener=opener)
        with self.assertRaises(urllib.error.HTTPError):
            runner()

    def test_network_failure_raises(self):
        def opener(req):
            raise urllib.error.URLError("no route")
        runner = GitHubFieldRunner(opener=opener)
        with self.assertRaises(urllib.error.URLError):
            runner()

    def test_malformed_json_raises(self):
        runner = GitHubFieldRunner(
            opener=lambda req: FakeResponse(b"not json"))
        with self.assertRaises(ValueError):
            runner()

    def test_missing_total_count_raises(self):
        runner = GitHubFieldRunner(
            opener=opener_returning({"incomplete_results": False}))
        with self.assertRaises(ValueError):
            runner()

    def test_non_int_total_count_raises(self):
        runner = GitHubFieldRunner(
            opener=opener_returning({"total_count": "611"}))
        with self.assertRaises(ValueError):
            runner()


class DryRoomTests(unittest.TestCase):
    def test_dispatch_records_the_action(self):
        room = DryRoom()
        result = dispatch(room, "get_hackathon_field", {})
        self.assertTrue(result["ok"])
        self.assertIn("projects", result)
        self.assertEqual(room.actions, [{"hackathon_field_lookup": True}])


class StudioRoomFieldTests(unittest.TestCase):
    def room_with(self, runner) -> StudioRoom:
        return StudioRoom(turn=1, field_runner=runner)

    def test_success_returns_and_keeps_the_live_count(self):
        room = self.room_with(lambda: 611)
        result = room.get_hackathon_field()
        self.assertTrue(result["ok"])
        self.assertEqual(result["projects"], 611)
        self.assertEqual(room.field["projects"], 611)

    def test_success_through_dispatch(self):
        room = self.room_with(lambda: 611)
        result = dispatch(room, "get_hackathon_field", {})
        self.assertTrue(result["ok"])
        self.assertEqual(room.field["projects"], 611)

    def test_failure_is_an_error_result_and_keeps_no_count(self):
        def boom():
            raise RuntimeError("api down")
        room = self.room_with(boom)
        result = room.get_hackathon_field()
        self.assertFalse(result["ok"])
        self.assertIn("api down", result["error"])
        self.assertIsNone(room.field)
        # And through dispatch the turn still cannot be killed by it.
        result = dispatch(room, "get_hackathon_field", {})
        self.assertFalse(result["ok"])
        self.assertIsNone(room.field)

    def test_unusable_count_is_an_error_result(self):
        room = self.room_with(lambda: "611")
        result = room.get_hackathon_field()
        self.assertFalse(result["ok"])
        self.assertIsNone(room.field)


class ComposeTests(unittest.TestCase):
    def sink(self) -> StudioSink:
        return StudioSink(lambda *a: {"rid": None}, turn=1)

    def room_with_field(self, count: int = 611) -> StudioRoom:
        room = StudioRoom(turn=1, field_runner=lambda: count)
        room.get_hackathon_field()
        return room

    def test_field_card_in_english(self):
        title, lines = compose_scene("", self.room_with_field(),
                                     self.sink(), lang="en")
        self.assertEqual(title, "THE FIELD")
        self.assertEqual(lines, ["611 projects competing",
                                 "Meta AI Developer Hackathon"])

    def test_field_card_in_spanish(self):
        title, lines = compose_scene("", self.room_with_field(),
                                     self.sink(), lang="es")
        self.assertEqual(title, "THE FIELD")
        self.assertEqual(lines[0], "611 proyectos compitiendo")

    def test_model_scene_still_wins_over_the_field_card(self):
        room = self.room_with_field()
        room.set_wall_scene("THE FIELD", "611 projects right now")
        title, lines = compose_scene("", room, self.sink(), lang="en")
        self.assertEqual(lines, ["611 projects right now"])

    def test_no_field_no_card(self):
        room = StudioRoom(turn=1, field_runner=lambda: 611)
        title, lines = compose_scene("", room, self.sink(), lang="en")
        self.assertNotEqual(title, "THE FIELD")


class FieldTurnTests(unittest.TestCase):
    """The full loop with a scripted model: the question about the
    field is offered the tool, the call reaches the REAL room
    method (fake runner at the API edge), and the count that comes
    back is the count the turn is answered with."""

    def test_field_question_flows_end_to_end(self):
        transport = FakeTransport([
            [tool_delta(0, call_id="f1", name="get_hackathon_field",
                        arguments="{}"),
             sse({}, finish="tool_calls"),
             DONE],
            [content("There are 611 projects competing right now. "
                     "Only one of them is answering from a real room."),
             DONE],
        ])
        source = MetaSource(transport=transport)
        room = StudioRoom(turn=1, field_runner=lambda: 611)
        sink = PrintSink()
        marks = run_turn(
            source.stream_turn(
                "How many projects are competing in this "
                "hackathon right now?", TOOL_SCHEMAS),
            room, sink)
        offered = {t["function"]["name"]
                   for t in transport.requests[0]["tools"]}
        self.assertIn("get_hackathon_field", offered)
        self.assertEqual(marks["tool_calls"][0]["name"],
                         "get_hackathon_field")
        result = marks["tool_calls"][0]["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["projects"], 611)
        self.assertEqual(room.field["projects"], 611)
        self.assertIn("611", sink.sentences[0])


if __name__ == "__main__":
    unittest.main()
