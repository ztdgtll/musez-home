# Live voice call — research log, Nov 5 night → Nov 6, 2026 (after submission)

Continuation of [voice-call-live-2026-11-05.md](voice-call-live-2026-11-05.md).
The submitted demo is unchanged; everything below is post-submission work on
the same room, logged with the same rule as the rest of this repo: what was
measured, what it does and does not prove.

Code: [`experiments/live-call/voice_call_room4.py`](../live-call/voice_call_room4.py)
(instrumented runner) and the updated
[`alsa_audio.py`](../live-call/alsa_audio.py) backend.

## What was verified

**Full-duplex conversation in the room.** After the file-fed test of Nov 5,
the call ran live from the room's microphone array and speakers in
full-duplex, including barge-in (speaking over a reply). The wake-word
turn system remains as a backup only; the live call is the room's primary
mode by design decision.

**A level defect in sustained capture, found and fixed.** In continuous
capture the microphone DSP's output fell ~15 dB after a few minutes (both
USB channels together, measured at the ALSA tap), and only very loud speech
created turns. A software gain (×3, clamped) plus a fixed ASR channel in
the backend restored normal-level speech: three consecutive phrases at
normal volume created turns. The root cause inside the DSP (beamforming /
AEC running without a far-end reference) is characterized but not closed —
see the echo section below.

**The local send path, proven bit-for-bit.** The runner was extended with
per-layer send accounting (attempted / completed / gated / failed) and a
SHA-256 over exactly the PCM whose send completed. Injected into a live
session, a 369-frame known sequence (silence + the Nov 5 question WAV +
silence) completed with the hash matching the offline expectation exactly
(`51f984c1…f2ab`, 708,480 bytes). In a 25-minute continuous control —
one known speech burst every 45 s, digital silence between, no shaping —
**35,671 of 35,671 appends completed, 0 gated, 0 failed**. Whatever goes
wrong in the failure modes below, it is not the local send path.

## Two server-side phenomena, separated

Mining the pre-dispatcher event log (the runner now records every inbound
record before the SDK dispatches it) surfaced an event we had been blind
to: `hatch.voice.input_suppressed`. The SDK's ESP32 code describes the
case as *"side speech: no turn, nothing for it plays."* With it, the
night's failures separate into two distinct phenomena:

- **S — suppression.** Input is detected (VAD fires; transcription
  sometimes begins) and then explicitly discarded server-side; no turn is
  created. It is frequent, including in otherwise healthy calls, and its
  criteria are not published. In the 25-minute continuous control, **24 of
  32 identical bursts were suppressed and 10 formed turns**. Suppression
  also consumed real phrases during healthy live calls without any visible
  sign in the room.
- **W — the wedge.** A rarer state in which *all* input events stop —
  no speech events, no verdicts — while transport keepalives continue and
  control still acknowledges a clean end-of-call. A known WAV injected
  into a wedged session produced zero inbound records. W has not
  reappeared since it was instrumented for (counted injection inside the
  wedge is staged and ready); whether S escalates into W is an open
  question, recorded as temporal association only.

Questions now in front of the maintainers, in this form: after emitting
`input_suppressed` for an item, does that item's closure complete and the
next item stay enabled? And during a W window: how many input bytes did
the stream receive, how many reached the provider, and what events came
back?

## Echo cancellation: measured, not assumed

The array (Seeed reSpeaker XVF3800) expects a far-end reference — the
playback signal — which our original routing never gave it, so its AEC
never converged. Feeding the reference turned out to be broken by
attenuation in two stages, both found by measuring the reference *inside*
the chip: the device's USB playback volume sat at −23 dB, and the far-end
tap is padded ~−36 dB by design, compensable with the chip's reference
gain (set from measurement, not from a copied value).

Alignment is the remaining blocker. Envelope cross-correlation between a
raw microphone tap and the post-gain reference tap puts the echo
**~270–295 ms after the reference, consistently across all four
microphones** — the two independent playback routes (USB DAC for the room,
USB endpoint for the reference) accumulate very different latencies. The
chip's system-delay compensation accepts at most +16 ms, so the offset
cannot be dialed out in this topology.

The device's native topology — room playback through the array's own
3.5 mm output, one clock domain — was then wired and tested: a second,
hidden playback volume for the analog output was found at 31% and raised
(microphone pickup of the playback rose ~10×), and its audio quality is
speakerphone-grade, as expected from a voice-first device. After 40 s of
sustained loud playback the chip's convergence flag still read 0, so
native routing alone does not settle it; the deciding test is functional
(does the residual echo in the ASR output decay over adaptation time?),
not the flag.

Candidate design for the room's v2, undecided: split the routes — music
and general audio keep the hi-fi DAC; only the call's voice uses the
array's output, where alignment holds by construction and voice-grade
quality is sufficient for speech.

## Instruments added this night

- Pre-dispatcher logging of every inbound record, with identity fields
  (suppression payloads — item id and any scalar fields — are preserved).
- Send-completion accounting + PCM hash (above).
- Per-flush duration timing (a suppression routes through the reply
  gate's flush path).
- A continuous control source and a distinguishable-stimuli source
  (Alfa / Bravo / Charlie — natural, clearly addressed questions,
  [`voice-call-2026-11-06-alfa.wav`](voice-call-2026-11-06-alfa.wav),
  [`-bravo`](voice-call-2026-11-06-bravo.wav),
  [`-charlie`](voice-call-2026-11-06-charlie.wav)) so each server verdict
  can be attributed to a specific stimulus: repetition vs content vs
  timing. That run is in progress as this log is published.
- A playback-robustness fix in the backend: the player is pre-spawned
  warm and `write()` drops-and-counts instead of raising when the drain
  stalls (an early version of this failure killed a healthy call).

## Still open

- The wake-word → call handover (listener yields the microphone to the
  call, resumes on close) — the v2 build's centerpiece.
- Suppression characterization (Alfa/Bravo/Charlie) and the staged
  counted-injection test if W returns.
- Functional AEC cancellation test; the split-route decision.
- The DSP level sag in sustained capture (beam/raw-mic discrimination
  run is designed, not yet executed).
