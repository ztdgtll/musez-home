# MuseZ Home

A low-latency voice bridge for a real room: the **Meta Model API** (Muse Spark, streaming) drives a Raspberry Pi ambient wall display and a room speaker, so a spoken request becomes a spoken answer and an on-wall scene in seconds — measured, not promised.

Built for the **Meta Global AI Developer Hackathon** (Oct 26 – Nov 4, 2026) — Indie Developer track, **Build for Your World** theme.

> **Status — Oct 29, 2026:** Live Meta Model API: credits applied 2026-10-29; live latency probe run 2026-10-29 — results in `experiments/results/latency-live-2026-10-29.md`. Room integration (listener → bridge) lands Oct 31 – Nov 1.

## Why

My assistant lives on my phone and on my computer: the same voice and the same memory, everywhere. My studio was the missing place — the room where I spend my days building. This bridge exists so she is present here too: the room that hears her, answers her and shows her.

## What it does

You say "Hey Muse" in the studio. The room listens, transcribes locally, and sends the text to Muse Spark through the Meta Model API with function calling wired to the room's real tools (wall scenes, voice replies, live data). Spark streams back; the bridge cuts the stream into sentences, speaks the first sentence as soon as it exists, and puts the result on the wall. Every stage of every turn is timed and logged, because for voice in a room, **latency is the product**.

## What was built during the hackathon (Oct 26 – Nov 4, 2026)

- The **bridge**: transcript → Meta Model API (Muse Spark streaming + function calling) → sentence-chunked speech and wall scenes, with escalation to **Muse Wonder** for turns that need deeper reasoning.
- The **latency probe and budget**: per-stage timing (first token, first complete sentence, TTS render, playback dispatch) with p50/p95 reporting over real turns.
- The tool layer that lets the models act on the room: wall scene control, voice reply dispatch, and live-data tools.
- The **studio integration**: the bridge loop driving the room's real surfaces — tool calls that act on the wall through the Pi's guarded state writer, and speech delivered through the studio's existing response transport (same voice, same player) — verified end to end in the room with the mock source; the live-source wiring is the Oct 31 – Nov 1 step (see Status above).
- This documentation, the measured results, and the demo.

## The room it runs in

MuseZ Home plugs into a personal studio its builder has been developing as a long-running project: a Raspberry Pi 5 running a projected ambient wall, a local wake-word listener ("Hey Muse") with a USB microphone array and a room speaker, and a speech pipeline with a watchdog that verifies spoken replies actually play. That system is the stage — a body that already existed. What this event adds is the brain: a dedicated, measured bridge from the room to Meta's Muse models.

## Architecture

```
voice → wake word → local STT → transcript
      → bridge → Meta Model API (Muse Spark, streaming, function calling)
      → sentence chunker → first sentence → TTS → room speaker
                        ↘ full reply → wall scene (Pi display)
      → per-stage timing log (every turn)
```

### Day-plan scenes

Planning turns ship a structured scene, not just a card: a day plan
(`bridge/day_plan.py`) is a validated timeline payload — an ordered
list of blocks (activity / rain / free / note) with the rain band
derived from the real hourly forecast — so a planned Saturday reads
as an infographic: the morning activity placed in the dry hours, the
rain drawn as a band. The model can submit one (`set_day_plan`), and
a deterministic composer builds the same payload from the forecast
with no model involved, so the demo's plan never depends on
free-form generation. A plan rides the turn's single wall write at
flush time, alongside a card summary composed from the same payload.
**Status:** the bridge validates and ships the payload today; the
wall page does not render the `plan` kind yet — rendering is a
separate prototype step, and until it lands the card summary is what
the wall shows.

## Latency budget (targets, validated by measurement during the event)

| Stage | Budget |
|---|---|
| Spark: first token | ≤ 2 s |
| Spark: first complete sentence (streaming) | ≤ 4 s |
| TTS render, first sentence | ≤ 3 s (measured baseline ~2 s) |
| Playback dispatch to the room | ≤ 4 s |
| **Perceived total to first spoken sentence** | **≤ ~12 s** (baseline, pre-event chat path: median transcript→first audio 50.9 s, n=7, measured 2026-10-23 — see `docs/latency-budget.md`) |

Measured results live in `experiments/results/` and are summarized in the demo. If a stage misses its budget, the writeup says so and shows the number.

## Repository layout

- `bridge/` — the bridge code (Spark client, sentence chunker, tool dispatch).
- `experiments/` — the latency probe (`latency_probe.py`) and its measured results.
- `docs/` — design notes, including the latency budget rationale.

## Running the latency probe

```bash
# Mock mode: no network, no API key — verifies the harness end to end
python3 experiments/latency_probe.py --mock --runs 5

# Live mode: needs META_API_KEY in the environment (never commit it)
META_API_KEY=... python3 experiments/latency_probe.py --live --runs 10
```

## License

MIT — see `LICENSE`.
