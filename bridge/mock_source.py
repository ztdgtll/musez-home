"""A fake model stream for developing the bridge without the API.

Yields the same event shapes the live Spark client will yield:
  ("token", text_delta)          streamed reply text
  ("tool_call", name, args)      a function call decided by the model
  ("ack", text)                  an escalation's spoken acknowledgement
                                 (the plan turn: the mock stand-in for
                                 Spark escalating to Wonder)

Four canned turns cover the bridge's paths: a knowledge answer (fast
path), a weather tool call, a wall scene tool call, and a day-plan
turn that composes its plan deterministically from the weather
tool's result (the bridge sends tool results back into the
generator). When the live client lands, it replaces this module —
the bridge loop does not change.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from .day_plan import compose_day_plan

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


def _next_saturday() -> str:
    today = date.today()
    return (today + timedelta(days=(5 - today.weekday()) % 7)).isoformat()


def plan_turn():
    """The demo's planning turn, composed without a model: the
    weather result comes back through send(), the deterministic
    composer (bridge/day_plan.py) turns it into the day plan, and
    the plan goes to the wall as a set_day_plan tool call — the same
    events a live planning model would emit, with numbers that come
    only from the forecast.

    It opens with an ack event: in the live path this turn is an
    escalation (Spark hands it to Wonder), and the ack is what the
    room hears while the deep model works. The text mirrors what
    Spark is asked to supply in its escalate call."""
    yield ("ack", "Dame un momento, armo tu sábado.")
    saturday = _next_saturday()
    weather = yield ("tool_call", "get_weather", {"date": saturday})
    plan = compose_day_plan(weather or {}, date=saturday)
    if plan is None:
        yield from _tokens(
            "No pude armar el plan del sábado: el pronóstico no llegó.")
        return
    yield ("tool_call", "set_day_plan",
           {"title": plan["title"], "date": plan["date"],
            "blocks": plan["blocks"]})
    if any(b["kind"] == "rain" for b in plan["blocks"]):
        yield from _tokens(
            "Listo, tu sábado está en la pared: actividad a la mañana "
            "y en casa antes de la lluvia.")
    else:
        yield from _tokens(
            "Listo, tu sábado está en la pared: no se espera lluvia.")


TURNS = {
    "knowledge": knowledge_turn,
    "weather": weather_turn,
    "wall": wall_turn,
    "plan": plan_turn,
}
