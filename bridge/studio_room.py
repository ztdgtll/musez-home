"""The real Room: bridge tool calls -> the studio's actual surfaces.

Same dispatch contract as `DryRoom` (tools.py), against the live room:

- `set_wall_scene` writes through the Pi's `setstate.py` — the wall's
  only sanctioned write path — via the bridge's Pi-side helper
  (wall_apply.py, deployed at ~/bridge/ on the Pi). One fixed ssh
  command; all content travels as JSON on stdin. Guard rejections
  (setstate exit 4) are reported in the tool result and never retried:
  a rejected write stays rejected.
- `get_weather` reads the same Open-Meteo feed (Tamarac, FL) the
  studio's fast path uses, via fastwx.sh.
- `say` routes into the turn's speech sink, so an acknowledgement is
  spoken in order, inside the same turn, not as a rival response.

Phase discipline mirrors the listener's ladder (1 listening, 2
processing, 3 thinking, 4 answer, 5 home): a tool scene written
mid-turn rides at phase 3 under the turn's id; the turn's spoken answer
(the player, via the sink transport) writes phase 4 under the same id
and stays ahead of it.
"""

from __future__ import annotations

import json
import os
import subprocess

SSH_CONFIG = os.path.expanduser("~/workspace/tools/ssh_config_pis")
SSH_HOST = "pi5"
WALL_APPLY_CMD = "python3 /home/ztdgtll/bridge/wall_apply.py"
MINT_TURN_CMD = "python3 /home/ztdgtll/pared/setstate.py --mint-turn"
DEFAULT_FASTWX = os.path.expanduser("~/workspace/studio/fastwx.sh")

WALL_PHASE_TOOL = 3  # mid-turn; the spoken answer writes phase 4

# WMO weathercode -> Spanish condition, the vocabulary the room speaks.
WMO_CONDITIONS_ES = {
    0: "despejado", 1: "mayormente despejado", 2: "parcialmente nublado",
    3: "nublado", 45: "niebla", 48: "niebla", 51: "llovizna leve",
    53: "llovizna", 55: "llovizna intensa", 56: "llovizna helada",
    57: "llovizna helada", 61: "lluvia leve", 63: "lluvia",
    65: "lluvia fuerte", 66: "lluvia helada", 67: "lluvia helada",
    71: "nieve", 73: "nieve", 75: "nieve", 77: "nieve",
    80: "chaparrones leves", 81: "chaparrones", 82: "chaparrones fuertes",
    85: "chaparrones de nieve", 86: "chaparrones de nieve",
    95: "tormenta", 96: "tormenta con granizo", 99: "tormenta con granizo",
}


def condition_es(weathercode) -> str:
    return WMO_CONDITIONS_ES.get(weathercode, "sin datos")


def _ssh(argv, stdin_text=None, timeout=60):
    return subprocess.run(
        argv, input=stdin_text, capture_output=True, text=True, timeout=timeout
    )


class SshJsonRunner:
    """Run ONE fixed remote command; the payload goes in as stdin JSON.

    The command string never contains content — the same fixed-string
    rule the studio's response receiver enforces.
    """

    def __init__(self, command: str, runner=None, host: str = SSH_HOST,
                 ssh_config: str = SSH_CONFIG):
        self.command = command
        self._run = runner or _ssh
        self.host = host
        self.ssh_config = ssh_config

    def __call__(self, payload: dict) -> dict:
        argv = ["ssh", "-F", self.ssh_config, self.host, self.command]
        try:
            proc = self._run(argv, stdin_text=json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            return {"applied": False, "rc": None, "error": f"transport: {e!r}"}
        out = (proc.stdout or "").strip()
        try:
            return json.loads(out.splitlines()[-1])
        except Exception:
            return {"applied": False, "rc": None,
                    "error": f"unparseable reply: {out[-200:]!r}"}


def mint_turn(runner=None) -> int:
    """Mint a fresh wall turn id from the Pi's own minter (lock-reserved,
    collision-free) — the same ids the listener's turns use."""
    run = runner or _ssh
    argv = ["ssh", "-F", SSH_CONFIG, SSH_HOST, MINT_TURN_CMD]
    proc = run(argv)
    if proc.returncode != 0:
        raise RuntimeError(f"mint-turn failed: {(proc.stderr or '').strip()[-200:]}")
    return int((proc.stdout or "").strip().split()[-1])


class FastWxRunner:
    """Today's Open-Meteo daily block for Tamarac, via fastwx.sh."""

    def __init__(self, script: str = None, runner=None):
        self.script = script or os.environ.get("BRIDGE_FASTWX_PATH", DEFAULT_FASTWX)
        self._run = runner or (
            lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=30)
        )

    def __call__(self) -> dict:
        proc = self._run(["bash", self.script])
        if proc.returncode != 0:
            raise RuntimeError(f"fastwx failed: {(proc.stderr or '').strip()[-200:]}")
        return json.loads(proc.stdout)


class StudioRoom:
    """The bridge's Room against the real studio. One instance per turn."""

    def __init__(self, turn: int, wall_runner=None, weather_runner=None, speak=None):
        self.turn = turn
        self._wall = wall_runner or SshJsonRunner(WALL_APPLY_CMD)
        self._weather = weather_runner or FastWxRunner()
        self._speak = speak
        self.wall_scenes: list[dict] = []
        self.weather = None

    def set_wall_scene(self, title: str, line1: str = "", line2: str = "") -> dict:
        lines = [x for x in (line1, line2) if x]
        payload = {
            "turn": self.turn,
            "phase": WALL_PHASE_TOOL,
            "kv": {"scene": "answer", "avatar": "working",
                   "title": title, "lines": lines},
        }
        res = self._wall(payload)
        if res.get("applied"):
            self.wall_scenes.append({"title": title, "lines": lines})
            return {"ok": True, "shown": title}
        # Guard rejection (rc 4), lock timeout (rc 3) or transport
        # failure: reported, never retried — a dropped write stays dropped.
        return {"ok": False, "shown": None, "rc": res.get("rc"),
                "rejected": res.get("rc") == 4}

    def get_weather(self) -> dict:
        daily = self._weather()
        code = daily["weathercode"][0]
        self.weather = {
            "temp_max_f": daily["temperature_2m_max"][0],
            "temp_min_f": daily["temperature_2m_min"][0],
            "precip_prob_max": daily["precipitation_probability_max"][0],
            "weathercode": code,
            "condition": condition_es(code),
            "source": "Open-Meteo (Tamarac, FL), via fastwx.sh",
        }
        return self.weather

    def say(self, text: str) -> dict:
        if self._speak is None:
            return {"ok": False, "error": "no speech sink wired to this room"}
        self._speak(text)
        return {"ok": True}
