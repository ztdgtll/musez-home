#!/usr/bin/env python3
"""wall_apply.py — the bridge's only Pi-side piece.

Deployed on the studio Pi at ~/bridge/wall_apply.py. The bridge (running
where the model call runs) reaches the wall through ONE fixed ssh command
— `python3 ~/bridge/wall_apply.py` — and all content travels as JSON on
stdin, never inside a command string, so titles and lines with $, quotes
or backticks round-trip byte-identical (the same lesson respond2 /
respond_recv.py already paid for on the speech path).

Input (one JSON object on stdin):
    {"turn": <int>, "phase": <int>, "sticky": <bool, optional>,
     "kv": {"scene": ..., "avatar": ..., "title": ..., "lines": [...]}}

A day-plan scene travels as kv {"scene": "plan", "plan": {...}} — the
structured payload bridge/day_plan.py validates (title, date and an
ordered block list). It is validated here too, to the same limits,
before any argv is built. NOTE: the wall page does not render the
"plan" kind yet — rendering timelines is a separate prototype /
approval step. This helper's contract ends at passing a well-formed,
validated payload through to setstate, byte-identical like any other
content.

It builds the setstate.py argv as a list and runs it locally — setstate.py
is the wall's only sanctioned write path, with its turn/phase guard and
exit codes (0 applied, 4 rejected by the guard, 3 lock timeout, 2 usage).
Scene content is validated against the wall's design budget before the
argv is built (title ≤24 chars, at most 2 lines of ≤48 chars, no
newlines); a violation is reported as rc 2 with the limit named.
The result is printed as one JSON line: {"applied": bool, "rc": int}.
Callers must treat a rejection as final for that write: never retry blind.
"""

import json
import os
import subprocess
import sys

SETSTATE = os.environ.get("WALL_APPLY_SETSTATE", "/home/ztdgtll/pared/setstate.py")

# The bridge may only touch scene-content keys — never bookkeeping.
ALLOWED_KEYS = {"scene", "avatar", "title", "lines", "text", "sub", "live", "wave", "plan"}

# The wall's closed design budget for one scene — the same limits
# bridge/studio_room.py validates before a scene is ever queued.
# This file deploys standalone on the Pi, so the constants are
# duplicated here on purpose; the two copies must stay in step.
SCENE_TITLE_MAX = 24
SCENE_LINE_MAX = 48
SCENE_MAX_LINES = 2

# The day-plan budget — mirrors bridge/day_plan.py (same standalone
# duplication rule): at most 6 timeline blocks with labels that fit
# the disc, ordered and non-overlapping within the day.
PLAN_MAX_BLOCKS = 6
PLAN_LABEL_MAX = 48
PLAN_BLOCK_KINDS = ("activity", "rain", "free", "note")


def _validate_scene(kv: dict) -> None:
    """Refuse a scene the closed design system cannot display.

    Raises ValueError naming the limit broken (apply() reports it as
    rc 2): an overlong title wraps out of the disc and long lines
    invade the avatar's exclusion zone, so they never reach setstate.
    """
    title = kv.get("title")
    if title is not None:
        if not isinstance(title, str):
            raise ValueError("title must be text")
        if "\n" in title or "\r" in title:
            raise ValueError("title must be a single line (no newlines)")
        if len(title) > SCENE_TITLE_MAX:
            raise ValueError(
                f"title is {len(title)} characters; "
                f"the limit is {SCENE_TITLE_MAX}")
    lines = kv.get("lines")
    if lines is not None:
        if not isinstance(lines, list) or any(
                not isinstance(x, str) for x in lines):
            raise ValueError("lines must be a list of text")
        if len(lines) > SCENE_MAX_LINES:
            raise ValueError(
                f"a scene takes at most {SCENE_MAX_LINES} lines; "
                f"got {len(lines)}")
        for line in lines:
            if "\n" in line or "\r" in line:
                raise ValueError("lines must be single lines (no newlines)")
            if len(line) > SCENE_LINE_MAX:
                raise ValueError(
                    f"line is {len(line)} characters; "
                    f"the limit is {SCENE_LINE_MAX}")


def _plan_minutes(value):
    """'HH:MM' -> minutes since midnight, or None (mirrors day_plan)."""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    if hour == 24 and minute != 0:
        return None
    return hour * 60 + minute


def _validate_plan(kv: dict) -> None:
    """Refuse a day plan the design system cannot display.

    Mirrors bridge/day_plan.py's plan_problem for the payload that
    travels here as kv["plan"] beside kv["scene"] == "plan"; raises
    ValueError naming the limit broken (apply() reports rc 2)."""
    plan = kv.get("plan")
    if plan is None:
        if kv.get("scene") == "plan":
            raise ValueError("scene 'plan' requires a plan payload")
        return
    if kv.get("scene") != "plan":
        raise ValueError("a plan payload requires scene 'plan'")
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    if plan.get("kind") != "plan":
        raise ValueError("plan kind must be 'plan'")
    title = plan.get("title")
    if not isinstance(title, str):
        raise ValueError("plan title must be text")
    if "\n" in title or "\r" in title:
        raise ValueError("plan title must be a single line (no newlines)")
    if len(title) > SCENE_TITLE_MAX:
        raise ValueError(
            f"plan title is {len(title)} characters; "
            f"the limit is {SCENE_TITLE_MAX}")
    blocks = plan.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("plan blocks must be a list")
    if not blocks:
        raise ValueError("a plan needs at least one block")
    if len(blocks) > PLAN_MAX_BLOCKS:
        raise ValueError(
            f"a plan takes at most {PLAN_MAX_BLOCKS} blocks; "
            f"got {len(blocks)}")
    prev_end = None
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ValueError(f"block {i} must be an object")
        if block.get("kind") not in PLAN_BLOCK_KINDS:
            raise ValueError(
                f"block {i} kind must be one of "
                f"{', '.join(PLAN_BLOCK_KINDS)}")
        label = block.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"block {i} label must be text")
        if "\n" in label or "\r" in label:
            raise ValueError(
                f"block {i} label must be a single line (no newlines)")
        if len(label) > PLAN_LABEL_MAX:
            raise ValueError(
                f"block {i} label is {len(label)} characters; "
                f"the limit is {PLAN_LABEL_MAX}")
        start = _plan_minutes(block.get("start"))
        end = _plan_minutes(block.get("end"))
        if start is None or end is None:
            raise ValueError(f"block {i} times must be HH:MM")
        if start >= end:
            raise ValueError(f"block {i} must start before it ends")
        if prev_end is not None and start < prev_end:
            raise ValueError(
                f"block {i} starts before block {i - 1} ends: "
                f"blocks must be ordered and non-overlapping")
        prev_end = end


def build_argv(payload: dict) -> list:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    turn = payload.get("turn")
    phase = payload.get("phase")
    kv = payload.get("kv")
    if not isinstance(turn, int) or isinstance(turn, bool):
        raise ValueError("turn must be an int")
    if not isinstance(phase, int) or isinstance(phase, bool):
        raise ValueError("phase must be an int")
    if not isinstance(kv, dict) or not kv:
        raise ValueError("kv must be a non-empty object")
    bad = set(kv) - ALLOWED_KEYS
    if bad:
        raise ValueError(f"keys not allowed: {sorted(bad)}")
    _validate_scene(kv)
    _validate_plan(kv)
    argv = ["python3", SETSTATE, f"--turn={turn}", f"--phase={phase}"]
    if payload.get("sticky"):
        argv.append("--sticky")
    for key, value in kv.items():
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        argv.append(f"{key}={value}")
    return argv


def apply(payload: dict, runner=None) -> dict:
    argv = build_argv(payload)
    run = runner or (lambda a: subprocess.run(a, capture_output=True, text=True))
    proc = run(argv)
    return {"applied": proc.returncode == 0, "rc": proc.returncode}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = apply(payload)
    except ValueError as e:
        print(json.dumps({"applied": False, "rc": 2, "error": str(e)}))
        return 0
    except Exception as e:  # stdin unreadable etc. — report, never traceback
        print(json.dumps({"applied": False, "rc": 2, "error": repr(e)}))
        return 0
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
