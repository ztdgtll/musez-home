"""Offline tests for the live model source — no network, no key.

A fake transport replays canned SSE streams and records every request, so
the tests verify the full contract the bridge depends on: token streaming,
tool calls assembled from fragments, tool results fed back to the model,
Spark -> Wonder escalation, and loud failure on HTTP errors.

Run from the repo root:  python3 -m unittest bridge.test_meta_source -v
"""

from __future__ import annotations

import copy
import json
import unittest

from .meta_source import SHORT_OPENER_GUIDANCE, MetaAPIError, MetaSource
from .tools import TOOL_SCHEMAS, DryRoom
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


def run(source: MetaSource, text: str = "hola"):
    room = DryRoom()
    sink = PrintSink()
    marks = run_turn(source.stream_turn(text, TOOL_SCHEMAS), room, sink)
    return marks, room, sink


class TextTurnTests(unittest.TestCase):
    def test_plain_answer_streams_tokens(self):
        transport = FakeTransport([[
            content("Una Raspberry Pi "),
            content("es una computadora completa."),
            DONE,
        ]])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "¿qué es una Raspberry Pi?")
        self.assertEqual(
            sink.sentences,
            ["Una Raspberry Pi es una computadora completa."])
        self.assertEqual(marks["tool_calls"], [])
        self.assertEqual(room.actions, [])
        self.assertFalse(source.last_turn["escalated"])
        body = transport.requests[0]
        self.assertEqual(body["model"], "muse-spark-1.3")
        offered = {t["function"]["name"] for t in body["tools"]}
        self.assertEqual(
            offered, {"set_wall_scene", "get_weather", "say", "escalate"})


class SystemPromptTests(unittest.TestCase):
    def test_system_prompt_carries_the_short_opener_guidance(self):
        transport = FakeTransport([[content("Listo."), DONE]])
        source = MetaSource(transport=transport)
        run(source)
        system = transport.requests[0]["messages"][0]
        self.assertEqual(system["role"], "system")
        self.assertIn(SHORT_OPENER_GUIDANCE, system["content"])


class ToolTurnTests(unittest.TestCase):
    def test_tool_call_assembled_from_fragments_and_result_fed_back(self):
        transport = FakeTransport([
            [
                tool_delta(0, call_id="c1", name="set_wall_scene",
                           arguments='{"title": "WEA'),
                tool_delta(0, arguments='THER", "line1": "29°C, soleado"}'),
                sse({}, finish="tool_calls"),
                DONE,
            ],
            [content("Listo, te lo puse en la pared."), DONE],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "poné el clima en la pared")
        self.assertEqual(room.actions[0]["wall"], "WEATHER")
        self.assertEqual(room.actions[0]["line1"], "29°C, soleado")
        self.assertEqual(marks["tool_calls"][0]["name"], "set_wall_scene")
        self.assertEqual(sink.sentences, ["Listo, te lo puse en la pared."])
        second = transport.requests[1]
        tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(json.loads(tool_msgs[0]["content"])["shown"],
                         "WEATHER")


class EscalationTests(unittest.TestCase):
    def test_escalate_hands_the_turn_to_wonder(self):
        transport = FakeTransport([
            [
                tool_delta(0, call_id="e1", name="escalate",
                           arguments='{"reason": "multi-step planning"}'),
                sse({}, finish="tool_calls"),
                DONE,
            ],
            [content("Armé tu sábado: moto a la mañana, vuelta antes de "
                      "la lluvia."), DONE],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "planificá mi sábado")
        self.assertEqual(room.actions, [])  # escalate never reaches the room
        second = transport.requests[1]
        self.assertEqual(second["model"], "muse-wonder-1.0")
        offered = {t["function"]["name"] for t in second["tools"]}
        self.assertNotIn("escalate", offered)
        self.assertTrue(source.last_turn["escalated"])
        self.assertTrue(any("escalated" in n for n in marks["notes"]))
        self.assertEqual(len(sink.sentences), 1)


class ErrorTests(unittest.TestCase):
    def test_http_error_is_loud(self):
        def failing(url, body, headers):
            raise MetaAPIError(401, "invalid api key")

        source = MetaSource(transport=failing)
        with self.assertRaises(MetaAPIError) as ctx:
            run(source)
        self.assertEqual(ctx.exception.status, 401)

    def test_missing_key_fails_at_construction(self):
        import os
        old = os.environ.pop("META_API_KEY", None)
        try:
            with self.assertRaises(MetaAPIError):
                MetaSource()
        finally:
            if old is not None:
                os.environ["META_API_KEY"] = old


if __name__ == "__main__":
    unittest.main()
