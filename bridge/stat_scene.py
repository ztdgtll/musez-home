"""Stat scenes: the wall's hero-number infographic for one number.

A card scene (title + two lines) can announce a number; it cannot
MAKE the number the exhibit. A stat scene can: one live figure as
the typographic hero of the disc — a small label above it, a short
caption below — composed from real data at flush time, so when the
room reports the live size of the hackathon field the wall shows
the count itself, huge, instead of two sentences about it. The
renderer draws the "stat" kind in bridge/wall_apply.py's payload
shape:

    {"kind": "stat", "value": 611, "label": "THE FIELD",
     "sub": "projects competing in the hackathon"}

Contract boundary — the same one bridge/day_plan.py lives under:
this module validates the payload and the bridge ships it with the
turn's ONE wall write at flush time (the sink carries it as the
dispatch's `scene`, live_turn publishes it as scene "stat"). The
card summary composed from the same payload (`field_stat_card`)
rides as the dispatch's title+lines — the avatar-zone lines the
spoken summary sits beside — never a different claim than the
hero number makes.

The label is a title-class element, so it is English like every
scene title; the caption follows the turn's language, like every
bridge-composed line (see studio_turn.compose_scene).
"""

from __future__ import annotations

STAT_KIND = "stat"

# The wall's closed design budget, stat-shaped (the same limits
# bridge/studio_room.py and bridge/day_plan.py validate against —
# the copies must stay in step): the label never wraps, the caption
# stays inside the disc under the hero number.
STAT_LABEL_MAX = 24
STAT_SUB_MAX = 48

# The one event this stat serves today. The label is title-class
# (always English); the caption is the bridge-composed line and
# follows the turn's language.
FIELD_LABEL = "THE FIELD"
FIELD_SUB = {
    "en": "projects competing in the hackathon",
    "es": "proyectos compitiendo en el hackathon",
}
FIELD_CARD_LINE2 = "Meta AI Developer Hackathon"


def stat_problem(stat) -> str | None:
    """Why a stat payload is not displayable, or None if it fits.

    Every message NAMES the limit it breaks, in the house style of
    day_plan.plan_problem / studio_room.scene_problem."""
    if not isinstance(stat, dict):
        return "stat must be an object"
    if stat.get("kind") != STAT_KIND:
        return "stat kind must be 'stat'"
    value = stat.get("value")
    if not isinstance(value, int) or isinstance(value, bool):
        return "stat value must be an integer"
    if value < 0:
        return "stat value must not be negative"
    label = stat.get("label")
    if not isinstance(label, str) or not label.strip():
        return "stat label must be text"
    if "\n" in label or "\r" in label:
        return "stat label must be a single line (no newlines)"
    if len(label) > STAT_LABEL_MAX:
        return (f"stat label is {len(label)} characters; "
                f"the limit is {STAT_LABEL_MAX}")
    sub = stat.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        return "stat sub must be text"
    if "\n" in sub or "\r" in sub:
        return "stat sub must be a single line (no newlines)"
    if len(sub) > STAT_SUB_MAX:
        return (f"stat sub is {len(sub)} characters; "
                f"the limit is {STAT_SUB_MAX}")
    return None


def compose_field_stat(field, lang: str = "es") -> dict | None:
    """The stat payload for a hackathon-field result, or None when
    the result carries no usable count — the caller then falls back
    to the plain card, and a failed lookup (room.field stays None)
    never reaches here at all. The number is the one the API
    returned THIS turn: never cached, never hard-coded."""
    if not isinstance(field, dict):
        return None
    stat = {"kind": STAT_KIND,
            "value": field.get("projects"),
            "label": FIELD_LABEL,
            "sub": FIELD_SUB.get(lang, FIELD_SUB["es"])}
    return None if stat_problem(stat) is not None else stat


def field_stat_card(stat: dict, lang: str = "es"):
    """The card summary (title, two lines) of a field stat — the
    same lines the plain field card has always carried, composed
    from the payload the flush ships, so the avatar-zone lines and
    the hero number never disagree."""
    n = stat.get("value")
    if lang == "en":
        line1 = f"{n} projects competing"
    else:
        line1 = f"{n} proyectos compitiendo"
    return stat.get("label", ""), [line1, FIELD_CARD_LINE2]
