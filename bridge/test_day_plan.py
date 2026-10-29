"""Offline tests for the day-plan infographic scene — fakes only.

Pins the contracts bridge/day_plan.py and its plumbing rely on:

- the payload validator refuses what the closed design system
  cannot display, naming the limit it broke;
- the rain band is derived from the REAL hourly probabilities
  (first hour >= 50% starts it, first hour back below ends it);
- the deterministic composer builds the demo's Saturday — morning
  activity, home before the rain — from a fixture forecast;
- a plan candidate rides the turn's ONE flush dispatch as its
  structured scene, and an invalid plan falls back to the card
  scene instead of losing the turn;
- the Pi-side helper validates the plan kind to the same limits
  before building any setstate argv.

Run from the repo root:  python3 -m unittest bridge.test_day_plan -v
"""

from __future__ import annotations

import unittest

from . import wall_apply
from .day_plan import (
    compose_day_plan,
    plan_card,
    plan_problem,
    rain_band,
)
from .studio_room import StudioRoom
from .studio_sink import StudioSink
from .studio_turn import compose_final_scene
from .tools import DryRoom, dispatch
from .turn import run_turn


def block(start, end, label, kind):
    return {"start": start, "end": end, "label": label, "kind": kind}


def plan(blocks, title="Saturday plan", date="2026-10-31"):
    return {"kind": "plan", "title": title, "date": date, "blocks": blocks}


GOOD_BLOCKS = [
    block("08:00", "12:00", "Salida a la mañana", "activity"),
    block("12:00", "13:00", "En casa", "free"),
    block("13:00", "18:00", "Lluvia", "rain"),
]


class PlanValidationTests(unittest.TestCase):
    def test_good_plan_passes(self):
        self.assertIsNone(plan_problem(plan(GOOD_BLOCKS)))

    def test_too_many_blocks_names_the_cap(self):
        blocks = [block(f"{h:02d}:00", f"{h + 1:02d}:00", "X", "free")
                  for h in range(7)]
        problem = plan_problem(plan(blocks))
        self.assertIn("6", problem)
        self.assertEqual(len(blocks), 7)  # the fixture really is over

    def test_overlong_label_names_the_limit(self):
        problem = plan_problem(plan([block("08:00", "09:00", "x" * 49,
                                           "activity")]))
        self.assertIn("48", problem)

    def test_overlong_title_names_the_limit(self):
        problem = plan_problem(plan(GOOD_BLOCKS, title="T" * 25))
        self.assertIn("24", problem)

    def test_unordered_blocks_are_refused(self):
        blocks = [block("08:00", "12:00", "A", "activity"),
                  block("11:00", "13:00", "B", "free")]  # starts inside A
        self.assertIn("ordered", plan_problem(plan(blocks)))
        blocks.reverse()  # descending is unordered too
        self.assertIsNotNone(plan_problem(plan(blocks)))

    def test_block_must_start_before_it_ends(self):
        problem = plan_problem(plan([block("12:00", "12:00", "A", "note")]))
        self.assertIn("before it ends", problem)

    def test_unknown_kind_and_bad_times_are_refused(self):
        self.assertIsNotNone(plan_problem(
            plan([block("08:00", "09:00", "A", "party")])))
        self.assertIsNotNone(plan_problem(
            plan([block("8am", "09:00", "A", "free")])))
        self.assertIsNotNone(plan_problem(
            plan([block("08:00", "09:00", "one\ntwo", "free")])))

    def test_empty_and_shapeless_plans_are_refused(self):
        self.assertIsNotNone(plan_problem(plan([])))
        self.assertIsNotNone(plan_problem({"kind": "plan"}))
        self.assertIsNotNone(plan_problem({"kind": "card", "title": "X",
                                           "blocks": GOOD_BLOCKS}))
        self.assertIsNotNone(plan_problem("not a plan"))


class RainBandTests(unittest.TestCase):
    def hourly(self, probs, start_hour=6):
        return [{"time": f"{start_hour + i:02d}:00",
                 "precip_probability": p} for i, p in enumerate(probs)]

    def test_band_starts_at_the_first_hour_at_or_above_50(self):
        # 06:00..13:00 — first >= 50 is 11:00 (55), first back below
        # is 14:00's predecessor slot at index 8 (40 at 14:00).
        hourly = self.hourly([5, 10, 20, 30, 49, 55, 80, 90, 40, 10])
        self.assertEqual(rain_band(hourly),
                         {"start": "11:00", "end": "14:00"})

    def test_no_hour_at_threshold_means_no_band(self):
        self.assertIsNone(rain_band(self.hourly([0, 10, 49, 20])))
        self.assertIsNone(rain_band([]))
        self.assertIsNone(rain_band(None))

    def test_band_runs_to_one_hour_past_the_last_entry(self):
        hourly = self.hourly([10, 60, 70], start_hour=21)
        self.assertEqual(rain_band(hourly),
                         {"start": "22:00", "end": "24:00"})

    def test_null_probability_never_starts_a_band(self):
        hourly = [{"time": "09:00", "precip_probability": None},
                  {"time": "10:00", "precip_probability": 30}]
        self.assertIsNone(rain_band(hourly))


def fixture_weather(rain_from=13, rain_to=17):
    """A get_weather-shaped dict for Saturday 2026-10-31: hourly
    probabilities at/above 80 from rain_from..rain_to inclusive,
    10 elsewhere."""
    hourly = [
        {"time": f"{h:02d}:00",
         "precip_probability": 80 if rain_from <= h <= rain_to else 10}
        for h in range(24)
    ]
    return {
        "forecast": [
            {"date": "2026-10-29", "temp_max_f": 84.0, "temp_min_f": 73.0,
             "precip_prob_max": 10, "weathercode": 0, "condition": "despejado"},
            {"date": "2026-10-30", "temp_max_f": 84.0, "temp_min_f": 73.0,
             "precip_prob_max": 10, "weathercode": 0, "condition": "despejado"},
            {"date": "2026-10-31", "temp_max_f": 79.0, "temp_min_f": 68.0,
             "precip_prob_max": 80, "weathercode": 61,
             "condition": "lluvia leve"},
        ],
        "hourly_date": "2026-10-31",
        "hourly_precipitation": hourly,
    }


class ComposerTests(unittest.TestCase):
    def test_demo_saturday_from_a_fixture_forecast(self):
        composed = compose_day_plan(fixture_weather(), date="2026-10-31")
        self.assertEqual(composed, {
            "kind": "plan", "date": "2026-10-31", "title": "Saturday plan",
            "blocks": [
                block("08:00", "12:00", "Salida a la mañana", "activity"),
                block("12:00", "13:00", "En casa", "free"),
                block("13:00", "18:00", "Lluvia", "rain"),
                block("18:00", "20:00", "En casa", "free"),
            ],
        })
        self.assertIsNone(plan_problem(composed))

    def test_no_rain_day_has_no_band(self):
        composed = compose_day_plan(fixture_weather(rain_from=25),
                                    date="2026-10-31")
        self.assertEqual([b["kind"] for b in composed["blocks"]],
                         ["activity", "free"])

    def test_rained_out_morning_drops_the_activity(self):
        composed = compose_day_plan(fixture_weather(rain_from=6, rain_to=9),
                                    date="2026-10-31")
        self.assertEqual(composed["blocks"][0],
                         block("06:00", "10:00", "Lluvia", "rain"))
        self.assertNotIn("activity",
                         [b["kind"] for b in composed["blocks"]])

    def test_date_outside_the_forecast_cannot_compose(self):
        self.assertIsNone(compose_day_plan(fixture_weather(),
                                           date="2027-01-01"))
        self.assertIsNone(compose_day_plan({}))
        self.assertIsNone(compose_day_plan(None))

    def test_card_summary_is_composed_from_the_same_plan(self):
        composed = compose_day_plan(fixture_weather(), date="2026-10-31")
        title, lines = plan_card(composed)
        self.assertEqual(title, "Saturday plan")
        self.assertEqual(lines, ["Salida a la mañana · 08:00–12:00",
                                 "Lluvia · 13:00–18:00"])
        for line in lines:
            self.assertLessEqual(len(line), 48)


class RoomPlanTests(unittest.TestCase):
    def make_room(self):
        return StudioRoom(99, weather_runner=lambda: {})

    def test_valid_plan_is_queued_without_writing(self):
        room = self.make_room()
        result = room.set_day_plan("Saturday plan", GOOD_BLOCKS,
                                   date="2026-10-31")
        self.assertEqual(result, {"ok": True, "queued": "Saturday plan"})
        self.assertEqual(room.plan_scenes,
                         [plan(GOOD_BLOCKS)])
        self.assertEqual(room.wall_scenes, [])  # no card was queued

    def test_invalid_plan_is_rejected_recorded_and_not_queued(self):
        room = self.make_room()
        result = room.set_day_plan("Saturday plan",
                                   [block("08:00", "09:00", "x" * 49,
                                          "activity")])
        self.assertFalse(result["ok"])
        self.assertIn("48", result["error"])
        self.assertEqual(room.plan_scenes, [])
        (rejection,) = room.wall_rejections
        self.assertEqual(rejection["kind"], "plan")

    def test_dispatch_routes_the_plan_tool(self):
        room = DryRoom()
        result = dispatch(room, "set_day_plan",
                          {"title": "Saturday plan", "blocks": GOOD_BLOCKS,
                           "date": "2026-10-31"})
        self.assertTrue(result["ok"])
        self.assertEqual(room.actions[0]["plan"], "Saturday plan")


class PlanFlushTests(unittest.TestCase):
    """One turn, one wall write — and the write carries the plan."""

    def make_sink(self):
        calls = []

        def transport(title, lines, text, turn, scene=None):
            calls.append({"title": title, "lines": lines, "text": text,
                          "turn": turn, "scene": scene})
            return {"rid": "1793-1-2"}

        return StudioSink(transport, turn=99), calls

    def test_plan_turn_ships_exactly_once_with_the_plan_kind(self):
        weather = fixture_weather()
        room = StudioRoom(
            99,
            weather_runner=lambda: {
                "time": ["2026-10-31"], "temperature_2m_max": [79.0],
                "temperature_2m_min": [68.0],
                "precipitation_probability_max": [80], "weathercode": [61],
            },
            hourly_runner=lambda: {
                "time": [f"2026-10-31T{h:02d}:00" for h in range(24)],
                "precipitation_probability": [
                    80 if 13 <= h <= 17 else 10 for h in range(24)],
            })
        room.weather = weather  # as a get_weather call would leave it
        sink, calls = self.make_sink()
        run_turn(iter([
            ("tool_call", "set_day_plan",
             {"title": "Saturday plan", "date": "2026-10-31",
              "blocks": GOOD_BLOCKS}),
            ("token", "Listo, tu sábado está en la pared."),
        ]), room, sink)
        self.assertEqual(calls, [])  # nothing dispatched mid-turn
        title, lines, scene = compose_final_scene("plan", room, sink)
        sink.set_scene(title, lines, scene=scene)
        sink.flush()
        (shipped,) = calls  # exactly one write for the whole turn
        self.assertEqual(shipped["scene"]["kind"], "plan")
        self.assertEqual(shipped["scene"]["blocks"], GOOD_BLOCKS)
        self.assertEqual(shipped["title"], "Saturday plan")
        self.assertEqual(shipped["lines"],
                         ["Salida a la mañana · 08:00–12:00",
                          "Lluvia · 13:00–18:00"])
        self.assertEqual(shipped["turn"], 99)

    def test_invalid_plan_falls_back_to_the_card_scene(self):
        # The room refuses the plan, so at flush there is no plan
        # candidate: composition falls through to the weather card
        # and the dispatch carries NO structured scene — the turn
        # and its spoken answer survive the bad payload.
        room = StudioRoom(99, weather_runner=lambda: {
            "time": ["2026-10-31"], "temperature_2m_max": [79.0],
            "temperature_2m_min": [68.0],
            "precipitation_probability_max": [80], "weathercode": [61],
        })
        sink, calls = self.make_sink()
        run_turn(iter([
            ("tool_call", "get_weather", {}),
            ("tool_call", "set_day_plan",
             {"title": "Saturday plan",
              "blocks": [block("08:00", "09:00", "x" * 49, "activity")]}),
            ("token", "El sábado llueve desde la una."),
        ]), room, sink)
        self.assertEqual(room.plan_scenes, [])
        self.assertEqual(len(room.wall_rejections), 1)
        title, lines, scene = compose_final_scene("plan", room, sink)
        self.assertIsNone(scene)
        sink.set_scene(title, lines, scene=scene)
        receipt = sink.flush()
        self.assertEqual(receipt["rid"], "1793-1-2")
        (shipped,) = calls
        self.assertIsNone(shipped["scene"])
        self.assertEqual(shipped["title"], "Tamarac weather")


class WallApplyPlanTests(unittest.TestCase):
    def payload(self, plan_payload):
        return {"turn": 1, "phase": 4,
                "kv": {"scene": "plan", "plan": plan_payload}}

    def test_plan_payload_builds_an_argv_with_the_plan_as_data(self):
        argv = wall_apply.build_argv(self.payload(plan(GOOD_BLOCKS)))
        plan_arg = next(a for a in argv if a.startswith("plan="))
        self.assertIn('"blocks"', plan_arg)
        self.assertIn("scene=plan", argv)

    def test_invalid_plan_is_refused_before_any_write(self):
        # build_argv raises (apply/main report it as rc 2); nothing
        # reaches the runner — the write never happens.
        ran = []
        bad = plan([block(f"{h:02d}:00", f"{h + 1:02d}:00", "X", "free")
                    for h in range(7)])
        with self.assertRaises(ValueError) as ctx:
            wall_apply.apply(
                self.payload(bad),
                runner=lambda a: ran.append(a) or _FakeProc(0))
        self.assertIn("6", str(ctx.exception))
        self.assertEqual(ran, [])

    def test_plan_without_the_plan_scene_is_refused(self):
        with self.assertRaises(ValueError):
            wall_apply.build_argv(
                {"turn": 1, "phase": 4,
                 "kv": {"scene": "answer", "plan": plan(GOOD_BLOCKS)}})


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


if __name__ == "__main__":
    unittest.main()
