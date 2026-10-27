"""The speaking sink: bridge sentences -> the studio's playback circuit.

`run_turn` (turn.py) speaks through a sink protocol — one `say(sentence)`
call per closed sentence, in order. This sink is the real-room end of that
protocol.

The transport is the studio's existing response path, `respond2.sh`: the
same Warm voice (avocado_v2:MAI_01, Spanish, speed 90), the same
sha256-verified shipping, and the same Pi-side receiver/player
(respond_recv.py -> sayseq) the room's answers already use. No parallel
player is introduced.

respond2 is strictly one-shot per response — it takes the full spoken
text, renders every sentence in parallel, ships the payload plus
sentence 0, launches the player, and keeps shipping the remaining
sentences while sentence 0 plays. So this sink buffers the turn's
sentences in order and flushes exactly once per turn; the audible
latency shape (playback starts at sentence 0, the rest streams in
behind it) is the transport's own and is preserved.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

DEFAULT_RESPOND2 = os.path.expanduser("~/workspace/studio/respond2.sh")
_LAUNCHED_RE = re.compile(r"^LAUNCHED id=(\S+) turn=(\S+) pid=(\d+)", re.M)


class TransportError(RuntimeError):
    """The response transport failed; nothing was played."""


class Respond2Transport:
    """Invoke respond2.sh once per turn: (title, line1, line2, text).

    Content travels as argv, never through a shell; the original turn id
    is threaded through RESPOND_TURN so the answer's wall writes belong
    causally to this turn, exactly like a listener-driven response.
    """

    def __init__(self, script: str = None, runner=None):
        self.script = script or os.environ.get("BRIDGE_RESPOND2_PATH", DEFAULT_RESPOND2)
        self._run = runner or (
            lambda argv, env: subprocess.run(
                argv, env=env, capture_output=True, text=True, timeout=300
            )
        )

    def __call__(self, title: str, lines: list, text: str, turn) -> dict:
        line1 = lines[0] if len(lines) > 0 else ""
        line2 = lines[1] if len(lines) > 1 else ""
        env = dict(os.environ)
        if turn is not None:
            env["RESPOND_TURN"] = str(turn)
        proc = self._run([self.script, title, line1, line2, text], env)
        if proc.returncode != 0:
            raise TransportError(
                f"respond2 exited {proc.returncode}: {(proc.stderr or '').strip()[-300:]}"
            )
        m = _LAUNCHED_RE.search(proc.stdout or "")
        if not m:
            raise TransportError(f"respond2 did not confirm a launch: {proc.stdout!r}")
        return {"rid": m.group(1), "turn": m.group(2), "pid": int(m.group(3))}


class StudioSink:
    """Sink protocol for run_turn, backed by the studio transport.

    `say()` appends sentences in the order the chunker closes them.
    `flush()` — called by the runner once the turn's stream ends —
    speaks the whole turn through the transport, once. The wall scene
    (title + up to two lines) can be set any time before flush; the
    runner composes it from the turn's tool results.
    """

    def __init__(self, transport, turn=None, title: str = "", lines=()):
        self.transport = transport
        self.turn = turn
        self.title = title
        self.lines = list(lines)
        self.sentences: list[str] = []
        self.events: list = []  # (perf_counter, kind) — stage marks
        self.receipt = None

    def set_scene(self, title: str, lines) -> None:
        self.title = title
        self.lines = list(lines)

    def say(self, sentence: str) -> None:
        if not self.sentences:
            self.events.append((time.perf_counter(), "first_say"))
        self.sentences.append(sentence)

    def flush(self) -> dict:
        if self.receipt is not None:
            return self.receipt
        if not self.sentences:
            self.receipt = {"rid": None, "skipped": "no sentences"}
            return self.receipt
        text = " ".join(self.sentences)
        self.receipt = self.transport(self.title, self.lines, text, self.turn)
        self.events.append((time.perf_counter(), "flush_done"))
        return self.receipt
