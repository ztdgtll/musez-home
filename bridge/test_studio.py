"""Offline tests for the studio integration — fakes only, no Pi.

Pins the contracts the live pieces rely on:

- the sink speaks the turn's sentences IN ORDER through the one-shot
  transport, once, threading the turn id — and that dispatch is the
  turn's ONLY wall write, carrying the one final scene;
- the room records wall scenes as validated CANDIDATES (it never
  writes the wall mid-turn), rejects scenes that break the design
  budget with the limit named, and keeps every rejection for the
  turn record;
- the Pi-side helper builds a setstate argv list (content as data,
  never a shell string), whitelists the keys the bridge may touch,
  and enforces the same scene budget before any write.

Run from the repo root:  python3 -m unittest bridge.test_studio -v
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from . import studio_turn, wall_apply
from .studio_room import StudioRoom, condition_es
from .studio_sink import (
    MAX_TEXT_CHARS,
    TRUNCATION_SENTENCE,
    LaunchUnknownError,
    Respond2Transport,
    StudioSink,
    TransportError,
)
from .studio_turn import (
    compose_scene,
    find_first_play,
    pick_turn,
    playback_status,
)
from .tools import DryRoom, dispatch
from .turn import run_turn


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

    def test_set_scene_rejects_more_than_two_lines(self):
        # The transport speaks exactly two lines; a third must be
        # refused here, never silently dropped downstream.
        sink = StudioSink(lambda *a: {"rid": None})
        with self.assertRaises(ValueError) as ctx:
            sink.set_scene("T", ["a", "b", "c"])
        self.assertIn("2", str(ctx.exception))
        sink.set_scene("T", ["a", "b"])  # two lines are fine
        self.assertEqual(sink.lines, ["a", "b"])

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
    def make_room(self):
        return StudioRoom(
            1793000000001,
            weather_runner=lambda: self.fail("no weather here"))

    def test_wall_scene_records_a_candidate_without_writing(self):
        # The room has no wall channel at all: set_wall_scene can only
        # record the candidate for the flush-time composition. There
        # is no mid-turn (phase 3) write to observe or to fake.
        room = self.make_room()
        result = room.set_wall_scene("WEATHER", "Tamarac · 29°C", "Sin lluvia")
        self.assertEqual(result, {"ok": True, "queued": "WEATHER"})
        self.assertEqual(room.wall_scenes,
                         [{"title": "WEATHER",
                           "lines": ["Tamarac · 29°C", "Sin lluvia"]}])
        self.assertEqual(room.wall_rejections, [])

    def test_invalid_scene_is_rejected_recorded_and_not_queued(self):
        room = self.make_room()
        result = room.set_wall_scene("T" * 25, "x")
        self.assertEqual(result["ok"], False)
        self.assertIn("24", result["error"])  # the limit is named
        self.assertEqual(room.wall_scenes, [])  # never queued
        (rejection,) = room.wall_rejections  # and never lost
        self.assertEqual(rejection["title"], "T" * 25)
        self.assertEqual(rejection["error"], result["error"])

    def test_weather_maps_the_daily_block(self):
        daily = {"time": ["2026-10-27"], "temperature_2m_max": [84.6],
                 "temperature_2m_min": [73.7], "precipitation_probability_max": [44],
                 "weathercode": [65]}
        room = StudioRoom(1,
                          weather_runner=lambda: daily)
        w = room.get_weather()
        self.assertEqual(w["temp_max_f"], 84.6)
        self.assertEqual(w["condition"], "lluvia fuerte")
        self.assertEqual(room.weather, w)

    def test_say_routes_into_the_speech_sink(self):
        spoken = []
        room = StudioRoom(1,
                          weather_runner=lambda: {}, speak=spoken.append)
        self.assertEqual(room.say("Un momento."), {"ok": True})
        self.assertEqual(spoken, ["Un momento."])

    def test_condition_vocabulary(self):
        self.assertEqual(condition_es(0), "despejado")
        self.assertEqual(condition_es(999), "sin datos")


class DispatchContractTests(unittest.TestCase):
    def test_unknown_tool_returns_an_error_result(self):
        result = dispatch(DryRoom(), "open_the_pod_bay_doors", {})
        self.assertFalse(result["ok"])
        self.assertIn("unknown tool", result["error"])

    def test_room_failure_returns_an_error_result(self):
        class BoomRoom(DryRoom):
            def set_wall_scene(self, title, line1="", line2=""):
                raise RuntimeError("wall exploded")

        result = dispatch(BoomRoom(), "set_wall_scene", {"title": "X"})
        self.assertFalse(result["ok"])
        self.assertIn("wall exploded", result["error"])

    def test_non_object_arguments_return_an_error_result(self):
        result = dispatch(DryRoom(), "get_weather", ["today"])
        self.assertFalse(result["ok"])

    def test_happy_path_is_unchanged(self):
        result = dispatch(DryRoom(), "get_weather", {})
        self.assertEqual(result["condition"], "soleado")


class WeatherForecastTests(unittest.TestCase):
    DAILY3 = {
        "time": ["2026-10-29", "2026-10-30", "2026-10-31"],
        "temperature_2m_max": [84.6, 85.0, 79.3],
        "temperature_2m_min": [73.7, 72.1, 68.4],
        "precipitation_probability_max": [44, 10, 90],
        "weathercode": [65, 0, 61],
    }

    def make_room(self, daily=None, hourly=None):
        return StudioRoom(
            1,
            weather_runner=lambda: self.DAILY3 if daily is None else daily,
            hourly_runner=hourly)

    def test_forecast_is_labeled_by_date_and_today_stays_on_top(self):
        room = self.make_room()
        w = room.get_weather()
        # Backward compatible: today's summary at the top level.
        self.assertEqual(w["temp_max_f"], 84.6)
        self.assertEqual(w["condition"], "lluvia fuerte")
        self.assertEqual([d["date"] for d in w["forecast"]],
                         ["2026-10-29", "2026-10-30", "2026-10-31"])
        saturday = w["forecast"][2]
        self.assertEqual(saturday["condition"], "lluvia leve")
        self.assertEqual(saturday["precip_prob_max"], 90)

    def test_hourly_precipitation_for_the_requested_date(self):
        hourly = {
            "time": ["2026-10-31T08:00", "2026-10-31T09:00",
                     "2026-11-01T08:00"],
            "precipitation_probability": [10, 80, 5],
        }
        room = self.make_room(hourly=lambda: hourly)
        w = room.get_weather(date="2026-10-31")
        self.assertEqual(w["hourly_date"], "2026-10-31")
        self.assertEqual(w["hourly_precipitation"], [
            {"time": "08:00", "precip_probability": 10},
            {"time": "09:00", "precip_probability": 80},
        ])

    def test_date_outside_the_window_is_an_error_result(self):
        room = self.make_room()
        result = room.get_weather(date="2027-01-01")
        self.assertFalse(result["ok"])
        self.assertIsNone(room.weather)

    def test_null_precip_today_is_an_error_result_not_a_crash(self):
        daily = dict(self.DAILY3)
        daily["precipitation_probability_max"] = [None, 10, 90]
        room = self.make_room(daily=daily)
        result = room.get_weather()
        self.assertFalse(result["ok"])
        self.assertIsNone(room.weather)  # compose_scene will fall back

    def test_missing_block_key_is_an_error_result(self):
        daily = {k: v for k, v in self.DAILY3.items() if k != "weathercode"}
        room = self.make_room(daily=daily)
        self.assertFalse(room.get_weather()["ok"])

    def test_feed_failure_is_an_error_result(self):
        def boom():
            raise RuntimeError("curl died")

        room = StudioRoom(1,
                          weather_runner=boom)
        result = room.get_weather()
        self.assertFalse(result["ok"])
        self.assertIn("curl died", result["error"])


class ComposeSceneTests(unittest.TestCase):
    def make_room(self):
        return StudioRoom(1,
                          weather_runner=lambda: {})

    def test_precip_none_composes_without_the_rain_part(self):
        room = self.make_room()
        room.weather = {"condition": "nublado", "temp_max_f": 83.0,
                        "temp_min_f": 74.0, "precip_prob_max": None}
        sink = StudioSink(lambda *a: {"rid": None})
        title, lines = compose_scene("weather", room, sink)
        self.assertEqual(title, "Tamarac weather")
        self.assertEqual(lines[0], "Tamarac · nublado")
        self.assertIn("Máx 83 °F", lines[1])
        self.assertNotIn("Lluvia", lines[1])


class SceneValidationTests(unittest.TestCase):
    """The design budget, enforced at the room: every violation is a
    tool error naming the limit, recorded on the room, never queued."""

    def make_room(self):
        return StudioRoom(1, weather_runner=lambda: {})

    def test_overlong_line_is_rejected_with_the_limit_named(self):
        room = self.make_room()
        result = room.set_wall_scene("WEATHER", "x" * 49)
        self.assertFalse(result["ok"])
        self.assertIn("48", result["error"])
        self.assertEqual(room.wall_scenes, [])
        self.assertEqual(len(room.wall_rejections), 1)

    def test_newlines_are_rejected_in_title_and_lines(self):
        room = self.make_room()
        self.assertFalse(room.set_wall_scene("TWO\nLINES")["ok"])
        self.assertFalse(room.set_wall_scene("FINE", "one\ntwo")["ok"])
        self.assertEqual(room.wall_scenes, [])
        self.assertEqual(len(room.wall_rejections), 2)
        for rejection in room.wall_rejections:
            self.assertIn("newline", rejection["error"])

    def test_rejection_does_not_block_a_later_valid_scene(self):
        room = self.make_room()
        self.assertFalse(room.set_wall_scene("T" * 25)["ok"])
        result = room.set_wall_scene("WEATHER", "Tamarac · soleado")
        self.assertEqual(result, {"ok": True, "queued": "WEATHER"})
        self.assertEqual(len(room.wall_scenes), 1)
        self.assertEqual(len(room.wall_rejections), 1)  # still on record


class SingleFinalSceneTests(unittest.TestCase):
    """One turn, one wall write: the flush-time transport dispatch.

    The counting transport stands in for the room's response path —
    its single call per turn IS the wall write (the player applies
    the title+lines it carries). Nothing may reach it before flush.
    """

    DAILY = {"time": ["2026-10-27"], "temperature_2m_max": [84.6],
             "temperature_2m_min": [73.7], "precipitation_probability_max": [44],
             "weathercode": [65]}

    def make_sink(self):
        calls = []

        def transport(title, lines, text, turn):
            calls.append((title, lines, text, turn))
            return {"rid": "1793-1-2"}

        return StudioSink(transport, turn=99), calls

    def finish_turn(self, name, room, sink):
        title, lines = compose_scene(name, room, sink)
        sink.set_scene(title, lines)
        return sink.flush()

    def test_tool_candidate_ships_once_at_flush_and_never_before(self):
        room = StudioRoom(99, weather_runner=lambda: self.fail("no weather"))
        sink, calls = self.make_sink()
        run_turn(iter([
            ("tool_call", "set_wall_scene",
             {"title": "WEATHER", "line1": "Tamarac · soleado",
              "line2": "Sin lluvia"}),
            ("token", "Listo, te lo puse en la pared."),
        ]), room, sink)
        # The tool call recorded a candidate; nothing was dispatched
        # mid-turn — the wall has not moved yet.
        self.assertEqual(calls, [])
        self.assertEqual(len(room.wall_scenes), 1)
        receipt = self.finish_turn("wall", room, sink)
        self.assertEqual(receipt["rid"], "1793-1-2")
        (shipped,) = calls  # exactly one write for the whole turn
        title, lines, text, turn = shipped
        self.assertEqual(title, "WEATHER")
        self.assertEqual(lines, ["Tamarac · soleado", "Sin lluvia"])
        self.assertEqual(text, "Listo, te lo puse en la pared.")
        self.assertEqual(turn, 99)

    def test_weather_composition_ships_once_at_flush(self):
        room = StudioRoom(99, weather_runner=lambda: self.DAILY)
        sink, calls = self.make_sink()
        run_turn(iter([
            ("tool_call", "get_weather", {}),
            ("token", "Ahora hay 29 grados y está soleado."),
        ]), room, sink)
        self.assertEqual(calls, [])
        self.finish_turn("weather", room, sink)
        (shipped,) = calls
        title, lines, _, _ = shipped
        self.assertEqual(title, "Tamarac weather")
        self.assertEqual(lines[0], "Tamarac · lluvia fuerte")
        self.assertIn("Lluvia 44%", lines[1])

    def test_turn_without_a_tool_scene_derives_lines_from_its_sentences(self):
        room = StudioRoom(99, weather_runner=lambda: self.fail("no weather"))
        sink, calls = self.make_sink()
        run_turn(iter([
            ("token", "Una Raspberry Pi es una computadora completa. "),
            ("token", "En este cuarto, sostiene la pared."),
        ]), room, sink)
        self.assertEqual(room.wall_scenes, [])
        self.finish_turn("knowledge", room, sink)
        (shipped,) = calls
        title, lines, _, _ = shipped
        self.assertEqual(title, "Raspberry Pi")
        self.assertEqual(lines, [
            "Una Raspberry Pi es una computadora completa.",
            "En este cuarto, sostiene la pared.",
        ])

    def test_derived_lines_are_cut_to_the_design_budget(self):
        room = StudioRoom(99, weather_runner=lambda: self.fail("no weather"))
        sink, calls = self.make_sink()
        long_sentence = ("Una Raspberry Pi es una computadora completa "
                         "del tamaño de una tarjeta de crédito.")
        run_turn(iter([("token", long_sentence)]), room, sink)
        self.finish_turn("knowledge", room, sink)
        (_, lines, _, _) = calls[0]
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 48)


class TransportHardeningTests(unittest.TestCase):
    def test_env_excludes_the_api_key(self):
        seen = {}

        def runner(argv, env):
            seen["env"] = env
            return FakeProc(0, "LAUNCHED id=r1 turn=1 pid=7\n")

        old = os.environ.get("META_API_KEY")
        os.environ["META_API_KEY"] = "must-not-leak"
        try:
            transport = Respond2Transport(script="/x/respond2.sh", runner=runner)
            transport("T", [], "Hola.", 1)
        finally:
            if old is None:
                os.environ.pop("META_API_KEY", None)
            else:
                os.environ["META_API_KEY"] = old
        self.assertNotIn("META_API_KEY", seen["env"])
        self.assertEqual(seen["env"]["RESPOND_TURN"], "1")

    def test_timeout_becomes_a_transport_error(self):
        def runner(argv, env):
            raise subprocess.TimeoutExpired(cmd="respond2", timeout=45)

        transport = Respond2Transport(script="/x/respond2.sh", runner=runner)
        with self.assertRaises(TransportError) as ctx:
            transport("T", [], "Hola.", None)
        self.assertIn("timed out", str(ctx.exception))

    def test_unparseable_launch_is_terminal_for_the_sink(self):
        calls = []

        def transport(title, lines, text, turn):
            calls.append(text)
            raise LaunchUnknownError("no LAUNCHED line")

        sink = StudioSink(transport, turn=1)
        sink.say("Hola.")
        with self.assertRaises(LaunchUnknownError):
            sink.flush()
        # A second flush returns the terminal receipt instead of
        # dispatching the turn again (it may already be playing).
        self.assertEqual(sink.flush(), {"rid": None, "launch_unknown": True})
        self.assertEqual(len(calls), 1)

    def test_long_text_is_capped_with_a_truncation_sentence(self):
        seen = {}

        def runner(argv, env):
            seen["argv"] = argv
            return FakeProc(0, "LAUNCHED id=r1 turn=1 pid=7\n")

        transport = Respond2Transport(script="/x/respond2.sh", runner=runner)
        transport("T", [], "palabra " * 1000, None)
        text = seen["argv"][4]
        self.assertLessEqual(len(text), MAX_TEXT_CHARS)
        self.assertTrue(text.endswith(TRUNCATION_SENTENCE))


class StudioTurnMainTests(unittest.TestCase):
    def test_transport_error_still_writes_the_record(self):
        class BoomTransport:
            def __call__(self, title, lines, text, turn):
                raise TransportError("respond2 exploded")

        saved = (studio_turn.mint_turn, studio_turn.read_wall_state,
                 studio_turn.Respond2Transport)
        studio_turn.mint_turn = lambda: 424242
        studio_turn.read_wall_state = lambda: {}
        studio_turn.Respond2Transport = lambda: BoomTransport()
        try:
            with tempfile.TemporaryDirectory() as td:
                results = Path(td) / "results.jsonl"
                with redirect_stdout(io.StringIO()):
                    rc = studio_turn.main(
                        ["¿qué es una Raspberry Pi?",
                         "--results", str(results)])
                self.assertEqual(rc, 1)
                lines = results.read_text().strip().splitlines()
                self.assertEqual(len(lines), 1)
                record = json.loads(lines[0])
                self.assertEqual(record["playback"], "error")
                self.assertIn("respond2 exploded", record["turn_error"])
                self.assertEqual(record["turn_id"], 424242)
        finally:
            (studio_turn.mint_turn, studio_turn.read_wall_state,
             studio_turn.Respond2Transport) = saved

    def test_record_names_the_wall_outcome_and_rejections(self):
        class OkTransport:
            def __call__(self, title, lines, text, turn):
                return {"rid": None}

        saved = (studio_turn.mint_turn, studio_turn.read_wall_state,
                 studio_turn.Respond2Transport)
        studio_turn.mint_turn = lambda: 424243
        studio_turn.read_wall_state = lambda: {}
        studio_turn.Respond2Transport = lambda: OkTransport()
        try:
            with tempfile.TemporaryDirectory() as td:
                results = Path(td) / "results.jsonl"
                with redirect_stdout(io.StringIO()):
                    rc = studio_turn.main(
                        ["¿qué es una Raspberry Pi?",
                         "--results", str(results)])
                self.assertEqual(rc, 0)
                record = json.loads(results.read_text().strip())
                # No tool scene, no weather: the single scene shipped
                # was derived from the spoken sentences themselves.
                self.assertEqual(record["wall_outcome"], "sentences")
                self.assertEqual(record["wall_rejections"], [])
        finally:
            (studio_turn.mint_turn, studio_turn.read_wall_state,
             studio_turn.Respond2Transport) = saved


class DeadlineTests(unittest.TestCase):
    def test_slow_turn_is_reported_as_a_timeout(self):
        value, err = studio_turn._run_with_deadline(
            lambda: time.sleep(2), 0.2)
        self.assertIsNone(value)
        self.assertIsInstance(err, TimeoutError)

    def test_fast_turn_returns_its_value(self):
        value, err = studio_turn._run_with_deadline(lambda: 42, 5)
        self.assertEqual((value, err), (42, None))


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

    def test_overlong_title_is_refused_with_the_limit_named(self):
        ran = []
        with self.assertRaises(ValueError) as ctx:
            wall_apply.apply(
                {"turn": 1, "phase": 4, "kv": {"title": "T" * 25}},
                runner=lambda a: ran.append(a) or FakeProc(0))
        self.assertIn("24", str(ctx.exception))
        self.assertEqual(ran, [])  # refused before any write

    def test_scene_budget_is_enforced_for_lines(self):
        base = {"turn": 1, "phase": 4}
        for kv, limit in [
            ({"lines": ["a", "b", "c"]}, "2"),
            ({"lines": ["x" * 49]}, "48"),
            ({"lines": ["one\ntwo"]}, "newline"),
            ({"title": "TWO\nLINES"}, "newline"),
        ]:
            with self.subTest(kv=kv):
                with self.assertRaises(ValueError) as ctx:
                    wall_apply.build_argv({**base, "kv": kv})
                self.assertIn(limit, str(ctx.exception))


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
