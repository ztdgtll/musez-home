# MuseZ Home

A low-latency voice bridge for a real room: the **Meta Model API** (Muse Spark, streaming) drives a Raspberry Pi ambient wall display and a room speaker, so a spoken request becomes a spoken answer and an on-wall scene in seconds — measured, not promised.

Built for the **Meta Global AI Developer Hackathon** (Oct 26 – Nov 4, 2026) — Indie Developer track, **Build for Your World** theme.

## What it does

You say "Hey Muse" in the studio. The room listens, transcribes locally, and sends the text to Muse Spark through the Meta Model API with function calling wired to the room's real tools (wall scenes, voice replies, live data). Spark streams back; the bridge cuts the stream into sentences, speaks the first sentence as soon as it exists, and puts the result on the wall. Every stage of every turn is timed and logged, because for voice in a room, **latency is the product**.

## What was built during the hackathon (Oct 26 – Nov 4, 2026)

This is the submission. Everything in this repository was created inside the event window:

- The **bridge**: transcript → Meta Model API (Muse Spark streaming + function calling) → sentence-chunked speech and wall scenes.
- The **latency probe and budget**: per-stage timing (first token, first complete sentence, TTS render, playback dispatch) with p50/p95 reporting over real turns.
- The tool layer that lets Spark act on the room: wall scene control, voice reply dispatch, and live-data tools.
- This documentation, the measured results, and the demo.

## Pre-existing infrastructure (NOT part of this submission)

The bridge plugs into a personal studio system that existed before the event and is only the environment it runs in — like a laptop under a hackathon project:

- A Raspberry Pi 5 running a MagicMirror ambient wall (projected) with a state-driven scene page.
- A local wake-word listener ("Hey Muse") with a USB microphone array and a room speaker.
- A text-to-speech pipeline and a response watchdog that verifies spoken replies actually play.

None of that is claimed as event work. The demo shows only the layer listed above.

## Architecture

```
voice → wake word → local STT → transcript
      → bridge → Meta Model API (Muse Spark, streaming, function calling)
      → sentence chunker → first sentence → TTS → room speaker
                        ↘ full reply → wall scene (Pi display)
      → per-stage timing log (every turn)
```

## Latency budget (targets, validated by measurement during the event)

| Stage | Budget |
|---|---|
| Spark: first token | ≤ 2 s |
| Spark: first complete sentence (streaming) | ≤ 4 s |
| TTS render, first sentence | ≤ 3 s (measured baseline ~2 s) |
| Playback dispatch to the room | ≤ 4 s |
| **Perceived total to first spoken sentence** | **≤ ~12 s** (vs ~24 s for the pre-event chat path) |

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
