"""Run ONE live bridge turn in the real room — Meta Model API source.

    python3 -m bridge.live_turn "¿qué clima hace?" --turn 1793138465796

The live counterpart of studio_turn.py (which stays the mock
integration runner). The transcript — in production the studio
listener's own STT output, handed over by bridge/daemon.py with the
turn id the LISTENER minted — runs through MetaSource (Spark first,
Wonder on escalation) against the real StudioRoom and StudioSink:
same room, same Warm transport (respond2), same single wall scene
composed at flush time. The model source is the only difference.

Presence: before the source is built, the runner reads the room's
presence file FROM THE PI (one fixed ssh `cat`; the file lives only
there — approved by Javier on 2026-10-31, never in this repo) and
appends it to the system prompt via meta_source.compose_system_prompt.
A failed read degrades to the bare prompt and the turn record says
so (presence_loaded) — never silently.

Auth follows the connector (custom.meta-model-api), exactly like
experiments/probe_live_prod.py: a dynamic credential surrogate is
attached per request; no key is read from the environment, printed
or logged.

The turn's FINAL wall state is published by the runner, once, the
moment the answer's FIRST segment is observed playing: the
ladder's last rank (phase 5), carrying the composed scene — the
validated plan payload when the turn produced one, else the card.
The answer's own player session walks the ladder like the classic
path: its phase-4 card lands when playback starts, and its own
phase-5 close (home, after a 30 s linger) drops harmlessly
against the runner's publish, which landed at first play. An
escalated turn's acknowledgement is dispatched kind="ack"
(bridge/studio_sink.py): its player session speaks WITHOUT any
wall writes, so it can neither take the answer's phases nor send
the wall home mid-answer. (Before that mark existed, the ack's
session consumed phase 4 with an empty scene and its linger close
consumed phase 5 some 30 s later — always before the answer's
publish could land, escalated turn after escalated turn: every
closing publish dropped with rc=4 and the composed scene never
reached the wall. Room evidence 2026-10-31, the 10:41 and 16:04
turns, read in the Pi's setstate/sayseq logs.)

The result then holds for RESULT_HOLD_S seconds past the end of
playback and the wall returns home on its own — Javier's rule
(2026-10-31: he preferred the classic behaviour, a result that
shows briefly and goes home, over a sticky result that waits to
be replaced). The return is scheduled by the runner and executed
by wall_apply's return_home op, which re-checks ownership on the
Pi at fire time and writes nothing if a newer turn owns the wall
by then. When playback is never observed (the OFF-hours gate
suppresses it, a transport failure), nothing is published and
the record says why. One exception: a turn that spoke an
acknowledgement but whose answer never launched a player session
(no sentences, a flush that failed before launch) has NO session
left to walk the wall home, so the runner schedules the same
ownership-checked return itself.

The API switch gates everything: with the switch off, the refusal
is recorded and the runner exits 3 before any network-shaped work —
the same contract as studio_turn. A live turn's deadline is 60 s
(the mock runner's 20 s would cut escalated turns: a full Wonder
plan measured a 43.5 s worst case on 2026-10-29, and the response
watchdog's first alert is at +90 s).

Exit codes: 0 ok · 1 turn error · 2 empty transcript · 3 switch off.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")

from .brain_switch import SWITCH_OFF_EXIT_CODE, SWITCH_OFF_MESSAGE, brain_mode
from .meta_source import (MetaAPIError, MetaSource, compose_system_prompt,
                          resolve_turn_lang)
from .studio_room import (
    SSH_CONFIG,
    SSH_HOST,
    WALL_APPLY_CMD,
    HourlyWxRunner,
    SshJsonRunner,
    StudioRoom,
)
from .studio_sink import Respond2Transport, StudioSink, TransportError
from .studio_turn import (
    SAYSEQ_LOG_CMD,
    _run_with_deadline,
    _ssh_cat,
    compose_final_scene,
    find_first_play,
    playback_status,
    read_wall_state,
)
from .turn import run_turn

PI_TZ = ZoneInfo("America/New_York")

LIVE_DEADLINE_S = 60.0
PUBLISH_WAIT_S = 120.0
RESULT_HOLD_S = 15.0
PRESENCE_CMD = "cat /home/ztdgtll/.musez-presence.md"
PLAYGATE_LOG = "/home/ztdgtll/pared-tools/playgate.log"

RESULTS_DEFAULT = (
    Path(__file__).resolve().parent.parent
    / "experiments" / "results" / "bridge-live.jsonl"
)


# -- auth: the connector's surrogate transport (probe_live_prod) -----


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def surrogate_transport(url: str, body: dict, headers: dict):
    """MetaSource transport authenticated per the connector: one
    surrogate-attached request, redirects disabled, HTTP errors
    mapped to MetaAPIError exactly like the production transport."""
    from dynamic_credentials import add_surrogate_to_request

    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **headers,
    })
    add_surrogate_to_request(req, "custom.meta-model-api",
                             allowed_hosts=["api.meta.ai"])
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        return opener.open(req, timeout=90)
    except urllib.error.HTTPError as exc:
        snippet = exc.read().decode("utf-8", "replace")[:200]
        raise MetaAPIError(exc.code, snippet) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MetaAPIError(0, f"network failure: {exc}") from exc


# -- presence ---------------------------------------------------------


def read_presence(runner=None) -> str:
    """The Pi's presence block, or "" when it cannot be read.

    One fixed ssh command; the file's content is data on stdout,
    never part of a command string."""
    try:
        return (_ssh_cat(PRESENCE_CMD, runner) or "").strip()
    except Exception:
        return ""


# -- the closing wall publish -----------------------------------------


def _played_done(sayseq_text: str, rid: str, last_index: int) -> bool:
    return f"PLAY_DONE seq-{rid}-{last_index}.ogg" in sayseq_text


def publish_final_scene(turn_id: int, title: str, lines: list,
                        plan, wall_runner=None) -> dict:
    """The turn's closing wall publish (module docstring): phase 5,
    NOT sticky — the composed scene, plan payload included when the
    turn produced one. Returns wall_apply's parsed reply."""
    kv: dict = {"scene": "plan" if plan else "answer",
                "title": title, "lines": lines, "avatar": "idle"}
    if plan:
        kv["plan"] = plan
    payload = {"turn": turn_id, "phase": 5, "kv": kv}
    return SshJsonRunner(WALL_APPLY_CMD, runner=wall_runner)(payload)


def _timer_scheduler(delay_s: float, fn):
    timer = threading.Timer(delay_s, fn)
    timer.daemon = True
    timer.start()
    return timer


def schedule_home_return(turn_id: int, delay_s: float = RESULT_HOLD_S,
                         *, wall_runner=None, scheduler=None) -> dict:
    """Send the wall home once the result has had its seconds.

    The write is wall_apply's return_home op, fired after delay_s;
    ownership is re-checked on the Pi at fire time, so a newer turn
    that took the wall in the meantime is never displaced. A failure
    is printed, never raised — the turn's answer already landed, and
    a wall left showing it is a cosmetic fault, not a failed turn."""
    def _fire() -> None:
        try:
            reply = SshJsonRunner(WALL_APPLY_CMD, runner=wall_runner)(
                {"op": "return_home", "expect_turn": turn_id})
            print(f"live turn {turn_id}: home return -> {reply}")
        except Exception as e:  # the timer thread must never die loud
            print(f"live turn {turn_id}: home return failed: "
                  f"{type(e).__name__}: {e}")

    (scheduler or _timer_scheduler)(delay_s, _fire)
    return {"scheduled_s": delay_s, "expect_turn": turn_id}


# -- one live turn ------------------------------------------------------


def run_live_turn(transcript: str, turn_id: int, *,
                  source=None, api_transport=None, room=None, sink=None,
                  respond_runner=None, wall_runner=None, sayseq_reader=None,
                  gate_reader=None, wall_reader=None,
                  presence_reader=None, results=None, skip_record=False,
                  deadline_s: float = LIVE_DEADLINE_S,
                  publish_wait_s: float = PUBLISH_WAIT_S,
                  home_scheduler=None,
                  home_delay_s: float = RESULT_HOLD_S,
                  lang: str = None) -> dict:
    """Run one live turn end to end and return its record (also
    appended to the live results JSONL unless skip_record).

    Every external surface is injectable for tests: the API
    transport, the respond2 runner, the wall-apply runner, the
    sayseq log reader and the presence reader."""
    read_seq = sayseq_reader or (lambda: _ssh_cat(SAYSEQ_LOG_CMD))

    # The turn's language: the caller's hint when it carries one
    # (the queue entry's STT language — the listener transcribed in
    # it, so it is ground truth), else decided deterministically
    # from the transcript (meta_source.resolve_turn_lang). It pins
    # the model's whole turn (in MetaSource), voices the sink's
    # dispatches in that language, and localizes the bridge-
    # composed card text.
    turn_lang = resolve_turn_lang(transcript, lang)
    lang_source = "stt" if lang in ("en", "es") else "detect"

    # The API switch, before ANY network-shaped work — the same
    # refusal contract as the mock runner, with the caller's turn id.
    mode = brain_mode()
    if mode != "bridge":
        record = {
            "mode": "switch_off", "source": "brain_switch",
            "brain_mode": mode, "ts": time.time(),
            "transcript": transcript, "turn_id": turn_id,
            "turn_lang": turn_lang, "lang_source": lang_source,
            "rid": None, "playback": "skipped", "turn_error": None,
            "tool_calls": [], "ack": None, "sentences": [],
            "presence_loaded": False, "wall_publish": None,
            "home_return": None,
            "wall_after": {},
        }
        _append_record(record, results, skip_record)
        print(f"live turn {turn_id}: {SWITCH_OFF_MESSAGE} "
              f"(brain mode: {mode}) — no API call made")
        return record

    presence = (presence_reader or read_presence)()
    if source is None:
        source = MetaSource(
            transport=api_transport or surrogate_transport,
            system_prompt=compose_system_prompt(presence))
    if sink is None:
        sink = StudioSink(Respond2Transport(runner=respond_runner),
                          turn=turn_id, lang=turn_lang)
    if room is None:
        room = StudioRoom(turn_id, speak=sink.say,
                          hourly_runner=HourlyWxRunner())

    t_start = datetime.now(PI_TZ).replace(tzinfo=None)  # Pi-local, like the logs
    p_start = time.perf_counter()
    marks: dict = {"tool_calls": []}
    turn_error = None
    deadline_exceeded = False
    try:
        marks, err = _run_with_deadline(
            lambda: run_turn(source.stream_turn(transcript, None,
                                                lang=turn_lang),
                             room, sink),
            deadline_s)
        if err is not None:
            deadline_exceeded = True
            turn_error = f"deadline: {err}"
            marks = {"tool_calls": [], "t_full": deadline_s * 1000.0}
    except Exception as e:
        turn_error = f"{type(e).__name__}: {e}"
    title, lines, plan = compose_final_scene("", room, sink,
                                             lang=turn_lang)
    sink.set_scene(title, lines, scene=plan)
    if plan is not None:
        wall_outcome = "plan"
    elif room.wall_scenes:
        wall_outcome = "tool_scene"
    elif room.weather is not None:
        wall_outcome = "weather"
    elif room.field is not None:
        wall_outcome = "field"
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

    # Playback, read back from the player's own log — the closing
    # wall publish fires at the FIRST observed play (the module
    # docstring has the phase-5 race), the home return is armed
    # once the LAST segment has played. Polling stops early when
    # the gate suppressed playback outright.
    rid = receipt.get("rid") if receipt else None
    playback = "no_launch"
    t_first_audio_ms = None
    wall_publish = None
    home_return = None
    if receipt and receipt.get("launch_unknown"):
        playback = "launch_unknown"
    elif turn_error is not None and not rid:
        playback = "error"
    elif rid:
        playback = "pending"
        # The gate's suppression line is keyed on the response id,
        # like the mock runner's identical check.
        gate_cmd = (f"grep {rid} {PLAYGATE_LOG} | tail -n 3"
                    if re.match(r"^[0-9][0-9-]*$", rid) else "true")
        read_gate = gate_reader or (lambda: _ssh_cat(gate_cmd))
        last_idx = max(0, len(sink.sentences) - 1)
        wait_deadline = time.monotonic() + publish_wait_s
        published = False
        while True:
            seq_text = read_seq()
            status = playback_status(rid, seq_text, read_gate())
            if status != "pending":
                playback = status
            if playback == "played" and t_first_audio_ms is None:
                play_dt = find_first_play(rid, t_start, seq_text)
                if play_dt is not None:
                    t_first_audio_ms = (play_dt - t_start).total_seconds() * 1000.0
            if playback == "suppressed_off":
                wall_publish = {"skipped": "playback suppressed (OFF hours)"}
                break
            if playback == "played" and not published:
                # First segment is playing: publish NOW, while the
                # answer session's own phase-5 close is still a
                # 30 s linger away — the publish takes the phase
                # and the close then drops harmlessly against it.
                wall_publish = publish_final_scene(
                    turn_id, title, lines, plan, wall_runner=wall_runner)
                published = True
            if _played_done(seq_text, rid, last_idx):
                if playback == "pending":
                    playback = "played"
                if not published:
                    wall_publish = publish_final_scene(
                        turn_id, title, lines, plan, wall_runner=wall_runner)
                    published = True
                home_return = schedule_home_return(
                    turn_id, home_delay_s,
                    wall_runner=wall_runner, scheduler=home_scheduler)
                break
            if time.monotonic() >= wait_deadline:
                if playback == "pending":
                    playback = "not_observed"
                if not published:
                    wall_publish = {"skipped": "last segment play not observed"}
                elif home_return is None:
                    home_return = schedule_home_return(
                        turn_id, home_delay_s,
                        wall_runner=wall_runner, scheduler=home_scheduler)
                break
            time.sleep(0.5)
    ack_rid = (sink.ack or {}).get("rid") if sink.ack else None
    if (home_return is None and wall_publish is None and not rid
            and ack_rid
            and not (receipt and receipt.get("launch_unknown"))):
        # The turn spoke an acknowledgement but its answer never
        # launched a player session (no sentences, or the flush
        # failed before launch): no session exists to walk the wall
        # home, so the runner schedules the ownership-checked
        # return itself (module docstring). A launch-unknown answer
        # is excluded — a session may exist and own that duty.
        home_return = schedule_home_return(
            turn_id, home_delay_s,
            wall_runner=wall_runner, scheduler=home_scheduler)
    wall = (wall_reader or read_wall_state)()
    last_turn = getattr(source, "last_turn", {}) or {}

    record = {
        "mode": "live",
        "source": "meta_model_api",
        "model_final": last_turn.get("model_final"),
        "escalated": last_turn.get("escalated"),
        "rounds": last_turn.get("rounds"),
        "round_marks": last_turn.get("round_marks"),
        "prompt_tokens": last_turn.get("prompt_tokens"),
        "completion_tokens": last_turn.get("completion_tokens"),
        "ts": time.time(),
        "transcript": transcript,
        "turn_id": turn_id,
        "turn_lang": turn_lang,
        "lang_source": lang_source,
        "rid": rid,
        "playback": playback,
        "turn_error": turn_error,
        "deadline_exceeded": deadline_exceeded,
        "t_first_sentence_ms": marks.get("t_first_sentence"),
        "t_full_ms": marks.get("t_full"),
        "t_first_audio_ms": t_first_audio_ms,
        "tool_calls": [tc["name"] for tc in marks["tool_calls"]],
        "ack": marks.get("ack"),
        "sentences": sink.sentences,
        "presence_loaded": bool(presence),
        "wall_outcome": wall_outcome,
        "plan": plan,
        "wall_rejections": [r["error"] for r in room.wall_rejections],
        "wall_publish": wall_publish,
        "home_return": home_return,
        "wall_after": wall,
    }
    _append_record(record, results, skip_record)
    print(f"live turn {turn_id}: {transcript!r}")
    print(f"  tools: {', '.join(record['tool_calls']) or 'none'}"
          f" | escalated: {record['escalated']}"
          f" | presence: {'loaded' if presence else 'absent'}")
    for s in sink.sentences:
        print(f"  SPEAK: {s}")
    print(f"  rid: {rid} | playback: {playback} | publish: {wall_publish}")
    if turn_error is not None:
        print(f"  turn error: {turn_error}")
    print(f"  wall: scene={wall.get('scene')} title={wall.get('title')!r} "
          f"turn={wall.get('turn')} phase={wall.get('phase')}")
    print(f"  wall-clock total: {(time.perf_counter() - p_start) * 1000:.0f} ms")
    return record


def _append_record(record: dict, results, skip_record: bool) -> None:
    if skip_record:
        return
    path = Path(results) if results else RESULTS_DEFAULT
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="One live bridge turn in the real room (Meta Model API).")
    ap.add_argument("transcript", nargs="?",
                    help="transcript text; stdin if omitted")
    ap.add_argument("--turn", type=int, required=True,
                    help="the turn id the listener minted for this turn")
    ap.add_argument("--results", default=str(RESULTS_DEFAULT),
                    help="JSONL results file to append to")
    ap.add_argument("--skip-record", action="store_true",
                    help="do not append to the results file")
    args = ap.parse_args(argv)

    transcript = args.transcript or sys.stdin.read().strip()
    if not transcript:
        print("live_turn: empty transcript", file=sys.stderr)
        return 2
    record = run_live_turn(transcript, args.turn,
                           results=args.results,
                           skip_record=args.skip_record)
    if record["mode"] == "switch_off":
        return SWITCH_OFF_EXIT_CODE
    return 1 if record["turn_error"] is not None else 0


if __name__ == "__main__":
    sys.exit(main())
