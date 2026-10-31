"""Offline tests for the live runner — fakes only, no Pi, no API.

Pins the wiring contracts the room depends on:

- the switch gates the live path before any source exists;
- the presence block (read from the Pi at runtime) is appended to
  the system prompt the source actually sends — and the bare prompt
  is used, byte-identical, when no presence loads;
- after the answer's last segment plays, the runner publishes the
  turn's final wall state once (phase 5, sticky): the plan payload
  for a plan turn, the composed card otherwise;
- when playback is never observed, nothing is published and the
  record says so.

Run from the repo root:  python3 -m unittest bridge.test_live_turn -v
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from .live_turn import read_presence, run_live_turn
from .meta_source import SYSTEM_PROMPT
from .studio_room import StudioRoom
from .studio_sink import Respond2Transport, StudioSink

TURN = 1793138465796
PI_TZ = ZoneInfo("America/New_York")
RID = "1793-7-7"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def sse_content(text: str) -> str:
    return "data: " + json.dumps(
        {"choices": [{"delta": {"content": text}}]})


DONE = "data: [DONE]"


def make_respond_runner(rid=RID):
    seen = []

    def runner(argv, env):
        seen.append((argv, env))
        return FakeProc(
            0, f"LAUNCHED id={rid} turn={env.get('RESPOND_TURN')} pid=99\n")

    return runner, seen


def make_wall_runner():
    calls = []

    def runner(argv, stdin_text=None):
        calls.append(json.loads(stdin_text))
        return FakeProc(0, '{"applied": true, "rc": 0}\n')

    return runner, calls


def sayseq_text(rid=RID, n=1):
    now = datetime.now(PI_TZ).strftime("%H:%M:%S.000")
    lines = []
    for i in range(n):
        lines.append(f"{now} PLAY seq-{rid}-{i}.ogg")
        lines.append(f"{now} PLAY_DONE seq-{rid}-{i}.ogg")
    return "\n".join(lines) + "\n"


def make_sink(respond_runner):
    return StudioSink(
        Respond2Transport(script="/x/respond2.sh", runner=respond_runner),
        turn=TURN)


def base_kwargs(**over):
    kw = dict(
        wall_runner=make_wall_runner()[0],
        sayseq_reader=lambda: sayseq_text(),
        gate_reader=lambda: "",
        wall_reader=lambda: {"scene": "answer", "title": "",
                             "lines": [], "turn": TURN, "phase": 4},
        presence_reader=lambda: "",
        skip_record=True,
        publish_wait_s=5,
    )
    kw.update(over)
    return kw


class SwitchOffTests(unittest.TestCase):
    def test_switch_off_records_and_never_builds_a_source(self):
        def exploding_transport(url, body, headers):
            raise AssertionError("no API call may happen with the switch off")

        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "classic"}):
            record = run_live_turn(
                "¿qué clima hace?", TURN,
                api_transport=exploding_transport, **base_kwargs())
        self.assertEqual(record["mode"], "switch_off")
        self.assertEqual(record["playback"], "skipped")
        self.assertEqual(record["turn_id"], TURN)
        self.assertFalse(record["presence_loaded"])


class PresenceTests(unittest.TestCase):
    def run_answer(self, presence):
        bodies = []

        def api_transport(url, body, headers):
            bodies.append(body)
            return iter([sse_content("Hola, todo bien."), DONE])

        respond_runner, _ = make_respond_runner()
        record = run_live_turn(
            "hola", TURN,
            api_transport=api_transport,
            sink=make_sink(respond_runner),
            presence_reader=lambda: presence,
            **{k: v for k, v in base_kwargs().items()
               if k != "presence_reader"})
        return record, bodies

    def test_presence_appended_to_the_sent_system_prompt(self):
        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "bridge"}):
            record, bodies = self.run_answer("PRESENCE — test block.")
        system = bodies[0]["messages"][0]["content"]
        self.assertTrue(system.startswith(SYSTEM_PROMPT))
        self.assertTrue(system.endswith("PRESENCE — test block."))
        self.assertTrue(record["presence_loaded"])

    def test_absent_presence_sends_the_bare_prompt(self):
        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "bridge"}):
            record, bodies = self.run_answer("")
        self.assertEqual(bodies[0]["messages"][0]["content"], SYSTEM_PROMPT)
        self.assertFalse(record["presence_loaded"])

    def test_read_presence_from_the_pi(self):
        def runner(argv):
            self.assertIn("cat /home/ztdgtll/.musez-presence.md", argv[-1])
            return FakeProc(0, "PRESENCE body\n")

        self.assertEqual(read_presence(runner), "PRESENCE body")
        self.assertEqual(
            read_presence(lambda argv: FakeProc(1, "", "no file")), "")


class FakeSource:
    """Canned bridge events, MetaSource-shaped (stream_turn + last_turn)."""

    def __init__(self, events):
        self._events = events
        self.last_turn = {"model_final": "fake-model", "escalated": False,
                          "rounds": 1, "round_marks": [],
                          "prompt_tokens": 10, "completion_tokens": 5}

    def stream_turn(self, user_text, room_tools):
        for event in self._events:
            yield event


PLAN_BLOCKS = [
    {"start": "08:00", "end": "11:00", "label": "Desayuno tranquilo",
     "kind": "free"},
    {"start": "11:00", "end": "13:00",
     "label": "Compras en el supermercado", "kind": "activity"},
    {"start": "13:00", "end": "16:00", "label": "Pileta",
     "kind": "activity"},
    {"start": "16:00", "end": "19:00", "label": "Lluvia", "kind": "rain"},
]


class PublishTests(unittest.TestCase):
    def test_plan_turn_publishes_the_plan_scene(self):
        source = FakeSource([
            ("tool_call", "set_day_plan",
             {"title": "Saturday plan", "blocks": PLAN_BLOCKS}),
            ("token", "Listo tu sábado."),
        ])
        respond_runner, _ = make_respond_runner()
        sink = make_sink(respond_runner)
        room = StudioRoom(TURN, speak=sink.say)
        wall_runner, wall_calls = make_wall_runner()
        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "bridge"}):
            record = run_live_turn(
                "planificá mi sábado", TURN,
                source=source, room=room, sink=sink,
                **base_kwargs(wall_runner=wall_runner))
        self.assertEqual(record["wall_outcome"], "plan")
        self.assertEqual(record["playback"], "played")
        self.assertEqual(record["wall_publish"], {"applied": True, "rc": 0})
        self.assertEqual(len(wall_calls), 1)
        payload = wall_calls[0]
        self.assertEqual(payload["turn"], TURN)
        self.assertEqual(payload["phase"], 5)
        self.assertTrue(payload["sticky"])
        kv = payload["kv"]
        self.assertEqual(kv["scene"], "plan")
        self.assertEqual(kv["title"], "Saturday plan")
        self.assertEqual(kv["plan"]["blocks"], PLAN_BLOCKS)
        # plan_card's contract: first activity + the rain block.
        self.assertEqual(
            kv["lines"],
            ["Compras en el supermercado · 11:00–13:00",
             "Lluvia · 16:00–19:00"])

    def test_card_turn_publishes_the_answer_scene(self):
        source = FakeSource([("token", "Hoy hace sol en Tamarac.")])
        respond_runner, _ = make_respond_runner()
        sink = make_sink(respond_runner)
        room = StudioRoom(TURN, speak=sink.say)
        wall_runner, wall_calls = make_wall_runner()
        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "bridge"}):
            record = run_live_turn(
                "¿qué clima hace?", TURN,
                source=source, room=room, sink=sink,
                **base_kwargs(wall_runner=wall_runner))
        self.assertEqual(record["wall_publish"], {"applied": True, "rc": 0})
        kv = wall_calls[0]["kv"]
        self.assertEqual(kv["scene"], "answer")
        self.assertEqual(kv["lines"], ["Hoy hace sol en Tamarac."])
        self.assertNotIn("plan", kv)

    def test_no_publish_when_playback_is_never_observed(self):
        source = FakeSource([("token", "Respuesta.")])
        respond_runner, _ = make_respond_runner()
        sink = make_sink(respond_runner)
        room = StudioRoom(TURN, speak=sink.say)
        wall_runner, wall_calls = make_wall_runner()
        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "bridge"}):
            record = run_live_turn(
                "hola", TURN,
                source=source, room=room, sink=sink,
                **base_kwargs(wall_runner=wall_runner,
                              sayseq_reader=lambda: "",
                              publish_wait_s=0.2))
        self.assertEqual(record["playback"], "not_observed")
        self.assertEqual(record["wall_publish"],
                         {"skipped": "last segment play not observed"})
        self.assertEqual(wall_calls, [])


if __name__ == "__main__":
    unittest.main()
