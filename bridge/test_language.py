"""Offline tests for the turn-language rule — no network, no Pi.

A turn's language follows the user's request in that turn
(meta_source.TURN_LANGUAGE_GUIDANCE), decided deterministically by
detect_language — never by the model, never ambient. These tests
pin the four surfaces the rule has to hold on:

- the detector itself (English wins only on clear function-word
  evidence; doubt, brevity and mixed input land on Spanish);
- the prompt: the standing guidance rides SYSTEM_PROMPT and the
  per-turn pin rides every request, ahead of the user's message;
- the ack: an English turn's fixed fallback is English;
- the voice: the sink passes lang="en" only to a lang-aware
  transport and only on an English turn, so the respond2 env
  carries RESPOND_LANG=en exactly then — a Spanish turn (and any
  plain four-argument transport) dispatches exactly as before.
- the STT hint: when the queue entry carries the language the
  listener transcribed in (its STT switch), that recorded language
  wins over the detector; without one the detector rules as
  before.

Run from the repo root:  python3 -m unittest bridge.test_language -v
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from .live_turn import run_live_turn
from .meta_source import (ACK_FALLBACK, ACK_FALLBACK_EN,
                          TURN_LANGUAGE_GUIDANCE, SYSTEM_PROMPT,
                          MetaSource, detect_language, resolve_turn_lang,
                          validate_ack)
from .studio_sink import Respond2Transport, StudioSink
from .test_live_turn import (FakeProc, FakeSource, base_kwargs,
                             make_respond_runner)
from .test_meta_source import DONE, FakeTransport, content, sse, tool_delta
from .tools import TOOL_SCHEMAS, DryRoom
from .turn import PrintSink, run_turn

DEMO_LINE_1 = ("Hey Muse, what's the weather like this weekend? "
               "Put it on the wall.")
DEMO_LINE_2 = ("Hey Muse, plan my Saturday: breakfast at home, "
               "groceries at noon, and the pool if the weather "
               "holds — I want to be home before the rain.")


class DetectorTests(unittest.TestCase):
    def test_demo_lines_are_english(self):
        self.assertEqual(detect_language(DEMO_LINE_1), "en")
        self.assertEqual(detect_language(DEMO_LINE_2), "en")

    def test_plain_english_requests(self):
        for text in (
            "Put it on the wall.",
            "What's the weather like this weekend?",
            "Thank you, that's all.",
            "Can you clean the studio, please?",
        ):
            self.assertEqual(detect_language(text), "en", text)

    def test_spanish_requests_stay_spanish(self):
        for text in (
            "¿Cómo va a estar el clima para este fin de semana?",
            "Planificame el sábado.",
            "Poné la aspiradora en el estudio.",
            "¿Qué clima hace hoy?",
            "Dame un momento y revisá el pronóstico de mañana.",
        ):
            self.assertEqual(detect_language(text), "es", text)

    def test_doubt_and_brevity_land_on_spanish(self):
        # The conservative rule: English must WIN on evidence.
        # A greeting alone, an empty turn and non-text input never
        # flip the room out of its default.
        self.assertEqual(detect_language("Hey Muse"), "es")
        self.assertEqual(detect_language(""), "es")
        self.assertEqual(detect_language("   "), "es")
        self.assertEqual(detect_language(None), "es")
        self.assertEqual(detect_language(42), "es")

    def test_mixed_input_follows_the_majority(self):
        # A stray English greeting over a Spanish request is a
        # Spanish turn; the reverse mix is an English one.
        self.assertEqual(
            detect_language("Hey Muse, ¿qué clima hace hoy?"), "es")
        self.assertEqual(
            detect_language("Che, what is the weather like today?"),
            "en")


# The demo's closing line, as transcribed live on 2026-11-01: too
# few function words for the detector, which pinned it to Spanish
# and got a Spanish chat referral — while the listener had in fact
# transcribed it in English mode. The queue entry now carries that
# STT language and it wins (resolve_turn_lang).
SHORT_CLOSER = "One more thing. Anything I should know."


class SttHintTests(unittest.TestCase):
    def test_detector_alone_misses_the_short_closer(self):
        # The documented limit the hint exists to cover.
        self.assertEqual(detect_language(SHORT_CLOSER), "es")

    def test_hint_wins_for_the_short_closer(self):
        self.assertEqual(resolve_turn_lang(SHORT_CLOSER, "en"), "en")

    def test_spanish_hint_wins_over_english_text(self):
        self.assertEqual(resolve_turn_lang(DEMO_LINE_1, "es"), "es")

    def test_missing_or_bad_hint_falls_back_to_the_detector(self):
        self.assertEqual(resolve_turn_lang(SHORT_CLOSER), "es")
        self.assertEqual(resolve_turn_lang(SHORT_CLOSER, None), "es")
        self.assertEqual(resolve_turn_lang(DEMO_LINE_1, None), "en")
        self.assertEqual(resolve_turn_lang(DEMO_LINE_1, "fr"), "en")
        self.assertEqual(resolve_turn_lang(DEMO_LINE_1, ""), "en")

    def test_stream_turn_hint_pins_the_short_closer_in_english(self):
        transport = FakeTransport([[content("Sure."), DONE]])
        source = MetaSource(transport=transport)
        run_turn(source.stream_turn(SHORT_CLOSER, TOOL_SCHEMAS,
                                    lang="en"),
                 DryRoom(), PrintSink())
        messages = transport.requests[0]["messages"]
        self.assertIn("Turn language: English",
                      messages[1]["content"])


class PromptPinTests(unittest.TestCase):
    def _messages_for(self, text):
        transport = FakeTransport([[content("Listo."), DONE]])
        source = MetaSource(transport=transport)
        run_turn(source.stream_turn(text, TOOL_SCHEMAS),
                 DryRoom(), PrintSink())
        return transport.requests[0]["messages"]

    def test_standing_guidance_rides_the_system_prompt(self):
        self.assertIn(TURN_LANGUAGE_GUIDANCE, SYSTEM_PROMPT)

    def test_english_turn_is_pinned_in_english(self):
        messages = self._messages_for(DEMO_LINE_1)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn(TURN_LANGUAGE_GUIDANCE, messages[0]["content"])
        self.assertEqual(messages[1]["role"], "system")
        self.assertIn("Turn language: English",
                      messages[1]["content"])
        self.assertEqual(messages[2]["role"], "user")
        self.assertEqual(messages[2]["content"], DEMO_LINE_1)

    def test_spanish_turn_is_pinned_in_spanish(self):
        messages = self._messages_for(
            "¿Cómo va a estar el clima para este fin de semana?")
        self.assertIn("Turn language: Spanish",
                      messages[1]["content"])


class AckLanguageTests(unittest.TestCase):
    def test_validate_ack_fallback_speaks_the_turn_language(self):
        self.assertEqual(validate_ack(None), ACK_FALLBACK)
        self.assertEqual(validate_ack(None, lang="en"),
                         ACK_FALLBACK_EN)
        self.assertEqual(validate_ack("Uno. Dos.", lang="en"),
                         ACK_FALLBACK_EN)
        # A valid model ack passes through untouched either way.
        self.assertEqual(validate_ack("One moment, please.",
                                      lang="en"),
                         "One moment, please.")

    def test_english_escalation_without_ack_hears_english(self):
        args = {"reason": "multi-step planning"}  # no ack passed
        transport = FakeTransport([
            [tool_delta(0, call_id="e1", name="escalate",
                        arguments=json.dumps(args)),
             sse({}, finish="tool_calls"), DONE],
            [content("Your Saturday is planned."), DONE],
        ])
        sink = PrintSink()
        run_turn(
            MetaSource(transport=transport).stream_turn(
                DEMO_LINE_2, TOOL_SCHEMAS),
            DryRoom(), sink)
        self.assertEqual(sink.announcements, [ACK_FALLBACK_EN])
        self.assertEqual(sink.sentences,
                         ["Your Saturday is planned."])


class SinkLangTests(unittest.TestCase):
    def _transport(self):
        seen = []

        def runner(argv, env):
            seen.append((argv, dict(env)))
            return FakeProc(0, "LAUNCHED id=r1 turn=7 pid=3\n")

        return (Respond2Transport(script="/x/respond2.sh",
                                  runner=runner), seen)

    def test_transport_sets_respond_lang_only_for_english(self):
        transport, seen = self._transport()
        receipt = transport("T", [], "Hello.", 7, lang="en")
        self.assertEqual(seen[0][1]["RESPOND_LANG"], "en")
        self.assertEqual(receipt["lang"], "en")

    def test_transport_without_lang_never_sets_the_env(self):
        transport, seen = self._transport()
        receipt = transport("T", [], "Hola.", 7)
        self.assertNotIn("RESPOND_LANG", seen[0][1])
        self.assertNotIn("lang", receipt)

    def test_inherited_respond_lang_is_stripped(self):
        # A turn's language is decided per turn, never ambient: a
        # stray RESPOND_LANG in the daemon's own environment must
        # not leak into a Spanish dispatch.
        transport, seen = self._transport()
        with mock.patch.dict(os.environ, {"RESPOND_LANG": "en"}):
            transport("T", [], "Hola.", 7)
        self.assertNotIn("RESPOND_LANG", seen[0][1])

    def test_english_sink_marks_flush_and_ack(self):
        transport, seen = self._transport()
        sink = StudioSink(transport, turn=7, lang="en")
        sink.announce("One moment.")
        sink.say("Sunny and warm.")
        sink.flush()
        self.assertEqual(
            [env.get("RESPOND_LANG") for _, env in seen],
            ["en", "en"])
        self.assertEqual(seen[0][1]["RESPOND_KIND"], "ack")

    def test_spanish_sink_dispatches_exactly_as_before(self):
        transport, seen = self._transport()
        sink = StudioSink(transport, turn=7)  # lang defaults to "es"
        sink.announce("Dame un momento.")
        sink.say("Soleado y cálido.")
        sink.flush()
        for _, env in seen:
            self.assertNotIn("RESPOND_LANG", env)
        self.assertEqual(seen[0][1]["RESPOND_KIND"], "ack")

    def test_plain_transport_keeps_the_four_argument_contract(self):
        calls = []

        def plain(title, lines, text, turn):
            calls.append((title, lines, text, turn))
            return {"rid": "x"}

        sink = StudioSink(plain, turn=7, lang="en")
        sink.announce("One moment.")
        sink.say("Sunny and warm.")
        receipt = sink.flush()
        self.assertEqual(receipt["rid"], "x")
        self.assertEqual(len(calls), 2)


class LiveTurnLangTests(unittest.TestCase):
    def _run(self, transcript, sentence, lang=None):
        source = FakeSource([("token", sentence)])
        respond_runner, seen = make_respond_runner()
        with mock.patch.dict(os.environ, {"MUSEZ_BRAIN": "bridge"}):
            record = run_live_turn(
                transcript, 1793999999999, source=source,
                respond_runner=respond_runner, lang=lang,
                **base_kwargs())
        return record, seen, source

    def test_english_turn_records_and_voices_english(self):
        record, seen, _ = self._run(DEMO_LINE_1,
                                    "Sunny and warm this weekend.")
        self.assertEqual(record["turn_lang"], "en")
        self.assertEqual(record["lang_source"], "detect")
        self.assertEqual(record["sentences"],
                         ["Sunny and warm this weekend."])
        envs = [env for _, env in seen]
        self.assertTrue(envs)
        for env in envs:
            self.assertEqual(env.get("RESPOND_LANG"), "en")

    def test_spanish_turn_records_spanish_and_sets_nothing(self):
        record, seen, _ = self._run(
            "¿Cómo va a estar el clima para este fin de semana?",
            "Soleado y cálido el finde.")
        self.assertEqual(record["turn_lang"], "es")
        self.assertEqual(record["lang_source"], "detect")
        for _, env in seen:
            self.assertNotIn("RESPOND_LANG", env)

    def test_stt_hint_makes_the_short_closer_an_english_turn(self):
        # The 2026-11-01 live failure, replayed: with the STT
        # language on the entry, the closer is an English turn end
        # to end — record, source pin and voice env.
        record, seen, source = self._run(
            SHORT_CLOSER, "Nothing pressing.", lang="en")
        self.assertEqual(record["turn_lang"], "en")
        self.assertEqual(record["lang_source"], "stt")
        self.assertEqual(source.lang_seen, "en")
        for _, env in seen:
            self.assertEqual(env.get("RESPOND_LANG"), "en")

    def test_unhinted_short_closer_still_detects_spanish(self):
        # Compatibility: no hint, the old verdict stands.
        record, _, source = self._run(SHORT_CLOSER, "Buena pregunta.")
        self.assertEqual(record["turn_lang"], "es")
        self.assertEqual(record["lang_source"], "detect")
        self.assertEqual(source.lang_seen, "es")


if __name__ == "__main__":
    unittest.main()
