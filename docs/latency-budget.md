# Latency budget — design notes

## Why this document exists

Before the event, the room's voice path ran through a general chat/agent
pipeline. Measured on the real system: ~24 s for a full voice turn on the fast
path, ~53 s when a reply involved a full web search, and first spoken
sentence at ~5 s when replies were split into sentences and synthesized in
parallel. The cost was not in one stage: text-to-speech rendered a typical
first sentence in ~2 s, while turn processing before dispatch and transport
to the Pi dominated.

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
