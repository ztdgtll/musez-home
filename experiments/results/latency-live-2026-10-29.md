# Live latency probe — 2026-10-29 (source=live)

First live-model numbers for the bridge, measured against the Meta Model API
now that the hackathon's US$150 free credits are applied in Billing. Raw
records: `probe-live-2026-10-29.jsonl` (20 calls, 0 errors, all HTTP 200).

## Method

- Streaming `POST https://api.meta.ai/v1/chat/completions`, bare user
  prompts (no bridge system prompt, no tools — this measures the model
  stage, not a full bridge turn).
- Canonical canned prompts, the bridge's own turn classes:
  weather = «¿Qué clima hace ahora en Tamarac?»;
  knowledge = «En una o dos oraciones, ¿qué es una Raspberry Pi?».
- 5 repetitions per model × prompt, interleaved round-robin so drift hits
  every arm evenly. Timing semantics identical to `latency_probe.py`
  (t0 at stream start; first sentence via `bridge/sentence_chunker`).
  Token counts are the API's own `usage` (requested with
  `stream_options.include_usage`).
- Prompts are synthetic on purpose: the account is spending free credits,
  whose terms warn content may be used for product improvement.

## No "Wonder Full" API arm

The account's model list was enumerated live through the connector
(`GET /v1/models`, 2026-10-29): `muse-wonder-1.0`, `muse-spark-1.3`,
`muse-spark-1.3-contributor`, `muse-spark-1.2`,
`muse-spark-1.2-contributor`, `muse-spark-1.1`,
`muse-voice-transcribe-1.0`, `muse-image-1.0`, `sam-3.1`. **No model ID
corresponding to a "Wonder Full" variant exists on this account**, so the
experiment has exactly two arms; nothing was approximated with
`muse-wonder-1.0` under another name.

## Results (ms)

### muse-spark-1.3 (n=10)

| Stage | p50 | p95 | min | max |
|---|---|---|---|---|
| First token | 3,635 | 20,005 | 2,921 | 31,726 |
| First complete sentence | 3,663 | 20,022 | 2,966 | 31,741 |
| Full response | 3,955 | 21,310 | 3,061 | 32,942 |

By class — knowledge (n=5): first token p50 3,363 / p95 3,637; first
sentence p50 3,465 / p95 3,666. Weather (n=5): first token p50 5,207;
first sentence p50 5,207 (p95s ≈ 26.5 s, driven by the cold start below).

### muse-wonder-1.0 (n=10)

| Stage | p50 | p95 | min | max |
|---|---|---|---|---|
| First token | 8,761 | 14,975 | 5,930 | 16,358 |
| First complete sentence | 8,877 | 15,116 | 5,978 | 16,438 |
| Full response | 9,424 | 16,354 | 6,311 | 17,502 |

By class — knowledge (n=5): first token p50 7,268 / p95 8,392; first
sentence p50 7,314 / p95 8,436. Weather (n=5): first token p50 12,786;
first sentence p50 13,011 / p95 15,851.

## Anomalies, stated plainly

- **Cold start:** the very first call of the session (Spark, weather,
  rep 1) took 31.7 s to first token; the remaining Spark weather reps ran
  3.0–5.7 s. It is kept in the data and is what lifts Spark's aggregate
  p95 to ~20 s. One observation, not a pattern — but a cold first turn is
  exactly what a room would feel after idle, so it stays visible.
- **Verbosity:** without the bridge's brevity system prompt, replies run
  long (Spark: 9,647 completion tokens over 10 calls; its weather rep 1
  even opened with «Hola, soy Muse Spark.»). Full-response times and
  token counts reflect that; first-token and first-sentence times are the
  stages the bridge budget is written against.

## Cost

Estimated at list prices (Spark $1.25/$4.25 per 1M in/out; Wonder
$4.00/$20.00) from the recorded usage: Spark $0.041 (195 prompt + 9,647
completion tokens), Wonder $0.140 (195 + 6,958). **Total ≈ $0.18** of the
free credit for the full 20-call run.

## Against the budget (docs/latency-budget.md)

- Spark first token ≤ 2 s: **missed** (p50 3.6 s; knowledge p50 3.4 s).
- Spark first complete sentence ≤ 4 s: **met at p50 for knowledge**
  (3.5 s) and in aggregate (3.7 s); weather p50 5.2 s misses.
- Wonder misses every stage budget by 2–4×: its weather first sentence
  alone (p50 13.0 s) exceeds the entire ~12 s perceived budget. The data
  backs the design: Spark first, Wonder only on escalation, where an
  acknowledgement covers the wait.
- Perceived total: Spark's model stage leaves ~8 s of headroom inside the
  ~12 s budget for STT + TTS (~2 s measured) + transport. For scale: the
  old chain's transcript → first audio was p50 50.9 s end to end, and the
  mock bridge pipeline measured 15.04 s mint → first audio with a fake
  model — the live model stage is no longer the bottleneck either way.
