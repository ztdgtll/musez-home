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

State (how many queue lines are done, plus a heartbeat) persists
in ~/.musez-bridge/state.json — liveness is a file read, not a
guess. A lock file keeps a second daemon instance from starting.
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


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} bridge-daemon: {msg}", flush=True)


def _ssh_text(command: str) -> str:
    """One fixed ssh command's stdout; RAISES on a failed fetch.

    The distinction matters: an unreachable Pi must never look like
    an empty queue — the drain keys progress on the entry count, and
    mistaking a transport failure for a shrunken file would replay
    turns the room already answered."""
    proc = subprocess.run(["ssh", "-F", SSH_CONFIG, SSH_HOST, command],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ssh fetch failed rc={proc.returncode}: "
            f"{(proc.stderr or '').strip()[-160:]}")
    return proc.stdout or ""


def parse_entries(text: str) -> list:
    """Valid queue entries from the queue file's text, in order.

    A line counts only as a complete JSON object with an int turn
    and a non-empty text; anything else (a line mid-append, damage)
    is skipped here and left behind in the count the caller keeps —
    the drain is keyed on VALID entries, so a bad line can neither
    wedge the queue nor be mistaken for a turn."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
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
        run_live_turn(entry["text"], entry["turn"])
    except Exception as e:
        log(f"turn {entry['turn']}: runner crashed: "
            f"{type(e).__name__}: {e}")


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lockf = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another daemon holds the lock; exiting")
        return 1
    state = _load_state()
    done = int(state.get("done") or 0)
    log(f"up; {done} queue entries already processed")
    while True:
        try:
            entries = read_queue_entries()
            if len(entries) < done:
                # The queue file shrank (rotation/repair): resync
                # instead of waiting forever for lines that exist.
                log(f"queue shrank ({len(entries)} < {done}); resyncing")
                done = len(entries)
            while done < len(entries):
                entry = entries[done]
                log(f"turn {entry['turn']}: {entry['text']!r}")
                process_entry(entry)
                done += 1
                _save_state({"done": done, "ts": time.time()})
        except Exception as e:
            log(f"poll failed: {type(e).__name__}: {e}")
        finally:
            # The heartbeat ticks even when the Pi is unreachable:
            # liveness must be provable exactly when the link is not.
            _save_state({"done": done, "ts": time.time()})
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
