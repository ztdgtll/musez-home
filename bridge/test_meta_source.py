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

from .meta_source import (MAX_ROUNDS, PLAN_GUARD_GUIDANCE,
                          SHORT_OPENER_GUIDANCE,
                          SPOKEN_ROUNDS_GUIDANCE,
                          SPARK_MAX_TOKENS, SPARK_REASONING_EFFORT,
                          SYSTEM_PROMPT, WONDER_MAX_TOKENS, MetaAPIError,
                          MetaSource, compose_system_prompt,
                          detect_plan_request)
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
            offered, {"set_wall_scene", "set_day_plan", "get_weather",
                      "get_hackathon_field",
                      "clean_studio", "say", "escalate"})


class SystemPromptTests(unittest.TestCase):
    def test_system_prompt_carries_the_short_opener_guidance(self):
        transport = FakeTransport([[content("Listo."), DONE]])
        source = MetaSource(transport=transport)
        run(source)
        system = transport.requests[0]["messages"][0]
        self.assertEqual(system["role"], "system")
        self.assertIn(SHORT_OPENER_GUIDANCE, system["content"])

    def test_system_prompt_carries_the_spoken_rounds_guidance(self):
        # The 2026-11-01 rehearsal failure: ack echoed, process
        # narrated mid-round, double close. The guidance against all
        # three must ride every request's system prompt.
        transport = FakeTransport([[content("Listo."), DONE]])
        source = MetaSource(transport=transport)
        run(source)
        system = transport.requests[0]["messages"][0]
        self.assertIn(SPOKEN_ROUNDS_GUIDANCE, system["content"])


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


PLAN_ARGS = ('{"title": "Saturday plan", "blocks": '
             '[{"start": "08:00", "end": "12:00", "label": "Salida", '
             '"kind": "activity"}]}')


def plan_tool_round(call_id="p1"):
    return [
        tool_delta(0, call_id=call_id, name="set_day_plan",
                   arguments=PLAN_ARGS),
        sse({}, finish="tool_calls"),
        DONE,
    ]


class EscalationTests(unittest.TestCase):
    def test_escalate_hands_the_turn_to_wonder(self):
        transport = FakeTransport([
            [
                tool_delta(0, call_id="e1", name="escalate",
                           arguments='{"reason": "multi-step planning"}'),
                sse({}, finish="tool_calls"),
                DONE,
            ],
            # A plan request: Wonder builds the plan with the tool
            # before answering (the plan guard refuses a prose-only
            # finish — PlanGuardTests pin that refusal itself).
            plan_tool_round(),
            [content("Armé tu sábado: moto a la mañana, vuelta antes de "
                      "la lluvia."), DONE],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "planificá mi sábado")
        # escalate never reaches the room; the plan tool does.
        self.assertEqual([a.get("plan") for a in room.actions],
                         ["Saturday plan"])
        second = transport.requests[1]
        self.assertEqual(second["model"], "muse-wonder-1.0")
        offered = {t["function"]["name"] for t in second["tools"]}
        self.assertNotIn("escalate", offered)
        self.assertTrue(source.last_turn["escalated"])
        self.assertTrue(source.last_turn["plan_built"])
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
            plan_tool_round(),
            [content("Acá está tu plan del sábado."), DONE],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "planificá mi sábado")
        # Escalation won: Spark's room call never ran, and Spark's
        # drafted text was discarded — the sink hears only Wonder.
        # (Wonder's own rounds here obey the plan contract: build
        # the plan, then answer.)
        self.assertEqual([a.get("plan") for a in room.actions],
                         ["Saturday plan"])
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


def usage_line(prompt_tokens: int, completion_tokens: int,
               with_choices: bool = True) -> str:
    """A usage-only SSE chunk, as the API sends at the end of a
    stream: choices empty — or absent altogether."""
    obj: dict = {"usage": {"prompt_tokens": prompt_tokens,
                           "completion_tokens": completion_tokens,
                           "total_tokens": prompt_tokens + completion_tokens}}
    if with_choices:
        obj["choices"] = []
    return "data: " + json.dumps(obj)


class UsageAndParamsTests(unittest.TestCase):
    def test_spark_params_and_per_round_marks(self):
        transport = FakeTransport([
            [
                tool_delta(0, call_id="w1", name="get_weather",
                           arguments="{}"),
                sse({}, finish="tool_calls"),
                usage_line(800, 12),
                DONE,
            ],
            [
                content("Hoy hace sol en Tamarac."),
                usage_line(850, 30),
                DONE,
            ],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "¿qué clima hace?")
        self.assertEqual(sink.sentences, ["Hoy hace sol en Tamarac."])
        # Every streaming request asks for usage and carries the
        # fast model's explicit shaping: minimal reasoning + cap.
        for body in transport.requests:
            self.assertEqual(body["stream_options"],
                             {"include_usage": True})
            self.assertEqual(body["reasoning_effort"],
                             SPARK_REASONING_EFFORT)
            self.assertEqual(body["max_tokens"], SPARK_MAX_TOKENS)
        # One mark per round, with the round's own model, tools and
        # the API's usage — not just turn-level totals.
        round_marks = source.last_turn["round_marks"]
        self.assertEqual(len(round_marks), 2)
        self.assertEqual(round_marks[0]["model"], "muse-spark-1.3")
        self.assertEqual(round_marks[0]["tool_names"], ["get_weather"])
        self.assertEqual(round_marks[0]["prompt_tokens"], 800)
        self.assertEqual(round_marks[0]["completion_tokens"], 12)
        self.assertEqual(round_marks[1]["tool_names"], [])
        self.assertEqual(round_marks[1]["completion_tokens"], 30)
        self.assertIsNotNone(round_marks[1]["ttft_ms"])
        self.assertGreaterEqual(round_marks[1]["duration_ms"],
                                round_marks[1]["ttft_ms"])
        self.assertEqual(source.last_turn["prompt_tokens"], 1650)
        self.assertEqual(source.last_turn["completion_tokens"], 42)

    def test_usage_chunk_without_choices_is_tolerated(self):
        transport = FakeTransport([[
            content("Una Raspberry Pi es una computadora completa."),
            usage_line(640, 25, with_choices=False),
            DONE,
        ]])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "¿qué es una Raspberry Pi?")
        self.assertEqual(
            sink.sentences,
            ["Una Raspberry Pi es una computadora completa."])
        self.assertEqual(source.last_turn["prompt_tokens"], 640)
        self.assertEqual(source.last_turn["completion_tokens"], 25)

    def test_wonder_round_gets_wonder_params(self):
        transport = FakeTransport([
            [
                tool_delta(0, call_id="e1", name="escalate",
                           arguments='{"reason": "planning"}'),
                sse({}, finish="tool_calls"),
                usage_line(810, 9),
                DONE,
            ],
            [
                tool_delta(0, call_id="p1", name="set_day_plan",
                           arguments=PLAN_ARGS),
                sse({}, finish="tool_calls"),
                usage_line(900, 210),
                DONE,
            ],
            [
                content("Armé tu sábado completo."),
                usage_line(950, 40),
                DONE,
            ],
        ])
        source = MetaSource(transport=transport)
        run(source, "planificá mi sábado")
        wonder_body = transport.requests[1]
        self.assertEqual(wonder_body["model"], "muse-wonder-1.0")
        # The deep model is shaped by its own generous cap only —
        # no minimal-reasoning squeeze on the model bought for depth.
        self.assertEqual(wonder_body["max_tokens"], WONDER_MAX_TOKENS)
        self.assertNotIn("reasoning_effort", wonder_body)
        # EVERY deep-model round is shaped by the deep model's own
        # parameters, wherever it runs in the turn.
        for body in transport.requests[1:]:
            self.assertEqual(body["model"], "muse-wonder-1.0")
            self.assertEqual(body["max_tokens"], WONDER_MAX_TOKENS)
            self.assertNotIn("reasoning_effort", body)
        models = [m["model"] for m in source.last_turn["round_marks"]]
        self.assertEqual(models, ["muse-spark-1.3", "muse-wonder-1.0",
                                  "muse-wonder-1.0"])
        self.assertEqual(source.last_turn["completion_tokens"], 259)


class PlanDetectorTests(unittest.TestCase):
    """detect_plan_request: planning signal + day reference,
    either language — and nothing else. The variants that used
    to fall through to a weather answer are the true cases; the
    pure-weather and field turns are the false ones."""

    def test_plan_requests_detected(self):
        for text in (
            # The exact transcript of the failed live turn.
            "Plan my Saturday and get me home before the rain.",
            "planificame el sábado",
            "Planificá mi sábado.",
            "¿Me armás un plan para mañana?",
            "Organize my Sunday around the rain.",
            "armame un plan para el finde",
            "Schedule my Friday around the weather.",
            "quiero planificar el domingo",
        ):
            self.assertTrue(detect_plan_request(text), text)

    def test_non_plan_turns_not_detected(self):
        for text in (
            "What's the weather like this weekend?",
            "Put the weekend weather on the wall.",
            "¿Qué clima hace el sábado?",
            "¿Cómo va a estar el clima para este fin de semana?",
            "Hey Muse, how many projects are competing in this "
            "hackathon right now?",
            "limpiá el estudio",
            "contame un chiste",
            "hola",
            "",
            "   ",
            None,
            42,
        ):
            self.assertFalse(detect_plan_request(text), repr(text))


class ScriptedRoom:
    """A room whose set_day_plan answers from a scripted result
    list — the dispatch contract (tools.dispatch) and nothing
    else, for the guard's rejected-plan test."""

    def __init__(self, plan_results):
        self._plan_results = list(plan_results)
        self.plan_calls = []

    def set_day_plan(self, title, blocks=None, date=None):
        self.plan_calls.append(title)
        return self._plan_results.pop(0)


class PlanGuardTests(unittest.TestCase):
    """The plan guard (stream_turn): a plan turn may not END on
    prose. Field evidence: turn 1793587782994, 2026-11-01."""

    def test_spark_prose_plan_is_refused_then_built(self):
        transport = FakeTransport([
            [tool_delta(0, call_id="w1", name="get_weather",
                        arguments="{}"),
             sse({}, finish="tool_calls"), DONE],
            # Spark tries to finish with a narrated plan. On a
            # fast-model round the draft is buffered — the guard
            # discards it like an escalation's, so the room never
            # hears a plan that does not exist.
            [content("Tu sábado: desayuno en casa, compras al "
                      "mediodía y pileta a la tarde."), DONE],
            plan_tool_round(),
            [content("Listo, tu sábado está en la pared."), DONE],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(
            source, "Plan my Saturday and get me home before the rain.")
        self.assertEqual([tc["name"] for tc in marks["tool_calls"]],
                         ["get_weather", "set_day_plan"])
        self.assertEqual(sink.sentences,
                         ["Listo, tu sábado está en la pared."])
        self.assertTrue(any("plan guard" in n
                            for n in marks.get("notes", [])))
        # The request after the refused finish carries the
        # correction as a system message.
        third = transport.requests[2]
        system_texts = [m["content"] for m in third["messages"]
                        if m["role"] == "system"]
        self.assertIn(PLAN_GUARD_GUIDANCE, system_texts)
        self.assertTrue(source.last_turn["plan_requested"])
        self.assertTrue(source.last_turn["plan_built"])

    def test_wonder_prose_plan_is_refused_then_built(self):
        transport = FakeTransport([
            [tool_delta(0, call_id="e1", name="escalate",
                        arguments='{"reason": "planning", "ack": '
                                  '"Give me a moment."}'),
             sse({}, finish="tool_calls"), DONE],
            # Wonder's prose streams (deep rounds are not buffered)
            # — it cannot be recalled, but the turn still may not
            # END there: the guard forces the plan round.
            [content("Your Saturday: breakfast, groceries, pool."),
             DONE],
            plan_tool_round(),
            [content("Your Saturday plan is on the wall."), DONE],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(
            source, "Plan my Saturday and get me home before the rain.")
        self.assertEqual([a.get("plan") for a in room.actions],
                         ["Saturday plan"])
        self.assertTrue(any("plan guard" in n
                            for n in marks.get("notes", [])))
        self.assertEqual(sink.sentences[-1],
                         "Your Saturday plan is on the wall.")
        self.assertTrue(source.last_turn["plan_built"])

    def test_rejected_plan_does_not_discharge_the_guard(self):
        room = ScriptedRoom([
            {"ok": False, "error": "block 0 must start before it ends"},
            {"ok": True, "queued": "Saturday plan"},
        ])
        transport = FakeTransport([
            plan_tool_round(),
            [content("Bueno, tu sábado queda así nomás."), DONE],
            plan_tool_round(call_id="p2"),
            [content("Ahora sí, plan en la pared."), DONE],
        ])
        source = MetaSource(transport=transport)
        sink = PrintSink()
        marks = run_turn(
            source.stream_turn("planificá mi sábado", TOOL_SCHEMAS),
            room, sink)
        # The rejected call did not count: the prose finish after
        # it was refused too, and only the accepted plan let the
        # turn end.
        self.assertEqual(len(room.plan_calls), 2)
        self.assertEqual(sink.sentences, ["Ahora sí, plan en la pared."])
        self.assertTrue(any("plan guard" in n
                            for n in marks.get("notes", [])))

    def test_weather_turn_never_trips_the_guard(self):
        transport = FakeTransport([
            [tool_delta(0, call_id="w1", name="get_weather",
                        arguments="{}"),
             sse({}, finish="tool_calls"), DONE],
            [content("El finde viene con lluvia."), DONE],
        ])
        source = MetaSource(transport=transport)
        marks, room, sink = run(
            source, "What's the weather like this weekend?")
        self.assertEqual(len(transport.requests), 2)
        for body in transport.requests:
            for m in body["messages"]:
                self.assertNotIn(PLAN_GUARD_GUIDANCE,
                                 m.get("content") or "")
        self.assertFalse(source.last_turn["plan_requested"])
        self.assertEqual(sink.sentences, ["El finde viene con lluvia."])

    def test_guard_yields_to_the_round_cap(self):
        # A model that spends every round on weather never gives
        # the guard a finish to refuse: the cap's final answer
        # ends the turn as before (the runner's composition
        # backstop owns the wall for that case — test_live_turn).
        tool_round = [
            tool_delta(0, call_id="w1", name="get_weather",
                       arguments="{}"),
            sse({}, finish="tool_calls"),
            DONE,
        ]
        transport = FakeTransport(
            [list(tool_round) for _ in range(MAX_ROUNDS)]
            + [[content("Respuesta final con lo que hay."), DONE]])
        source = MetaSource(transport=transport)
        marks, room, sink = run(source, "planificá mi sábado")
        self.assertEqual(len(transport.requests), MAX_ROUNDS + 1)
        self.assertEqual(sink.sentences,
                         ["Respuesta final con lo que hay."])
        self.assertFalse(source.last_turn["plan_built"])


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


class ComposeSystemPromptTests(unittest.TestCase):
    """The presence block: appended verbatim after the bare prompt
    when loaded, and the bare prompt — byte-identical — when not.
    The measured probes all ran the bare prompt; loading presence
    must be an explicit, visible change, never a silent drift."""

    def test_absent_presence_is_the_bare_prompt(self):
        self.assertEqual(compose_system_prompt(), SYSTEM_PROMPT)
        self.assertEqual(compose_system_prompt(None), SYSTEM_PROMPT)
        self.assertEqual(compose_system_prompt(""), SYSTEM_PROMPT)
        self.assertEqual(compose_system_prompt("  \n "), SYSTEM_PROMPT)

    def test_presence_is_appended_after_the_prompt(self):
        presence = "PRESENCE — who you are, who he is.\n- His Saturday: slow."
        out = compose_system_prompt(presence)
        self.assertTrue(out.startswith(SYSTEM_PROMPT))
        self.assertTrue(out.endswith(presence))
        self.assertIn("\n\n", out[len(SYSTEM_PROMPT):])

    def test_source_speaks_with_the_composed_prompt(self):
        transport = FakeTransport([[content("Hola."), DONE]])
        source = MetaSource(
            transport=transport,
            system_prompt=compose_system_prompt("PRESENCE test block"))
        run(source, "hola")
        system = transport.requests[0]["messages"][0]
        self.assertEqual(system["role"], "system")
        self.assertTrue(system["content"].startswith(SYSTEM_PROMPT))
        self.assertTrue(system["content"].endswith("PRESENCE test block"))


if __name__ == "__main__":
    unittest.main()
