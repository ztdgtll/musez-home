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


class LaunchUnknownError(TransportError):
    """respond2 exited 0 but its LAUNCHED line could not be parsed.

    The response may or may not be playing: this is terminal. Never
    re-dispatch blind — verify against the Pi's sayseq/playgate logs
    first, or the room could hear the turn twice."""


# The response travels as ONE argv element; cap it well below any
# exec limit. Spoken replies are one or two sentences, so the cap
# only ever bites on a runaway generation — and says so, out loud.
MAX_TEXT_CHARS = 4000
TRUNCATION_SENTENCE = "Respuesta recortada por longitud."
TRANSPORT_TIMEOUT_S = 45


def _cap_text(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    budget = MAX_TEXT_CHARS - len(TRUNCATION_SENTENCE) - 1
    cut = text.rfind(" ", 0, budget)
    head = text[: cut if cut > 0 else budget].rstrip()
    return f"{head} {TRUNCATION_SENTENCE}"


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
                argv, env=env, capture_output=True, text=True,
                timeout=TRANSPORT_TIMEOUT_S
            )
        )

    def __call__(self, title: str, lines: list, text: str, turn) -> dict:
        line1 = lines[0] if len(lines) > 0 else ""
        line2 = lines[1] if len(lines) > 1 else ""
        # The bridge's API key is not the room's business: respond2
        # gets the environment minus META_API_KEY.
        env = {k: v for k, v in os.environ.items() if k != "META_API_KEY"}
        if turn is not None:
            env["RESPOND_TURN"] = str(turn)
        try:
            proc = self._run(
                [self.script, title, line1, line2, _cap_text(text)], env)
        except subprocess.TimeoutExpired as e:
            raise TransportError(
                f"respond2 timed out after {TRANSPORT_TIMEOUT_S} s") from e
        if proc.returncode != 0:
            raise TransportError(
                f"respond2 exited {proc.returncode}: {(proc.stderr or '').strip()[-300:]}"
            )
        m = _LAUNCHED_RE.search(proc.stdout or "")
        if not m:
            raise LaunchUnknownError(
                f"respond2 did not confirm a launch: {proc.stdout!r}")
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
        try:
            self.receipt = self.transport(self.title, self.lines, text, self.turn)
        except LaunchUnknownError:
            # Terminal receipt: the response may be playing, so a
            # later flush() returns this instead of dispatching again.
            self.receipt = {"rid": None, "launch_unknown": True}
            raise
        self.events.append((time.perf_counter(), "flush_done"))
        return self.receipt
