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
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# sayseq.log timestamps are Pi-local (America/New_York). The runner's
# own wall clock may be UTC, so log-derived timings are computed
# against a Pi-local t_start, never the host's local time.
PI_TZ = ZoneInfo("America/New_York")

from .mock_source import TURNS
from .studio_room import SSH_CONFIG, SSH_HOST, StudioRoom, mint_turn
from .studio_sink import Respond2Transport, StudioSink
from .turn import run_turn

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
    if "clima" in t:
        return "weather"
    return "knowledge"


def _truncate(text: str, limit: int = 90) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def compose_scene(name: str, room: StudioRoom, sink: StudioSink):
    """The answer's wall scene: the turn's own wall action wins; a
    weather result composes from the real data; otherwise the spoken
    sentences themselves summarize (mock-grade scene for knowledge)."""
    if room.wall_scenes:
        last = room.wall_scenes[-1]
        return last["title"], last["lines"]
    if room.weather is not None:
        w = room.weather
        line1 = f"Tamarac · {w['condition']}"
        line2 = (f"Máx {w['temp_max_f']:.0f} °F · Mín {w['temp_min_f']:.0f} °F"
                 f" · Lluvia {w['precip_prob_max']:.0f}%")
        return "Tamarac weather", [line1, line2]
    if name == "knowledge":
        lines = [_truncate(s) for s in sink.sentences[:2]]
        return "Raspberry Pi", lines
    return "", []


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

    turn_id = mint_turn()
    if read_wall_state().get("avatar") == "off":
        print("studio_turn: warning — the room is in its OFF state; the "
              "playgate will suppress playback (OFF-hours policy). The "
              "pipeline stages still run.", file=sys.stderr)
    sink = StudioSink(Respond2Transport(), turn=turn_id)
    room = StudioRoom(turn_id, speak=sink.say)

    t_start = datetime.now(PI_TZ).replace(tzinfo=None)  # Pi-local, like the logs
    p_start = time.perf_counter()
    marks = run_turn(TURNS[name](), room, sink)
    title, lines = compose_scene(name, room, sink)
    sink.set_scene(title, lines)
    receipt = sink.flush()

    # transcript -> first audio, the repo's standing definition,
    # read back from the player's own log on the Pi. The room's
    # playgate may suppress playback outright (OFF hours, no live mic
    # turn): that is the room's policy, not a bridge failure — observe
    # it, record it, and stop polling early.
    t_first_audio_ms = None
    play_dt = None
    playback = "no_launch"
    rid = receipt.get("rid")
    if rid:
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
        "t_first_token_ms": marks.get("t_first_token"),
        "t_first_sentence_ms": marks.get("t_first_sentence"),
        "t_full_ms": marks.get("t_full"),
        "t_first_audio_ms": t_first_audio_ms,
        "t_start_pi_local": t_start.isoformat(),
        "play_time_of_day": (play_dt.strftime("%H:%M:%S.%f")[:-3]
                             if play_dt is not None else None),
        "tool_calls": [tc["name"] for tc in marks["tool_calls"]],
        "sentences": sink.sentences,
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
          f"{marks['t_full']:.0f} ms | first audio: {audio_txt}")
    print(f"  wall: scene={wall.get('scene')} title={wall.get('title')!r} "
          f"lines={wall.get('lines')} turn={wall.get('turn')} phase={wall.get('phase')}")
    print(f"  wall-clock total: {(time.perf_counter() - p_start) * 1000:.0f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
