"""Room tools exposed to the model via function calling.

The schemas below are what Muse Spark sees. `dispatch` routes a tool call to
a Room implementation: `DryRoom` records actions (used by the local mock run
and tests); the live Pi implementation is wired at integration time without
changing these schemas.
"""

from __future__ import annotations

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "set_wall_scene",
            "description": "Show a scene on the room's wall display: a title and up to two lines of content. The scene is queued and lands on the wall together with your spoken answer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Scene title, in English, at most 24 characters, single line."},
                    "line1": {"type": "string", "description": "At most 48 characters, single line."},
                    "line2": {"type": "string", "description": "At most 48 characters, single line."},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_day_plan",
            "description": "Show a day plan on the room's wall as a timeline infographic: an ordered list of blocks (morning activity, free time, the rain band). Use for planning a day, not for a single fact. The plan is queued and lands on the wall together with your spoken answer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Plan title, in English, at most 24 characters, single line (e.g. 'Saturday plan')."},
                    "date": {"type": "string", "description": "The planned date, YYYY-MM-DD."},
                    "blocks": {
                        "type": "array",
                        "description": "At most 6 blocks, ordered, non-overlapping, within the day.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start": {"type": "string", "description": "Start time, HH:MM (24h)."},
                                "end": {"type": "string", "description": "End time, HH:MM (24h), after start."},
                                "label": {"type": "string", "description": "Block label, in Spanish, at most 48 characters, single line."},
                                "kind": {"type": "string", "enum": ["activity", "rain", "free", "note"]},
                            },
                            "required": ["start", "end", "label", "kind"],
                        },
                    },
                },
                "required": ["title", "blocks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather for the home location (Tamarac, FL): today plus a 7-day forecast, with hourly precipitation probability for one date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Optional date (YYYY-MM-DD) for the hourly precipitation detail; defaults to today.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "say",
            "description": "Speak a short text aloud in the room. Used for acknowledgements while work runs.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
]


class DryRoom:
    """Records room actions instead of performing them (tests / mock runs)."""

    def __init__(self) -> None:
        self.actions: list[dict] = []

    def set_wall_scene(self, title: str, line1: str = "", line2: str = "") -> dict:
        self.actions.append({"wall": title, "line1": line1, "line2": line2})
        return {"ok": True, "shown": title}

    def set_day_plan(self, title: str, blocks: list = None,
                     date: str = None) -> dict:
        self.actions.append({"plan": title, "blocks": blocks, "date": date})
        return {"ok": True, "queued": title}

    def get_weather(self, date: str = None) -> dict:
        self.actions.append({"weather_lookup": True, "date": date})
        return _sample_weather(date)


def _sample_weather(date: str = None) -> dict:
    """A deterministic stand-in for the real feed, in the same shape
    StudioRoom returns: a 7-day forecast starting today and one
    date's hourly precipitation — dry all morning, rain at or above
    the 50% band threshold from 13:00 to 17:00, dry again after.
    Fixed patterns (only the dates move), so the deterministic plan
    composer produces the same Saturday every run."""
    from datetime import date as _date, timedelta

    today = _date.today()
    target = date or today.isoformat()
    forecast = []
    for i in range(7):
        day = today + timedelta(days=i)
        forecast.append({
            "date": day.isoformat(),
            "temp_max_f": 84.0, "temp_min_f": 73.0,
            "precip_prob_max": 80 if i % 3 == 2 else 10,
            "weathercode": 61 if i % 3 == 2 else 0,
            "condition": "lluvia leve" if i % 3 == 2 else "despejado",
        })
    hourly = [
        {"time": f"{h:02d}:00",
         "precip_probability": 80 if 13 <= h <= 17 else 10}
        for h in range(24)
    ]
    today_entry = forecast[0]
    return {
        "temp_c": 29, "condition": "soleado",
        "note": "dry-run sample data",
        "temp_max_f": today_entry["temp_max_f"],
        "temp_min_f": today_entry["temp_min_f"],
        "precip_prob_max": today_entry["precip_prob_max"],
        "weathercode": today_entry["weathercode"],
        "forecast": forecast,
        "hourly_date": target,
        "hourly_precipitation": hourly,
        "source": "dry-run sample data",
    }

    def say(self, text: str) -> dict:
        self.actions.append({"say": text})
        return {"ok": True}


_TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}


def dispatch(room, name: str, args: dict) -> dict:
    """Route one tool call to the room. Never raises.

    Unknown tools, malformed arguments and room failures all come back
    as {"ok": False, "error": ...} — a result the model can react to —
    instead of propagating and killing the turn mid-stream.
    """
    if name not in _TOOL_NAMES:
        return {"ok": False, "error": f"unknown tool: {name}"}
    fn = getattr(room, name, None)
    if not callable(fn):
        return {"ok": False, "error": f"unknown tool: {name}"}
    if not isinstance(args, dict):
        return {"ok": False,
                "error": f"tool arguments must be a JSON object, "
                         f"got {type(args).__name__}"}
    try:
        return fn(**args)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
