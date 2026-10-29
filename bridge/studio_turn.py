"""Run ONE bridge turn end to end in the real room — mock source only.

    python3 -m bridge.studio_turn "¿qué clima hace?"
    echo "¿qué clima hace?" | python3 -m bridge.studio_turn

The transcript (stand-in for local STT output) selects one of the canned
mock turns; the bridge loop (turn.py) then runs it against the real
studio: tool calls act on the room through StudioRoom, and the spoken
reply goes through StudioSink — the studio's existing response
transport — so the answer is heard on the room speaker and its scene
lands on the wall under a turn id minted by the Pi's own minter.

This is an on-demand integration entry point. It does not touch the
listener or the room's production response path. During the room's OFF
hours the playgate suppresses playback without a live mic turn's
authorization — the bridge observes and records that like any other
outcome. Stage timings are appended to
experiments/results/bridge-studio-mock.jsonl, labeled mode=mock:
these are pipeline timings with a fake model, never live-model numbers.

The API switch (bridge/brain_switch.py) gates this runner too: with
the switch off, main() records the refusal, prints the switch-off
line and exits 3 — a clean, expected state, distinct from a turn
error (1) — before minting a turn or touching the network at all.
Exit codes: 0 ok · 1 turn error · 2 empty transcript · 3 switch off.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# sayseq.log timestamps are Pi-local (America/New_York). The runner's
# own wall clock may be UTC, so log-derived timings are computed
# against a Pi-local t_start, never the host's local time.
PI_TZ = ZoneInfo("America/New_York")

from .brain_switch import SWITCH_OFF_EXIT_CODE, SWITCH_OFF_MESSAGE, brain_mode
from .day_plan import plan_card
from .mock_source import TURNS
from .studio_room import (
    SCENE_LINE_MAX,
    SSH_CONFIG,
    SSH_HOST,
    HourlyWxRunner,
    StudioRoom,
    mint_turn,
)
from .studio_sink import Respond2Transport, StudioSink, TransportError
from .turn import run_turn

# A turn that cannot finish inside this budget is recorded as failed
# and its partial sentences are still flushed: the room hears what
# exists instead of hanging in silence while a stream drips.
TURN_DEADLINE_S = 20.0

RESULTS_DEFAULT = (
    Path(__file__).resolve().parent.parent
    / "experiments" / "results" / "bridge-studio-mock.jsonl"
)
SAYSEQ_LOG_CMD = "tail -n 80 /home/ztdgtll/pared-tools/sayseq.log"
PLAYGATE_LOG = "/home/ztdgtll/pared-tools/playgate.log"
WALL_STATE_CMD = "cat /home/ztdgtll/pared/state.json"
_RID_RE = re.compile(r"^[0-9][0-9A-Za-z-]*$")
_RID_STRICT_RE = re.compile(r"^[0-9][0-9-]*$")


def playback_status(rid: str, sayseq_text: str, playgate_text: str) -> str:
    """What the room did with this turn's sequence: 'played',
    'suppressed_off' (the playgate refused it — the room's OFF-hours
    policy: no playback without a live mic turn's authorization), or
    'pending' (nothing observed yet)."""
    if rid and f"PLAY seq-{rid}-0.ogg" in sayseq_text:
        return "played"
    if rid and "PLAY_SUPPRESSED_OFF" in playgate_text and rid in playgate_text:
        return "suppressed_off"
    return "pending"


def pick_turn(transcript: str) -> str:
    """Map a transcript to a canned mock turn (mock stand-in for the
    model's own routing decision)."""
    t = unicodedata.normalize("NFKD", transcript.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    if "pared" in t:
        return "wall"
    if "sabado" in t or "plan" in t:
        return "plan"
    if "clima" in t:
        return "weather"
    return "knowledge"


def _truncate(text: str, limit: int = 90) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def compose_scene(name: str, room: StudioRoom, sink: StudioSink):
    """The answer's ONE wall scene, composed at flush time: the
    turn's own scene candidate wins; a weather result composes from
    the real data; otherwise the first spoken sentences themselves
    are the lines (the wall reads as her voice), cut to the design
    budget. This is the only scene the turn ever ships — the tool
    call recorded a candidate, it did not write a draft.

    None-safe by construction: any weather field the feed left null
    is simply left out of the line — composing the scene must never
    crash after the stream and take the spoken answer down with it."""
    if room.wall_scenes:
        last = room.wall_scenes[-1]
        return last["title"], last["lines"]
    if room.weather is not None:
        w = room.weather
        line1 = f"Tamarac · {w.get('condition') or 'sin datos'}"
        parts = []
        if w.get("temp_max_f") is not None:
            parts.append(f"Máx {w['temp_max_f']:.0f} °F")
        if w.get("temp_min_f") is not None:
            parts.append(f"Mín {w['temp_min_f']:.0f} °F")
        if w.get("precip_prob_max") is not None:
            parts.append(f"Lluvia {w['precip_prob_max']:.0f}%")
        return "Tamarac weather", [line1, " · ".join(parts)]
    if sink.sentences:
        lines = [_truncate(s, SCENE_LINE_MAX) for s in sink.sentences[:2]]
        return ("Raspberry Pi" if name == "knowledge" else ""), lines
    return "", []


def compose_final_scene(name: str, room: StudioRoom, sink: StudioSink):
    """The turn's final scene as (title, lines, scene): a validated
    day plan outranks every card composition — the flush then ships
    the plan payload as the dispatch's structured scene, with a card
    summary of the SAME plan as its title+lines. Without a plan this
    is exactly compose_scene and scene is None. A plan the room
    rejected never got queued, so an invalid plan lands here as its
    absence — the card fallback below is the fallback, and the turn
    is never lost over a bad payload."""
    if room.plan_scenes:
        plan = room.plan_scenes[-1]
        title, lines = plan_card(plan)
        return title, lines, plan
    title, lines = compose_scene(name, room, sink)
    return title, lines, None


def _ssh_cat(command: str, runner=None) -> str:
    run = runner or (
        lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=30)
    )
    proc = run(["ssh", "-F", SSH_CONFIG, SSH_HOST, command])
    return proc.stdout or ""


def find_first_play(rid: str, t_start: datetime, log_text: str):
    """First PLAY timestamp for seq-<rid>-0.ogg in sayseq.log, as a
    datetime on t_start's date (log lines carry time-of-day only)."""
    if not _RID_RE.match(rid or ""):
        return None
    pat = re.compile(
        r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3}) PLAY seq-" + re.escape(rid) + r"-0\.ogg",
        re.M,
    )
    m = pat.search(log_text)
    if not m:
        return None
    hh, mm, ss, ms = (int(g) for g in m.groups())
    play = t_start.replace(hour=hh, minute=mm, second=ss, microsecond=ms * 1000)
    if play < t_start - timedelta(minutes=1):
        play += timedelta(days=1)
    return play


def read_wall_state(runner=None) -> dict:
    try:
        state = json.loads(_ssh_cat(WALL_STATE_CMD, runner))
    except Exception:
        return {}
    return {k: state.get(k) for k in ("scene", "avatar", "title", "lines", "turn", "phase")}


def _run_with_deadline(fn, deadline_s: float):
    """Run `fn` on a daemon thread; return (value, None) on success,
    re-raise its exception, or return (None, TimeoutError) if it is
    still running at the deadline. The abandoned thread is a daemon:
    it can append a late sentence to the sink, but it cannot block
    the flush of what the turn already produced."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:  # ferried to the calling thread
            box["error"] = e

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(deadline_s)
    if th.is_alive():
        return None, TimeoutError(
            f"turn exceeded the {deadline_s:.0f} s deadline")
    if "error" in box:
        raise box["error"]
    return box.get("value"), None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="One bridge turn in the real room (mock source).")
    ap.add_argument("transcript", nargs="?", help="transcript text; stdin if omitted")
    ap.add_argument("--results", default=str(RESULTS_DEFAULT),
                    help="JSONL results file to append to")
    ap.add_argument("--skip-record", action="store_true",
                    help="do not append to the results file")
    args = ap.parse_args(argv)

    transcript = args.transcript or sys.stdin.read().strip()
    if not transcript:
        print("studio_turn: empty transcript", file=sys.stderr)
        return 2
    name = pick_turn(transcript)

    # The API switch, checked before ANY network-shaped work (even
    # minting a turn is an SSH call): with the switch off, the bridge
    # does not run in this room at all — the classic path owns it.
    # The refusal is recorded like every other outcome.
    mode = brain_mode()
    if mode != "bridge":
        record = {
            "mode": "switch_off",
            "source": "brain_switch",
            "brain_mode": mode,
            "ts": time.time(),
            "transcript": transcript,
            "turn_name": name,
            "turn_id": None,
            "rid": None,
            "playback": "skipped",
            "turn_error": None,
            "tool_calls": [],
            "sentences": [],
            "wall_after": {},
        }
        if not args.skip_record:
            path = Path(args.results)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"turn[{name}] transcript: {transcript!r}")
        print(f"  {SWITCH_OFF_MESSAGE} (brain mode: {mode}) — "
              f"no turn minted, no API call made")
        return SWITCH_OFF_EXIT_CODE

    turn_id = mint_turn()
    if read_wall_state().get("avatar") == "off":
        print("studio_turn: warning — the room is in its OFF state; the "
              "playgate will suppress playback (OFF-hours policy). The "
              "pipeline stages still run.", file=sys.stderr)
    sink = StudioSink(Respond2Transport(), turn=turn_id)
    room = StudioRoom(turn_id, speak=sink.say, hourly_runner=HourlyWxRunner())

    t_start = datetime.now(PI_TZ).replace(tzinfo=None)  # Pi-local, like the logs
    p_start = time.perf_counter()
    # The turn runs under a deadline and its outcome is ALWAYS
    # recorded — including transport failures. (The results file used
    # to contain only successes: a turn whose flush raised vanished
    # without a trace.)
    marks: dict = {"tool_calls": []}
    turn_error = None
    deadline_exceeded = False
    try:
        marks, err = _run_with_deadline(
            lambda: run_turn(TURNS[name](), room, sink), TURN_DEADLINE_S)
        if err is not None:
            deadline_exceeded = True
            turn_error = f"deadline: {err}"
            marks = {"tool_calls": [],
                     "t_full": TURN_DEADLINE_S * 1000.0}
    except Exception as e:
        turn_error = f"{type(e).__name__}: {e}"
    title, lines, scene = compose_final_scene(name, room, sink)
    sink.set_scene(title, lines, scene=scene)
    # Which composition the turn's single wall write carried, and
    # every scene validation turned away on the way — neither is
    # left implicit in the wall's after-state.
    if scene is not None:
        wall_outcome = "plan"
    elif room.wall_scenes:
        wall_outcome = "tool_scene"
    elif room.weather is not None:
        wall_outcome = "weather"
    elif lines:
        wall_outcome = "sentences"
    else:
        wall_outcome = "none"
    receipt = sink.receipt
    try:
        receipt = sink.flush()
    except TransportError as e:
        if turn_error is None:
            turn_error = f"{type(e).__name__}: {e}"
        receipt = sink.receipt

    # transcript -> first audio, the repo's standing definition,
    # read back from the player's own log on the Pi. The room's
    # playgate may suppress playback outright (OFF hours, no live mic
    # turn): that is the room's policy, not a bridge failure — observe
    # it, record it, and stop polling early.
    t_first_audio_ms = None
    play_dt = None
    playback = "no_launch"
    rid = receipt.get("rid") if receipt else None
    if receipt and receipt.get("launch_unknown"):
        playback = "launch_unknown"
    elif turn_error is not None:
        playback = "error"
    elif rid:
        gate_cmd = (f"grep {rid} {PLAYGATE_LOG} | tail -n 3"
                    if _RID_STRICT_RE.match(rid) else "true")
        playback = "pending"
        for _ in range(10):
            seq_text = _ssh_cat(SAYSEQ_LOG_CMD)
            status = playback_status(rid, seq_text, _ssh_cat(gate_cmd))
            if status != "pending":
                playback = status
                if status == "played":
                    play_dt = find_first_play(rid, t_start, seq_text)
                    if play_dt is not None:
                        t_first_audio_ms = (play_dt - t_start).total_seconds() * 1000.0
                break
            time.sleep(1.0)
        if playback == "pending":
            playback = "not_observed"
    wall = read_wall_state()

    record = {
        "mode": "mock",
        "source": "mock",
        "model": "mock_source (fake model — pipeline timings only)",
        "ts": time.time(),
        "transcript": transcript,
        "turn_name": name,
        "turn_id": turn_id,
        "rid": rid,
        "playback": playback,
        "turn_error": turn_error,
        "deadline_exceeded": deadline_exceeded,
        "t_first_token_ms": marks.get("t_first_token"),
        "t_first_sentence_ms": marks.get("t_first_sentence"),
        "t_full_ms": marks.get("t_full"),
        "t_first_audio_ms": t_first_audio_ms,
        "t_start_pi_local": t_start.isoformat(),
        "play_time_of_day": (play_dt.strftime("%H:%M:%S.%f")[:-3]
                             if play_dt is not None else None),
        "tool_calls": [tc["name"] for tc in marks["tool_calls"]],
        "sentences": sink.sentences,
        "wall_outcome": wall_outcome,
        "plan": scene,
        "wall_rejections": [r["error"] for r in room.wall_rejections],
        "wall_after": wall,
    }
    if not args.skip_record:
        path = Path(args.results)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"turn[{name}] transcript: {transcript!r} (wall turn {turn_id})")
    print(f"  tools: {', '.join(record['tool_calls']) or 'none'}")
    for s in sink.sentences:
        print(f"  SPEAK: {s}")
    audio_txt = (f"{t_first_audio_ms:.0f} ms (sayseq log)"
                 if t_first_audio_ms is not None
                 else f"none — playback: {playback}")
    print(f"  rid: {rid} | first sentence at "
          f"{marks.get('t_first_sentence', 0):.0f} ms | full stream "
          f"{marks.get('t_full', 0):.0f} ms | first audio: {audio_txt}")
    if turn_error is not None:
        print(f"  turn error: {turn_error}")
    print(f"  wall: scene={wall.get('scene')} title={wall.get('title')!r} "
          f"lines={wall.get('lines')} turn={wall.get('turn')} phase={wall.get('phase')}")
    print(f"  wall-clock total: {(time.perf_counter() - p_start) * 1000:.0f} ms")
    return 1 if turn_error is not None else 0


if __name__ == "__main__":
    sys.exit(main())
