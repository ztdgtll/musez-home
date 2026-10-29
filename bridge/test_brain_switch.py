"""Offline tests for the API switch (bridge/brain_switch.py).

The switch is the spend gate for the whole live path, so the proofs
that matter are negative ones: with the switch off, the live source
makes ZERO transport calls, the runner mints ZERO turns, and both
refuse with the same recorded, recognizable line — while unset or
explicitly "bridge" leaves today's behavior untouched.

Run from the repo root:  python3 -m unittest bridge.test_brain_switch -v
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

from . import studio_turn
from .brain_switch import (
    SWITCH_OFF_EXIT_CODE,
    SWITCH_OFF_MESSAGE,
    ApiSwitchOff,
    brain_mode,
    bridge_enabled,
    require_bridge,
)
from .meta_source import MetaSource
from .test_meta_source import DONE, FakeTransport, content
from .tools import TOOL_SCHEMAS, DryRoom
from .turn import PrintSink, run_turn


@contextmanager
def brain_env(**overrides):
    """Set the switch variables for a test; None removes a variable.
    Everything the switch reads is restored on exit."""
    keys = ("MUSEZ_BRAIN", "MUSEZ_BRAIN_FILE")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    for k, v in overrides.items():
        if v is not None:
            os.environ[k] = v
    try:
        yield
    finally:
        for k in keys:
            if saved[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved[k]


class BrainModeResolutionTests(unittest.TestCase):
    def test_unset_defaults_to_bridge(self):
        with brain_env():
            self.assertEqual(brain_mode(), "bridge")
            self.assertTrue(bridge_enabled())

    def test_env_var_classic_and_normalization(self):
        with brain_env(MUSEZ_BRAIN=" Classic "):
            self.assertEqual(brain_mode(), "classic")
            self.assertFalse(bridge_enabled())

    def test_json_file_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "brain.json"
            path.write_text(json.dumps({"mode": "classic"}))
            with brain_env(MUSEZ_BRAIN_FILE=str(path)):
                self.assertEqual(brain_mode(), "classic")

    def test_env_var_wins_over_the_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "brain.json"
            path.write_text(json.dumps({"mode": "classic"}))
            with brain_env(MUSEZ_BRAIN="bridge",
                           MUSEZ_BRAIN_FILE=str(path)):
                self.assertEqual(brain_mode(), "bridge")

    def test_missing_or_garbage_file_defaults_to_bridge(self):
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td) / "nope.json")
            with brain_env(MUSEZ_BRAIN_FILE=missing):
                self.assertEqual(brain_mode(), "bridge")
            bad = Path(td) / "bad.json"
            bad.write_text("{not json")
            with brain_env(MUSEZ_BRAIN_FILE=str(bad)):
                self.assertEqual(brain_mode(), "bridge")

    def test_unknown_value_fails_closed(self):
        # A typo must silence the paid API, not quietly bill it.
        with brain_env(MUSEZ_BRAIN="brige"):
            self.assertFalse(bridge_enabled())
            with self.assertRaises(ApiSwitchOff):
                require_bridge()

    def test_require_bridge_returns_the_mode_when_on(self):
        with brain_env():
            self.assertEqual(require_bridge(), "bridge")


class CountingTransport:
    """Would answer like the real API — the proof is it never runs."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, url, body, headers):
        self.calls += 1
        return iter([content("No deberías escuchar esto."), DONE])


class LiveSourceGateTests(unittest.TestCase):
    def test_switch_off_makes_zero_transport_calls(self):
        transport = CountingTransport()
        source = MetaSource(transport=transport)
        with brain_env(MUSEZ_BRAIN="classic"):
            with self.assertRaises(ApiSwitchOff) as caught:
                run_turn(source.stream_turn("hola", TOOL_SCHEMAS),
                         DryRoom(), PrintSink())
        self.assertEqual(str(caught.exception), SWITCH_OFF_MESSAGE)
        self.assertEqual(transport.calls, 0)

    def test_switch_on_leaves_the_live_path_unchanged(self):
        transport = FakeTransport([[
            content("Una Raspberry Pi "),
            content("es una computadora completa."),
            DONE,
        ]])
        source = MetaSource(transport=transport)
        with brain_env(MUSEZ_BRAIN="bridge"):
            marks, room, sink = None, DryRoom(), PrintSink()
            marks = run_turn(source.stream_turn("¿qué es una Pi?",
                                                TOOL_SCHEMAS),
                             room, sink)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(
            sink.sentences,
            ["Una Raspberry Pi es una computadora completa."])

    def test_unset_switch_also_leaves_the_live_path_unchanged(self):
        transport = FakeTransport([[content("Hola."), DONE]])
        source = MetaSource(transport=transport)
        with brain_env():
            run_turn(source.stream_turn("hola", TOOL_SCHEMAS),
                     DryRoom(), PrintSink())
        self.assertEqual(len(transport.requests), 1)


def _explode(*args, **kwargs):
    raise AssertionError("the gate must refuse before any network work")


class StudioTurnGateTests(unittest.TestCase):
    def _patched_runner(self):
        saved = (studio_turn.mint_turn, studio_turn.read_wall_state,
                 studio_turn.Respond2Transport)
        studio_turn.mint_turn = _explode
        studio_turn.read_wall_state = _explode
        studio_turn.Respond2Transport = _explode
        return saved

    def _restore(self, saved):
        (studio_turn.mint_turn, studio_turn.read_wall_state,
         studio_turn.Respond2Transport) = saved

    def test_switch_off_refuses_records_and_exits_3(self):
        saved = self._patched_runner()
        try:
            with tempfile.TemporaryDirectory() as td:
                results = Path(td) / "results.jsonl"
                out = io.StringIO()
                with brain_env(MUSEZ_BRAIN="classic"), \
                        redirect_stdout(out):
                    rc = studio_turn.main(
                        ["limpiá el studio",
                         "--results", str(results)])
                self.assertEqual(rc, SWITCH_OFF_EXIT_CODE)
                self.assertNotIn(rc, (0, 1, 2))  # distinct from errors
                self.assertIn(SWITCH_OFF_MESSAGE, out.getvalue())
                lines = results.read_text().strip().splitlines()
                self.assertEqual(len(lines), 1)
                record = json.loads(lines[0])
                self.assertEqual(record["mode"], "switch_off")
                self.assertEqual(record["brain_mode"], "classic")
                self.assertEqual(record["playback"], "skipped")
                self.assertIsNone(record["turn_id"])
        finally:
            self._restore(saved)

    def test_switch_off_with_skip_record_writes_nothing(self):
        saved = self._patched_runner()
        try:
            with tempfile.TemporaryDirectory() as td:
                results = Path(td) / "results.jsonl"
                with brain_env(MUSEZ_BRAIN="classic"), \
                        redirect_stdout(io.StringIO()):
                    rc = studio_turn.main(
                        ["hola", "--results", str(results),
                         "--skip-record"])
                self.assertEqual(rc, SWITCH_OFF_EXIT_CODE)
                self.assertFalse(results.exists())
        finally:
            self._restore(saved)

    def test_switch_on_runs_the_turn_as_before(self):
        class OkTransport:
            def __call__(self, title, lines, text, turn):
                return {"rid": None}

        saved = (studio_turn.mint_turn, studio_turn.read_wall_state,
                 studio_turn.Respond2Transport)
        studio_turn.mint_turn = lambda: 424244
        studio_turn.read_wall_state = lambda: {}
        studio_turn.Respond2Transport = lambda: OkTransport()
        try:
            with tempfile.TemporaryDirectory() as td:
                results = Path(td) / "results.jsonl"
                with brain_env(MUSEZ_BRAIN="bridge"), \
                        redirect_stdout(io.StringIO()):
                    rc = studio_turn.main(
                        ["¿qué es una Raspberry Pi?",
                         "--results", str(results)])
                self.assertEqual(rc, 0)
                record = json.loads(results.read_text().strip())
                self.assertEqual(record["mode"], "mock")
                self.assertEqual(record["turn_id"], 424244)
        finally:
            (studio_turn.mint_turn, studio_turn.read_wall_state,
             studio_turn.Respond2Transport) = saved


if __name__ == "__main__":
    unittest.main()
