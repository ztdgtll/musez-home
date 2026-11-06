"""Realtime voice call through the studio's ALSA path + wall bridge.

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
from musegadget.link_client import DeviceDescription, LinkSession  # noqa: E402
from musegadget.service import DEFAULT_NOISE_HOST  # noqa: E402
from musegadget.voice_call import (  # noqa: E402
    FRAME_BYTES, VoiceCall, pcm_rms, positive_seconds)

AUTH_KEY = "vm_auth" + "_token"
C_PATH = "/home/ztdgtll/voice-test/room3-C.raw"
SETSTATE = "/home/ztdgtll/pared/setstate.py"


def log(msg: str) -> None:
    print(f"[probe {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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
    # Copied verbatim from listener.py (recalibrated there 2026-10-26
    # for the room's live capture range; the min(1.0, ...) clamp
    # saturates the wave instead of breaking it if the chain runs
    # hot).
    v = max(0.0, rms - 5.0) / 120.0
    return round(min(1.0, v ** 0.7), 3)


class LevelsPublisher:
    """Feeds ~/pared/levels.json while this call session is live.

    Mirrors listener.py's publisher — same payload shape, same
    normalization, same ~100 ms write throttle, atomic tmp + rename —
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


class LoggingCall(VoiceCall):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._c_file = open(C_PATH, "wb")
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
            await super()._event(event)  # raises _InputGated if invalidated
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
            f"submission={event.get('submission_id', '?')}")
        return result

    def _confirm(self, event) -> None:
        super()._confirm(event)
        log("session READY (confirmed)")

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
        poller = asyncio.create_task(call.poll_state())
        try:
            return await call.run(patient, audio, realtime_input=True)
        finally:
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
