"""The real Room: bridge tool calls -> the studio's actual surfaces.

Same dispatch contract as `DryRoom` (tools.py), against the live room:

- `set_wall_scene` validates the scene against the wall's closed
  design budget and records it as the turn's scene CANDIDATE — it
  never writes the wall itself. The turn's single wall write ships
  with the spoken answer at flush time: the sink transport carries
  the final title+lines and the player writes them through the Pi's
  `setstate.py` (the wall's only sanctioned write path). The wall
  therefore never shows a mid-turn draft that the voice then
  corrects. An invalid scene comes back as an {"ok": False} result
  naming the limit it broke, and the rejection is kept on the room
  (`wall_rejections`) so the turn record does not lose it.
- `get_weather` reads the same Open-Meteo feed (Tamarac, FL) the
  studio's fast path uses, via fastwx.sh: today plus a 7-day forecast,
  with hourly precipitation probability for one date from the same
  feed. Feed problems come back as {"ok": False, ...} tool results —
  a weather lookup never raises into the turn.
- `say` routes into the turn's speech sink, so an acknowledgement is
  spoken in order, inside the same turn, not as a rival response.

Phase discipline mirrors the listener's ladder (1 listening, 2
processing, 3 thinking, 4 answer, 5 home): nothing the bridge does
mid-turn touches the wall — the answer's scene lands exactly once,
at phase 4 under the turn's id, together with the voice.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request

SSH_CONFIG = os.path.expanduser("~/workspace/tools/ssh_config_pis")
SSH_HOST = "pi5"
WALL_APPLY_CMD = "python3 /home/ztdgtll/bridge/wall_apply.py"
MINT_TURN_CMD = "python3 /home/ztdgtll/pared/setstate.py --mint-turn"
DEFAULT_FASTWX = os.path.expanduser("~/workspace/studio/fastwx.sh")

# The wall's closed design budget for one scene: an English title
# that never wraps, at most two lines that stay inside the disc.
# The Pi-side helper (wall_apply.py) enforces the same limits before
# any setstate write; the two copies must stay in step.
SCENE_TITLE_MAX = 24
SCENE_LINE_MAX = 48
SCENE_MAX_LINES = 2


def scene_problem(title, lines) -> str | None:
    """Why a wall scene is not displayable, or None if it fits.

    Every message NAMES the limit it breaks, so a model whose scene
    is rejected can shorten and retry instead of guessing."""
    if not isinstance(title, str):
        return "scene title must be text"
    if "\n" in title or "\r" in title:
        return "scene title must be a single line (no newlines)"
    if len(title) > SCENE_TITLE_MAX:
        return (f"scene title is {len(title)} characters; "
                f"the limit is {SCENE_TITLE_MAX}")
    if len(lines) > SCENE_MAX_LINES:
        return (f"a scene takes at most {SCENE_MAX_LINES} lines; "
                f"got {len(lines)}")
    for line in lines:
        if not isinstance(line, str):
            return "scene lines must be text"
        if "\n" in line or "\r" in line:
            return "scene lines must be single lines (no newlines)"
        if len(line) > SCENE_LINE_MAX:
            return (f"scene line is {len(line)} characters; "
                    f"the limit is {SCENE_LINE_MAX}")
    return None

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


FORECAST_DAYS = 7


class FastWxRunner:
    """The Open-Meteo daily block for Tamarac, via fastwx.sh.

    Asks the script for the full forecast window so `get_weather` can
    answer about the weekend, not just today."""

    def __init__(self, script: str = None, runner=None, days: int = FORECAST_DAYS):
        self.script = script or os.environ.get("BRIDGE_FASTWX_PATH", DEFAULT_FASTWX)
        self.days = days
        self._run = runner or (
            lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=30)
        )

    def __call__(self) -> dict:
        proc = self._run(["bash", self.script, str(self.days)])
        if proc.returncode != 0:
            raise RuntimeError(f"fastwx failed: {(proc.stderr or '').strip()[-200:]}")
        return json.loads(proc.stdout)


class HourlyWxRunner:
    """Hourly precipitation probability for Tamarac, from the same
    Open-Meteo feed fastwx.sh reads for the daily block.

    Returns the parsed hourly block, or None on ANY failure: hourly
    detail is an enrichment of the forecast, and its absence must
    never kill a weather answer."""

    URL = ("https://api.open-meteo.com/v1/forecast?latitude=26.2131"
           "&longitude=-80.2498&hourly=precipitation_probability"
           f"&timezone=America%2FNew_York&forecast_days={FORECAST_DAYS}")

    def __init__(self, opener=None, timeout: int = 10):
        self._open = opener or (
            lambda url: urllib.request.urlopen(url, timeout=timeout))

    def __call__(self):
        try:
            with self._open(self.URL) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            hourly = data.get("hourly") or {}
            if not hourly.get("time"):
                return None
            return hourly
        except Exception:
            return None


_DAILY_KEYS = ("time", "temperature_2m_max", "temperature_2m_min",
               "precipitation_probability_max", "weathercode")


def _daily_problem(daily) -> str | None:
    """Why the daily block is unusable, or None if it validates.

    Open-Meteo legitimately returns nulls (precipitation probability
    above all); indexing them blind used to crash the turn after the
    model had already streamed its answer."""
    if not isinstance(daily, dict):
        return "weather feed returned no daily block"
    for key in _DAILY_KEYS:
        if not isinstance(daily.get(key), list) or not daily[key]:
            return f"daily block is missing {key}"
    n = len(daily["time"])
    if any(len(daily[key]) < n for key in _DAILY_KEYS):
        return "daily block arrays have inconsistent lengths"
    for key in _DAILY_KEYS[1:]:
        if daily[key][0] is None:
            return f"daily block has no value for {key} today"
    return None


def _hourly_for_date(hourly, date: str):
    """[{time: 'HH:MM', precip_probability}] for one date, or None."""
    if not hourly:
        return None
    times = hourly.get("time") or []
    probs = hourly.get("precipitation_probability") or []
    out = [
        {"time": t[11:16], "precip_probability": p}
        for t, p in zip(times, probs)
        if isinstance(t, str) and t[:10] == date
    ]
    return out or None


class StudioRoom:
    """The bridge's Room against the real studio. One instance per turn."""

    def __init__(self, turn: int, weather_runner=None,
                 speak=None, hourly_runner=None):
        self.turn = turn
        self._weather = weather_runner or FastWxRunner()
        self._hourly = hourly_runner  # None = no hourly detail available
        self._speak = speak
        # Scene candidates recorded by set_wall_scene (validated, in
        # call order; the runner composes the turn's ONE final scene
        # from the last one) and the scenes validation turned away.
        self.wall_scenes: list[dict] = []
        self.wall_rejections: list[dict] = []
        self.weather = None

    def set_wall_scene(self, title: str, line1: str = "", line2: str = "") -> dict:
        """Record the turn's scene candidate. Does NOT write the wall:
        the single write happens at flush time, carrying the final
        scene together with the spoken answer — a mid-turn write
        would let the wall show a draft and then change its answer
        in public. An invalid scene is rejected with the limit it
        broke (so the model can shorten and retry) and kept in
        `wall_rejections` for the turn record."""
        lines = [x for x in (line1, line2) if x]
        problem = scene_problem(title, lines)
        if problem is not None:
            self.wall_rejections.append(
                {"title": title, "lines": lines, "error": problem})
            return {"ok": False, "error": problem}
        self.wall_scenes.append({"title": title, "lines": lines})
        return {"ok": True, "queued": title}

    def get_weather(self, date: str = None) -> dict:
        """Today's summary (top level, as always) plus a labeled 7-day
        `forecast` and the `hourly_precipitation` for `date`
        (YYYY-MM-DD, default today). Any feed or validation problem
        returns {"ok": False, "error": ...} instead of raising — and
        leaves `self.weather` untouched, so scene composition falls
        back instead of crashing on a null."""
        try:
            daily = self._weather()
        except Exception as e:
            return {"ok": False, "error": f"weather feed failed: {e}"}
        problem = _daily_problem(daily)
        if problem:
            return {"ok": False, "error": problem}
        forecast = [
            {
                "date": daily["time"][i],
                "temp_max_f": daily["temperature_2m_max"][i],
                "temp_min_f": daily["temperature_2m_min"][i],
                "precip_prob_max": daily["precipitation_probability_max"][i],
                "weathercode": daily["weathercode"][i],
                "condition": condition_es(daily["weathercode"][i]),
            }
            for i in range(len(daily["time"]))
        ]
        target = date or forecast[0]["date"]
        if target not in daily["time"]:
            return {"ok": False,
                    "error": f"date {target} is outside the forecast "
                             f"window {daily['time'][0]}..{daily['time'][-1]}"}
        hourly = self._hourly() if self._hourly is not None else None
        today = forecast[0]
        self.weather = {
            "temp_max_f": today["temp_max_f"],
            "temp_min_f": today["temp_min_f"],
            "precip_prob_max": today["precip_prob_max"],
            "weathercode": today["weathercode"],
            "condition": today["condition"],
            "forecast": forecast,
            "hourly_date": target,
            "hourly_precipitation": _hourly_for_date(hourly, target),
            "source": "Open-Meteo (Tamarac, FL), via fastwx.sh",
        }
        return self.weather

    def say(self, text: str) -> dict:
        if self._speak is None:
            return {"ok": False, "error": "no speech sink wired to this room"}
        self._speak(text)
        return {"ok": True}
