"""Offline tests for the studio integration — fakes only, no Pi.

Pins the contracts the live pieces rely on:

- the sink speaks the turn's sentences IN ORDER through the one-shot
  transport, once, threading the turn id;
- the room writes wall scenes through the Pi helper at the tool phase,
  reports guard rejections (setstate rc 4) in the tool result and
  never retries a dropped write;
- the Pi-side helper builds a setstate argv list (content as data,
  never a shell string) and whitelists the keys the bridge may touch.

Run from the repo root:  python3 -m unittest bridge.test_studio -v
"""

from __future__ import annotations

import unittest

from . import wall_apply
from .studio_room import StudioRoom, condition_es
from .studio_sink import Respond2Transport, StudioSink, TransportError
from .studio_turn import find_first_play, pick_turn, playback_status


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class SinkTests(unittest.TestCase):
    def test_flush_speaks_sentences_in_order_once(self):
        calls = []

        def transport(title, lines, text, turn):
            calls.append((title, lines, text, turn))
            return {"rid": "1793-1-2"}

        sink = StudioSink(transport, turn=1793000000001, title="T", lines=["a", "b"])
        sink.say("Primera oración.")
        sink.say("Segunda oración.")
        receipt = sink.flush()
        self.assertEqual(receipt["rid"], "1793-1-2")
        self.assertEqual(len(calls), 1)
        title, lines, text, turn = calls[0]
        self.assertEqual((title, lines), ("T", ["a", "b"]))
        self.assertEqual(text, "Primera oración. Segunda oración.")
        self.assertEqual(turn, 1793000000001)
        # A second flush does not re-speak the turn.
        self.assertIs(sink.flush(), receipt)
        self.assertEqual(len(calls), 1)

    def test_flush_without_sentences_is_a_noop(self):
        sink = StudioSink(lambda *a: self.fail("transport must not run"))
        self.assertEqual(sink.flush()["rid"], None)

    def test_transport_threads_turn_and_parses_launch(self):
        seen = {}

        def runner(argv, env):
            seen["argv"] = argv
            seen["env"] = env
            return FakeProc(0, "LAUNCHED id=1793-9-9 turn=1793000000001 pid=4242\n")

        transport = Respond2Transport(script="/x/respond2.sh", runner=runner)
        receipt = transport("Title", ["l1"], "Hola.", 1793000000001)
        self.assertEqual(receipt["rid"], "1793-9-9")
        self.assertEqual(seen["argv"], ["/x/respond2.sh", "Title", "l1", "", "Hola."])
        self.assertEqual(seen["env"]["RESPOND_TURN"], "1793000000001")

    def test_transport_failure_is_loud(self):
        transport = Respond2Transport(
            script="/x/respond2.sh",
            runner=lambda argv, env: FakeProc(1, "", "boom"),
        )
        with self.assertRaises(TransportError):
            transport("T", [], "Hola.", None)


class RoomTests(unittest.TestCase):
    def make_room(self, wall_reply):
        calls = []

        def wall(payload):
            calls.append(payload)
            return wall_reply

        room = StudioRoom(1793000000001, wall_runner=wall,
                          weather_runner=lambda: self.fail("no weather here"))
        return room, calls

    def test_wall_scene_writes_at_tool_phase(self):
        room, calls = self.make_room({"applied": True, "rc": 0})
        result = room.set_wall_scene("WEATHER", "Tamarac · 29°C", "Sin lluvia")
        self.assertEqual(result, {"ok": True, "shown": "WEATHER"})
        (payload,) = calls
        self.assertEqual(payload["turn"], 1793000000001)
        self.assertEqual(payload["phase"], 3)
        self.assertEqual(payload["kv"]["scene"], "answer")
        self.assertEqual(payload["kv"]["lines"], ["Tamarac · 29°C", "Sin lluvia"])
        self.assertEqual(room.wall_scenes[0]["title"], "WEATHER")

    def test_guard_rejection_is_reported_and_not_retried(self):
        room, calls = self.make_room({"applied": False, "rc": 4})
        result = room.set_wall_scene("WEATHER", "x")
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["rejected"], True)
        self.assertEqual(result["rc"], 4)
        self.assertEqual(len(calls), 1)  # exactly one attempt, no retry
        self.assertEqual(room.wall_scenes, [])

    def test_weather_maps_the_daily_block(self):
        daily = {"time": ["2026-10-27"], "temperature_2m_max": [84.6],
                 "temperature_2m_min": [73.7], "precipitation_probability_max": [44],
                 "weathercode": [65]}
        room = StudioRoom(1, wall_runner=lambda p: {"applied": True, "rc": 0},
                          weather_runner=lambda: daily)
        w = room.get_weather()
        self.assertEqual(w["temp_max_f"], 84.6)
        self.assertEqual(w["condition"], "lluvia fuerte")
        self.assertEqual(room.weather, w)

    def test_say_routes_into_the_speech_sink(self):
        spoken = []
        room = StudioRoom(1, wall_runner=lambda p: {"applied": True, "rc": 0},
                          weather_runner=lambda: {}, speak=spoken.append)
        self.assertEqual(room.say("Un momento."), {"ok": True})
        self.assertEqual(spoken, ["Un momento."])

    def test_condition_vocabulary(self):
        self.assertEqual(condition_es(0), "despejado")
        self.assertEqual(condition_es(999), "sin datos")


class WallApplyTests(unittest.TestCase):
    def test_argv_is_a_list_with_content_as_data(self):
        argv = wall_apply.build_argv({
            "turn": 1793000000001, "phase": 3,
            "kv": {"scene": "answer", "title": "Nike $86 — \"x\"",
                   "lines": ["a", "b"], "live": False},
        })
        self.assertIn("--turn=1793000000001", argv)
        self.assertIn("--phase=3", argv)
        self.assertIn('lines=["a", "b"]', argv)
        self.assertIn("live=false", argv)
        self.assertIn('title=Nike $86 — "x"', argv)  # one argv element, untouched

    def test_unknown_keys_are_refused_before_any_write(self):
        ran = []
        with self.assertRaises(ValueError):
            wall_apply.apply({"turn": 1, "phase": 3, "kv": {"floor": 9}},
                             runner=lambda a: ran.append(a) or FakeProc(0))
        self.assertEqual(ran, [])

    def test_apply_reports_setstate_exit_code(self):
        result = wall_apply.apply(
            {"turn": 1, "phase": 4, "kv": {"scene": "answer"}},
            runner=lambda a: FakeProc(4),
        )
        self.assertEqual(result, {"applied": False, "rc": 4})


class PickTurnTests(unittest.TestCase):
    def test_transcripts_route_to_canned_turns(self):
        self.assertEqual(pick_turn("¿qué clima hace?"), "weather")
        self.assertEqual(pick_turn("poné el clima en la pared"), "wall")
        self.assertEqual(pick_turn("¿qué es una Raspberry Pi?"), "knowledge")


class PlaybackStatusTests(unittest.TestCase):
    RID = "1793133077664-7956-180172280"

    def test_played_when_sayseq_logged_the_first_segment(self):
        seq = f"16:31:27.100 PLAY seq-{self.RID}-0.ogg at_offset=.21\n"
        self.assertEqual(playback_status(self.RID, seq, ""), "played")

    def test_suppressed_off_when_the_playgate_refused_it(self):
        gate = f"16:31:27 PLAY_SUPPRESSED_OFF owner=resp2-{self.RID} kind=sayseq\n"
        self.assertEqual(playback_status(self.RID, "", gate), "suppressed_off")

    def test_pending_when_nothing_observed(self):
        self.assertEqual(playback_status(self.RID, "", ""), "pending")
        other = "16:31:27 PLAY_SUPPRESSED_OFF owner=resp2-999-1-2 kind=sayseq\n"
        self.assertEqual(playback_status(self.RID, "", other), "pending")

    def test_first_play_parses_against_pi_local_start(self):
        from datetime import datetime

        t_start = datetime(2026, 10, 27, 18, 1, 5, 796000)
        log = f"18:01:20.837 PLAY seq-{self.RID}-0.ogg at_offset=.230\n"
        play = find_first_play(self.RID, t_start, log)
        self.assertIsNotNone(play)
        self.assertAlmostEqual((play - t_start).total_seconds(), 15.041, places=2)


if __name__ == "__main__":
    unittest.main()
