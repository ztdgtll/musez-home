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
sentence-level streaming so speech starts at the first complete sentence, and
per-stage timing on every turn so regressions are visible.

## Budget (targets to validate with the probe)

| Stage | Budget | Notes |
|---|---|---|
| Wake → end of capture | ≤ 2 s after last word | local VAD endpointing (pre-existing) |
| Local STT | ≤ 3 s typical clips | local/hybrid (pre-existing) |
| Spark first token | ≤ 2 s | minimal reasoning effort, short prompt |
| Spark first complete sentence | ≤ 4 s | streaming; never wait for the full reply |
| TTS first sentence | ≤ 3 s | measured baseline ~1.97 s for a 7-word sentence |
| Transport + playback start | ≤ 4 s healthy | measured and reported when degraded |
| **Perceived total to first speech** | **≤ ~12 s** | realistic floor for knowledge-only turns ≈ 3.7 s of model+TTS alone |

## Rules of honesty for the demo

- The demo shows a table of 3–5 real measured turns in the room, not a
  promised average.
- Any stage that misses its budget is reported with its number and a reason
  (e.g. transport over a degraded tunnel, measured separately).
