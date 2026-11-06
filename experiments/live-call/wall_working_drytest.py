"""Isolated dry test for room3's three 2026-11-05 additions:
avatar=working window, wave/levels publishing, PatientSource.

Runs on the Pi against a SCRATCH copy of voice_call_room3.py (the
caller's cwd). FULLY isolated: WallDriver._setstate and _mint_turn
are replaced with recorders (setstate.py NEVER runs), the levels
publisher writes to a temp file, and the patient source consumes
synthetic PCM. The real wall, ~/pared/state.json and
~/pared/levels.json are never written by the test (asserted via a
fingerprint check on the real state: the test's minted turn and
2099 expiry must be absent), no call is launched and no runner
process is touched.

Sections:
  WORKING — turn_settled -> exactly one avatar=working write;
    replayed settle writes nothing; playback start -> speaking;
    response ends with no audio -> fallback to listening; barge-in
    (pending and applied working) -> immediate listening; _flush
    cancels a pending working; event storm coalesces; session
    start/end toggle wave=true/false in the recorded scene writes.
  LEVELS — payload shape (48 floats in 0..1, active, ts), listener
    normalization anchors, ~100 ms write cadence, final
    active=false write on stop.
  PATIENT — (a) 1 s mid-utterance pause compressed to the ~200 ms
    silence + onset-tail shape, speech intact and ordered, levels
    fed once per input frame; (b) 5 s pause released in full after
    the hangover; (c) frames are never held while "playback" is
    active, including playback starting mid-hold; (d) frame size
    unchanged and hangover=0 is pure pass-through. The patient
    section proves OUR output stream shape only — the provider
    effect can only be proven in a live room test.
"""

import asyncio
import collections
import json
import os
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from musegadget.voice_call import FRAME_BYTES, FRAME_SAMPLES, VoiceCall  # noqa: E402
from musegadget.voice_gate import ReplyGate  # noqa: E402

import voice_call_room3 as room3  # noqa: E402

STATE = "/home/ztdgtll/pared/state.json"
LEVELS_REAL = "/home/ztdgtll/pared/levels.json"
FAILURES = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {extra}", flush=True)
    if not cond:
        FAILURES.append(label)


# Stub the network-side effects the test does not exercise.
async def _barge_stub(self):
    return None


VoiceCall.barge_in = _barge_stub
VoiceCall._flush = lambda self: None


def avatars(writes):
    out = []
    for args in writes:
        for tok in args:
            if tok.startswith("avatar="):
                out.append(tok.split("=", 1)[1])
    return out


async def working_section():
    writes = []
    wall = room3.WallDriver(log_fn=lambda m: None)

    def recorder(args, what):
        writes.append(list(args))
        return 0

    wall._setstate = recorder
    wall._mint_turn = lambda: 424242
    wall.session_start("2099-01-01T00:00:00Z")
    await asyncio.sleep(0.5)
    check("session start wrote listening once",
          avatars(writes) == ["listening"], f"got {avatars(writes)}")
    check("session start write carries wave=true",
          "wave=true" in writes[0], f"got {writes[0]}")

    call = object.__new__(room3.WallCall)
    call._wall = wall
    call._was_playing = False
    call._gen_since = None
    call._gen_saw_response = False
    call._active_turns = {}
    call._settled_turns = {}
    call._activity = 0.0
    call.held = False
    call._gated = lambda: False
    call._playback = collections.deque()
    call._audible_frames = 0
    call._playing_until = 0.0
    call._last_state = None
    call._reply_gate = ReplyGate()
    gate = call._reply_gate
    poller = asyncio.create_task(call.poll_state())

    def started(mid, iid):
        return {"type": "hatch.voice.turn_started",
                "submission_message_id": mid, "item_id": iid}

    def settled(mid):
        return {"type": "hatch.voice.turn_settled",
                "response_message_id": mid}

    def working_writes():
        return avatars(writes).count("working")

    # 1. turn start (deduped, already listening) then settle -> working x1
    call._track_turn(started("m1", "i1"))
    await asyncio.sleep(0.5)
    check("turn_started while listening writes nothing new",
          avatars(writes) == ["listening"], f"got {avatars(writes)}")
    call._track_turn(settled("m1"))
    await asyncio.sleep(0.6)
    check("turn_settled -> exactly one working write",
          working_writes() == 1 and avatars(writes)[-1] == "working",
          f"got {avatars(writes)}")
    call._track_turn(settled("m1"))  # replay: no longer tracked
    await asyncio.sleep(0.5)
    check("replayed settle writes nothing", working_writes() == 1,
          f"got {avatars(writes)}")

    # 2. playback start -> speaking supersedes
    gate.event({"type": "response.created", "response": {"id": "r1"}})
    call._audible_frames = 5
    await asyncio.sleep(0.7)
    check("playback start -> speaking",
          avatars(writes)[-1] == "speaking"
          and avatars(writes).count("speaking") == 1,
          f"got {avatars(writes)}")
    check("generation window closed by playback", call._gen_since is None)

    # drain -> listening
    call._audible_frames = 0
    gate.event({"type": "response.done", "response": {"id": "r1"}})
    await asyncio.sleep(0.7)
    check("playback drain -> listening", avatars(writes)[-1] == "listening",
          f"got {avatars(writes)}")

    # 3. response with NO audio -> fallback to listening
    call._track_turn(started("m2", "i2"))
    call._track_turn(settled("m2"))
    await asyncio.sleep(0.6)
    check("second settle -> working again", working_writes() == 2
          and avatars(writes)[-1] == "working", f"got {avatars(writes)}")
    gate.event({"type": "response.created", "response": {"id": "r2"}})
    await asyncio.sleep(0.3)  # let the poller observe the open response
    gate.event({"type": "response.completed", "response": {"id": "r2"}})
    await asyncio.sleep(0.7)
    check("no-audio response -> fallback listening",
          avatars(writes)[-1] == "listening" and call._gen_since is None,
          f"got {avatars(writes)}")

    # 4. barge-in while working is still PENDING (inside debounce)
    call._track_turn(started("m3", "i3"))
    call._track_turn(settled("m3"))
    await call.barge_in()
    await asyncio.sleep(0.7)
    check("barge-in during pending working: working never written",
          working_writes() == 2, f"got {avatars(writes)}")
    check("barge-in during pending working: listening holds",
          avatars(writes)[-1] == "listening", f"got {avatars(writes)}")

    # 5. barge-in while working is APPLIED -> immediate listening
    call._track_turn(started("m4", "i4"))
    call._track_turn(settled("m4"))
    await asyncio.sleep(0.6)
    check("third settle -> working applied", working_writes() == 3
          and avatars(writes)[-1] == "working", f"got {avatars(writes)}")
    n_before = len(writes)
    await call.barge_in()
    await asyncio.sleep(0.35)  # < debounce: only an immediate write lands
    check("barge-in during applied working -> immediate listening",
          len(writes) > n_before and avatars(writes)[-1] == "listening",
          f"got {avatars(writes)}")

    # 6. _flush during the generation window cancels pending working
    call._track_turn(started("m5", "i5"))
    call._track_turn(settled("m5"))
    call._flush()
    await asyncio.sleep(0.7)
    check("flush during pending working: working never written",
          working_writes() == 3, f"got {avatars(writes)}")

    # 7. rapid event storm -> coalesced into one final write
    n_before = len(writes)
    for k in range(3):
        call._track_turn(started(f"m6{k}", f"i6{k}"))
        call._track_turn(settled(f"m6{k}"))
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.9)
    new = avatars(writes[n_before:])
    check("event storm coalesced to a single working write",
          new == ["working"], f"got {new}")

    poller.cancel()
    wall.session_end()
    await asyncio.sleep(0.4)
    check("session end -> idle", avatars(writes)[-1] == "idle",
          f"got {avatars(writes)}")
    check("session end write carries wave=false",
          "wave=false" in writes[-1], f"got {writes[-1]}")
    wall.close()
    print(f"WRITES TRACE: {avatars(writes)}", flush=True)


def levels_section():
    check("levels norm anchors match listener scale",
          room3._levels_norm(5.0) == 0.0
          and room3._levels_norm(125.0) == 1.0
          and room3._levels_norm(65.0) == 0.616
          and room3._levels_norm(10000.0) == 1.0)
    fd, tmp = tempfile.mkstemp(prefix="levels-test-", suffix=".json")
    os.close(fd)
    try:
        pub = room3.LevelsPublisher(path=tmp, log_fn=lambda m: None)
        pub.start()
        with open(tmp) as f:
            p0 = json.load(f)
        check("levels start: inactive payload with ts",
              p0["active"] is False and isinstance(p0["ts"], int))
        for _ in range(60):
            pub.push(65.0)
        time.sleep(0.12)  # let the 100 ms throttle let a write land
        pub.push(65.0)
        with open(tmp) as f:
            p1 = json.load(f)
        check("levels payload shape: 48 floats in 0..1, active, fresh ts",
              len(p1["levels"]) == 48
              and all(isinstance(v, float) and 0.0 <= v <= 1.0
                      for v in p1["levels"])
              and p1["active"] is True
              and abs(p1["ts"] - int(time.time() * 1000)) < 5000,
              f"first={p1['levels'][:3]}")
        check("levels values normalized on the listener scale",
              all(v == 0.616 for v in p1["levels"]))
        # cadence: distinct mtimes over ~0.4 s of steady pushes
        pub2 = room3.LevelsPublisher(path=tmp, log_fn=lambda m: None)
        pub2.start()
        seen = set()
        t_end = time.time() + 0.4
        while time.time() < t_end:
            pub2.push(100.0)
            seen.add(os.path.getmtime(tmp))
            time.sleep(0.01)
        check("levels write cadence ~100 ms", 3 <= len(seen) <= 6,
              f"distinct mtimes={len(seen)}")
        pub2.stop()
        with open(tmp) as f:
            p2 = json.load(f)
        check("levels stop: final write active=false",
              p2["active"] is False)
        pub.stop()
    finally:
        os.unlink(tmp)


async def patient_section():
    SIL = b"\0" * FRAME_BYTES

    def loud(v):
        return struct.pack("<h", v) * FRAME_SAMPLES

    class FakeClock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            return self.t

    def scripted(frames, clock, dt=0.04, flip_at=None, flag=None):
        async def gen():
            for i, f in enumerate(frames):
                if flip_at is not None and i == flip_at \
                        and flag is not None:
                    flag["playing"] = True
                clock.t += dt
                yield f
        return gen()

    class Sink:
        def __init__(self):
            self.values = []

        def push(self, rms):
            self.values.append(rms)

    async def collect(patient):
        return [f async for f in patient]

    # (a) 1 s mid-utterance pause -> compressed; speech intact
    clock = FakeClock()
    sink = Sink()
    seg1 = [loud(5000 + i) for i in range(5)]
    seg2 = [loud(6000 + i) for i in range(5)]
    frames_a = seg1 + [SIL] * 25 + [loud(350)] + seg2
    pat = room3.PatientSource(scripted(frames_a, clock), hangover_s=3.0,
                              levels=sink, now_fn=clock)
    out_a = await collect(pat)
    mid = out_a[5:-5]
    check("(a) speech on both sides intact and ordered",
          out_a[:5] == seg1 and out_a[-5:] == seg2, f"out={len(out_a)}")
    check("(a) 1 s pause compressed to preroll tail (7 sil + soft onset)",
          len(out_a) == 18 and mid[:7] == [SIL] * 7
          and mid[7] == loud(350), f"mid={len(mid)} frames")
    check("(d) frame size unchanged (40 ms PCM16LE 24 kHz)",
          all(len(f) == FRAME_BYTES for f in out_a))
    check("levels fed once per input frame at native (pre-gain) RMS",
          len(sink.values) == len(frames_a)
          and abs(sink.values[0] - 5000 / 3) < 1.0,
          f"n={len(sink.values)}")

    # (b) 5 s pause -> released in full after the hangover. The 10
    # trailing silence frames start a NEW hold (a pause following
    # speech) and are dropped at synthetic EOF — the documented
    # edge, same as the last case below; in a live call the stream
    # continues and they are compressed or released like any pause.
    clock = FakeClock()
    frames_b = [loud(5000)] * 3 + [SIL] * 125 + [loud(6000)] * 3 \
        + [SIL] * 10
    pat = room3.PatientSource(scripted(frames_b, clock), hangover_s=3.0,
                              now_fn=clock)
    out_b = await collect(pat)
    check("(b) 5 s pause released in full, in order, uncompressed",
          out_b == frames_b[:131] and out_b[3:128] == [SIL] * 125,
          f"in={len(frames_b)} out={len(out_b)}")

    # (c) playing: never held — including playback starting mid-hold
    clock = FakeClock()
    frames_c = [loud(5000)] * 3 + [SIL] * 50 + [loud(6000)] * 3
    pat = room3.PatientSource(scripted(frames_c, clock), hangover_s=3.0,
                              playing_fn=lambda: True, now_fn=clock)
    out_c = await collect(pat)
    check("(c) playing throughout: output identical to input",
          out_c == frames_c)
    clock = FakeClock()
    flag = {"playing": False}
    pat = room3.PatientSource(
        scripted(frames_c, clock, flip_at=13, flag=flag), hangover_s=3.0,
        playing_fn=lambda: flag["playing"], now_fn=clock)
    out_c2 = await collect(pat)
    check("(c) playback starting mid-hold releases it in full",
          out_c2 == frames_c)

    # (d) hangover=0 -> pure pass-through
    clock = FakeClock()
    frames_d = [loud(5000)] * 2 + [SIL] * 25 + [loud(6000)] * 2
    pat = room3.PatientSource(scripted(frames_d, clock), hangover_s=0.0,
                              now_fn=clock)
    out_d = await collect(pat)
    check("(d) hangover=0 disables shaping", out_d == frames_d)

    # leading silence is never held; a held tail is dropped at EOF
    clock = FakeClock()
    frames_e = [SIL] * 10 + [loud(5000)] * 3 + [SIL] * 2
    pat = room3.PatientSource(scripted(frames_e, clock), hangover_s=3.0,
                              now_fn=clock)
    out_e = await collect(pat)
    check("leading silence passes through; held tail dropped at EOF",
          out_e == frames_e[:13], f"out={len(out_e)}")


async def main():
    mtime_state = os.path.getmtime(STATE)
    mtime_levels = os.path.getmtime(LEVELS_REAL)
    await working_section()
    levels_section()
    await patient_section()
    # Isolation proof that survives a live room: the test's session
    # used minted turn 424242 and a 2099 expiry; neither fingerprint
    # may appear in the real wall state. (Plain mtime guards are
    # reported as notes only — the live system legitimately writes
    # both files while the test runs.)
    with open(STATE) as f:
        real_state = json.load(f)
    check("real wall carries no test fingerprint",
          real_state.get("turn") != 424242
          and "2099" not in str(real_state.get("expiresAt")),
          f"turn={real_state.get('turn')}")
    if os.path.getmtime(STATE) != mtime_state:
        print("[NOTE] state.json mtime moved during the run — the "
              "live system's own writes (recorder captured every "
              "driver write above; no real setstate was reachable)",
              flush=True)
    if os.path.getmtime(LEVELS_REAL) != mtime_levels:
        print("[NOTE] levels.json mtime moved during the run — "
              "the live system's own publisher (this test's "
              "publisher only ever opened its temp path)", flush=True)
    print("DRYTEST " + ("FAILED: " + ", ".join(FAILURES)
                        if FAILURES else "ALL PASS"), flush=True)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
