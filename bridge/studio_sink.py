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

    A structured scene (bridge/day_plan.py) rides the dispatch as the
    `scene` keyword: respond2's contract is exactly four arguments, so
    the card summary composed from the same plan (title + lines) is
    what it carries, and the validated payload is recorded on the
    receipt. The runner publishes the payload itself to the wall at
    first play (bridge/live_turn.py).

    A dispatch's ROLE in the turn rides as the `kind` keyword and
    travels env -> payload: respond2.sh copies a non-empty
    RESPOND_KIND into the payload's "kind" field (a payload without
    the field is byte-identical to the classic path's), and the Pi's
    respond_recv acts on it. The one kind today is "ack": an
    acknowledgement is spoken WITHOUT any wall writes — sayseq skips
    its phase-4 scene and its phase-5 close for the session — because
    the turn's scene ladder belongs to the answer session alone
    (room evidence 2026-10-31: an ack session's writes consumed both
    phases before the answer could publish, and every escalated
    turn's closing publish was dropped by the wall guard, rc=4).

    The turn's LANGUAGE rides the same road as the `lang` keyword:
    respond2.sh synthesizes in Spanish unless RESPOND_LANG says
    otherwise, so the env travels ONLY on an English turn
    (lang="en" -> RESPOND_LANG=en) and a dispatch without it is
    byte-identical to the classic path's — Spanish turns and the
    classic path itself never see the variable set, and an
    inherited RESPOND_LANG is stripped exactly like RESPOND_KIND:
    a turn's language is decided per turn (meta_source.
    detect_language), never ambient.
    """

    # The sink passes `kind` only to transports that declare this.
    supports_kind = True
    # …and `lang`, under the same rule.
    supports_lang = True

    def __init__(self, script: str = None, runner=None):
        self.script = script or os.environ.get("BRIDGE_RESPOND2_PATH", DEFAULT_RESPOND2)
        self._run = runner or (
            lambda argv, env: subprocess.run(
                argv, env=env, capture_output=True, text=True,
                timeout=TRANSPORT_TIMEOUT_S
            )
        )

    def __call__(self, title: str, lines: list, text: str, turn,
                 scene: dict = None, kind: str = None,
                 lang: str = None) -> dict:
        line1 = lines[0] if len(lines) > 0 else ""
        line2 = lines[1] if len(lines) > 1 else ""
        # The bridge's API key is not the room's business: respond2
        # gets the environment minus META_API_KEY. A RESPOND_KIND
        # inherited from our own process environment is stripped the
        # same way — a dispatch's kind is set explicitly per call,
        # never ambient. RESPOND_LANG follows the same discipline,
        # and is set only for an English turn (lang="en"): Spanish
        # is respond2's own default, so nothing travels for it.
        env = {k: v for k, v in os.environ.items()
               if k not in ("META_API_KEY", "RESPOND_KIND",
                            "RESPOND_LANG")}
        if turn is not None:
            env["RESPOND_TURN"] = str(turn)
        if kind:
            env["RESPOND_KIND"] = kind
        if lang == "en":
            env["RESPOND_LANG"] = "en"
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
        receipt = {"rid": m.group(1), "turn": m.group(2), "pid": int(m.group(3))}
        if scene is not None:
            receipt["scene_kind"] = scene.get("kind")
        if lang == "en":
            receipt["lang"] = "en"
        return receipt


class StudioSink:
    """Sink protocol for run_turn, backed by the studio transport.

    `say()` appends sentences in the order the chunker closes them.
    `flush()` — called by the runner once the turn's stream ends —
    speaks the whole turn through the transport, once, and that one
    dispatch is also the turn's only wall write: the scene (title +
    up to two lines) is set before flush, composed by the runner from
    the turn's scene candidate if it recorded one, else from the
    weather result, else from the first spoken sentences themselves —
    the wall's lines read as her voice, never as a mid-turn draft.

    When the turn's final scene is a STRUCTURED payload (a day plan,
    bridge/day_plan.py), `set_scene` also takes it as `scene`: flush
    then passes it to the transport as a `scene` keyword on the same
    single call. Card-only turns call the transport exactly as
    before — the protocol grows only when a structured scene exists.

    `announce()` is the sink's one immediate dispatch: an escalated
    turn's spoken acknowledgement, sent through the same transport
    the moment the turn escalates — while the deep model is still
    working — under the same turn id, marked kind="ack". The mark
    matters: an acknowledgement is audio only. Its player session
    writes NO wall state (no phase-4 scene, no phase-5 close), so
    the turn's scene ladder belongs to the answer session alone and
    the wall keeps the listener's working scene while the deep
    model thinks. The ack text never enters `sentences`, so the
    final answer never repeats it. An ack failure is recorded on
    `self.ack` and swallowed — the ack must never fail or delay
    the turn's answer.
    """

    def __init__(self, transport, turn=None, title: str = "", lines=(),
                 lang: str = "es", ack_external: bool = False):
        self.transport = transport
        self.turn = turn
        self.title = title
        self.lines = list(lines)
        # ack_external: this turn's ONE acknowledgement already
        # sounded in the room before the turn reached the bridge —
        # the listener's pre-recorded filler, in the turn's language,
        # fired as the turn was queued (the queue entry carried the
        # fact; bridge/live_turn wires it here). announce() then
        # records the ack as discharged externally and dispatches
        # nothing: exactly one ack per turn, and the early one is
        # the only one that can be early — a synthesized ack takes
        # ~12 s of render+ship before it can sound (room evidence
        # 2026-11-01: the plan's ack, dispatched ~5 s into the
        # bridge's work, was heard 31 s after the turn was queued).
        self.ack_external = ack_external
        # The turn's language (meta_source.detect_language, decided
        # by the runner): "es" — the default — dispatches exactly
        # as before this parameter existed; "en" asks a lang-aware
        # transport to synthesize the turn in English (see
        # Respond2Transport). The ack and the answer share the
        # sink, so both dispatches carry the turn's language.
        self.lang = lang
        self.scene: dict | None = None
        self.sentences: list[str] = []
        self.events: list = []  # (perf_counter, kind) — stage marks
        self.receipt = None
        self.ack: dict | None = None

    def set_scene(self, title: str, lines, scene: dict = None) -> None:
        lines = list(lines)
        if len(lines) > 2:
            # The transport speaks exactly two lines; accepting more
            # would silently drop the rest. Refuse loudly instead.
            raise ValueError(
                f"a wall scene takes at most 2 lines; got {len(lines)}")
        self.title = title
        self.lines = lines
        self.scene = scene

    def say(self, sentence: str) -> None:
        if not self.sentences:
            self.events.append((time.perf_counter(), "first_say"))
        self.sentences.append(sentence)

    def _dispatch(self, title: str, lines, text: str, kind: str = None):
        """One transport call for this turn.

        A `kind` (the dispatch's role in the turn) is passed only to
        transports that declare `supports_kind`: plain callables —
        test doubles, older transports — keep the original
        four-argument contract and simply never mark a dispatch.
        Only the room's real transport needs the mark. The turn's
        language follows the same rule (`supports_lang`, and only
        on an English turn)."""
        kwargs: dict = {}
        if kind is not None and getattr(self.transport,
                                        "supports_kind", False):
            kwargs["kind"] = kind
        if self.lang == "en" and getattr(self.transport,
                                         "supports_lang", False):
            kwargs["lang"] = "en"
        return self.transport(title, lines, text, self.turn, **kwargs)

    def announce(self, text: str) -> dict:
        """Dispatch the escalation acknowledgement NOW, on its own.

        Same transport, same turn id, kind="ack" — audio only, no
        wall writes; see the class docstring. Once only: a repeat
        call returns the recorded outcome without dispatching again.
        Never raises: success, transport failure and even a launch
        whose outcome is unknown all come back as the outcome dict
        ({"ok": ...}) and stay on `self.ack` for the turn record.
        A launch-unknown ack is NOT retried — it may already be
        playing."""
        if self.ack is not None:
            return self.ack
        if self.ack_external:
            self.ack = {"ok": True, "text": text,
                        "external": "listener_filler"}
            return self.ack
        self.events.append((time.perf_counter(), "ack_dispatch"))
        try:
            receipt = self._dispatch("", [], text, kind="ack")
            self.ack = {"ok": True, "text": text,
                        "rid": receipt.get("rid") if receipt else None}
        except LaunchUnknownError as e:
            self.ack = {"ok": False, "text": text, "launch_unknown": True,
                        "error": f"{type(e).__name__}: {e}"}
        except Exception as e:
            self.ack = {"ok": False, "text": text,
                        "error": f"{type(e).__name__}: {e}"}
        return self.ack

    def flush(self) -> dict:
        if self.receipt is not None:
            return self.receipt
        if not self.sentences:
            self.receipt = {"rid": None, "skipped": "no sentences"}
            return self.receipt
        text = " ".join(self.sentences)
        extra: dict = {}
        if self.lang == "en" and getattr(self.transport,
                                         "supports_lang", False):
            extra["lang"] = "en"
        try:
            if self.scene is None:
                self.receipt = self.transport(
                    self.title, self.lines, text, self.turn, **extra)
            else:
                self.receipt = self.transport(
                    self.title, self.lines, text, self.turn,
                    scene=self.scene, **extra)
        except LaunchUnknownError:
            # Terminal receipt: the response may be playing, so a
            # later flush() returns this instead of dispatching again.
            self.receipt = {"rid": None, "launch_unknown": True}
            raise
        self.events.append((time.perf_counter(), "flush_done"))
        return self.receipt
