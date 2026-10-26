"""A fake model stream for developing the bridge without the API.

Yields the same event shapes the live Spark client will yield:
  ("token", text_delta)          streamed reply text
  ("tool_call", name, args)      a function call decided by the model

Three canned turns cover the bridge's paths: a knowledge answer (fast path),
a weather tool call, and a wall scene tool call. When the live client lands,
it replaces this module — the bridge loop does not change.
"""

from __future__ import annotations

import time

_PACE = 0.02  # seconds between fake tokens


def _tokens(text: str):
    for word in text.split(" "):
        time.sleep(_PACE)
        yield ("token", word + " ")


def knowledge_turn():
    yield from _tokens(
        "Una Raspberry Pi es una computadora completa del tamaño de una tarjeta. "
        "En este cuarto, es la que sostiene la pared que estás viendo."
    )


def weather_turn():
    yield ("tool_call", "get_weather", {})
    yield from _tokens(
        "Ahora hay 29 grados y está soleado en Tamarac. "
        "Para la tarde no se espera lluvia."
    )


def wall_turn():
    yield ("tool_call", "set_wall_scene",
           {"title": "WEATHER", "line1": "Tamarac · 29°C · Soleado",
            "line2": "Sin lluvia para la tarde"})
    yield from _tokens("Listo, te lo puse en la pared.")


TURNS = {
    "knowledge": knowledge_turn,
    "weather": weather_turn,
    "wall": wall_turn,
}
