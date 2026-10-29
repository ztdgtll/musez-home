"""Day plans: the wall's structured infographic scene for one day.

A card scene (title + two lines) can announce an answer; it cannot
SHOW a day. A day plan can: an ordered timeline of blocks — the
morning activity placed in the dry hours, the rain drawn as a band —
composed from real data, so when Wonder plans Javier's Saturday the
wall displays the whole day at once instead of two sentences about
it (Wonder review finding 40: the bridge only emitted answer cards).

Payload (one JSON object):

    {"kind": "plan", "date": "2026-10-31", "title": "Saturday plan",
     "blocks": [{"start": "08:00", "end": "12:00",
                 "label": "Salida a la mañana", "kind": "activity"},
                {"start": "12:00", "end": "13:00",
                 "label": "En casa", "kind": "free"},
                {"start": "13:00", "end": "18:00",
                 "label": "Lluvia", "kind": "rain"}]}

Contract boundary — read before wiring anything else: this module
validates the payload and the bridge ships it with the turn's ONE
wall write at flush time (the sink carries it as the dispatch's
`scene`). The wall page does not render the "plan" kind yet: making
it draw timelines is a separate prototype/approval step. Until then
the payload reaches the flush write well-formed and validated, and
the card summary composed from the same plan (`plan_card`) is what
the existing wall path shows.
"""

from __future__ import annotations

from datetime import date as _date

PLAN_KIND = "plan"
BLOCK_KINDS = ("activity", "rain", "free", "note")

# The same closed design budget the card scenes live under
# (bridge/studio_room.py SCENE_TITLE_MAX / SCENE_LINE_MAX — the two
# copies must stay in step): a title that never wraps, labels that
# stay inside the disc, and few enough blocks to read at a glance.
PLAN_TITLE_MAX = 24
PLAN_LABEL_MAX = 48
PLAN_MAX_BLOCKS = 6

# The rain band starts at the first hour whose precipitation
# probability reaches this threshold and runs until the first hour
# back below it. 50% is the "more likely than not" line: the band
# is a forecast claim, so it should start when rain becomes the
# probable outcome, not at the first nonzero chance.
RAIN_THRESHOLD_PCT = 50


def _minutes(value) -> int | None:
    """'HH:MM' -> minutes since midnight, or None if not a time.

    Accepts a one- or two-digit hour; 24:00 is a valid END of day."""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    if hour == 24 and minute != 0:
        return None
    return hour * 60 + minute


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def plan_problem(plan) -> str | None:
    """Why a day-plan payload is not displayable, or None if it fits.

    Every message NAMES the limit it breaks — the same contract the
    card scenes keep — so a model whose plan is rejected can fix the
    exact problem and retry."""
    if not isinstance(plan, dict):
        return "plan must be an object"
    if plan.get("kind") != PLAN_KIND:
        return f"plan kind must be {PLAN_KIND!r}"
    title = plan.get("title")
    if not isinstance(title, str):
        return "plan title must be text"
    if "\n" in title or "\r" in title:
        return "plan title must be a single line (no newlines)"
    if len(title) > PLAN_TITLE_MAX:
        return (f"plan title is {len(title)} characters; "
                f"the limit is {PLAN_TITLE_MAX}")
    day = plan.get("date")
    if day is not None:
        try:
            _date.fromisoformat(day)
        except (TypeError, ValueError):
            return "plan date must be YYYY-MM-DD"
    blocks = plan.get("blocks")
    if not isinstance(blocks, list):
        return "plan blocks must be a list"
    if not blocks:
        return "a plan needs at least one block"
    if len(blocks) > PLAN_MAX_BLOCKS:
        return (f"a plan takes at most {PLAN_MAX_BLOCKS} blocks; "
                f"got {len(blocks)}")
    prev_end = None
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            return f"block {i} must be an object"
        if block.get("kind") not in BLOCK_KINDS:
            return (f"block {i} kind must be one of "
                    f"{', '.join(BLOCK_KINDS)}")
        label = block.get("label")
        if not isinstance(label, str) or not label.strip():
            return f"block {i} label must be text"
        if "\n" in label or "\r" in label:
            return f"block {i} label must be a single line (no newlines)"
        if len(label) > PLAN_LABEL_MAX:
            return (f"block {i} label is {len(label)} characters; "
                    f"the limit is {PLAN_LABEL_MAX}")
        start = _minutes(block.get("start"))
        end = _minutes(block.get("end"))
        if start is None or end is None:
            return f"block {i} times must be HH:MM"
        if start >= end:
            return f"block {i} must start before it ends"
        if prev_end is not None and start < prev_end:
            return (f"block {i} starts before block {i - 1} ends: "
                    f"blocks must be ordered and non-overlapping")
        prev_end = end
    return None


def rain_band(hourly_precipitation, threshold: int = RAIN_THRESHOLD_PCT):
    """The day's rain band from hourly precipitation probability.

    `hourly_precipitation` is get_weather's per-date list of
    {"time": "HH:MM", "precip_probability": number|None}. The band
    starts at the first hour at or above `threshold` and ends at the
    first later hour back below it (or one hour past the last entry
    if the rain never lets up). A null probability is unknown, not
    rain: it never starts a band and it ends one. Returns
    {"start": "HH:MM", "end": "HH:MM"} or None when no hour reaches
    the threshold."""
    if not hourly_precipitation:
        return None
    entries = []
    for item in hourly_precipitation:
        if not isinstance(item, dict):
            continue
        start = _minutes(item.get("time"))
        if start is None:
            continue
        prob = item.get("precip_probability")
        entries.append((start, prob if isinstance(prob, (int, float))
                        and not isinstance(prob, bool) else None))
    entries.sort()
    band_start = None
    for i, (start, prob) in enumerate(entries):
        if band_start is None:
            if prob is not None and prob >= threshold:
                band_start = start
        elif prob is None or prob < threshold:
            return {"start": _hhmm(band_start), "end": _hhmm(start)}
    if band_start is None:
        return None
    last = entries[-1][0]
    return {"start": _hhmm(band_start), "end": _hhmm(min(last + 60, 24 * 60))}


def compose_day_plan(weather, activity_label: str = "Salida a la mañana",
                     date: str = None) -> dict | None:
    """Deterministically compose a day plan from a get_weather result.

    The demo's plan — a morning activity, home before the rain — built
    from the REAL forecast (7-day daily block + the requested date's
    hourly precipitation), so it works before any free-form model
    planning is trusted with the wall. Returns a payload that passes
    `plan_problem`, or None when the weather data cannot support a
    plan for the date (the caller then falls back to a card scene).

    Shape of the day: the activity runs from 08:00 until the rain
    starts or 12:00, whichever comes first; gaps are "En casa"; the
    rain band is drawn from the hourly probabilities; after the rain,
    the rest of the day until 20:00 is "En casa". A morning already
    rained out gets no activity block — the day starts with the band.
    """
    if not isinstance(weather, dict):
        return None
    forecast = weather.get("forecast")
    if not isinstance(forecast, list) or not forecast:
        return None
    target = date or weather.get("hourly_date") or forecast[0].get("date")
    entry = next((d for d in forecast
                  if isinstance(d, dict) and d.get("date") == target), None)
    if entry is None:
        return None
    try:
        weekday = _date.fromisoformat(target).strftime("%A")
    except (TypeError, ValueError):
        return None
    hourly = (weather.get("hourly_precipitation")
              if weather.get("hourly_date") == target else None)
    band = rain_band(hourly)

    morning_start, morning_end, day_end = "08:00", "12:00", "20:00"
    blocks: list[dict] = []

    def add(start, end, label, kind):
        if _minutes(start) < _minutes(end):
            blocks.append({"start": start, "end": end,
                           "label": label, "kind": kind})

    if band is not None and band["start"] <= morning_start:
        # Rained out from early: no dry morning to place the activity.
        add(band["start"], band["end"], "Lluvia", "rain")
        add(band["end"], day_end, "En casa", "free")
    elif band is not None:
        activity_end = min(band["start"], morning_end,
                           key=lambda t: _minutes(t))
        add(morning_start, activity_end, activity_label, "activity")
        add(activity_end, band["start"], "En casa", "free")
        add(band["start"], band["end"], "Lluvia", "rain")
        add(band["end"], day_end, "En casa", "free")
    else:
        add(morning_start, morning_end, activity_label, "activity")
        add(morning_end, day_end, "En casa", "free")

    plan = {"kind": PLAN_KIND, "date": target,
            "title": f"{weekday} plan", "blocks": blocks}
    return plan if plan_problem(plan) is None else None


def _truncate(text: str, limit: int = PLAN_LABEL_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def plan_card(plan: dict):
    """The card summary (title, up to two lines) of a day plan.

    What the existing card path shows for a plan turn while the wall
    page cannot render the plan kind yet: the activity's span and
    the rain band, composed from the same payload the flush ships —
    never a different claim than the infographic makes."""
    blocks = plan.get("blocks") or []
    lines: list[str] = []
    activity = next((b for b in blocks if b.get("kind") == "activity"), None)
    rain = next((b for b in blocks if b.get("kind") == "rain"), None)
    if activity is not None:
        lines.append(_truncate(
            f"{activity['label']} · {activity['start']}–{activity['end']}"))
    if rain is not None:
        lines.append(_truncate(
            f"Lluvia · {rain['start']}–{rain['end']}"))
    elif activity is not None:
        lines.append("Sin lluvia")
    if not lines and blocks:
        first = blocks[0]
        lines.append(_truncate(
            f"{first['label']} · {first['start']}–{first['end']}"))
    return plan.get("title", ""), lines[:2]
