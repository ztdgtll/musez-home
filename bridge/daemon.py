"""The bridge daemon: studio listener queue -> live bridge turns.

    python3 -m bridge.daemon

When the API switch is in bridge mode, the studio listener appends
one JSON line per mic turn to ~/pared-tools/bridge-queue.jsonl on
the Pi instead of sending the transcript to the chat (the classic
path). This daemon — a systemd service on the VM
(deploy/musez-bridge.service) — drains that queue over the same ssh
channel the bridge already uses and runs each entry through
bridge.live_turn with the turn id the LISTENER minted, so the
answer's wall writes, the response watchdog's evidence and the
room's choreography all keep the original turn's identity.

Before any paid call, each entry is re-gated where the listener
read the switch — the Pi's own ~/.musez-brain.json: an explicit
non-bridge mode there drains the entry as a recorded switch-off
refusal instead of spending (the live runner's own gate, on this
machine, is the second lock). An entry whose queue timestamp is
older than STALE_AFTER_S when its turn comes up is drained the
same way — recorded "stale_skipped", never run, never spoken: a
queue that backed up while the daemon was down must not fire its
turns late, on top of whatever the room did meanwhile (room
evidence 2026-10-31: a revived daemon spoke a 3-minute-old turn
over the response watchdog's own lines). Entries without a
timestamp still run — dropping a turn that might be live is the
worse failure. A crash on one entry is recorded and never stops
the loop; a lost ssh connection just delays the drain, and
entries are only marked done after they were processed, so a
restart resumes mid-queue instead of losing turns.

State persists in ~/.musez-bridge/state.json: a heartbeat plus
the cursor — the set of turn ids already processed. The cursor is
keyed on ids, never on line position: a queue that shrank or was
rewritten used to re-sync by count and could swallow the turn
that arrived in the same second (room evidence 2026-11-01: the
listener restarted, two entries landed glued on one physical
line, the valid count dropped 8 -> 7 and the re-sync ate a LIVE
turn). With ids, a smaller queue is a non-event: an entry runs
iff its id was never processed. A legacy count-only state is
upgraded once, seeded from the count's own claim unioned with
the turn ids the results file proves were processed. Liveness
is a file read, not a guess. A lock file keeps a second daemon
instance from starting.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

from .live_turn import RESULTS_DEFAULT, run_live_turn
from .studio_room import SSH_CONFIG, SSH_HOST

QUEUE_CMD = "cat /home/ztdgtll/pared-tools/bridge-queue.jsonl"
BRAIN_CMD = "cat /home/ztdgtll/.musez-brain.json"
STATE_DIR = Path("~/.musez-bridge").expanduser()
STATE_PATH = STATE_DIR / "state.json"
LOCK_PATH = STATE_DIR / "daemon.lock"
POLL_S = 0.7
STALE_AFTER_S = 90.0
# The processed-id cursor is bounded: the queue is append-only in
# practice, so 500 ids reach far beyond any realistic rewrite, and
# pruning never drops an id still present in the queue read.
MAX_DONE_IDS = 500


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} bridge-daemon: {msg}", flush=True)


def _ssh_text(command: str) -> str:
    """One fixed ssh command's stdout; RAISES on a failed fetch.

    The distinction matters: an unreachable Pi must never look like
    an empty queue — a silently-empty read would hide fresh turns
    behind a healthy-looking poll and stall the drain until the
    next success, with nothing in the log to say why."""
    proc = subprocess.run(["ssh", "-F", SSH_CONFIG, SSH_HOST, command],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ssh fetch failed rc={proc.returncode}: "
            f"{(proc.stderr or '').strip()[-160:]}")
    return proc.stdout or ""


def _line_objects(line: str) -> list:
    """JSON values on one queue line: normally exactly one.

    An append that lands before the previous line's newline glues
    two entries onto ONE physical line (room evidence 2026-11-01:
    a synthetic entry and the next LIVE turn shared a line; the
    plain per-line parse made both invisible, the valid count
    dropped 8 -> 7 and the count cursor's re-sync ate the live
    turn). Recover by decoding consecutive objects; a partial tail
    (the queue is polled mid-append) yields only what is complete,
    and a line with no complete object yields nothing."""
    try:
        return [json.loads(line)]
    except ValueError:
        pass
    out = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(line):
        while idx < len(line) and line[idx] in " \t,":
            idx += 1
        if idx >= len(line):
            break
        try:
            obj, end = decoder.raw_decode(line, idx)
        except ValueError:
            break
        out.append(obj)
        idx = end
    return out


def parse_entries(text: str) -> list:
    """Valid queue entries from the queue file's text, in order.

    An entry counts only as a complete JSON object with an int
    turn and a non-empty text; anything else (a line mid-append,
    damage) is skipped. The drain is keyed on turn ids, so a bad
    line can neither wedge the queue nor be mistaken for a turn."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for entry in _line_objects(line):
            if (isinstance(entry, dict)
                    and isinstance(entry.get("turn"), int)
                    and not isinstance(entry.get("turn"), bool)
                    and isinstance(entry.get("text"), str)
                    and entry["text"].strip()):
                out.append(entry)
    return out


def read_queue_entries() -> list:
    """The queue's valid entries. A missing queue file is a NORMAL
    state — no bridge turn has been queued yet — not a fetch
    failure; every other ssh error still raises (see _ssh_text for
    why a failed fetch must never look like an empty queue)."""
    try:
        return parse_entries(_ssh_text(QUEUE_CMD))
    except RuntimeError as e:
        if "No such file or directory" in str(e):
            return []
        raise


def pi_brain_mode(reader=None) -> str | None:
    """The Pi's configured brain mode, or None when unconfigured
    (no file / unreadable — the contract's default then applies,
    which the live runner enforces on its own side)."""
    read = reader or (lambda: _ssh_text(BRAIN_CMD))
    try:
        data = json.loads(read() or "")
        mode = data.get("mode")
        if mode is not None and str(mode).strip():
            return str(mode).strip().lower()
    except Exception:
        pass
    return None


def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"done": 0}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_PATH)


def _record_gate_refusal(entry: dict, mode: str) -> None:
    record = {
        "mode": "switch_off", "source": "daemon_gate",
        "brain_mode": mode, "ts": time.time(),
        "transcript": entry["text"], "turn_id": entry["turn"],
        "rid": None, "playback": "skipped", "turn_error": None,
        "tool_calls": [], "ack": None, "sentences": [],
        "presence_loaded": False, "wall_publish": None, "wall_after": {},
    }
    RESULTS_DEFAULT.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_DEFAULT, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _record_stale_skip(entry: dict, age_s: float) -> None:
    record = {
        "mode": "stale_skipped", "source": "daemon_stale",
        "brain_mode": None, "ts": time.time(),
        "transcript": entry["text"], "turn_id": entry["turn"],
        "queue_ts": entry.get("ts"), "age_s": round(age_s, 1),
        "rid": None, "playback": "skipped", "turn_error": None,
        "tool_calls": [], "ack": None, "sentences": [],
        "presence_loaded": False, "wall_publish": None, "wall_after": {},
    }
    RESULTS_DEFAULT.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_DEFAULT, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def process_entry(entry: dict, mode_reader=None, now: float = None) -> None:
    """One queue entry: the staleness check first (module
    docstring), then the Pi-side gate, then the live turn.
    Never raises — a failure is recorded or logged, and the caller
    still marks the entry done (a poison entry must not wedge the
    queue; the response watchdog remains the room's safety net for
    a turn whose answer never landed)."""
    queue_ts = entry.get("ts")
    if (isinstance(queue_ts, (int, float))
            and not isinstance(queue_ts, bool)):
        age_s = (time.time() if now is None else now) - float(queue_ts)
        if age_s > STALE_AFTER_S:
            _record_stale_skip(entry, age_s)
            log(f"turn {entry['turn']}: queued {age_s:.0f} s ago "
                f"(> {STALE_AFTER_S:.0f} s) — skipped as stale, "
                f"not run, not spoken")
            return
    mode = pi_brain_mode(mode_reader)
    if mode is not None and mode != "bridge":
        _record_gate_refusal(entry, mode)
        log(f"turn {entry['turn']}: switch off on the Pi "
            f"(mode {mode!r}) — drained without a paid call")
        return
    try:
        # The entry's STT language rides along when the listener
        # recorded one (entries without it — older lines, hand-fed
        # lines — pass None and the runner detects from the text,
        # exactly as before this field existed).
        run_live_turn(entry["text"], entry["turn"],
                      lang=entry.get("lang"))
    except Exception as e:
        log(f"turn {entry['turn']}: runner crashed: "
            f"{type(e).__name__}: {e}")


def _recorded_turn_ids(path=None) -> set:
    """Turn ids already present in the results file — the ground
    truth of what the bridge has processed (every path through
    process_entry writes a record). Used once, to seed a legacy
    count-only state file."""
    ids = set()
    try:
        with open(path or RESULTS_DEFAULT) as f:
            for line in f:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    tid = record.get("turn_id")
                    if (isinstance(tid, int)
                            and not isinstance(tid, bool)):
                        ids.add(tid)
    except OSError:
        pass
    return ids


def _seed_done_ids(state: dict, entries: list) -> list:
    """The processed-id cursor for a legacy count-only state.

    Seeds from BOTH witnesses of the old cursor: the first `done`
    entries of the queue as read today (what the count claimed)
    unioned with every turn id the results file carries (what was
    actually processed). After a queue rewrite the two can
    disagree; an id either witness names must never run again."""
    count = int(state.get("done") or 0)
    ids = _recorded_turn_ids()
    for entry in entries[:count]:
        ids.add(entry["turn"])
    in_queue = [e["turn"] for e in entries if e["turn"] in ids]
    rest = sorted(ids.difference(in_queue))
    return in_queue + rest


def _prune_done_ids(done_ids: list, present_ids: set) -> list:
    """Bound the cursor at MAX_DONE_IDS without ever dropping an
    id still present in the queue (a dropped present id would be
    re-seen — and re-recorded as stale — on every poll)."""
    if len(done_ids) <= MAX_DONE_IDS:
        return done_ids
    present = [t for t in done_ids if t in present_ids]
    others = [t for t in done_ids if t not in present_ids]
    keep = MAX_DONE_IDS - len(present)
    keep_set = set(present) | (set(others[-keep:]) if keep > 0 else set())
    return [t for t in done_ids if t in keep_set]


def _pending_entries(entries: list, done_set: set) -> list:
    """Queue entries whose turn id was never processed, in file
    order. Keyed on ids, not position: a queue that shrinks,
    rotates or is rewritten changes nothing — an entry is pending
    iff its id is unknown to the cursor."""
    return [e for e in entries if e["turn"] not in done_set]


def _save_cursor(done_ids: list) -> None:
    # `done` (the count) rides along for anything still reading
    # the state file the old way; the cursor is `done_ids`.
    _save_state({"done": len(done_ids), "done_ids": done_ids,
                 "ts": time.time()})


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lockf = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another daemon holds the lock; exiting")
        return 1
    state = _load_state()
    stored = state.get("done_ids")
    needs_seed = not isinstance(stored, list)
    done_ids = ([t for t in stored
                 if isinstance(t, int) and not isinstance(t, bool)]
                if isinstance(stored, list) else [])
    done_set = set(done_ids)
    log(f"up; {len(done_set)} queue entries already processed")
    while True:
        try:
            entries = read_queue_entries()
            if needs_seed:
                # One-time upgrade of a legacy count-only state.
                done_ids = _seed_done_ids(state, entries)
                done_set = set(done_ids)
                needs_seed = False
                log(f"cursor upgraded to turn ids; {len(done_set)} "
                    f"adopted as processed")
            done_ids = _prune_done_ids(
                done_ids, {e["turn"] for e in entries})
            done_set = set(done_ids)
            for entry in _pending_entries(entries, done_set):
                log(f"turn {entry['turn']}: {entry['text']!r}")
                process_entry(entry)
                done_set.add(entry["turn"])
                done_ids.append(entry["turn"])
                _save_cursor(done_ids)
        except Exception as e:
            log(f"poll failed: {type(e).__name__}: {e}")
        finally:
            # The heartbeat ticks even when the Pi is unreachable:
            # liveness must be provable exactly when the link is not.
            _save_cursor(done_ids)
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
