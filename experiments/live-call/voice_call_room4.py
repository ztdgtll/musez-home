"""ROOM4 = room3 + experiment instrumentation (2026-11-05 night session).

Adds, on top of everything room3 does:
  * Pre-dispatcher logging: musegadget.voice_call.read_ndjson is wrapped
    so EVERY inbound record is logged (seq#, type, and for "error"
    records the structured code/type — never the message) BEFORE
    VoiceCall._receive dispatches it. Post-dispatcher silence can no
    longer be confused with "nothing arrived".
  * In-session injection (InjectSource): dropping a trigger file
    (~/voice-test/INJECT or INJECT2) swaps the mic source, inside the
    SAME call/session/thread, for a scripted sequence:
      INJECT  = 2 s digital zeros -> the exact question.wav PCM that
                always works via the file path -> 5 s digital zeros
                -> mic.
      INJECT2 = 2 s digital zeros -> a 20 s excerpt of THIS system's
                own sent stream from a stalled call (real speech that
                produced no turn) -> live mic (the ambient tail is the
                third experimental arm).
    Discriminates orphaned session vs. problematic audio vs.
    endpointing conditioned by what follows the speech.
  * END line: when run() exits (any path), logs reason,
    end_call_acknowledged, cleanup_error, media_drain_incomplete,
    advisory_error_count and session_id. SIGTERM/SIGINT already cancel
    the run task (room3 wiring) and VoiceCall's shielded shutdown
    performs the remote end-call — closes are clean by construction;
    this makes them VERIFIABLE.

Realtime voice call through the studio's ALSA path + wall bridge.

Same instrumented runner as voice_call_room2.py (point-C capture of the
exact PCM sent, per-second SENT stats, held/gated state poller, turn
logging), plus the event -> wall bridge of coreografia v2 (capas Sesion
y Piso): a WallDriver translates call events into wall state, writing
ONLY through ~/pared/setstate.py.

Wall vocabulary (verified in setstate.py + pared/index.html, 2026-11-05):
  scene:  home | info | answer | showcase | versus | live | off
  avatar: idle | working | thinking | speaking | listening | off
  wave:   home-scene key; while true the page animates the logo "M"
          with ~/pared/levels.json (polled ~120 ms). The session
          scene write sets wave=true, the session-end home write sets
          wave=false, and a LevelsPublisher (below) feeds the file
          for exactly the session's lifetime — the turn system's
          listener is stopped in that window, so publishers never
          overlap.

Event -> wall mapping (floor states use avatar= only; the session scene
is written once, so floor flips never touch the lienzo):
  session ready (transport up, call about to run)
      -> scene write: scene=home avatar=listening title="LIVE CALL"
         text="ON CALL" wave=true, generous expiresAt, via
         --mint-turn + --phase=4 --sticky (published state;
         turn-system cleanups cannot displace it). Mint failure
         falls back to --manual.
  hatch.voice.turn_started  -> avatar=listening (Javier has the floor;
         "listening" is the renderer's attentive state)
  hatch.voice.turn_settled (tracked) -> avatar=working (response-
         generation window: Javier's turn is retired, the reply is
         being generated but is not audible yet). Playback starting
         supersedes it with speaking; if the response ends with no
         audio, the poller falls back to listening — working never
         lingers past its window.
  playback starts (playback queue non-empty / playing window open,
         polled at 100 ms)    -> avatar=speaking
  playback drains (response over) -> avatar=listening
  barge_in() / _flush() (reply audio cut) -> avatar=listening,
         IMMEDIATE: no debounce. Closing the mouth the instant the
         reply is cut is the trust gesture of the call.
  session end (any exit path of run()) -> --manual scene=home
         avatar=idle title="" text="studiolab online" sub="" lines=[]
         (mirrors setstate's SAFE_HOME; the scene write clears the
         sticky flag and the expiry).

Floor writes go through setstate's --manual (operator) class: they
always apply, and an avatar-only merge leaves the session's
turn/phase/sticky bookkeeping untouched. Rapid floor flips are
debounced/coalesced in the driver (250 ms settle, latest wins,
duplicates dropped) — except the barge-in path above. Exit 4 from
setstate means the guard rejected the write: it is logged and NEVER
retried blindly.

Input shaping (PatientSource): the provider's VAD endpoints a turn
after ~1 s of pause and the SDK exposes no endpointing parameter
("Provider VAD owns turn boundaries" — voice_call.py), so the mic
stream is shaped client-side. A pause that FOLLOWS speech is held
locally for up to --pause-hangover seconds (default 3.0): if speech
resumes in time, the provider receives ~200 ms of silence plus the
soft onset tail instead of the real pause; if the hangover expires,
the held pause is released in full and the turn closes naturally.
While reply audio is playing nothing is ever held (barge-in is
never delayed), and silence before a turn's first speech is never
held. The SDK's _input paces appends at realtime and re-bases its
pacing clock after every append (verified in the installed
voice_call.py), so shaped frames meter out at realtime pace. What
the dry test proves is the shape of OUR emitted stream; whether
the provider still endpoints on the (<= hangover) append gap can
only be proven in a live room test.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/home/ztdgtll/pared-tools")

from alsa_audio import GAIN, AlsaAudio  # noqa: E402
from musegadget import __version__, config, identity, muse_api  # noqa: E402
from musegadget import voice_call as _vc_mod  # noqa: E402
from musegadget.link_client import DeviceDescription, LinkSession  # noqa: E402
from musegadget.service import DEFAULT_NOISE_HOST  # noqa: E402
from musegadget.voice_call import (  # noqa: E402
    FRAME_BYTES, VoiceCall, _InputGated, pcm_rms, positive_seconds)

AUTH_KEY = "vm_auth" + "_token"
C_PATH = "/home/ztdgtll/voice-test/room3-C.raw"
SETSTATE = "/home/ztdgtll/pared/setstate.py"
TRIG_DIR = "/home/ztdgtll/voice-test"
QUESTION_WAV = "/home/ztdgtll/voice-test/question.wav"
FAILED_CLIP = "/home/ztdgtll/voice-test/failed-clip-test21.raw"


def log(msg: str) -> None:
    print(f"[probe {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# -- pre-dispatcher logging ------------------------------------------------
_orig_read_ndjson = _vc_mod.read_ndjson


_AUDIO_EVENT_TYPES = {
    "continuous_audio_video.delta", "response.output_audio.delta",
    "response.audio.delta",
}
_ID_KEYS = ("item_id", "submission_message_id", "response_message_id",
            "call_id", "session_id", "thread_id")


async def _logging_read_ndjson(stream):
    n = 0
    async for event in _orig_read_ndjson(stream):
        n += 1
        if isinstance(event, dict):
            etype = event.get("type")
            extra = ""
            if etype == "error" and isinstance(event.get("error"), dict):
                extra = (f" code={event['error'].get('code')}"
                         f" type={event['error'].get('type')}")
            elif etype not in _AUDIO_EVENT_TYPES:
                # Identity/payload inventory (Astra, brief-4 follow-up):
                # suppression verdicts must be attributable to an item,
                # and any reason/code the server sends must survive.
                for k in _ID_KEYS:
                    if k in event:
                        extra += f" {k}={event[k]}"
                item = event.get("item")
                if isinstance(item, dict) and "id" in item:
                    extra += f" item.id={item['id']}"
                if etype == "hatch.voice.input_suppressed":
                    extra += f" keys={sorted(event.keys())}"
                    for k in sorted(event.keys()):
                        if k != "type" and k not in _ID_KEYS and \
                                isinstance(event[k], (str, int, float, bool)):
                            extra += f" {k}={event[k]!r}"
            log(f"RAW#{n} {etype}{extra}")
        else:
            log(f"RAW#{n} <{type(event).__name__}>")
        yield event


_vc_mod.read_ndjson = _logging_read_ndjson


class WallDriver:
    """Serializes every wall write for the call onto one worker thread.

    Floor targets are coalesced: a non-immediate target is written only
    after it has been the latest desire for DEBOUNCE_S; a target equal
    to the last applied value is dropped. Immediate targets (barge-in)
    skip the settle window. Session start/end run as queued jobs and
    the caller waits (bounded) for them to land.
    """

    DEBOUNCE_S = 0.25

    def __init__(self, log_fn=log):
        self._log = log_fn
        self._lock = threading.Lock()
        self._jobs: collections.deque = collections.deque()
        self._floor_want: tuple[str, float, bool] | None = None
        self._floor_applied: str | None = None
        self._session_active = False
        self._closed = False
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="wall-driver")
        self._thread.start()

    # -- low level ----------------------------------------------------
    def _setstate(self, args: list[str], what: str) -> int | None:
        t0 = time.monotonic()
        try:
            proc = subprocess.run([SETSTATE, *args], capture_output=True,
                                  text=True, timeout=10)
            rc = proc.returncode
        except Exception as exc:
            self._log(f"WALL {what} FAILED to run: "
                      f"{type(exc).__name__}: {exc}")
            return None
        ms = int((time.monotonic() - t0) * 1000)
        if rc == 0:
            self._log(f"WALL {what} ok ({ms}ms)")
        elif rc == 4:
            self._log(f"WALL {what} REJECTED rc=4 by the guard "
                      f"(logged, not retrying)")
        else:
            self._log(f"WALL {what} rc={rc} "
                      f"stderr={(proc.stderr or '').strip()[:160]}")
        return rc

    def _mint_turn(self) -> int | None:
        try:
            proc = subprocess.run([SETSTATE, "--mint-turn"],
                                  capture_output=True, text=True,
                                  timeout=10)
        except Exception as exc:
            self._log(f"WALL mint-turn failed to run: "
                      f"{type(exc).__name__}: {exc}")
            return None
        if proc.returncode != 0:
            self._log(f"WALL mint-turn rc={proc.returncode}")
            return None
        for tok in reversed(proc.stdout.split()):
            if tok.isdigit():
                return int(tok)
        self._log(f"WALL mint-turn unparsable output: "
                  f"{proc.stdout.strip()[:80]}")
        return None

    # -- worker -------------------------------------------------------
    def _loop(self) -> None:
        while True:
            job = None
            with self._lock:
                if self._jobs:
                    job = self._jobs.popleft()
                want = self._floor_want
                closed = self._closed
            if job is not None:
                job()
                continue
            if want is not None:
                avatar, since, immediate = want
                if avatar == self._floor_applied:
                    with self._lock:
                        if self._floor_want is want:
                            self._floor_want = None
                    continue
                if immediate or (time.monotonic() - since
                                 >= self.DEBOUNCE_S):
                    rc = self._setstate(["--manual", f"avatar={avatar}"],
                                        f"floor->{avatar}")
                    with self._lock:
                        if rc == 0:
                            self._floor_applied = avatar
                        if self._floor_want is want:
                            self._floor_want = None
                    continue
            if closed and want is None:
                return
            time.sleep(0.02)

    def _run_job(self, job, timeout: float = 6.0) -> None:
        done = threading.Event()

        def wrapped():
            try:
                job()
            finally:
                done.set()

        with self._lock:
            self._jobs.append(wrapped)
        if not done.wait(timeout):
            self._log("WALL job did not finish within "
                      f"{timeout:.0f}s (continuing)")

    # -- public API ---------------------------------------------------
    def floor(self, avatar: str, *, immediate: bool = False) -> None:
        with self._lock:
            cur = self._floor_want
            if cur is not None and cur[0] == avatar:
                if immediate and not cur[2]:
                    self._floor_want = (avatar, cur[1], True)
                return
            if cur is None and not immediate \
                    and avatar == self._floor_applied:
                return
            self._floor_want = (avatar, time.monotonic(), immediate)

    def session_start(self, expires_at: str) -> None:
        def job():
            turn = self._mint_turn()
            if turn is not None:
                args = [f"--turn={turn}", "--phase=4", "--sticky"]
                what = f"session start (sticky turn={turn})"
            else:
                args = ["--manual"]
                what = "session start (manual fallback)"
            args += ["scene=home", "avatar=listening",
                     "title=LIVE CALL", "text=ON CALL", "wave=true",
                     f"expiresAt={expires_at}"]
            rc = self._setstate(args, what)
            if rc == 0:
                self._session_active = True
                self._floor_applied = "listening"
                with self._lock:
                    if self._floor_want is not None \
                            and self._floor_want[0] == "listening":
                        self._floor_want = None

        self._run_job(job)

    def session_end(self) -> None:
        with self._lock:
            if not self._session_active:
                return
            self._session_active = False
            self._floor_want = None

        def job():
            rc = self._setstate(
                ["--manual", "scene=home", "avatar=idle", "title=",
                 "text=studiolab online", "sub=", "lines=[]",
                 "wave=false"],
                "session end -> home")
            if rc == 0:
                self._floor_applied = "idle"

        self._run_job(job)

    def close(self) -> None:
        with self._lock:
            self._closed = True


def _levels_norm(rms: float) -> float:
    # Recalibrated for the LIVE CALL path 2026-11-05 (do NOT copy the
    # listener.py scale here — it was calibrated for a different
    # capture chain). Input is pre-gain RMS (push divides by GAIN=3).
    # Measured in the room that night: ambient/silence pre-gain RMS
    # ~75-85 (SENT ambient ~230-260 post-gain) mapped to 0.72 with
    # the old (rms-5)/120 scale, so the "M" waved on room noise
    # alone — Javier: "la M se mueve random sin que yo hable."
    # New scale: floor 120 sits safely above ambient, so silence
    # maps to exactly 0.0; speech frames (pre-gain ~400 -> 0.46,
    # ~700 -> 0.77, peaks ~1000 -> 1.0; good voice seconds ran
    # rms_mean 850-1200+ post-gain, i.e. ~280-400 pre-gain mean
    # with peaks in the hundreds to ~1500) fill the wave without
    # living at the clamp. The min(1.0, ...) clamp still saturates
    # the wave instead of breaking it if the chain runs hot.
    v = max(0.0, rms - 120.0) / 850.0
    return round(min(1.0, v ** 0.7), 3)


class LevelsPublisher:
    """Feeds ~/pared/levels.json while this call session is live.

    Mirrors listener.py's publisher — same payload shape, same
    ~100 ms write throttle, atomic tmp + rename (normalization is
    the call-specific _levels_norm above, NOT the listener scale) —
    for exactly the call's lifetime (the listener is stopped in that
    window, so the two publishers never overlap). The value pushed
    per frame is the RMS of the exact appended PCM divided by
    alsa_audio.GAIN: the listener's scale is calibrated on native
    mic level, and the x3 software gain is a transport fix, not
    part of the room's level scale. Whether the wave FEEL matches
    turn mode can only be judged in the live room.
    """

    PATH = "/home/ztdgtll/pared/levels.json"
    N = 48

    def __init__(self, path: str = PATH, log_fn=log):
        self._path = path
        self._log = log_fn
        self._hist: list[float] = []
        self._last_write = 0.0

    def start(self) -> None:
        self._hist = []
        self._write(False, force=True)

    def push(self, rms: float) -> None:
        self._hist.append(rms)
        if len(self._hist) > self.N * 4:
            del self._hist[: self.N * 2]
        self._write(True)

    def stop(self) -> None:
        self._write(False, force=True)

    def _write(self, active: bool, force: bool = False) -> None:
        now = time.time()
        if active and not force and now - self._last_write < 0.1:
            return
        self._last_write = now
        try:
            payload = {"levels": [_levels_norm(v)
                                  for v in self._hist[-self.N:]],
                       "active": bool(active), "ts": int(now * 1000)}
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.rename(tmp, self._path)
        except Exception:
            pass


class PatientSource:
    """Async-iterator wrapper that shapes the mic frame stream so
    the provider's VAD does not endpoint Javier mid-thought.

    Speech passes through untouched (zero added latency). A pause
    that follows speech is held locally: if speech resumes within
    the hangover, the stream continues with COMPRESSED_FRAMES of
    true silence plus the last PREROLL_FRAMES of the held buffer
    (the soft onset tail) — the provider sees at most ~200 ms of
    silence where the real pause was up to the hangover long. If
    the hangover expires, the held pause is emitted in full and
    everything passes through again, so the turn closes on real
    silence. While reply audio is playing (playing_fn) nothing is
    ever held and a pending hold is released in full: barge-in is
    never delayed. Silence before a turn's first speech is never
    held. Held frames are dropped only if the stream itself ends.
    """

    SPEECH_RMS = 450        # post-gain frame RMS: room floor ~200,
                            # soft speech ~900 (measured 2026-11-05)
    PREROLL_FRAMES = 8      # ~300 ms of held tail replayed on resume
    COMPRESSED_FRAMES = 5  # ~200 ms of true silence on resume

    def __init__(self, source, *, hangover_s: float = 3.0,
                 playing_fn=None, levels: LevelsPublisher | None = None,
                 now_fn=time.monotonic):
        self._frames = source.__aiter__()
        self._hangover = max(0.0, hangover_s)
        self._playing = playing_fn or (lambda: False)
        self._levels = levels
        self._now = now_fn
        self._out: collections.deque = collections.deque()
        self._held: list[bytes] = []
        self._hold_since: float | None = None
        self._in_speech = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        while not self._out:
            frame = await self._frames.__anext__()
            self._step(frame)
        return self._out.popleft()

    def _flush_held(self) -> None:
        self._out.extend(self._held)
        self._held = []
        self._hold_since = None

    def _step(self, frame: bytes) -> None:
        rms = pcm_rms(frame)
        if self._levels is not None:
            self._levels.push(rms / GAIN)
        speech = rms >= self.SPEECH_RMS
        if self._hangover <= 0.0:
            self._out.append(frame)
            return
        if self._playing():
            if self._held:
                self._flush_held()
            self._out.append(frame)
            self._in_speech = speech
            return
        if self._held:
            now = self._now()
            if now - (self._hold_since or now) >= self._hangover:
                # Hangover expired: release the pause in full and
                # return to plain pass-through (silence included);
                # the provider closes the turn on the real pause.
                self._flush_held()
                self._out.append(frame)
                self._in_speech = speech
            elif speech:
                tail = self._held[-self.PREROLL_FRAMES:]
                fill = self.COMPRESSED_FRAMES - len(tail)
                if fill > 0:
                    self._out.extend([b"\0" * FRAME_BYTES] * fill)
                self._out.extend(tail)
                self._out.append(frame)
                self._held = []
                self._hold_since = None
                self._in_speech = True
            else:
                self._held.append(frame)
            return
        if speech:
            self._in_speech = True
            self._out.append(frame)
        elif self._in_speech:
            # First quiet frame after speech: hold the pause
            # instead of streaming it.
            self._held = [frame]
            self._hold_since = self._now()
        else:
            self._out.append(frame)


def _wav_frames(path: str) -> list[bytes]:
    import wave as _wave
    with _wave.open(path) as w:
        raw = w.readframes(w.getnframes())
    return [raw[i:i + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")
            for i in range(0, len(raw), FRAME_BYTES)]


def _raw_frames(path: str) -> list[bytes]:
    with open(path, "rb") as f:
        raw = f.read()
    return [raw[i:i + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")
            for i in range(0, len(raw), FRAME_BYTES)]


class InjectSource:
    """Mic source with file-triggered in-session injection scripts.

    Wraps the PatientSource. Between mic frames it polls for trigger
    files; on a trigger it emits the script INSTEAD of mic frames
    (the inner source is simply not pulled, so its state freezes).
    Scripts are pre-framed 24 kHz mono; the SDK paces the appends.
    """

    ZEROS_2S = 50
    ZEROS_5S = 125

    PRE_ZEROS = 75  # 3 s of zeros before an at-start script

    def __init__(self, inner, levels=None, inject_at_start=False):
        self._inner = inner.__aiter__()
        self._levels = levels
        self._script: collections.deque = collections.deque()
        self._poll = 0
        self._pulled = 0
        self._started = False
        self._kind = "wav-at-start"
        if inject_at_start:
            zeros = b"\0" * FRAME_BYTES
            frames = _wav_frames(QUESTION_WAV)
            self._script = collections.deque(
                [zeros] * (self.PRE_ZEROS + self.ZEROS_2S) + frames
                + [zeros] * self.ZEROS_5S)

    def __aiter__(self):
        return self

    def _check_triggers(self) -> None:
        for name, kind in (("INJECT", "wav"), ("INJECT2", "clip")):
            path = os.path.join(TRIG_DIR, name)
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
                zeros = b"\0" * FRAME_BYTES
                if kind == "wav":
                    frames = _wav_frames(QUESTION_WAV)
                    self._script = collections.deque(
                        [zeros] * self.ZEROS_2S + frames
                        + [zeros] * self.ZEROS_5S)
                else:
                    frames = _raw_frames(FAILED_CLIP)
                    self._script = collections.deque(
                        [zeros] * self.ZEROS_2S + frames)
                self._kind = kind
                return

    async def __anext__(self) -> bytes:
        if not self._script:
            self._poll += 1
            if self._poll % 10 == 0:
                self._check_triggers()
        if self._script:
            if not self._started:
                self._started = True
                log(f"INJECT_START kind={self._kind} "
                    f"frames={len(self._script)}")
            frame = self._script.popleft()
            self._pulled += 1
            if self._levels is not None:
                self._levels.push(pcm_rms(frame) / GAIN)
            if not self._script:
                log(f"INJECT_END pulled={self._pulled} (mic resumes)")
                self._started = False
                self._pulled = 0
            return frame
        return await self._inner.__anext__()


class MeteredSource:
    """Wraps the final source handed to VoiceCall.run and measures how
    long _input waits for each frame. While the SDK awaits
    source.__anext__() it sends NOTHING (zeros only follow EOF or a
    received frame) — so source waits are genuine holes in the append
    stream, distinct from send time inside _event. Window stats are
    read/reset by the call's poll_state every ~10 s."""

    HOLE_S = 0.2

    def __init__(self, inner):
        self._inner = inner.__aiter__()
        self.frames = 0
        self._w_frames = 0
        self._w_wait_max = 0.0
        self._w_holes = 0
        self._w_hole_time = 0.0

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        t0 = time.monotonic()
        frame = await self._inner.__anext__()
        wait = time.monotonic() - t0
        self.frames += 1
        self._w_frames += 1
        self._w_wait_max = max(self._w_wait_max, wait)
        if wait > self.HOLE_S:
            self._w_holes += 1
            self._w_hole_time += wait
        return frame

    def window(self) -> dict:
        out = {"frames": self._w_frames, "wait_max": self._w_wait_max,
               "holes": self._w_holes, "hole_time": self._w_hole_time}
        self._w_frames = 0
        self._w_wait_max = 0.0
        self._w_holes = 0
        self._w_hole_time = 0.0
        return out


class ContinuousSource:
    """Endless file-fed source for the continuous-input control
    (Astra, brief-4 follow-up): one known speech burst per 45 s
    cycle, digital zeros elsewhere, no shaping of any kind. If the
    wedge appears under this source, source pauses are excluded as
    the entry path; if it never appears, they become the suspect."""

    CYCLE_FRAMES = 45 * 25

    def __init__(self, levels=None):
        self._levels = levels
        self._speech = _wav_frames(QUESTION_WAV)
        self._pos = 0
        self._cycle = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._pos == 0:
            self._cycle += 1
            log(f"CYCLE {self._cycle} (speech burst)")
        if self._pos < len(self._speech):
            frame = self._speech[self._pos]
        else:
            frame = b"\0" * FRAME_BYTES
        self._pos = (self._pos + 1) % self.CYCLE_FRAMES
        if self._levels is not None:
            self._levels.push(pcm_rms(frame) / GAIN)
        return frame


class SequenceSource:
    """Distinguishable-stimuli source (Astra, brief-4 follow-up):
    cycles named bursts (Alfa/Bravo/Charlie) separated by digital
    silence, logging each burst so server verdicts (turn vs
    input_suppressed, RAW-logged with item ids) attribute per
    stimulus: repetition effect vs content vs timing."""

    GAP_FRAMES = 40 * 25

    def __init__(self, spec, levels=None):
        self._levels = levels
        self._bursts = []
        for item in spec:
            tag, path = item.split("=", 1)
            frames = _wav_frames(path)
            self._bursts.append((tag, frames))
            log(f"SEQ loaded tag={tag} frames={len(frames)}")
        self._gen = self._make()

    def _make(self):
        rnd = 0
        while True:
            rnd += 1
            for tag, frames in self._bursts:
                log(f"BURST round={rnd} tag={tag}")
                yield from frames
                for _ in range(self.GAP_FRAMES):
                    yield b"\0" * FRAME_BYTES

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        frame = next(self._gen)
        if self._levels is not None:
            self._levels.push(pcm_rms(frame) / GAIN)
        return frame


class LoggingCall(VoiceCall):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._c_file = open(C_PATH, "wb")
        # Send-completion accounting (Astra brief-4 follow-up): C alone
        # cannot exclude appends discarded inside _event; count every
        # layer and hash exactly the PCM whose send completed.
        self._att = 0
        self._comp = 0
        self._gated_n = 0
        self._failed_n = 0
        self._comp_bytes = 0
        self._h = hashlib.sha256()
        self._lat_max = 0.0
        self._gap_max = 0.0
        self._last_comp_t = 0.0
        self.hash_at = 0
        self._flush_n = 0
        self._flush_max = 0.0
        self._sent_frames = 0
        self._sec_frames = 0
        self._sec_rms_sum = 0
        self._sec_rms_max = 0
        self._sec_silent = 0
        self._sec_start = time.monotonic()
        self._last_state = None

    async def _event(self, event):
        etype = event.get("type")
        if etype == "input_audio_buffer.append":
            pcm = base64.b64decode(event["audio"])
            self._att += 1
            _t0 = time.monotonic()
            try:
                await super()._event(event)  # raises _InputGated if invalidated
            except _InputGated:
                self._gated_n += 1
                raise
            except Exception:
                self._failed_n += 1
                raise
            _now = time.monotonic()
            self._lat_max = max(self._lat_max, _now - _t0)
            if self._last_comp_t:
                self._gap_max = max(self._gap_max, _now - self._last_comp_t)
            self._last_comp_t = _now
            self._comp += 1
            self._comp_bytes += len(pcm)
            self._h.update(pcm)
            if self.hash_at and self._comp == self.hash_at:
                log(f"HASHAT n={self._comp} "
                    f"sha256={self._h.hexdigest()} "
                    f"bytes={self._comp_bytes}")
            self._c_file.write(pcm)
            self._sent_frames += 1
            rms = int(pcm_rms(pcm))
            self._sec_frames += 1
            self._sec_rms_sum += rms
            self._sec_rms_max = max(self._sec_rms_max, rms)
            if rms < 50:
                self._sec_silent += 1
            now = time.monotonic()
            if now - self._sec_start >= 1.0:
                mean = self._sec_rms_sum // max(1, self._sec_frames)
                log(f"SENT {self._sec_frames} frames rms_mean={mean} "
                    f"rms_max={self._sec_rms_max} silent={self._sec_silent} "
                    f"held={self.held} gated={self._gated()}")
                self._sec_frames = self._sec_rms_sum = self._sec_rms_max = 0
                self._sec_silent = 0
                self._sec_start = now
            return
        if etype != "input_audio_buffer.commit":
            log(f"OUT event {etype}")
        await super()._event(event)

    def _track_turn(self, event) -> bool:
        result = super()._track_turn(event)
        log(f"IN {event.get('type')} tracked={result} "
            f"submission={event.get('submission_message_id', '?')} "
            f"item={event.get('item_id', '?')} "
            f"response_msg={event.get('response_message_id', '?')}")
        return result

    def _confirm(self, event) -> None:
        super()._confirm(event)
        _stream = getattr(self, "_stream", None)
        log(f"READY ids call={self.call_id} session={self.session_id} "
            f"thread={self.thread_id} "
            f"stream_id={getattr(_stream, 'stream_id', None)} "
            f"stream_type={type(_stream).__name__}")
        log("session READY (confirmed)")

    def _flush(self) -> None:
        # A suppression (and speech_started) can route here through
        # the ReplyGate; time it — a blocked flush would stall the
        # receive loop itself (Astra, brief-4 follow-up).
        _t0 = time.monotonic()
        super()._flush()
        _dur = time.monotonic() - _t0
        self._flush_n += 1
        self._flush_max = max(self._flush_max, _dur)
        if _dur > 0.05:
            log(f"FLUSH slow dur={_dur:.3f} n={self._flush_n}")

    async def poll_state(self) -> None:
        while True:
            state = (self.held, self._gated(), self._audible_frames,
                     round(max(0.0, self._playing_until - time.monotonic()), 1))
            if state != self._last_state:
                log(f"STATE held={state[0]} gated={state[1]} "
                    f"audible_frames={state[2]} playing_in={state[3]}s")
                self._last_state = state
            await asyncio.sleep(0.1)


class WallCall(LoggingCall):
    """LoggingCall + the wall bridge (capas Sesion y Piso)."""

    # Safety net for the generation window: if no response was ever
    # created and nothing became audible within this long after the
    # turn settled, the window is considered closed (see poll_state).
    GEN_FALLBACK_S = 20.0

    def __init__(self, *a, wall: WallDriver | None = None, **kw):
        super().__init__(*a, **kw)
        self._wall = wall
        self._was_playing = False
        self._gen_since: float | None = None
        self._gen_saw_response = False

    def _playing_now(self) -> bool:
        return bool(self._playback) or self._audible_frames > 0 \
            or time.monotonic() < self._playing_until

    def _track_turn(self, event) -> bool:
        result = super()._track_turn(event)
        if self._wall is not None:
            etype = event.get("type")
            if etype == "hatch.voice.turn_started":
                # Javier has the floor: attentive listening face.
                self._gen_since = None
                self._wall.floor("listening")
            elif etype == "hatch.voice.turn_settled" and result:
                # A tracked turn was actually retired: the response-
                # generation window opens. Show working until reply
                # audio owns the floor (unless it already does).
                if not self._playing_now():
                    self._gen_since = time.monotonic()
                    self._gen_saw_response = False
                    self._wall.floor("working")
        return result

    async def barge_in(self) -> None:
        # Mouth-close gesture: the wall yields BEFORE the network
        # cancel goes out, and without any debounce.
        if self._wall is not None:
            self._gen_since = None
            self._wall.floor("listening", immediate=True)
        await super().barge_in()

    def _flush(self) -> None:
        # Reply audio was cut (gate flush / interruption): same
        # immediate yield as barge-in. Also closes a generation
        # window whose pending reply was fenced off.
        if self._wall is not None and (self._was_playing
                                       or self._gen_since is not None):
            self._gen_since = None
            self._wall.floor("listening", immediate=True)
        super()._flush()

    async def poll_state(self) -> None:
        while True:
            state = (self.held, self._gated(), self._audible_frames,
                     round(max(0.0, self._playing_until - time.monotonic()), 1))
            if state != self._last_state:
                log(f"STATE held={state[0]} gated={state[1]} "
                    f"audible_frames={state[2]} playing_in={state[3]}s")
                self._last_state = state
            if self.advisory_error_count != getattr(self, "_last_advisory", 0):
                self._last_advisory = self.advisory_error_count
                log(f"ADVISORY count={self.advisory_error_count}")
            if time.monotonic() - getattr(self, "_stats_t", 0.0) >= 10.0:
                self._stats_t = time.monotonic()
                _meter = getattr(self, "_meter", None)
                _w = _meter.window() if _meter is not None else {}
                log(f"STATS att={self._att} comp={self._comp} "
                    f"gated={self._gated_n} failed={self._failed_n} "
                    f"flush_n={self._flush_n} "
                    f"flush_max={self._flush_max:.3f} "
                    f"src_frames={_w.get('frames')} "
                    f"src_wait_max={_w.get('wait_max', 0.0):.3f} "
                    f"src_holes={_w.get('holes')}")
            playing = bool(self._playback) or self._audible_frames > 0 \
                or time.monotonic() < self._playing_until
            if self._wall is not None:
                if playing and not self._was_playing:
                    self._wall.floor("speaking")
                elif not playing and self._was_playing:
                    self._wall.floor("listening")
                if playing:
                    # Reply audio owns the floor: generation window
                    # is superseded, nothing to close.
                    self._gen_since = None
                elif self._gen_since is not None:
                    if self._reply_gate.open_response is not None:
                        self._gen_saw_response = True
                    elif self._gen_saw_response or (
                            time.monotonic() - self._gen_since
                            > self.GEN_FALLBACK_S):
                        # The response ended (or never began) with
                        # no audible reply: working must not linger.
                        self._gen_since = None
                        self._wall.floor("listening")
            self._was_playing = playing
            await asyncio.sleep(0.1)


async def run(audio, args) -> str:
    pairing = config.load_json(config.PAIRING_FILE)
    if not pairing or not pairing.get("access_token"):
        raise RuntimeError("not paired")
    ident = identity.load_or_create()
    logging.getLogger("websockets").setLevel(logging.WARNING)
    vms, status = await asyncio.to_thread(
        muse_api.fetch_vms_with_status, pairing["access_token"],
        muse_api.api_root(pairing.get("api_url_v2", "")))
    if status != 200:
        raise ConnectionError("VM lookup failed")
    matches = [vm for vm in vms if vm["vm_id"] == args.vm_id]
    if len(matches) != 1:
        raise ValueError("--vm-id must exactly match one leased VM")
    vm = matches[0]
    session = LinkSession(
        noise_host=pairing.get("noise_host") or DEFAULT_NOISE_HOST,
        vm_id=vm["vm_id"], vm_auth_token=vm.get(AUTH_KEY), app_id=args.app_id,
        register_device=False,
        device=DeviceDescription(ident.node_id, "Muse voice", __version__, {}),
        run_command=lambda *unused: {"ok": False, "error": "unsupported command"},
    )
    stop = asyncio.Event()
    connection = asyncio.create_task(session.run(stop))
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, current.cancel)
    wall = WallDriver()
    try:
        await session.wait_transport_ready(35)
        call = WallCall(
            session, device_id=ident.node_id, thread_id=args.thread_id,
            half_duplex=not args.full_duplex,
            mic_threshold=args.mic_threshold, idle_timeout=args.idle_timeout,
            overall_timeout=args.overall_timeout, wall=wall)
        log(f"call created half_duplex={not args.full_duplex} "
            f"mic_threshold={args.mic_threshold}")
        expires = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + args.overall_timeout + 1800))
        wall.session_start(expires)
        levels = LevelsPublisher()
        levels.start()
        playing_gate: dict = {"fn": lambda: False}
        patient = PatientSource(
            audio.frames(), hangover_s=args.pause_hangover,
            playing_fn=lambda: playing_gate["fn"](), levels=levels)
        playing_gate["fn"] = call._playing_now
        if args.sequence:
            source = SequenceSource(args.sequence, levels=levels)
        elif args.continuous:
            source = ContinuousSource(levels=levels)
        else:
            source = InjectSource(patient, levels=levels,
                                  inject_at_start=args.inject_at_start)
        meter = MeteredSource(source)
        if args.mute_playback:
            audio.write = lambda pcm: None
            log("playback MUTED for this run (replies stay out of the room)")
        call._meter = meter
        call.hash_at = args.hash_at
        poller = asyncio.create_task(call.poll_state())
        reason = None
        try:
            reason = await call.run(meter, audio, realtime_input=True)
            return reason
        finally:
            log(f"END reason={reason or call.reason} "
                f"ack={call.end_call_acknowledged} "
                f"cleanup_error={call.cleanup_error!r} "
                f"drain_incomplete={call.media_drain_incomplete} "
                f"advisory={call.advisory_error_count} "
                f"session={call.session_id}")
            log(f"SENDSTATS att={call._att} comp={call._comp} "
                f"gated={call._gated_n} failed={call._failed_n} "
                f"inflight={call._att - call._comp - call._gated_n - call._failed_n} "
                f"bytes={call._comp_bytes} "
                f"sha256={call._h.hexdigest()} "
                f"lat_max={call._lat_max:.3f} gap_max={call._gap_max:.3f}")
            log(f"PLAYBACK drops={audio.playback_drops}")
            poller.cancel()
            wall.session_end()
            levels.stop()
    finally:
        stop.set()
        try:
            await asyncio.wait_for(connection, 3)
        except (Exception, asyncio.CancelledError):
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
        wall.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm-id", required=True)
    parser.add_argument("--app-id", default="hatch-web")
    parser.add_argument("--full-duplex", action="store_true")
    parser.add_argument("--mic-threshold", type=int, default=0)
    parser.add_argument("--pause-hangover", type=float, default=3.0)
    parser.add_argument("--idle-timeout", type=positive_seconds, default=90)
    parser.add_argument("--overall-timeout", type=positive_seconds, default=600)
    parser.add_argument("--thread-id")
    parser.add_argument("--inject-at-start", action="store_true")
    parser.add_argument("--hash-at", type=int, default=0)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--sequence", nargs="*", default=None,
                        metavar="TAG=PATH")
    parser.add_argument("--mute-playback", action="store_true")
    args = parser.parse_args()
    audio = AlsaAudio()
    try:
        reason = asyncio.run(run(audio, args))
        print(f"Voice call ended: {reason}.", flush=True)
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Voice call stopped.", flush=True)
        return 130
    except Exception as exc:
        print(f"Voice call failed: {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        audio.close()


if __name__ == "__main__":
    raise SystemExit(main())
