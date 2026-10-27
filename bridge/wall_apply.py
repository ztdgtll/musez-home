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

It builds the setstate.py argv as a list and runs it locally — setstate.py
is the wall's only sanctioned write path, with its turn/phase guard and
exit codes (0 applied, 4 rejected by the guard, 3 lock timeout, 2 usage).
The result is printed as one JSON line: {"applied": bool, "rc": int}.
Callers must treat a rejection as final for that write: never retry blind.
"""

import json
import os
import subprocess
import sys

SETSTATE = os.environ.get("WALL_APPLY_SETSTATE", "/home/ztdgtll/pared/setstate.py")

# The bridge may only touch scene-content keys — never bookkeeping.
ALLOWED_KEYS = {"scene", "avatar", "title", "lines", "text", "sub", "live", "wave"}


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
