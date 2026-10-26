#!/usr/bin/env python3
"""Latency probe for the MuseZ Home bridge.

Measures, per run, the stages that decide whether a voice answer feels fast:

  t_first_token     request start -> first streamed token from the model
  t_first_sentence  request start -> first COMPLETE sentence (via the chunker)
  t_full            request start -> stream finished
  t_tts_first       optional: time to render the first sentence with a TTS
                    command (sentence passed on stdin via --tts-cmd)

Modes:
  --mock   No network, no key. Simulates a token stream locally so the whole
           harness (timing, chunking, stats, logging) can be verified.
  --live   POSTs to https://api.meta.ai/v1/chat/completions with
           stream=true. Reads the key from the META_API_KEY environment
           variable at runtime; the key is never printed or logged.

Results are appended as JSONL to experiments/results/probe-<mode>.jsonl and a
summary table (mean / p50 / p95 / min / max per stage) is printed.

Stdlib only: python3 experiments/latency_probe.py --mock --runs 5
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.sentence_chunker import SentenceChunker  # noqa: E402

API_URL = "https://api.meta.ai/v1/chat/completions"
DEFAULT_MODEL = "muse-spark-1.3"
DEFAULT_PROMPT = "En una o dos oraciones, ¿qué es una Raspberry Pi?"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def summarize(name: str, values: list[float]) -> str:
    return (
        f"{name:<18} mean={statistics.mean(values):7.0f} ms  "
        f"p50={percentile(values, 50):7.0f}  p95={percentile(values, 95):7.0f}  "
        f"min={min(values):7.0f}  max={max(values):7.0f}  n={len(values)}"
    )


def run_tts(cmd: str, sentence: str) -> float:
    start = time.perf_counter()
    subprocess.run(cmd, shell=True, input=sentence.encode(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=False)
    return (time.perf_counter() - start) * 1000.0


def mock_stream(prompt: str):
    """Yield (delay_seconds, token) pairs simulating a streamed reply."""
    text = ("Una Raspberry Pi es una computadora pequeña y barata. "
            "Sirve para aprender y construir proyectos reales.")
    for token in text.split(" "):
        yield random.uniform(0.01, 0.05), token + " "


def live_stream(model: str, prompt: str, api_key: str):
    """Yield (delay_seconds, token) pairs from the Meta Model API stream."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }).encode()
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    last = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            now = time.perf_counter()
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
                delta = obj["choices"][0]["delta"].get("content") or ""
            except (ValueError, KeyError, IndexError):
                continue
            if delta:
                yield now - last, delta
                last = now


def one_run(stream, tts_cmd: str | None) -> dict:
    chunker = SentenceChunker()
    t0 = time.perf_counter()
    marks: dict[str, float] = {}
    first_sentence: str | None = None
    for delay, token in stream:
        if delay:
            # mock stream encodes its own pacing; live yields real gaps = 0
            pass
        now = (time.perf_counter() - t0) * 1000.0
        marks.setdefault("t_first_token", now)
        for sentence in chunker.feed(token):
            if first_sentence is None:
                first_sentence = sentence
                marks["t_first_sentence"] = (time.perf_counter() - t0) * 1000.0
    tail = chunker.flush()
    if first_sentence is None and tail:
        first_sentence = tail[0]
        marks["t_first_sentence"] = (time.perf_counter() - t0) * 1000.0
    marks["t_full"] = (time.perf_counter() - t0) * 1000.0
    if tts_cmd and first_sentence:
        marks["t_tts_first"] = run_tts(tts_cmd, first_sentence)
    marks["first_sentence"] = first_sentence or ""
    return marks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--live", action="store_true")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--tts-cmd", default=None,
                    help="shell command that reads a sentence on stdin")
    args = ap.parse_args()

    api_key = ""
    if args.live:
        api_key = os.environ.get("META_API_KEY", "")
        if not api_key:
            print("ERROR: --live needs META_API_KEY in the environment.",
                  file=sys.stderr)
            return 2

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    mode_name = "live" if args.live else "mock"
    log_path = RESULTS_DIR / f"probe-{mode_name}.jsonl"

    runs: list[dict] = []
    for i in range(args.runs):
        if args.live:
            stream = live_stream(args.model, args.prompt, api_key)
        else:
            # materialize mock pacing as sleeps inside the generator
            def mock_gen():
                for delay, token in mock_stream(args.prompt):
                    time.sleep(delay)
                    yield 0.0, token
            stream = mock_gen()
        marks = one_run(stream, args.tts_cmd)
        marks.update({"mode": mode_name, "model": args.model,
                      "run": i + 1, "ts": time.time()})
        runs.append(marks)
        with open(log_path, "a") as fh:
            fh.write(json.dumps(marks, ensure_ascii=False) + "\n")
        print(f"run {i + 1}: first_token={marks['t_first_token']:.0f} ms  "
              f"first_sentence={marks['t_first_sentence']:.0f} ms  "
              f"full={marks['t_full']:.0f} ms")

    print()
    for stage in ("t_first_token", "t_first_sentence", "t_full", "t_tts_first"):
        values = [r[stage] for r in runs if stage in r]
        if values:
            print(summarize(stage, values))
    print(f"\nresults appended to {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
