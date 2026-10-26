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
            "description": "Show a scene on the room's wall display: a title and up to two lines of content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Scene title, in English."},
                    "line1": {"type": "string"},
                    "line2": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather and short forecast for the home location (Tamarac, FL).",
            "parameters": {"type": "object", "properties": {}},
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

    def get_weather(self) -> dict:
        self.actions.append({"weather_lookup": True})
        return {"temp_c": 29, "condition": "soleado", "note": "dry-run sample data"}

    def say(self, text: str) -> dict:
        self.actions.append({"say": text})
        return {"ok": True}


def dispatch(room, name: str, args: dict) -> dict:
    """Route one tool call to the room. Unknown tools fail loudly."""
    fn = getattr(room, name, None)
    if fn is None:
        raise ValueError(f"unknown tool: {name}")
    return fn(**args)
