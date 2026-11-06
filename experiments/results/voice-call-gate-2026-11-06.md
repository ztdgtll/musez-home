# Nov 6 (later) — the wake-word gate, the playback deaths, and what fixed them

Post-submission research log. Everything below ran live in the room on
2026-11-06 (ET). The system under test: saying the wake word in the room
now opens the live voice call (the Nov 5 build's resting state, decided
that night), the captured first phrase is injected as the call's first
input, and the conversation proceeds full-duplex. Turn-based mode
remains only as a technical backup.

Code: `experiments/live-call/listener.py` (the production wake
listener; the gate is its `run_gate` path),
`experiments/live-call/callwrap.sh` (the call supervisor),
`experiments/live-call/voice_call_room5.py` (the runner),
`experiments/live-call/alsa_audio.py` (the ALSA/PipeWire backend).

## The morning's gate calls — suppression is the wall

| # | Time | Config | Outcome |
|---|------|--------|---------|
| 1 | 08:19 | ASR channel, capture empty | 3 interventions, **3 suppressed**, 0 turns |
| 2 | 08:27 | ASR channel | injected opener suppressed; 1 live suppression |
| 3 | 08:33 | ASR channel + new AGC | opener suppressed; almost no input events |
| 4 | 08:38 | Conference channel + AGC | 2 suppressions, 0 turns (speech + transcripts seen) |
| 5 | 09:02 | Conference ch., fixed ×3 gain, hangover 0, DSP profile set+verified | **10 turns, 3 suppressions** — a real conversation |

Same server session throughout. Note what this table does *not* show:
calls 4 and 5 were not identically configured (AGC on vs off; gain and
hangover changed between them), and the early calls' entries were cold
injected openers while call 5's turns happened inside an established
conversation — different positions, different context. The closest
honest comparison is call 5 (10 turns, 3 suppressions) against the
09:13 call's single suppressed injection, which demonstrates no rate
change at all. What *is* measured is that verdicts alternate within
single calls under one configuration (call 5; the soak below), so no
fixed per-call property explains them. `S` (server-side
`input_suppressed`) remains the one open problem, and this log does
not pretend otherwise. An external review of this log made both
corrections — configuration and position — before publication; the
text already includes them.

## The playback deaths — and the fix

Three calls died with the SDK raising `ValueError: voice playback
buffer full` (its reply queue is capped at 5 s of audio; a paced task
drains one 40 ms frame at a time):

- test25 (Nov 5, earlier runner)
- call 5 above, at ~3 min — final stats: input gap_max 1.207 s
- the 09:10 call, at 69 s — input gap_max **5.927 s**, a burst of
  inbound records in the second before death

Root cause (ours, not the SDK's): our runner logged **every inbound
record synchronously** (`print(flush=True)` per record, inside the
receive chain). Audio deltas arrive in bursts of hundreds; each flush
is a file write, and the accumulated stalls starved the SDK's drain
task until the queue overflowed. The minimal earlier runner — logging
limited to per-second summaries — had sustained a 10-minute call the
night before; every build that died carried the per-record logging.
An independent second-opinion review of the (sanitized) evidence
ranked the same cause first, and noted a single multi-second stall
can be sufficient: audio buffered upstream during the stall lands in
the queue all at once when the loop resumes.

Fixes, deployed as one bundle:

1. **Buffered logging** — `log()` now only enqueues lines in memory;
   a writer thread batches them to disk. Audio deltas are counted,
   not logged (one summary line per 250).
2. **Single playback feed** — with the speakers moved to the array's
   own output, the audio was reaching the same sink twice (the main
   player plus a "reference feed" built for the earlier DAC
   topology), two independently clocked streams summing into one
   device. The player is now pinned to the array sink and the
   reference feed is disabled in this topology. (This one explains
   the *choppiness* reported in the room, not the deaths.)
3. Player writes batched in ~200 ms chunks.

Attribution honesty: the three changes shipped together, so the
survival below validates the bundle; the analysis assigns the deaths
to (1).

## Verification

- **09:13–09:43 call**: ran the **full 30 minutes** and ended on the
  client's overall-timeout, cleanly (end-call acknowledged, no
  cleanup error, playback drops 0). The downlink is a continuous
  ~25 fps stream: **44,750 audio delta records were received**,
  counted at the raw record level. That is a reception figure, not a
  queue figure: per the SDK source, all-zero PCM before the first
  received audio and DROP-verdict audio never enter the playback
  queue, and an idle call's stream is mostly silence — so this call
  exercised the queue far less than the raw count suggests. What it
  proves is narrower and sufficient: the bundle completed 30 minutes
  under the observed load, with a clean close and without the
  previous error. 42,619 input frames sent (81.8 MB); the largest gap
  between send completions was 2.360 s — a real separation between
  completions, kept as a health datum, not evidence of an unbroken
  stream. This call also carried no conversation: its injected opener
  was suppressed (see S) and the room's occupant had left for a
  meeting.
- **Soak, 11:12–11:39** (`soak1.sh`): a gate call fed by file
  triggers injecting the canonical question WAV every ~75 s, answered
  from chat. **10/10 injections became turns**, ~27 min of
  conversation, closed cleanly by the operator. The queue-level
  evidence lives here rather than in the survival call above: ten
  real spoken replies played end-to-end through the queue, drops 0.
  Coverage gaps, stated plainly: no 15–30 s replies and no deliberate
  barge-in were exercised, and first entries were not separated from
  later ones in that run; a varied-stimulus soak with pre-registered
  coverage targets is the designed follow-up. 6 suppressions logged
  in the window, consistent with the array hearing the replies' own
  echo (no per-item mapping kept, so that reading is an
  interpretation, not a measurement).

## Gate probation (same day)

The gate also opened **by itself three times** that morning (wake
scores 0.50–0.72 on house noise; one with an empty capture). Ambient
sound keeps the SDK's idle watchdog fed, so those calls ran 25–30
minutes in an empty room. A raw shorter idle timeout would not have
helped — the discriminator that actually separates the cases is
*turns*: accidental calls produced zero.

Rule now in the runner (`--probation-secs`, armed by the wrapper only
when the wake capture is empty): if no turn starts within **60 s of
READY**, the call ends itself through the normal clean shutdown.
Any real turn lifts probation for the rest of the call. Verified with
a controlled unattended call: expired at 60 s, clean end, room
restored. Known residual: an accidental wake *with* ambient speech
captured still opens a full call; if those recur, probation extends
to every gate opening.

## Still open

- **S** (`input_suppressed`): frequent, criteria unpublished;
  verdicts alternate within single calls under one configuration. The wedge (total input
  silence) has not recurred since 2026-11-05 23:49; the counted
  protocol for its return stays armed.
- The chosen spoken close phrase ("Ok bye Muse") is still untested
  against the server.
- Echo work was preventive throughout; the functional residual-echo
  test and the split-route decision (music via DAC, call voice via
  the array) remain scheduled, not forgotten.

## Publication note

`callwrap.sh` is published with the leased VM's id replaced by
`${MUSEZ_VM_ID}`; in production that argument is a literal. The id is
an identifier, not a credential — the SDK auth token never leaves the
Pi and appears nowhere in this repository.
