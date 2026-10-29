"""Offline tests for the escalation acknowledgement — plan item 19.

An escalated turn used to go silent: Spark's draft is discarded by
design and the room heard nothing until Wonder's first sentence
(~8 s, measured). Now the escalate call carries an ack, the turn
loop dispatches it immediately through the sink, and these tests pin
the contract: exactly one ack, before the deep model's first
sentence and before the final flush; the model's own wording when
valid, the fixed fallback otherwise; never part of the answer's
sentences; and a failed ack dispatch never fails the turn.

Run from the repo root:  python3 -m unittest bridge.test_ack -v
"""

from __future__ import annotations

import json
import unittest

from .meta_source import ACK_FALLBACK, MetaSource, validate_ack
from .mock_source import plan_turn
from .studio_sink import StudioSink, TransportError
from .test_meta_source import DONE, FakeTransport, content, sse, tool_delta
from .tools import TOOL_SCHEMAS, DryRoom
from .turn import PrintSink, run_turn

MODEL_ACK = "Dame un momento, armo tu sábado."
WONDER_LINE = "Armé tu sábado: salís a la mañana y volvés antes de la lluvia."


def escalation_source(ack_marker, wonder=None) -> MetaSource:
    """A MetaSource whose Spark escalates, then Wonder answers.

    `ack_marker`: the ack value to put in the escalate arguments, or
    None to omit the property entirely."""
    args: dict = {"reason": "multi-step planning"}
    if ack_marker is not None:
        args["ack"] = ack_marker
    transport = FakeTransport([
        [
            tool_delta(0, call_id="e1", name="escalate",
                       arguments=json.dumps(args, ensure_ascii=False)),
            sse({}, finish="tool_calls"),
            DONE,
        ],
        wonder if wonder is not None else [content(WONDER_LINE), DONE],
    ])
    return MetaSource(transport=transport)


def run_live(source, sink, text="planificá mi sábado"):
    return run_turn(source.stream_turn(text, TOOL_SCHEMAS), DryRoom(), sink)


class ValidateAckTests(unittest.TestCase):
    def test_valid_model_ack_passes_through(self):
        self.assertEqual(validate_ack(MODEL_ACK), MODEL_ACK)
        self.assertEqual(validate_ack("Un momento, reviso eso."),
                         "Un momento, reviso eso.")

    def test_fallback_when_missing_or_not_text(self):
        self.assertEqual(validate_ack(None), ACK_FALLBACK)
        self.assertEqual(validate_ack(42), ACK_FALLBACK)
        self.assertEqual(validate_ack("   "), ACK_FALLBACK)

    def test_fallback_when_too_long(self):
        long_ack = ("Dame un momento que estoy revisando todo el "
                    "pronóstico del fin de semana completo.")
        self.assertGreater(len(long_ack), 60)
        self.assertEqual(validate_ack(long_ack), ACK_FALLBACK)

    def test_fallback_when_not_a_single_sentence(self):
        self.assertEqual(validate_ack("Uno. Dos."), ACK_FALLBACK)
        self.assertEqual(validate_ack("Dame un momento. Ya vuelvo"),
                         ACK_FALLBACK)

    def test_whitespace_collapses_to_one_spoken_line(self):
        # An ack is spoken, not displayed: newlines and runs of
        # blanks collapse to a single line before validation, so
        # nothing multi-line ever reaches the transport as-is.
        self.assertEqual(validate_ack("Dame un momento,\n  armo eso."),
                         "Dame un momento, armo eso.")


class AckEscalationTests(unittest.TestCase):
    def test_model_ack_announced_once_before_wonder(self):
        sink = PrintSink()
        marks = run_live(escalation_source(MODEL_ACK), sink)
        self.assertEqual(sink.announcements, [MODEL_ACK])
        self.assertEqual(sink.sentences, [WONDER_LINE])
        self.assertEqual(marks["ack"]["text"], MODEL_ACK)
        self.assertTrue(marks["ack"]["dispatched"])
        self.assertLess(marks["t_ack"], marks["t_first_sentence"])
        self.assertNotIn(MODEL_ACK, sink.sentences[0])

    def test_missing_ack_falls_back(self):
        sink = PrintSink()
        marks = run_live(escalation_source(None), sink)
        self.assertEqual(sink.announcements, [ACK_FALLBACK])
        self.assertEqual(marks["ack"]["text"], ACK_FALLBACK)

    def test_invalid_ack_falls_back(self):
        sink = PrintSink()
        run_live(escalation_source("Uno. Dos."), sink)
        self.assertEqual(sink.announcements, [ACK_FALLBACK])

    def test_no_escalation_no_ack(self):
        transport = FakeTransport([[content("Respuesta simple."), DONE]])
        sink = PrintSink()
        marks = run_live(MetaSource(transport=transport), sink,
                         text="hola")
        self.assertEqual(sink.announcements, [])
        self.assertNotIn("ack", marks)

    def test_escalate_schema_offers_ack_reason_stays_required(self):
        source = escalation_source(MODEL_ACK)
        run_live(source, PrintSink())
        offered = {t["function"]["name"]: t["function"]
                   for t in source._transport.requests[0]["tools"]}
        params = offered["escalate"]["parameters"]
        self.assertIn("ack", params["properties"])
        self.assertEqual(params["required"], ["reason"])


class AckSinkTests(unittest.TestCase):
    def test_ack_dispatch_precedes_flush_and_stays_out_of_the_answer(self):
        calls = []

        def transport(title, lines, text, turn):
            calls.append((title, list(lines), text, turn))
            return {"rid": f"rid-{len(calls)}"}

        sink = StudioSink(transport, turn=4242)
        events = iter([("ack", MODEL_ACK),
                       ("token", "Respuesta final de Wonder.")])
        marks = run_turn(events, DryRoom(), sink)
        receipt = sink.flush()
        # Two dispatches: the ack first (empty scene — voice only),
        # then the turn's single final flush.
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ("", [], MODEL_ACK, 4242))
        self.assertEqual(calls[1][2], "Respuesta final de Wonder.")
        self.assertNotIn("Dame un momento", calls[1][2])
        self.assertEqual(receipt["rid"], "rid-2")
        self.assertEqual(sink.sentences, ["Respuesta final de Wonder."])
        self.assertTrue(marks["ack"]["dispatched"])
        self.assertEqual(marks["ack"]["rid"], "rid-1")
        self.assertTrue(sink.ack["ok"])
        # The final flush stays the once-only dispatch it always was.
        self.assertIs(sink.flush(), receipt)
        self.assertEqual(len(calls), 2)

    def test_ack_transport_failure_never_fails_the_turn(self):
        calls = []

        def transport(title, lines, text, turn):
            calls.append(text)
            if len(calls) == 1:
                raise TransportError("respond2 exited 1: boom")
            return {"rid": "final-rid"}

        sink = StudioSink(transport, turn=4243)
        events = iter([("ack", MODEL_ACK),
                       ("token", "La respuesta llega igual.")])
        marks = run_turn(events, DryRoom(), sink)
        receipt = sink.flush()
        self.assertEqual(receipt["rid"], "final-rid")
        self.assertEqual(sink.sentences, ["La respuesta llega igual."])
        self.assertFalse(marks["ack"]["dispatched"])
        self.assertFalse(marks["ack"]["ok"])
        self.assertIn("boom", marks["ack"]["error"])
        self.assertIn("boom", sink.ack["error"])

    def test_raising_announce_is_contained_by_the_loop(self):
        class RaisingSink:
            def __init__(self):
                self.sentences = []

            def say(self, sentence):
                self.sentences.append(sentence)

            def announce(self, text):
                raise RuntimeError("kaboom")

        sink = RaisingSink()
        events = iter([("ack", MODEL_ACK), ("token", "Todo bien.")])
        marks = run_turn(events, DryRoom(), sink)
        self.assertEqual(sink.sentences, ["Todo bien."])
        self.assertFalse(marks["ack"]["ok"])
        self.assertIn("kaboom", marks["ack"]["error"])

    def test_announce_dispatches_once_only(self):
        calls = []

        def transport(title, lines, text, turn):
            calls.append(text)
            return {"rid": "ack-rid"}

        sink = StudioSink(transport, turn=4244)
        first = sink.announce(MODEL_ACK)
        second = sink.announce("Otro texto distinto.")
        self.assertIs(first, second)
        self.assertEqual(calls, [MODEL_ACK])


class MockAckTests(unittest.TestCase):
    def test_mock_plan_turn_announces_before_speaking(self):
        sink = PrintSink()
        marks = run_turn(plan_turn(), DryRoom(), sink)
        self.assertEqual(sink.announcements, [MODEL_ACK])
        self.assertEqual(marks["ack"]["text"], MODEL_ACK)
        self.assertTrue(marks["ack"]["dispatched"])
        self.assertLess(marks["t_ack"], marks["t_first_sentence"])
        self.assertTrue(sink.sentences)
        for sentence in sink.sentences:
            self.assertNotIn("Dame un momento", sentence)


if __name__ == "__main__":
    unittest.main()
