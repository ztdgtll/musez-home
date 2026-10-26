# Latency probe

Voice in a room lives or dies by latency, so the bridge is built around a
budget and this probe is how the budget gets checked — with real turns, not
averages promised in advance.

## What it measures

Per run: time to first token, time to first **complete** sentence (the moment
speech can start), time to full response, and optionally the TTS render time
of that first sentence (`--tts-cmd`, sentence on stdin). Across runs it
reports mean / p50 / p95 / min / max per stage and appends raw results as
JSONL under `results/`.

## The honest promise

Two different claims, kept separate on purpose:

- Arbitrary questions: yes — a general model with tools can answer them.
- A correct answer in 2–4 s: only for knowledge questions that need no tools
  or search. For everything else the goal is the shortest honest time to the
  first useful result, and the acknowledgement ("one moment") is timed
  separately.

## Usage

```bash
python3 latency_probe.py --mock --runs 5     # no network, no key
META_API_KEY=... python3 latency_probe.py --live --runs 10
```
