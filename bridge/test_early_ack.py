"""Offline tests for the early acknowledgement — exactly one ack
per bridge turn, and the early one is the listener's.

Room evidence (2026-11-01): the bridge's own ack is a synthesized
respond2 dispatch — render + ship costs ~12 s before it can sound —
so even an escalated turn's ack, dispatched ~5 s into the bridge's
work, was heard 31 s after the turn was queued (the 19:14 plan),
and non-escalated tool turns got no early signal at all. The
classic path never had this problem: its ack is the listener's
pre-recorded filler, played locally the moment the turn is sent.

The fix is coordination, not a second ack: the listener fires its
filler for bridge turns too — in the turn's STT language — and the
queue entry carries the fact ("filler": true); the daemon hands it
to the runner as ack_prefilled, the sink is built ack_external,
and its announce() records the escalation's ack as discharged
externally WITHOUT dispatching. These tests pin that contract:
suppression is total (no transport call for the ack), the answer
is untouched, the record tells the truth, and without the flag
the bridge ack behaves exactly as before.

Run from the repo root:  python3 -m unittest bridge.test_early_ack -v
"""

from __future__ import annotations

import unittest

from .live_turn import run_live_turn
from .studio_sink import StudioSink
from .test_live_turn import (FakeSource, base_kwargs,
                             make_respond_runner)
from .tools import DryRoom
from .turn import run_turn

ACK_TEXT = "Give me a moment, I am building your Saturday."
ANSWER = "Your Saturday is ready."


def counting_transport():
    calls = []

    def transport(title, lines, text, turn, **kwargs):
        calls.append({"title": title, "lines": list(lines),
                      "text": text, "turn": turn, "kwargs": kwargs})
        return {"rid": f"rid-{len(calls)}"}

    return transport, calls


class ExternalAckSinkTests(unittest.TestCase):
    def test_announce_dispatches_nothing_and_records_external(self):
        transport, calls = counting_transport()
        sink = StudioSink(transport, turn=4242, ack_external=True)
        outcome = sink.announce(ACK_TEXT)
        self.assertEqual(calls, [])
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["external"], "listener_filler")
        self.assertEqual(outcome["text"], ACK_TEXT)
        self.assertIs(sink.ack, outcome)
        # Once-only, like a dispatched ack: a second announce is
        # the recorded outcome, still without any dispatch.
        self.assertIs(sink.announce("Another ack."), outcome)
        self.assertEqual(calls, [])

    def test_answer_flush_is_the_only_dispatch(self):
        transport, calls = counting_transport()
        sink = StudioSink(transport, turn=4243, ack_external=True)
        events = iter([("ack", ACK_TEXT), ("token", ANSWER)])
        marks = run_turn(events, DryRoom(), sink)
        receipt = sink.flush()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["text"], ANSWER)
        self.assertEqual(receipt["rid"], "rid-1")
        self.assertEqual(sink.sentences, [ANSWER])
        self.assertEqual(marks["ack"]["external"], "listener_filler")
        self.assertNotIn("rid", sink.ack)

    def test_without_the_flag_the_ack_still_dispatches(self):
        # Control: ack_external defaults off and the escalation ack
        # goes out through the transport exactly as before.
        transport, calls = counting_transport()
        sink = StudioSink(transport, turn=4244)
        outcome = sink.announce(ACK_TEXT)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["text"], ACK_TEXT)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["rid"], "rid-1")
        self.assertNotIn("external", outcome)


class PrefilledLiveTurnTests(unittest.TestCase):
    def run_turn_with(self, events, **over):
        respond_runner, seen = make_respond_runner()
        record = run_live_turn(
            "Plan my Saturday and get me home before the rain.",
            1793000000001,
            source=FakeSource(events),
            respond_runner=respond_runner,
            **base_kwargs(**over))
        return record, seen

    def test_escalated_prefilled_turn_dispatches_no_bridge_ack(self):
        record, seen = self.run_turn_with(
            [("ack", ACK_TEXT), ("token", ANSWER)],
            ack_prefilled=True)
        # One respond2 call only: the answer's flush. The ack text
        # was never synthesized by the bridge.
        self.assertEqual(len(seen), 1)
        argv, _env = seen[0]
        self.assertIn(ANSWER, argv[4])
        self.assertNotIn(ACK_TEXT, argv[4])
        self.assertEqual(record["ack"]["external"], "listener_filler")
        self.assertEqual(record["sentences"], [ANSWER])

    def test_prefilled_turn_without_escalation_records_the_filler(self):
        record, seen = self.run_turn_with(
            [("token", ANSWER)], ack_prefilled=True)
        self.assertEqual(len(seen), 1)
        self.assertEqual(record["ack"]["external"], "listener_filler")
        self.assertIsNone(record["ack"]["text"])
        self.assertFalse(record["ack"]["dispatched"])

    def test_unprefilled_escalated_turn_keeps_the_bridge_ack(self):
        record, seen = self.run_turn_with(
            [("ack", ACK_TEXT), ("token", ANSWER)])
        # Two dispatches: the ack (kind=ack) and the answer.
        self.assertEqual(len(seen), 2)
        ack_argv, ack_env = seen[0]
        self.assertIn(ACK_TEXT, ack_argv[4])
        self.assertEqual(ack_env.get("RESPOND_KIND"), "ack")
        self.assertNotIn("external", record["ack"])


if __name__ == "__main__":
    unittest.main()
