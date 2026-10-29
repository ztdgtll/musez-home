# Latency budget — design notes

## Why this document exists

Before the event, the room's voice path ran through a general chat/agent
pipeline. Baseline measured from the room's own turn and watchdog logs
(Pi 5, 2026-10-23; n = 7 real user turns after excluding ghost wakes and
ambient captures): transcript sent → first response audio at **p50 50.9 s,
p95 73.9 s** — almost all of it the agent leg; local playback start added
~0.2 s. A single fast-path turn measured live on 2026-10-20 came in at
~24 s: a best case, not the median. The log metric is a floor for
perceived latency (end of speech precedes the transcript timestamp by a
few seconds of endpointing and STT). The bridge's numbers are reported on
the same transcript → first-audio definition, on comparable requests. The
cost was never in one stage: text-to-speech renders a typical first
sentence in ~2 s, while turn processing before dispatch dominated.

The bridge attacks those two stages: a direct streaming call to Muse Spark
with a short fixed prompt and minimal reasoning effort for simple requests,
sentence-level composition — the reply is cut into sentences as the stream
arrives — and per-stage timing on every turn so regressions are visible.
One shape matters for every number below: the sink dispatches the
**finished turn in a single pass** through the room's existing response
path (respond2 renders the sentences in parallel, ships sentence 0,
launches playback, and keeps shipping the rest behind it). Until a
speculative renderer exists, voice start is therefore computed from the
full turn (`t_full`), never from the first sentence — the first-sentence
marks describe the model's streaming, not the room's voice start.

## The budget, closed

Stages are **sequential** (they add) or **nested** (contained in the stage
below them — they never add). Conditional stages fire only on the turns
that need them. Owners: *Pi* = the pre-existing room, *Bridge* = this
repository, *Meta API* = the provider.

| Stage | Owner | Counts | Measured so far | Original target — status |
|---|---|---|---|---|
| Wake → end of capture (endpointing) | Pi | Sequential — Total A only | Not re-measured in this window | ≤ 2 s after last word — carried from the pre-existing design |
| Local STT | Pi | Sequential — Total A only | Not re-measured in this window | ≤ 3 s, typical clips — carried from the pre-existing design |
| Turn mint (SSH round trip; `setstate.py --mint-turn`) | Bridge ↔ Pi | Sequential — both totals | Inside the Oct 27 mock room record (mint → first audio 15.04 s for the whole pipeline, fake model); not split out on its own yet | No target was set — it is in the table now because it is in the total |
| Spark round 1: first token | Meta API | Nested in first sentence | Median 1,761–1,806 ms across both classes, worst point 2,159 ms (production path, n = 5 per arm, Oct 29). The same day's bare-prompt probe — no production request shaping — measured p50 3.6 s | **≤ 2 s — met at median through the production path**; the bare probe's miss was a property of that configuration, not of the bridge |
| Spark round 1: first complete sentence | Meta API | Nested in full response | Median 1.8 s for both classes, worst point 2.2 s (production path, Oct 29) | **≤ 4 s — met at median for both classes** |
| Spark round 1: full response | Meta API | Sequential — the voice-start basis while dispatch is whole-turn | Median 2.68 s (knowledge) / 5.77 s (weather, tools at measured latency) — production path, Oct 29 | — |
| Tool execution (`get_weather`, wall candidates) | Bridge → room feeds | Sequential — tool turns only | `get_weather` round trip measured on its own: median 870 ms (10 timed fetches, Oct 29); the production-path probe dispatches tools to a stub at that latency | — |
| Model round 2 (continuation after tool results) | Meta API | Sequential — tool turns only | Visible in the production probe's per-round marks: a production turn is 2–5 rounds, re-sending a ~1.4K-token context each round | — |
| Escalation: Wonder round (replaces the rest of round 1) | Meta API | Sequential — escalated turns only | Escalated planning turns (n = 5, escalated 5/5): Wonder's first sentence median 8.37 s, full plan median 29.1 s (21.6–43.5) — production path, Oct 29. (Bare probe: Wonder first sentence p50 8.9 s.) | Escalated turns carry their own, larger expectation; a spoken acknowledgement covers the wait |
| Final flush: the turn's single dispatch | Bridge | Sequential | One respond2 invocation per turn, by construction | — |
| TTS render (sentences in parallel; sentence 0 gates playback) | Pi transport | Sequential | First sentence ~1.97 s (7-word baseline) | ≤ 3 s — met on the baseline sentence |
| Transport + playback start | Pi | Sequential | Local playback start added ~0.2 s in the baseline's healthy path; tunnel health is reported per turn | ≤ 4 s healthy — carried target |

### The two totals

- **Total A — last word → first voice** (what the person in the room feels):
  endpointing + STT + mint + model round 1 (full) [+ tool execution + round 2 on tool turns] [+ the Wonder round on escalated turns] + flush/transport + TTS to sentence 0.
- **Total B — transcript → first voice** (the baseline's definition; the comparable number):
  Total A − endpointing − STT.

The old version of this table summed to ~16 s against a declared ~12 s
total, because nested stages were added as if sequential and mint, tools,
round 2, escalation and flush were missing entirely. With the formulas
above, every second is counted exactly once.

### The measured floor today

Total B's core for a simple, no-tool turn, from the Oct 29 production-path
probe plus the TTS baseline:

- First-sentence basis: 1.76 s (Spark, knowledge turns) + 1.97 s ≈ **3.7 s**.
- Whole-turn basis — the operative one while the turn dispatches whole:
  2.68 s (Spark full turn, knowledge median) + 1.97 s ≈ **4.6 s**;
  weather turns run 5.77 s + 1.97 s ≈ **7.7 s** on the same basis.

Both are floors *before* mint, transport and playback start — and neither
is a room total. Room totals on the Total B definition get published when
the listener → bridge integration lands (Oct 31 – Nov 1); until then, no
number in this repo claims one.

## Rules of honesty for the demo

- The demo shows a table of 3–5 real measured turns in the room, not a
  promised average.
- Any stage that misses its budget is reported with its number and a reason
  (e.g. transport over a degraded tunnel, measured separately).
- Small samples are published as all points + median + range. A p95 is
  only claimed on ≥ 30 warm calls per arm, with the method stated — the
  Oct 29 bare probe (n = 10 per arm) publishes its interpolated p95s
  alongside min, max and the cold-start anomaly that drives them, and
  they should be read with that sample size in mind. The production-path
  run the same day (n = 5 per arm) publishes points + median + range
  only, and supersedes the bare run for any claim about bridge turns.
- A cold first call is reported cold (the Oct 29 probe's 31.7 s first
  call is in the data), never silently dropped or averaged away.
