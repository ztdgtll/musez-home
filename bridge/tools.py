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

    def get_weather(self, date: str = None) -> dict:
        self.actions.append({"weather_lookup": True, "date": date})
        return {"temp_c": 29, "condition": "soleado",
                "note": "dry-run sample data",
                "forecast": [], "hourly_precipitation": None}

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
