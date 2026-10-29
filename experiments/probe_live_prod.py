#!/usr/bin/env python3
"""Production-path live latency probe — 2026-10-29 (source=live-prod).

The morning's live probe (probe-live-2026-10-29.jsonl) measured BARE
prompts through a thin runner: no system prompt, no tool schemas, no
reasoning settings. It measured the model stage of a system the bridge
never runs. This probe measures the system the bridge DOES run:

- every turn goes through `MetaSource.stream_turn` — production system
  prompt, production tool schemas, the `escalate` offer, and the
  per-model request shaping (Spark: reasoning_effort="minimal" +
  max_tokens cap; Wonder: generous max_tokens cap);
- tools dispatch to a stub room (DryRoom data) whose `get_weather`
  sleeps a RECORDED latency: 870 ms, the median of 10 timed round
  trips of the very fetch StudioRoom performs (fastwx.sh ->
  Open-Meteo) from this VM on 2026-10-29 (samples 488–1197 ms; the
  API itself was over its daily limit from this egress, so the
  payload is the DryRoom sample and only the latency is real). The
  wall/scene tools are local bookkeeping in the bridge and return
  instantly, as DryRoom does; their real cost lives in the flush
  path, not the model loop;
- timing comes from run_turn's marks plus the source's own per-round
  marks (model, ttft, duration, tool names, API usage per round).

Arms (10 repetitions each, interleaved round-robin):
  spark-weather / spark-knowledge   the default source (Spark first)
  wonder-weather / wonder-knowledge the deep model answering directly
  escalation-planning               Spark first on a planning prompt;
                                    whether it escalated is recorded

Prompts are the canonical canned ones, synthetic on purpose: the
account spends free credits whose terms warn content may be used for
product improvement.

Auth follows the connector (custom.meta-model-api): a dynamic
credential surrogate is attached per request; no key is read from
the environment, printed or logged. A hard budget guard stops the
run before cumulative list-price cost passes --budget-usd.

Stdlib only: python3 experiments/probe_live_prod.py [--runs 10]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")

os.environ.setdefault("MUSEZ_BRAIN", "bridge")  # the switch, explicit

from dynamic_credentials import add_surrogate_to_request  # noqa: E402

from bridge.meta_source import DEEP_MODEL, FAST_MODEL, MetaAPIError, MetaSource  # noqa: E402
from bridge.tools import TOOL_SCHEMAS, DryRoom  # noqa: E402
from bridge.turn import run_turn  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
LOG_PATH = RESULTS_DIR / "probe-live-prod-2026-10-29.jsonl"

WX_STUB_LATENCY_S = 0.870  # recorded, see module docstring

PRICES = {  # USD per 1M tokens (in, out), list prices
    FAST_MODEL: (1.25, 4.25),
    DEEP_MODEL: (4.00, 20.00),
}

PROMPTS = {
    "weather": "¿Qué clima hace ahora en Tamarac?",
    "knowledge": "En una o dos oraciones, ¿qué es una Raspberry Pi?",
    "planning": ("Planificá mi sábado: una actividad al aire libre a "
                 "la mañana y estar en casa antes de que empiece "
                 "la lluvia."),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def surrogate_transport(url: str, body: dict, headers: dict):
    """MetaSource transport contract, authenticated per the connector:
    one surrogate-attached request, redirects disabled, HTTP errors
    mapped to MetaAPIError exactly like the production transport."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **headers,
    })
    add_surrogate_to_request(req, "custom.meta-model-api",
                             allowed_hosts=["api.meta.ai"])
    opener = urllib.request.build_opener(NoRedirect())
    try:
        return opener.open(req, timeout=90)
    except urllib.error.HTTPError as exc:
        snippet = exc.read().decode("utf-8", "replace")[:200]
        raise MetaAPIError(exc.code, snippet) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MetaAPIError(0, f"network failure: {exc}") from exc


class StubRoom(DryRoom):
    """DryRoom data at recorded tool latencies (module docstring)."""

    def get_weather(self, date: str = None) -> dict:
        time.sleep(WX_STUB_LATENCY_S)
        return super().get_weather(date)


class ListSink:
    def __init__(self) -> None:
        self.sentences: list[str] = []

    def say(self, sentence: str) -> None:
        self.sentences.append(sentence)


def turn_cost_usd(round_marks: list) -> float:
    total = 0.0
    for m in round_marks:
        price_in, price_out = PRICES.get(m["model"], (0.0, 0.0))
        total += (m.get("prompt_tokens") or 0) / 1e6 * price_in
        total += (m.get("completion_tokens") or 0) / 1e6 * price_out
    return total


def one_turn(arm: str, prompt_class: str, prompt: str, run: int) -> dict:
    if arm.startswith("wonder"):
        source = MetaSource(transport=surrogate_transport,
                            fast_model=DEEP_MODEL, deep_model=DEEP_MODEL)
    else:
        source = MetaSource(transport=surrogate_transport)
    room = StubRoom()
    sink = ListSink()
    record: dict = {
        "source": "live-prod", "mode": "live-prod", "arm": arm,
        "prompt_class": prompt_class, "prompt": prompt, "run": run,
        "ts": time.time(), "error": None,
    }
    try:
        marks = run_turn(source.stream_turn(prompt, TOOL_SCHEMAS),
                         room, sink)
        record.update({
            "t_first_token_ms": marks.get("t_first_token"),
            "t_first_sentence_ms": marks.get("t_first_sentence"),
            "t_full_ms": marks.get("t_full"),
            "tool_calls": [tc["name"] for tc in marks["tool_calls"]],
            "sentences": sink.sentences,
        })
    except MetaAPIError as exc:
        record["error"] = f"HTTP {exc.status}: {exc}"
    last = source.last_turn
    record.update({
        "model_final": last.get("model_final"),
        "escalated": last.get("escalated"),
        "round_marks": last.get("round_marks", []),
        "prompt_tokens": last.get("prompt_tokens"),
        "completion_tokens": last.get("completion_tokens"),
    })
    record["cost_usd"] = round(turn_cost_usd(record["round_marks"]), 5)
    record["usage_complete"] = all(
        m.get("prompt_tokens") is not None for m in record["round_marks"])
    return record


ARMS = [
    ("spark-weather", "weather"),
    ("spark-knowledge", "knowledge"),
    ("wonder-weather", "weather"),
    ("wonder-knowledge", "knowledge"),
    ("escalation-planning", "planning"),
]


def summarize(records: list) -> None:
    print()
    for arm, _cls in ARMS:
        rows = [r for r in records if r["arm"] == arm and not r["error"]]
        if not rows:
            print(f"{arm:<22} no successful turns")
            continue
        for stage in ("t_first_token_ms", "t_first_sentence_ms",
                      "t_full_ms"):
            vals = [r[stage] for r in rows if r.get(stage) is not None]
            if vals:
                print(f"{arm:<22} {stage:<20} "
                      f"median={statistics.median(vals):7.0f} ms  "
                      f"min={min(vals):7.0f}  max={max(vals):7.0f}  "
                      f"n={len(vals)}")
        esc = sum(1 for r in rows if r.get("escalated"))
        if arm == "escalation-planning":
            print(f"{arm:<22} escalated {esc}/{len(rows)} turns")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--budget-usd", type=float, default=0.90)
    ap.add_argument("--smoke", action="store_true",
                    help="1 rep of spark-knowledge + escalation-planning")
    args = ap.parse_args()

    arms = [("spark-knowledge", "knowledge"),
            ("escalation-planning", "planning")] if args.smoke else ARMS
    runs = 1 if args.smoke else args.runs

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    spent = 0.0
    stopped = None
    for rep in range(1, runs + 1):
        for arm, prompt_class in arms:
            record = one_turn(arm, prompt_class, PROMPTS[prompt_class],
                              rep)
            records.append(record)
            spent += record["cost_usd"]
            with open(LOG_PATH, "a") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            if record["error"]:
                print(f"{arm} rep {rep}: ERROR {record['error']}")
            else:
                print(f"{arm} rep {rep}: "
                      f"first_sentence="
                      f"{record['t_first_sentence_ms']:.0f} ms  "
                      f"full={record['t_full_ms']:.0f} ms  "
                      f"tools={record['tool_calls']}  "
                      f"escalated={record['escalated']}  "
                      f"cost=${record['cost_usd']:.4f}  "
                      f"(cumulative ${spent:.4f})")
            if spent > args.budget_usd:
                stopped = (f"budget guard: cumulative ${spent:.4f} "
                           f"> ${args.budget_usd:.2f}")
                break
        if stopped:
            break
    if stopped:
        print(f"\nSTOPPED: {stopped}")
    errors = sum(1 for r in records if r["error"])
    print(f"\n{len(records)} turns recorded ({errors} errors), "
          f"total cost ${spent:.4f}, appended to {LOG_PATH}")
    summarize(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
