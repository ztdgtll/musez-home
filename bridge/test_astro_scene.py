"""Offline tests for the Astro dispatch card (`astro` scenes) —
no robot, no network.

The payload is composed deterministically from the dispatch the
room recorded (destination + ok), the way bridge/stat_scene.py
composes the field stat from the count. What is pinned here:

- the room records a dispatch ONLY on success (`room.astro`),
  exactly the `room.field` discipline: a failed, unconfigured or
  unknown dispatch leaves no trace for composition to find;
- scene composition ships the astro payload, outranking the
  model's own scene candidate, with the card title
  ("SPECIAL DELIVERY") and the model's own reply sentences as
  its lines;
- a successful dispatch appends the standing signature line
  (astro_scene.ASTRO_CAPTIONS) as the turn's FINAL spoken
  sentence, in the same dispatch and in the turn's language —
  voice only, never printed on the card; no success, no line;
- the Pi-side gate (wall_apply) builds an argv only for a
  well-formed payload beside scene "astro" and refuses the rest
  before any write.

Run from the repo root:  python3 -m unittest bridge.test_astro_scene -v
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from . import wall_apply
from .astro_client import FakeAstroClient
from .astro_scene import (ASTRO_CAPTIONS, ASTRO_TITLE, astro_caption_for,
                          astro_problem, compose_astro_dispatch)
from .live_turn import run_live_turn
from .studio_room import ASTRO_URL_ENVS, StudioRoom
from .studio_sink import StudioSink
from .studio_turn import compose_final_scene
from .test_astro import astro_env
from .test_live_turn import (FakeSource, base_kwargs, make_respond_runner,
                             make_sink, make_wall_runner, TURN)

FAKE_STUDIO_URL = ("https://api-v3.voicemonkey.io/trigger"
                   "?token=FAKE-TOKEN&device=fake-studio-trigger")


def room_dispatched(destination: str = "studio") -> StudioRoom:
    client = FakeAstroClient()
    room = StudioRoom(turn=1, astro_client=client)
    with astro_env({"studio": FAKE_STUDIO_URL,
                    "kitchen": FAKE_STUDIO_URL}):
        result = room.call_astro(destination)
    assert result["ok"]
    return room


class AstroSceneTests(unittest.TestCase):
    """The delivery-card payload: composed from the recorded
    dispatch, validated to the wall's rules — never from the
    model's wording, never hard-coded to a request."""

    def test_composes_from_the_recorded_dispatch(self):
        payload = compose_astro_dispatch(
            {"destination": "studio", "ok": True}, lang="en")
        self.assertEqual(payload, {
            "kind": "astro", "destination": "studio", "ok": True})
        self.assertIsNone(astro_problem(payload))

    def test_payload_does_not_vary_with_language(self):
        # The wall payload is language-independent: the piece of
        # a dispatch that follows the turn's language is its
        # SPOKEN signature (AstroCaptionTests), never the card.
        dispatch = {"destination": "studio", "ok": True}
        expected = {"kind": "astro", "destination": "studio",
                    "ok": True}
        self.assertEqual(compose_astro_dispatch(dispatch, lang="en"),
                         expected)
        self.assertEqual(compose_astro_dispatch(dispatch, lang="es"),
                         expected)
        self.assertEqual(compose_astro_dispatch(dispatch), expected)

    def test_the_kitchen_dispatch_composes_too(self):
        payload = compose_astro_dispatch(
            {"destination": "kitchen", "ok": True})
        self.assertEqual(payload["destination"], "kitchen")
        self.assertIsNone(astro_problem(payload))

    def test_no_successful_dispatch_composes_nothing(self):
        for bad in (None, {}, {"destination": "studio"},
                    {"destination": "studio", "ok": False},
                    {"destination": "studio", "ok": "yes"},
                    {"destination": "garage", "ok": True},
                    {"ok": True}):
            self.assertIsNone(compose_astro_dispatch(bad),
                              msg=f"dispatch={bad!r}")

    def test_problem_names_the_limit_broken(self):
        good = {"kind": "astro", "destination": "studio", "ok": True}
        self.assertIn("kind", astro_problem({**good, "kind": "stat"}))
        self.assertIn("ok", astro_problem({**good, "ok": False}))
        self.assertIn("destination",
                      astro_problem({**good, "destination": "garage"}))
        self.assertIn("object", astro_problem("astro"))

    def test_title_is_the_english_card_title(self):
        self.assertEqual(ASTRO_TITLE, "SPECIAL DELIVERY")


class StudioRoomAstroRecordTests(unittest.TestCase):
    """room.astro is set only by a successful call_astro — the
    same discipline as room.field, so a failed dispatch can never
    leak a delivery card (or the signature line) into a turn."""

    def test_success_records_the_dispatch(self):
        room = room_dispatched("studio")
        self.assertEqual(room.astro,
                         {"destination": "studio", "ok": True})

    def test_failure_records_nothing(self):
        client = FakeAstroClient(
            result={"ok": False, "error": "routine did not run"})
        room = StudioRoom(turn=1, astro_client=client)
        with astro_env({"studio": FAKE_STUDIO_URL}):
            result = room.call_astro("studio")
        self.assertFalse(result["ok"])
        self.assertIsNone(room.astro)

    def test_unconfigured_records_nothing(self):
        room = StudioRoom(turn=1, astro_client=FakeAstroClient())
        with astro_env({}):
            result = room.call_astro("studio")
        self.assertFalse(result["ok"])
        self.assertIsNone(room.astro)

    def test_unknown_destination_records_nothing(self):
        room = StudioRoom(turn=1, astro_client=FakeAstroClient())
        with astro_env({"studio": FAKE_STUDIO_URL}):
            result = room.call_astro("garage")
        self.assertFalse(result["ok"])
        self.assertIsNone(room.astro)


class FinalSceneAstroTests(unittest.TestCase):
    """compose_final_scene precedence: the astro payload outranks
    the model's own scene candidate — it is composed from this
    turn's recorded dispatch, while the candidate is an
    improvisation. The card's lines are the model's own reply
    sentences, as with any scene."""

    def sink(self, *sentences: str) -> StudioSink:
        sink = StudioSink(lambda *a: {"rid": None}, turn=1)
        for s in sentences:
            sink.say(s)
        return sink

    def test_astro_turn_ships_the_astro_payload(self):
        sink = self.sink("Astro is on his way with your water.")
        title, lines, scene = compose_final_scene(
            "", room_dispatched(), sink, lang="en")
        self.assertEqual(scene, {
            "kind": "astro", "destination": "studio", "ok": True})
        self.assertEqual(title, "SPECIAL DELIVERY")
        self.assertEqual(lines, ["Astro is on his way with your water."])

    def test_spanish_turn_ships_the_same_payload(self):
        sink = self.sink("Astro ya va en camino con tu agua.")
        title, lines, scene = compose_final_scene(
            "", room_dispatched(), sink, lang="es")
        self.assertEqual(scene, {
            "kind": "astro", "destination": "studio", "ok": True})
        self.assertEqual(title, "SPECIAL DELIVERY")

    def test_astro_outranks_the_models_own_scene(self):
        room = room_dispatched()
        room.set_wall_scene("Robot time", "Astro is coming right now")
        sink = self.sink("Astro is on his way with your water.")
        title, lines, scene = compose_final_scene(
            "", room, sink, lang="en")
        self.assertEqual(scene["kind"], "astro")
        self.assertEqual(title, "SPECIAL DELIVERY")
        self.assertEqual(lines, ["Astro is on his way with your water."])

    def test_no_dispatch_no_payload(self):
        room = StudioRoom(turn=1, astro_client=FakeAstroClient())
        sink = self.sink("Astro is on his way with your water.")
        title, lines, scene = compose_final_scene(
            "", room, sink, lang="en")
        self.assertIsNone(scene)


class AstroCaptionTests(unittest.TestCase):
    """The standing signature: spoken only for a successful
    dispatch, in the turn's language — the same per-turn language
    rule every bridge-composed line follows."""

    def test_caption_for_a_successful_dispatch_in_english(self):
        self.assertEqual(
            astro_caption_for({"destination": "studio", "ok": True},
                              lang="en"),
            "The delivery guy doesn't take tips.")

    def test_caption_for_a_successful_dispatch_in_spanish(self):
        self.assertEqual(
            astro_caption_for({"destination": "studio", "ok": True},
                              lang="es"),
            "El repartidor no acepta propinas.")
        self.assertEqual(ASTRO_CAPTIONS["es"],
                         "El repartidor no acepta propinas.")
        self.assertEqual(ASTRO_CAPTIONS["en"],
                         "The delivery guy doesn't take tips.")

    def test_no_caption_without_a_successful_dispatch(self):
        for bad in (None, {}, {"destination": "studio", "ok": False},
                    {"destination": "garage", "ok": True}):
            self.assertIsNone(astro_caption_for(bad, lang="en"),
                              msg=f"dispatch={bad!r}")
            self.assertIsNone(astro_caption_for(bad, lang="es"),
                              msg=f"dispatch={bad!r}")


class WallApplyAstroTests(unittest.TestCase):
    """The Pi-side gate: an astro payload builds an argv only
    beside scene 'astro', and an unusable payload is refused
    before any write — the same discipline the stat payload
    lives under."""

    GOOD_ASTRO = {"kind": "astro", "destination": "studio",
                  "ok": True}

    def payload(self, kv):
        return {"turn": 1, "phase": 5, "kv": kv}

    def test_astro_payload_builds_an_argv_with_the_astro_as_data(self):
        argv = wall_apply.build_argv(self.payload(
            {"scene": "astro", "title": "SPECIAL DELIVERY",
             "lines": ["Astro is on his way with your water."],
             "astro": self.GOOD_ASTRO}))
        self.assertIn("scene=astro", argv)
        astro_arg = next(a for a in argv if a.startswith("astro="))
        self.assertIn('"destination": "studio"', astro_arg)

    def test_astro_without_the_astro_scene_is_refused(self):
        with self.assertRaises(ValueError):
            wall_apply.build_argv(self.payload(
                {"scene": "answer", "astro": self.GOOD_ASTRO}))

    def test_astro_scene_without_a_payload_is_refused(self):
        with self.assertRaises(ValueError):
            wall_apply.build_argv(self.payload({"scene": "astro"}))

    def test_unusable_astro_is_refused_before_any_write(self):
        ran = []

        class Proc:
            returncode = 0

        with self.assertRaises(ValueError) as ctx:
            wall_apply.apply(
                self.payload({"scene": "astro",
                              "astro": {**self.GOOD_ASTRO,
                                        "ok": False}}),
                runner=lambda a: ran.append(a) or Proc())
        self.assertIn("ok", str(ctx.exception))
        self.assertEqual(ran, [])

class AstroLiveTurnTests(unittest.TestCase):
    """The full live runner with a scripted model: an astro turn
    publishes the astro scene AND speaks the signature line last;
    a failed dispatch does neither."""

    def run_astro_turn(self, client, transcript, reply, lang):
        source = FakeSource([
            ("tool_call", "call_astro", {"destination": "studio"}),
            ("token", reply),
        ])
        respond_runner, _ = make_respond_runner()
        sink = make_sink(respond_runner)
        room = StudioRoom(TURN, speak=sink.say, astro_client=client)
        wall_runner, wall_calls = make_wall_runner()
        env = {"MUSEZ_BRAIN": "bridge",
               ASTRO_URL_ENVS["studio"]: FAKE_STUDIO_URL}
        with mock.patch.dict(os.environ, env):
            record = run_live_turn(
                transcript, TURN,
                source=source, room=room, sink=sink, lang=lang,
                **base_kwargs(wall_runner=wall_runner))
        return record, wall_calls

    def test_success_publishes_the_card_and_speaks_the_signature_last(self):
        record, wall_calls = self.run_astro_turn(
            FakeAstroClient(), "Hey Muse, send Astro with my water.",
            "Astro is on his way with your water.", lang="en")
        self.assertEqual(record["wall_outcome"], "astro")
        self.assertEqual(record["astro"], {
            "kind": "astro", "destination": "studio", "ok": True})
        self.assertEqual(record["wall_publish"],
                         {"applied": True, "rc": 0})
        # The model's own sentence first, the signature last —
        # in the turn's language.
        self.assertEqual(record["sentences"],
                         ["Astro is on his way with your water.",
                          "The delivery guy doesn't take tips."])
        kv = wall_calls[0]["kv"]
        self.assertEqual(kv["scene"], "astro")
        self.assertEqual(kv["title"], "SPECIAL DELIVERY")
        self.assertEqual(kv["astro"], record["astro"])
        # The card's lines are the model's own — the signature
        # is spoken only, never a card line.
        self.assertEqual(kv["lines"],
                         ["Astro is on his way with your water."])

    def test_spanish_turn_speaks_the_spanish_signature(self):
        record, wall_calls = self.run_astro_turn(
            FakeAstroClient(), "Che Muse, mandá a Astro con mi agua.",
            "Astro ya va en camino con tu agua.", lang="es")
        self.assertEqual(record["wall_outcome"], "astro")
        self.assertEqual(record["astro"], {
            "kind": "astro", "destination": "studio", "ok": True})
        self.assertEqual(record["sentences"],
                         ["Astro ya va en camino con tu agua.",
                          "El repartidor no acepta propinas."])
        self.assertEqual(wall_calls[0]["kv"]["title"],
                         "SPECIAL DELIVERY")

    def test_failed_dispatch_publishes_no_card_and_speaks_no_signature(self):
        client = FakeAstroClient(
            result={"ok": False, "error": "routine did not run"})
        record, wall_calls = self.run_astro_turn(
            client, "Hey Muse, send Astro with my water.",
            "Astro is on his way with your water.", lang="en")
        self.assertIsNone(record["astro"])
        self.assertNotEqual(record["wall_outcome"], "astro")
        self.assertEqual(record["sentences"],
                         ["Astro is on his way with your water."])
        kv = wall_calls[0]["kv"]
        self.assertEqual(kv["scene"], "answer")
        self.assertNotIn("astro", kv)


if __name__ == "__main__":
    unittest.main()
