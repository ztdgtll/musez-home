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

from .meta_source import MAX_ROUNDS, SHORT_OPENER_GUIDANCE, MetaAPIError, MetaSource
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


class MixedEscalationTests(unittest.TestCase):
    def test_escalate_plus_room_call_escalation_wins_and_all_calls_answered(self):
        # Both calls arrive WITHOUT ids: the source must normalize them.
        transport = FakeTransport([
            [
                content("Te armo el plan... "),
                tool_delta(0, name="escalate",
                           arguments='{"reason": "planning"}'),
                tool_delta(1, name="set_wall_scene",
                           arguments='{"title": "PLAN"}'),
                sse({}, finish="tool_calls"),
                DONE,
            ],
            [content("Acá está tu plan del sábado."), DONE],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "planificá mi sábado")
        # Escalation won: Spark's room call never ran, and Spark's
        # drafted text was discarded — the sink hears only Wonder.
        self.assertEqual(room.actions, [])
        self.assertTrue(source.last_turn["escalated"])
        self.assertEqual(sink.sentences, ["Acá está tu plan del sábado."])
        # The follow-up request's history pairs up: the assistant
        # message lists both calls under the normalized ids, and BOTH
        # have a tool response — no call is left unanswered.
        second = transport.requests[1]
        self.assertEqual(second["model"], "muse-wonder-1.0")
        assistant = [m for m in second["messages"]
                     if m["role"] == "assistant"][-1]
        ids = [tc["id"] for tc in assistant["tool_calls"]]
        self.assertEqual(ids, ["call_1_0", "call_1_1"])
        self.assertTrue(
            all(tc["function"]["arguments"] for tc in assistant["tool_calls"]))
        tool_msgs = {m["tool_call_id"]: json.loads(m["content"])
                     for m in second["messages"] if m["role"] == "tool"}
        self.assertEqual(set(tool_msgs), set(ids))
        self.assertTrue(tool_msgs["call_1_0"]["ok"])       # the escalation
        self.assertFalse(tool_msgs["call_1_1"]["ok"])      # the room call
        self.assertTrue(tool_msgs["call_1_1"]["deferred"])


class TextPlusEscalateTests(unittest.TestCase):
    def test_text_and_escalate_sink_hears_only_wonder(self):
        transport = FakeTransport([
            [
                content("Respuesta rápida de Spark. "),
                content("Otra oración de Spark."),
                tool_delta(0, call_id="e1", name="escalate",
                           arguments='{"reason": "deep reasoning"}'),
                sse({}, finish="tool_calls"),
                DONE,
            ],
            [content("Respuesta profunda de Wonder."), DONE],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "pensalo bien")
        self.assertEqual(sink.sentences, ["Respuesta profunda de Wonder."])
        self.assertTrue(source.last_turn["escalated"])


class BadToolArgsTests(unittest.TestCase):
    def test_truncated_arguments_are_not_dispatched(self):
        transport = FakeTransport([
            [
                tool_delta(0, call_id="c1", name="set_wall_scene",
                           arguments='{"title": "WEA'),
                sse({}, finish="tool_calls"),
                DONE,
            ],
            [content("Perdón, no pude mostrarlo."), DONE],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "poné el clima en la pared")
        # Never coerced to {} and dispatched: the room saw nothing,
        # and the model got an error result it can correct.
        self.assertEqual(room.actions, [])
        self.assertEqual(marks["tool_calls"], [])
        second = transport.requests[1]
        tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        self.assertFalse(json.loads(tool_msgs[0]["content"])["ok"])
        self.assertEqual(sink.sentences, ["Perdón, no pude mostrarlo."])

    def test_non_object_arguments_are_not_dispatched(self):
        transport = FakeTransport([
            [
                tool_delta(0, call_id="c1", name="get_weather",
                           arguments='["today"]'),
                sse({}, finish="tool_calls"),
                DONE,
            ],
            [content("Sin datos del clima."), DONE],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "¿qué clima hace?")
        self.assertEqual(room.actions, [])
        self.assertEqual(marks["tool_calls"], [])
        tool_msgs = [m for m in transport.requests[1]["messages"]
                     if m["role"] == "tool"]
        self.assertFalse(json.loads(tool_msgs[0]["content"])["ok"])


class RoundBudgetTests(unittest.TestCase):
    def test_budget_brake_fires_at_the_top_of_the_loop(self):
        tool_round = [
            tool_delta(0, call_id="w1", name="get_weather", arguments="{}"),
            sse({}, finish="tool_calls"),
            DONE,
        ]
        transport = FakeTransport(
            [list(tool_round) for _ in range(MAX_ROUNDS)]
            + [[content("Respuesta final con lo que hay."), DONE]])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "¿y el clima?")
        # MAX_ROUNDS tool rounds + exactly one final no-tools answer.
        # (FakeTransport raises if the source asks for one more.)
        self.assertEqual(len(transport.requests), MAX_ROUNDS + 1)
        self.assertNotIn("tools", transport.requests[-1])
        self.assertEqual(source.last_turn["rounds"], MAX_ROUNDS)
        self.assertEqual(sink.sentences,
                         ["Respuesta final con lo que hay."])


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
