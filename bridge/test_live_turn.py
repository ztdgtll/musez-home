"""Offline tests for the live runner — fakes only, no Pi, no API.

Pins the wiring contracts the room depends on:

- the switch gates the live path before any source exists;
- the presence block (read from the Pi at runtime) is appended to
  the system prompt the source actually sends — and the bare prompt
  is used, byte-identical, when no presence loads;
- after the answer's last segment plays, the runner publishes the
  turn's final wall state once (phase 5, not sticky): the plan
  payload for a plan turn, the composed card otherwise — and
  schedules the wall's return home 15 s later (wall_apply's
  return_home op, ownership re-checked on the Pi at fire time);
- when playback is never observed, nothing is published and the
  record says so.

Run from the repo root:  python3 -m unittest bridge.test_live_turn -v
"""

from __future__ import annotations

import json
import os
import time
import unittest
from datetime import date, datetime, timedelta
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


def _inert_scheduler(delay, fn):
    return None


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
        home_scheduler=_inert_scheduler,
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

    def stream_turn(self, user_text, room_tools, lang=None):
        self.lang_seen = lang
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
        scheduled = []
        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "bridge"}):
            record = run_live_turn(
                "planificá mi sábado", TURN,
                source=source, room=room, sink=sink,
                **base_kwargs(
                    wall_runner=wall_runner,
                    home_scheduler=lambda d, f: scheduled.append((d, f))))
        self.assertEqual(record["wall_outcome"], "plan")
        self.assertEqual(record["playback"], "played")
        self.assertEqual(record["wall_publish"], {"applied": True, "rc": 0})
        self.assertEqual(len(wall_calls), 1)
        payload = wall_calls[0]
        self.assertEqual(payload["turn"], TURN)
        self.assertEqual(payload["phase"], 5)
        self.assertFalse(payload.get("sticky"))
        kv = payload["kv"]
        self.assertEqual(kv["scene"], "plan")
        self.assertEqual(kv["title"], "Saturday plan")
        self.assertEqual(kv["plan"]["blocks"], PLAN_BLOCKS)
        # plan_card's contract: first activity + the rain block.
        self.assertEqual(
            kv["lines"],
            ["Compras en el supermercado · 11:00–13:00",
             "Lluvia · 16:00–19:00"])
        # The result holds 15 s, then the wall goes home on its own.
        self.assertEqual(record["home_return"],
                         {"scheduled_s": 15.0, "expect_turn": TURN})
        self.assertEqual([d for d, _ in scheduled], [15.0])
        scheduled[0][1]()  # the timer fires: the home-return op is sent
        self.assertEqual(wall_calls[-1],
                         {"op": "return_home", "expect_turn": TURN})

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
        self.assertFalse(wall_calls[0].get("sticky"))
        self.assertIsNotNone(record["home_return"])
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
        self.assertIsNone(record["home_return"])
        self.assertEqual(wall_calls, [])

    def test_ack_without_an_answer_still_returns_the_wall_home(self):
        # The turn escalated and spoke its acknowledgement, then
        # produced no answer sentences at all: no answer session
        # exists to walk the wall home, so the runner schedules the
        # ownership-checked return itself — and publishes nothing.
        source = FakeSource([("ack", "Dame un momento, armo tu sábado.")])
        respond_runner, _ = make_respond_runner()
        sink = make_sink(respond_runner)
        room = StudioRoom(TURN, speak=sink.say)
        wall_runner, wall_calls = make_wall_runner()
        scheduled = []
        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "bridge"}):
            record = run_live_turn(
                "planificá mi sábado", TURN,
                source=source, room=room, sink=sink,
                **base_kwargs(
                    wall_runner=wall_runner,
                    home_scheduler=lambda d, f: scheduled.append((d, f))))
        self.assertIsNotNone(sink.ack)
        self.assertTrue(sink.ack["ok"])
        self.assertIsNone(record["rid"])
        self.assertIsNone(record["wall_publish"])
        self.assertEqual(record["home_return"],
                         {"scheduled_s": 15.0, "expect_turn": TURN})
        self.assertEqual([d for d, _ in scheduled], [15.0])
        scheduled[0][1]()  # the timer fires: the home-return op is sent
        self.assertEqual(wall_calls,
                         [{"op": "return_home", "expect_turn": TURN}])


def _daily_fixture():
    today = date.today()
    return {
        "time": [(today + timedelta(days=i)).isoformat()
                 for i in range(3)],
        "temperature_2m_max": [84.0, 83.0, 82.0],
        "temperature_2m_min": [73.0, 72.0, 71.0],
        "precipitation_probability_max": [80, 10, 10],
        "weathercode": [61, 0, 0],
    }


def _hourly_fixture():
    # Dry until 13:00, rain band 13:00–18:00, dry again after:
    # the composed plan must draw exactly that shape.
    today = date.today().isoformat()
    return {
        "time": [f"{today}T{h:02d}:00" for h in range(24)],
        "precipitation_probability":
            [80 if 13 <= h < 18 else 10 for h in range(24)],
    }


class SlowSource:
    """A source whose stream only starts after a delay — the
    deadline tests' stand-in for a slow deep model."""

    def __init__(self, delay, events):
        self._delay = delay
        self._events = events
        self.last_turn = {"model_final": "fake-model",
                          "escalated": False, "rounds": 1,
                          "round_marks": [], "prompt_tokens": 10,
                          "completion_tokens": 5}

    def stream_turn(self, user_text, room_tools, lang=None):
        time.sleep(self._delay)
        yield from self._events


class PlanRoutingTests(unittest.TestCase):
    """The runner's plan backstops (field evidence: turn
    1793587782994 ended as a weather card because the deadline
    cut the turn mid-plan and composition read partial state)."""

    def test_plan_turn_without_a_plan_falls_back_to_the_composed_plan(self):
        # The source fetched weather and answered in prose, never
        # building a plan: the runner composes the plan from the
        # turn's own forecast instead of publishing the weather
        # card the partial state offers.
        source = FakeSource([
            ("tool_call", "get_weather", {}),
            ("token", "Suena bien tu sábado."),
        ])
        respond_runner, _ = make_respond_runner()
        sink = make_sink(respond_runner)
        room = StudioRoom(TURN, speak=sink.say,
                          weather_runner=lambda: _daily_fixture(),
                          hourly_runner=lambda: _hourly_fixture())
        wall_runner, wall_calls = make_wall_runner()
        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "bridge"}):
            record = run_live_turn(
                "planificá mi sábado", TURN,
                source=source, room=room, sink=sink,
                **base_kwargs(wall_runner=wall_runner))
        self.assertTrue(record["plan_requested"])
        self.assertEqual(record["plan_fallback"],
                         "composed_from_weather")
        self.assertEqual(record["wall_outcome"], "plan")
        self.assertIsNotNone(record["plan"])
        kinds = [b["kind"] for b in record["plan"]["blocks"]]
        self.assertEqual(kinds, ["activity", "free", "rain", "free"])
        rain = record["plan"]["blocks"][2]
        self.assertEqual((rain["start"], rain["end"]),
                         ("13:00", "18:00"))
        self.assertEqual(record["wall_publish"],
                         {"applied": True, "rc": 0})
        self.assertEqual(wall_calls[0]["kv"]["scene"], "plan")

    def test_plan_turn_recovered_whole_after_the_deadline(self):
        # The plan turn crosses the deadline but finishes inside
        # the grace: it is answered whole — real marks, real
        # sentences, no deadline error.
        source = SlowSource(1.0, [("token", "Plan recuperado.")])
        respond_runner, _ = make_respond_runner()
        sink = make_sink(respond_runner)
        room = StudioRoom(TURN, speak=sink.say)
        wall_runner, wall_calls = make_wall_runner()
        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "bridge"}):
            record = run_live_turn(
                "planificá mi sábado", TURN,
                source=source, room=room, sink=sink, deadline_s=0.3,
                **base_kwargs(wall_runner=wall_runner))
        self.assertTrue(record["deadline_exceeded"])
        self.assertTrue(record["deadline_recovered"])
        self.assertIsNone(record["turn_error"])
        self.assertEqual(record["sentences"], ["Plan recuperado."])
        self.assertEqual(record["playback"], "played")
        self.assertEqual(record["wall_publish"],
                         {"applied": True, "rc": 0})

    def test_slow_non_plan_turn_still_times_out_as_before(self):
        # No grace for ordinary turns: the deadline discards the
        # turn exactly as it did before the plan backstops.
        source = SlowSource(1.0, [("token", "Tarde.")])
        respond_runner, _ = make_respond_runner()
        sink = make_sink(respond_runner)
        room = StudioRoom(TURN, speak=sink.say)
        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "bridge"}):
            record = run_live_turn(
                "hola", TURN,
                source=source, room=room, sink=sink, deadline_s=0.3,
                **base_kwargs())
        self.assertTrue(record["deadline_exceeded"])
        self.assertFalse(record["deadline_recovered"])
        self.assertTrue(record["turn_error"].startswith("deadline:"))
        self.assertFalse(record["plan_requested"])
        self.assertIsNone(record["plan_fallback"])


class PollResilienceTests(unittest.TestCase):
    def test_a_dead_poll_reader_cannot_kill_the_turn(self):
        # Room evidence 2026-11-01 (turn 1793588978983): the
        # answer had already played when the closing phase went
        # on polling over a degraded ssh route — a poll read is
        # no news, never a fatal error.
        def api_transport(url, body, headers):
            return iter([sse_content("Hola, todo bien."), DONE])

        respond_runner, _ = make_respond_runner()
        sink = make_sink(respond_runner)

        def dead_reader():
            raise RuntimeError("ssh route down")

        record = run_live_turn(
            "¿qué clima hace?", TURN,
            api_transport=api_transport, sink=sink,
            **base_kwargs(sayseq_reader=dead_reader,
                          gate_reader=dead_reader,
                          publish_wait_s=0.3))
        self.assertEqual(record["playback"], "not_observed")
        self.assertIsNone(record["turn_error"])
        self.assertEqual(record["wall_publish"],
                         {"skipped": "last segment play not observed"})


if __name__ == "__main__":
    unittest.main()
