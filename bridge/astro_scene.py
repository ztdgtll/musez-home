"""Astro scenes: the wall's dispatch card for a robot errand.

A plain card scene (title + two lines) can SAY Astro is coming; it
cannot SHOW it. An astro scene can: the turn that successfully
dispatched the household robot ships a structured payload and the
wall draws its delivery card — the scene title, a large line
drawing of the robot with its cargo, and the reply's own lines on
the avatar's anchored line. The renderer draws the "astro" kind
from bridge/wall_apply.py's payload shape:

    {"kind": "astro", "destination": "studio", "ok": true}

Contract boundary — the same one bridge/stat_scene.py lives
under: this module validates the payload and the bridge ships it
with the turn's ONE wall write at flush time (the sink carries it
as the dispatch's `scene`, live_turn publishes it as scene
"astro"). The payload is composed deterministically in the bridge
from the tool result the room recorded (bridge/studio_room.py's
`astro`: destination + ok, set only by a successful call_astro) —
never from the model's wording, never hard-coded to a request.
The card's title is title-class, so it is English like every
scene title; its lines are the model's own reply sentences for
the turn, exactly as a plain scene's lines are (see
studio_turn.compose_scene). The dispatch also carries a
standing signature line: a bridge-composed constant that follows
the turn's language like every bridge-composed line, spoken as
the turn's final sentence. It lives ONLY in the audio — printed
under the card's drawing it crowded the reply's own lines, so
Javier cut it from the wall (2026-11-02); the card is title,
drawing and the reply's lines.
"""

from __future__ import annotations

ASTRO_KIND = "astro"

# The card's title — title-class, so English, and inside the
# wall's title budget (bridge/studio_room.py's SCENE_TITLE_MAX,
# enforced again Pi-side by bridge/wall_apply.py).
ASTRO_TITLE = "SPECIAL DELIVERY"

# The destinations a dispatch can name — mirrors
# bridge/studio_room.py's ASTRO_URL_ENVS keys (the copies must
# stay in step): a payload naming anything else is not a dispatch
# this system can have made.
ASTRO_DESTINATIONS = ("studio", "kitchen")

# The dispatch's standing signature line. It is SPOKEN as the
# successful turn's final sentence (appended by the runners at
# flush time, after the model's own sentences, in the same
# dispatch — never a separate turn). A personality signature,
# not turn content — the garage wall's standing jokes are the
# house precedent — but bridge-composed, so it follows the
# TURN's language like every bridge-composed line. Voice only:
# it is NOT part of the wall payload (see the module docstring).
ASTRO_CAPTIONS = {
    "en": "The delivery guy doesn't take tips.",
    "es": "El repartidor no acepta propinas.",
}


def astro_problem(astro) -> str | None:
    """Why an astro payload is not displayable, or None if it fits.

    Every message NAMES the limit it breaks, in the house style of
    stat_scene.stat_problem / day_plan.plan_problem."""
    if not isinstance(astro, dict):
        return "astro must be an object"
    if astro.get("kind") != ASTRO_KIND:
        return "astro kind must be 'astro'"
    if astro.get("ok") is not True:
        return "astro ok must be true (only a successful dispatch ships)"
    destination = astro.get("destination")
    if destination not in ASTRO_DESTINATIONS:
        return (f"astro destination must be one of "
                f"{', '.join(ASTRO_DESTINATIONS)}; got {destination!r}")
    return None


def compose_astro_dispatch(dispatch, lang: str = "es") -> dict | None:
    """The astro payload for a recorded dispatch, or None when the
    record carries no successful dispatch — the caller then falls
    back to the plain card, and a failed dispatch (room.astro stays
    None) never reaches here at all. The destination is the one
    THIS turn's tool call named: never cached, never hard-coded.
    The payload does not vary with `lang` (the parameter stays
    for call-site symmetry): the language-dependent piece of a
    dispatch is its spoken signature, astro_caption_for below."""
    if not isinstance(dispatch, dict):
        return None
    payload = {"kind": ASTRO_KIND,
               "destination": dispatch.get("destination"),
               "ok": dispatch.get("ok")}
    return None if astro_problem(payload) is not None else payload


def astro_caption_for(dispatch, lang: str = "es") -> str | None:
    """The spoken signature for a recorded dispatch, in the turn's
    language, or None when the record is not a successful
    dispatch — the line is spoken exactly where the card ships,
    and nowhere else. Voice only: it is not printed on the card
    (Javier's call, 2026-11-02 — under the drawing it crowded
    the reply's own lines)."""
    if compose_astro_dispatch(dispatch) is None:
        return None
    return ASTRO_CAPTIONS.get(lang, ASTRO_CAPTIONS["es"])
