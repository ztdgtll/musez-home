# Live latency probe, production path — 2026-10-29 (source=live-prod)

The same canonical prompts as the morning's bare probe, re-measured
through the system the bridge actually runs. Raw records:
`probe-live-prod-2026-10-29.jsonl` (25 turns, 0 errors). The bare-probe
numbers live in `latency-live-2026-10-29.md`; this file supersedes
them for any claim about bridge turns.

## Method

- Every turn runs through `MetaSource.stream_turn` with the production
  system prompt, the production tool schemas, the `escalate` offer,
  and the per-model request shaping that shipped the same day:
  Spark rounds send `reasoning_effort="minimal"` + `max_tokens=1024`;
  Wonder rounds send `max_tokens=32768` (its hidden reasoning burned
  10–27K tokens on the review packages; a tight cap returns empty
  content). Production now also requests `usage` on every stream and
  records per-round marks (model, ttft, duration, tool names, tokens)
  in `last_turn` — the token and cost figures below are the API's own.
- Tools dispatch to a stub room (DryRoom data) at recorded latencies:
  `get_weather` sleeps 870 ms, the median of 10 timed round trips of
  the exact fetch StudioRoom performs (fastwx.sh → Open-Meteo) from
  this VM today (samples 488–1197 ms; Open-Meteo itself was over its
  daily limit from this egress, so the payload is the sample and only
  the latency is real). Scene tools are local bookkeeping in the
  bridge and return instantly; their cost lives in the flush path.
- Arms: Spark-first (the default source) on weather and knowledge;
  Wonder answering directly on both; Spark-first on a planning prompt
  (the escalation arm). Interleaved round-robin, 5 repetitions each —
  the run's budget guard stopped it there (see Cost): at the observed
  per-turn prices, the planned 10×5 grid prices out at ≈ $1.92, over
  this task's ~$1 ceiling. **n=5 per arm, so this file reports every
  point with median / min–max and no p95** — a percentile over five
  points is an interpolation, not a measurement (finding B-2).
- Prompts stay synthetic on purpose (free credits; content may be
  used for product improvement).

## Results — every point, ms (median in bold)

### spark-weather (production, Spark first)

| Stage | Points | Median | Min–Max |
|---|---|---|---|
| First token | 1397, 1421, 1806, 1924, 2159 | **1806** | 1397–2159 |
| First complete sentence | 1397, 1421, 1806, 1924, 2159 | **1806** | 1397–2159 |
| Full turn | 5043, 5253, 5768, 6073, 6601 | **5768** | 5043–6601 |

Every turn: `get_weather` (870 ms stub) then `set_wall_scene`, then the
spoken answer. Prompt tokens ≈ 6,237/turn across its rounds (round 1
alone is 1,388: system prompt + schemas, vs ~19 for a bare prompt).

### spark-knowledge (production, Spark first)

| Stage | Points | Median | Min–Max |
|---|---|---|---|
| First token | 1430, 1478, 1761, 2031, 2063 | **1761** | 1430–2063 |
| First complete sentence | 1430, 1478, 1761, 2031, 2063 | **1761** | 1430–2063 |
| Full turn | 2551, 2564, 2679, 2935, 3855 | **2679** | 2551–3855 |

Spark also puts knowledge answers on the wall (`set_wall_scene` in
5/5 turns) — production behavior the bare probe could not show.

### wonder-weather (Wonder answering directly)

| Stage | Points | Median | Min–Max |
|---|---|---|---|
| First token | 2547, 2992, 4049, 4133, 12533 | **4049** | 2547–12533 |
| First complete sentence | 2547, 2992, 4049, 4133, 12533 | **4049** | 2547–12533 |
| Full turn | 13352, 15155, 16537, 18530, 24007 | **16537** | 13352–24007 |

### wonder-knowledge (Wonder answering directly)

| Stage | Points | Median | Min–Max |
|---|---|---|---|
| First token | 7098, 8384, 12727, 16795, 20087 | **12727** | 7098–20087 |
| First complete sentence | 7098, 8384, 12727, 16795, 20089 | **12727** | 7098–20089 |
| Full turn | 7098, 14146, 18296, 20818, 24253 | **18296** | 7098–24253 |

Wonder's variance is the story of its arms: a 7–20 s spread on the
same canned question. The medians here and in the bare run disagree
in both directions (weather faster, knowledge slower) — at n=5 with
this spread, that is noise, and this file says so rather than
averaging it away.

### escalation-planning (Spark first, planning prompt)

Escalated **5/5**. Anatomy of one turn, from the per-round marks:
Spark's `escalate` round is fast (42–838 ms, ~1,400 prompt tokens);
Wonder then works 3–4 rounds — `get_weather` twice in 3/5 turns
(forecast, then the planned date's hourly detail), `set_day_plan`,
and a closing text round.

| Stage | Points | Median | Min–Max |
|---|---|---|---|
| First token (Wonder's, after hand-off) | 7027, 7425, 8366, 9472, 11194 | **8366** | 7027–11194 |
| First complete sentence | 7027, 7425, 8366, 9472, 11194 | **8366** | 7027–11194 |
| Full turn (plan delivered) | 21578, 28767, 29080, 34124, 43539 | **29080** | 21578–43539 |

Because Spark's draft text is discarded on escalation by design, the
room hears nothing until Wonder's first sentence at ~8.4 s (median).
That silence is the known cost of the invisible hand-off, and it is
why escalated turns are designed to be covered by an acknowledgement
(plan item 19) — not something these numbers make go away.

First token equals first sentence in nearly every turn above: under
the production brevity prompt, the first streamed delta typically
already closes the short opener (clause cut or sentence end), so the
two stages land on the same feed.

## How and why these differ from the bare probe

1. **Spark is faster to first sentence** (knowledge median 3,465 →
   1,761 ms; weather 5,207 → 1,806 ms). The bare run set no reasoning
   parameter; the production request fixes `reasoning_effort=
   "minimal"`, and the brevity system prompt makes the first sentence
   close almost immediately. The earlier "Spark misses ≤2 s first
   token" conclusion was a property of the bare configuration, not
   of the bridge.
2. **Full turns are longer, honestly so**: a production turn is 2–5
   API rounds (tool round-trips, the ~1.4K-token context re-sent each
   round), where the bare probe made exactly one call. Wonder's full
   turns (16.5–18.3 s medians vs 9.4 bare) add a scene round and
   longer, structured answers; its completion tokens run ~1,300/turn
   vs ~700 bare.
3. **Prompt size is now visible**: ~1,388 tokens before the user says
   a word (system + 4 room schemas + escalate), re-sent per round.
   Escalated planning turns total ~10,400 prompt tokens each.
4. **The escalation path exists in these numbers** and did not in the
   bare run at all: 5/5 escalations, with per-round marks showing
   exactly where the ~29 s goes.

## Against the budget (docs/latency-budget.md)

- Spark first token ≤ 2 s: **met at median through the production
  path** (1,761–1,806 ms; worst point 2,159 ms) — the bare probe's
  miss does not survive contact with the production configuration.
- Spark first complete sentence ≤ 4 s: **met at median for both
  classes** (1.8 s), worst point 2.2 s.
- Escalated turns: first sentence median 8.4 s sits inside the ~12 s
  perceived budget; the full plan (median 29.1 s) does not — the plan
  is a wall deliverable, and its spoken summary is what the budget
  binds. Stated plainly: a planned Saturday takes about half a
  minute to be fully on the wall.
- Nothing here measures the room end to end (STT, TTS, transport,
  playback): these are model-stage numbers through the production
  client, and the in-room end-to-end turns remain the weekend's
  measurement, as the budget doc already requires.

## Cost

From the API's own usage, at list prices: spark-weather $0.0495,
spark-knowledge $0.0263, wonder-weather $0.2459, wonder-knowledge
$0.1819, escalation-planning $0.4319 — **run total $0.9355** for 25
turns (a 2-turn verification smoke earlier the same day cost $0.0703;
task total ≈ $1.01). The escalation arm is 46% of the run's cost:
deep planning through the production path costs ~$0.06–0.10 a turn.
The budget guard stopped the run after rep 5 of the planned 10;
everything above is computed only from recorded turns.
