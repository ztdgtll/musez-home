"""Offline tests for the bridge daemon's queue drain — no Pi, no API.

Pins: queue parsing keeps only complete, well-shaped entries (a
line mid-append can neither wedge nor fake a turn), the Pi-side
switch gate drains an entry as a recorded refusal when the Pi says
non-bridge — without ever calling the live runner — an entry older
than the staleness bound drains as a recorded skip without ever
reaching the gate or the runner, and a bridge entry reaches the
runner with the listener's own turn id.

Run from the repo root:  python3 -m unittest bridge.test_daemon -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import daemon

GOOD_1 = json.dumps({"turn": 111, "base": 111, "text": "hola",
                     "ts": 1793138465.0})
GOOD_2 = json.dumps({"turn": 222, "base": 222, "text": "¿qué clima hace?",
                     "ts": 1793138500.0})


class ParseEntriesTests(unittest.TestCase):
    def test_keeps_valid_entries_in_order(self):
        text = GOOD_1 + "\n" + GOOD_2 + "\n"
        entries = daemon.parse_entries(text)
        self.assertEqual([e["turn"] for e in entries], [111, 222])

    def test_skips_partial_and_malformed_lines(self):
        text = "\n".join([
            GOOD_1,
            '{"turn": 333, "text": "a mitad de escr',  # mid-append
            "not json at all",
            json.dumps({"turn": True, "text": "bool no es turn"}),
            json.dumps({"turn": "444", "text": "turn como string"}),
            json.dumps({"turn": 555, "text": "   "}),
            json.dumps({"text": "sin turn"}),
            GOOD_2,
        ])
        entries = daemon.parse_entries(text)
        self.assertEqual([e["turn"] for e in entries], [111, 222])

    def test_empty_queue(self):
        self.assertEqual(daemon.parse_entries(""), [])
        self.assertEqual(daemon.parse_entries(None), [])


class ReadQueueTests(unittest.TestCase):
    def test_missing_queue_file_is_an_empty_queue(self):
        def missing(command):
            raise RuntimeError(
                "ssh fetch failed rc=1: cat: /home/ztdgtll/pared-tools/"
                "bridge-queue.jsonl: No such file or directory")

        with mock.patch.object(daemon, "_ssh_text", missing):
            self.assertEqual(daemon.read_queue_entries(), [])

    def test_other_fetch_failures_still_raise(self):
        def down(command):
            raise RuntimeError("ssh fetch failed rc=255: connection refused")

        with mock.patch.object(daemon, "_ssh_text", down):
            with self.assertRaises(RuntimeError):
                daemon.read_queue_entries()


class PiBrainModeTests(unittest.TestCase):
    def test_reads_the_pi_mode(self):
        self.assertEqual(
            daemon.pi_brain_mode(lambda: '{"mode": "classic"}'), "classic")
        self.assertEqual(
            daemon.pi_brain_mode(lambda: '{"mode": " Bridge "}'), "bridge")

    def test_unconfigured_or_unreadable_is_none(self):
        self.assertIsNone(daemon.pi_brain_mode(lambda: ""))
        self.assertIsNone(daemon.pi_brain_mode(lambda: "garbage"))
        self.assertIsNone(daemon.pi_brain_mode(lambda: "{}"))

        def boom():
            raise RuntimeError("ssh down")

        self.assertIsNone(daemon.pi_brain_mode(boom))


class ProcessEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.results = Path(self.tmp.name) / "bridge-live.jsonl"
        patcher = mock.patch.object(daemon, "RESULTS_DEFAULT", self.results)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.ran = []
        runner_patcher = mock.patch.object(
            daemon, "run_live_turn",
            lambda text, turn: self.ran.append((text, turn)))
        runner_patcher.start()
        self.addCleanup(runner_patcher.stop)

    def test_pi_switch_off_drains_without_a_paid_call(self):
        entry = {"turn": 111, "text": "hola"}
        daemon.process_entry(entry, mode_reader=lambda: '{"mode": "classic"}')
        self.assertEqual(self.ran, [])
        lines = self.results.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["mode"], "switch_off")
        self.assertEqual(record["source"], "daemon_gate")
        self.assertEqual(record["turn_id"], 111)
        self.assertEqual(record["brain_mode"], "classic")

    def test_bridge_entry_reaches_the_runner_with_its_turn(self):
        entry = {"turn": 222, "text": "¿qué clima hace?"}
        daemon.process_entry(entry, mode_reader=lambda: '{"mode": "bridge"}')
        self.assertEqual(self.ran, [("¿qué clima hace?", 222)])
        self.assertFalse(self.results.exists())

    def test_unconfigured_pi_still_reaches_the_runner(self):
        # No mode file on the Pi: the contract's default applies and
        # the runner's own gate is the deciding lock.
        entry = {"turn": 333, "text": "hola"}
        daemon.process_entry(entry, mode_reader=lambda: "")
        self.assertEqual(self.ran, [("hola", 333)])

    def test_a_crashing_runner_never_raises(self):
        def boom(text, turn):
            raise RuntimeError("runner exploded")

        with mock.patch.object(daemon, "run_live_turn", boom):
            daemon.process_entry({"turn": 444, "text": "hola"},
                                 mode_reader=lambda: '{"mode": "bridge"}')

    def test_stale_entry_is_recorded_and_never_run(self):
        # A queue that backed up while the daemon was down must not
        # fire its turns late: past STALE_AFTER_S the entry drains
        # as a recorded skip — no gate read, no runner, no speech.
        gate_reads = []

        def reader():
            gate_reads.append(1)
            return '{"mode": "bridge"}'

        entry = {"turn": 999, "text": "turno viejo", "ts": 1_000_000.0}
        daemon.process_entry(entry, mode_reader=reader, now=1_000_200.0)
        self.assertEqual(self.ran, [])
        self.assertEqual(gate_reads, [])
        lines = self.results.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["mode"], "stale_skipped")
        self.assertEqual(record["source"], "daemon_stale")
        self.assertEqual(record["turn_id"], 999)
        self.assertEqual(record["queue_ts"], 1_000_000.0)
        self.assertEqual(record["age_s"], 200.0)
        self.assertEqual(record["playback"], "skipped")

    def test_fresh_entry_still_runs(self):
        entry = {"turn": 111, "text": "hola", "ts": 1_000_000.0}
        daemon.process_entry(entry,
                             mode_reader=lambda: '{"mode": "bridge"}',
                             now=1_000_010.0)
        self.assertEqual(self.ran, [("hola", 111)])
        self.assertFalse(self.results.exists())

    def test_entry_without_a_timestamp_still_runs(self):
        # Dropping a turn that might be live is the worse failure:
        # only a KNOWN-old entry is skipped.
        entry = {"turn": 222, "text": "sin ts"}
        daemon.process_entry(entry,
                             mode_reader=lambda: '{"mode": "bridge"}',
                             now=9_999_999.0)
        self.assertEqual(self.ran, [("sin ts", 222)])


if __name__ == "__main__":
    unittest.main()
