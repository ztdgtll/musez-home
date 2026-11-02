"""Live model source: the Meta Model API, behind the bridge event protocol.

Speaks the same events as `mock_source` —

  ("token", text_delta)        streamed reply text
  ("tool_call", name, args)    a room tool the model decided to call
  ("ack", text)                the escalation acknowledgement: one short
                               sentence the bridge speaks immediately
                               while the deep model works (see below)
  ("note", text)               bridge-level events (e.g. an escalation)

— plus one extension: after a `tool_call` event, the bridge sends the tool's
result back into the generator (`result = yield event`), and this source
feeds it to the model for the next round. Plain token turns never read the
sent value, so sources stay interchangeable.

Routing (the design this project measures): every turn starts on the fast
model (Muse Spark). The fast model can hand the turn to the deep model
(Muse Wonder) by calling the internal `escalate` tool before answering;
the hand-off is invisible to the room — the same turn continues, deeper.
Room tools never include `escalate`; the room never sees it.

Transport is injectable for tests: `transport(url, body, headers)` must
return an iterable of raw SSE lines (bytes or str) or raise MetaAPIError.
The default transport is stdlib urllib with a Bearer key taken from the
constructor or the META_API_KEY environment variable. The key is never
printed or logged.

The API switch (bridge/brain_switch.py) gates this source: with the
switch off, `stream_turn` raises ApiSwitchOff before the first
transport call — no paid call happens at all and the classic path
owns the room.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request

from .brain_switch import ApiSwitchOff, require_bridge

API_URL = "https://api.meta.ai/v1/chat/completions"
FAST_MODEL = "muse-spark-1.3"
DEEP_MODEL = "muse-wonder-1.0"
MAX_ROUNDS = 5
TIMEOUT_S = 90

# Explicit per-model request shaping. The latency budget is written
# against the fast model running at minimal reasoning effort; these
# constants make that a fact of the request instead of an assumption,
# and the tests pin them so the budget and the client cannot drift
# apart silently.
SPARK_REASONING_EFFORT = "minimal"
# Spoken replies are one or two short sentences (~100 tokens under the
# production brevity prompt); this cap is ~10x that — a bound on a
# runaway turn, not a shaping of the answer.
SPARK_MAX_TOKENS = 1024
# The deep model spends a large, variable share of its output budget
# on hidden reasoning (observed 10–27K reasoning tokens on the
# 2026-10-29 review packages; calls capped at 16000 returned empty
# content). Its cap stays generous for the same reason: it bounds a
# pathological turn, it does not shape short spoken answers.
WONDER_MAX_TOKENS = 32768

# The bridge starts speaking at the first complete unit of text, so the
# opener's length is on the latency path: ask for a brief first sentence
# and the voice starts sooner. The clause-level release in the chunker is
# the backstop when an opener still runs long.
SHORT_OPENER_GUIDANCE = (
    "Open with a short first sentence — one brief clause — then "
    "continue normally."
)

# Spoken text leaves the room exactly once per round, INCLUDING text
# produced in a round that also calls tools — it is streamed, not
# held for the final answer. Field evidence (live rehearsal
# 2026-11-01): the deeper model, its ack already spoken by the room,
# opened by re-speaking the ack in extended form and narrating its
# process mid-round ('Es domingo 1, así que planifico el sábado 7 y
# reviso su lluvia por hora.'), then closed twice — a 'Listo.' line
# AND a full summary — so the room heard 'estoy en eso' three times
# over. The guidance: tool rounds stay silent, an escalated reply
# opens with the result (the ack already covered the wait), and the
# answer ends exactly once.
SPOKEN_ROUNDS_GUIDANCE = (
    "Any text you produce is spoken aloud immediately — even in a "
    "round where you also call tools. Never narrate your process "
    "('I will check…', 'I am planning…'): in tool-calling rounds "
    "produce no spoken text at all, and let the reply itself carry "
    "the result. When you escalate, the ack you pass is spoken for "
    "you at hand-off: the deeper reply must open with the result — "
    "never repeat, rephrase or extend the acknowledgement — and "
    "close with one short closing sentence, not a closing line "
    "followed by a summary that restates it."
)

# A turn's language follows the REQUEST in that turn — never the
# language the room's standing context happens to be written in.
# Field evidence (live session 2026-11-01): an English request got
# a Spanish answer, because the presence block the runner appends
# is written entirely in Spanish and, as standing context, outweighs
# a bare request. The guidance below is the standing rule; the
# per-turn pin (turn_language_directive, inserted by stream_turn
# from detect_language's deterministic verdict) is what makes the
# rule land on THIS turn: spoken sentences, the ack, wall titles
# and lines, plan labels and card content all follow it. Spanish
# stays the room's default — detection is conservative on purpose,
# and a Spanish turn changes nothing anywhere downstream.
TURN_LANGUAGE_GUIDANCE = (
    "The language of each turn follows the user's request in that "
    "turn, whatever language any standing context, memory or "
    "presence text is written in: if the request is in English, "
    "the ENTIRE turn is in English — spoken replies, the "
    "acknowledgement, wall titles and lines, plan labels and card "
    "content; if it is in Spanish, everything is in Spanish."
)

# Function-word evidence for detect_language. Deliberately free of
# words both languages share (a, no…) and of content words: only
# short, frequent function/discourse words vote, so a borrowed
# noun can never flip a turn.
_EN_FUNCTION_WORDS = frozenset("""
    the be is are was were will would can could should shall may might
    what whats how when where who why it its this that these those
    my your his her our their me you we they he she him them us
    and or but if then than on in at to for with without about
    do does did not dont im youre lets
    put please thank thanks hey hello off up down out over under
    want need like just now today tomorrow
""".split())
_ES_FUNCTION_WORDS = frozenset("""
    el la los las un una unos unas de del en y o pero si cuando donde
    como que cual cuales quien es son esta estan estaba va van hay
    hace para por con sin sobre mi mis tu tus su sus me te se nos
    lo le les este esta estos estas ese esa eso esto muy mas ya
    hoy manana pon pone
""".split())


def detect_language(text) -> str:
    """The turn's language: "en" or "es", decided deterministically
    from the transcript's function words — never by the model.

    Conservative by design (the room's default is Spanish): English
    must WIN, not merely appear — at least two English function
    words and strictly more than the Spanish count. Inverted marks
    (¿ ¡), ñ and accented vowels are strong Spanish evidence and
    each outvotes an English word. Short or ambiguous turns, empty
    text and non-text input all land on Spanish, so a Spanish turn
    is never reclassified and its downstream dispatches stay
    byte-identical to before this rule existed."""
    if not isinstance(text, str) or not text.strip():
        return "es"
    es_score = 0
    # Strong orthographic evidence, read off the RAW text before
    # normalization strips it: these marks exist only in Spanish.
    es_score += 3 * sum(text.count(c) for c in "¿¡ñ")
    es_score += sum(1 for c in text if c in "áéíóúÁÉÍÓÚ")
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    en_score = 0
    for token in re.findall(r"[a-z]+", folded):
        if token in _EN_FUNCTION_WORDS:
            en_score += 1
        elif token in _ES_FUNCTION_WORDS:
            es_score += 1
    if en_score >= 2 and en_score > es_score:
        return "en"
    return "es"


def resolve_turn_lang(text, hint=None) -> str:
    """The turn's language when the room may already KNOW it.

    A valid hint ("en"/"es") wins over the detector: the studio
    listener transcribes with the language of its STT switch, so a
    queue entry that carries that language is ground truth the
    transcript alone cannot beat — a short English line ("One more
    thing. Anything I should know.") carries too few function words
    for the detector and was pinned to Spanish live on 2026-11-01.
    Anything else (no hint, an unrecognized value) falls back to
    detect_language, so turns that arrive without a hint — the
    classic path, older queue entries — behave exactly as before."""
    if hint in ("en", "es"):
        return hint
    return detect_language(text)


def turn_language_directive(lang: str) -> str:
    """The per-turn system pin stream_turn inserts ahead of the
    user's message: the detected language, stated as a rule for
    every surface the turn produces (see TURN_LANGUAGE_GUIDANCE)."""
    name = "English" if lang == "en" else "Spanish"
    return (
        f"Turn language: {name}. The user's request in this turn "
        f"is in {name}: the ENTIRE turn is in {name} — every "
        "spoken sentence, the acknowledgement, wall titles and "
        "lines, plan labels and card content — regardless of the "
        "language of any standing context, memory or presence "
        "text, and regardless of earlier turns."
    )


SYSTEM_PROMPT = (
    "You are MuseZ, the voice of a smart room. You answer out loud, so "
    "keep every spoken reply to one or two short, plain sentences: no "
    "markdown, no lists, no spelling out URLs. Answer in the same language "
    "the user speaks. Use set_wall_scene to show things on the room's wall "
    "(scene titles always in English), get_weather for weather, and "
    "set_day_plan to lay out a day plan as a timeline on the wall. When a "
    "request needs planning, weighing options, or several steps of "
    "reasoning, call escalate BEFORE answering: a deeper model will take "
    "the turn from there. When you escalate, pass an ack: one short "
    "sentence in the user's language that the room speaks immediately "
    "while the deeper model works (for example, 'Dame un momento, armo "
    "tu sábado.'). Use clean_studio to start the robot vacuum "
    "cleaning the studio. "
    + SHORT_OPENER_GUIDANCE + " "
    + SPOKEN_ROUNDS_GUIDANCE + " "
    + TURN_LANGUAGE_GUIDANCE
)

def compose_system_prompt(presence: str | None = None) -> str:
    """The system prompt a live source speaks with: the bare
    SYSTEM_PROMPT, or SYSTEM_PROMPT + the room's presence block.

    The presence file — the user's distilled memory plus the
    assistant's persona extract, approved by Javier on 2026-10-31 —
    lives ONLY on the studio Pi and is read at runtime by the live
    runner (bridge/live_turn.py); it is never part of this
    repository and never committed. Absent or empty presence is not
    an error: the bridge then speaks with the bare prompt, exactly
    as every measured probe did, and the turn record says which
    happened (presence_loaded) instead of leaving it implicit."""
    if not presence or not presence.strip():
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + "\n\n" + presence.strip()


ESCALATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "escalate",
        "description": (
            "Hand this turn to the deeper reasoning model (planning, "
            "multi-constraint requests, comparisons, anything uncertain). "
            "Call before producing any answer text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "ack": {
                    "type": "string",
                    "description": (
                        "One short sentence, in the user's language, "
                        "that the room speaks aloud immediately while "
                        "the deeper model works — the user hears it "
                        "instead of silence. At most 60 characters, a "
                        "single sentence."
                    ),
                },
            },
            "required": ["reason"],
        },
    },
}

# The escalation acknowledgement (plan item 19). An escalated turn's
# first deep-model sentence lands ~8 s after the hand-off (measured,
# experiments/results/latency-live-prod-2026-10-29.md); until then the
# room hears nothing, because Spark's buffered draft is discarded by
# design. The ack covers that wait: Spark words it in the escalate
# call (it knows the topic), the bridge validates it here, and the
# turn loop speaks it at once through the room transport — a topic
# text is never hard-coded in the bridge. A missing or malformed ack
# degrades to the one fixed generic line, never to silence.
ACK_FALLBACK = "Dame un momento."
# The fallback speaks the turn's language like everything else in
# the turn (see TURN_LANGUAGE_GUIDANCE): an English turn whose
# Spark passed no usable ack hears this, never the Spanish line.
ACK_FALLBACK_EN = "One moment."
ACK_MAX_CHARS = 60


def validate_ack(text, lang: str = "es") -> str:
    """Spark's ack when it is one short sentence, else the fallback.

    Whitespace is collapsed first (an ack is one spoken line), then
    the line must be non-empty, within the character cap, and a
    single sentence: at most one sentence-ending mark, at the end.
    The fallback is ACK_FALLBACK, or ACK_FALLBACK_EN on an English
    turn (`lang` is detect_language's verdict for the turn).
    """
    fallback = ACK_FALLBACK_EN if lang == "en" else ACK_FALLBACK
    if not isinstance(text, str):
        return fallback
    text = " ".join(text.split())
    if not text or len(text) > ACK_MAX_CHARS:
        return fallback
    endings = sum(text.count(c) for c in ".!?…")
    if endings > 1 or (endings == 1
                       and not text.endswith((".", "!", "?", "…"))):
        return fallback
    return text


class MetaAPIError(RuntimeError):
    """A failed API call: `status` is the HTTP code (0 = network level)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Meta Model API error (HTTP {status}): {message}")
        self.status = status


def _default_transport(api_key: str):
    def transport(url: str, body: dict, headers: dict):
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **headers,
        })
        last_error: Exception | None = None
        for _attempt in range(2):  # one retry, and only before bytes flow
            try:
                return urllib.request.urlopen(req, timeout=TIMEOUT_S)
            except urllib.error.HTTPError as exc:
                snippet = exc.read().decode("utf-8", "replace")[:200]
                raise MetaAPIError(exc.code, snippet) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
        raise MetaAPIError(0, f"network failure: {last_error}")

    return transport


class MetaSource:
    """One conversation with the Meta Model API, streamed turn by turn."""

    def __init__(self, api_key: str | None = None, transport=None,
                 fast_model: str = FAST_MODEL, deep_model: str = DEEP_MODEL,
                 system_prompt: str = SYSTEM_PROMPT) -> None:
        if transport is None:
            key = api_key or os.environ.get("META_API_KEY", "")
            if not key:
                raise MetaAPIError(
                    0, "no API key: pass api_key or set META_API_KEY")
            transport = _default_transport(key)
        self._transport = transport
        self.fast_model = fast_model
        self.deep_model = deep_model
        self.system_prompt = system_prompt
        self.last_turn: dict = {}

    # -- stream plumbing -------------------------------------------------

    def _request_params(self, model: str) -> dict:
        """Explicit per-model request shaping (the constants above).

        Keyed on the model, not on the round's role, so a deep-model
        round gets the deep model's parameters wherever it runs —
        first, or after an escalation."""
        if model == self.deep_model:
            return {"max_tokens": WONDER_MAX_TOKENS}
        return {"reasoning_effort": SPARK_REASONING_EFFORT,
                "max_tokens": SPARK_MAX_TOKENS}

    def _post_stream(self, model: str, messages: list, tools: list):
        body = {"model": model, "messages": messages, "stream": True,
                "stream_options": {"include_usage": True},
                **self._request_params(model)}
        if tools:
            body["tools"] = tools
        resp = self._transport(API_URL, body, {})
        t0 = time.perf_counter()
        ttft_ms = None
        usage = None
        text_parts: list[str] = []
        calls: dict[int, dict] = {}
        for raw in resp:
            line = (raw.decode("utf-8", "replace")
                    if isinstance(raw, bytes) else raw).strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            # Usage rides its own final chunk, whose choices are empty
            # (or absent): read it before the delta parse so a
            # usage-only chunk is never mistaken for a parse failure,
            # and keep the last usage seen — it totals the request.
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            choices = obj.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            first = choices[0]
            delta = first.get("delta") if isinstance(first, dict) else None
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if content:
                if ttft_ms is None:
                    ttft_ms = (time.perf_counter() - t0) * 1000.0
                text_parts.append(content)
                yield ("token", content)
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0),
                                        {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
        yield ("_round_end", {
            "text": "".join(text_parts),
            "calls": [calls[k] for k in sorted(calls)],
            "usage": usage,
            "ttft_ms": ttft_ms,
            "duration_ms": (time.perf_counter() - t0) * 1000.0,
        })

    # -- one conversational turn ----------------------------------------

    def stream_turn(self, user_text: str, room_tools: list,
                    lang: str = None):
        """Yield bridge events for one user turn; read tool results back.

        `room_tools` are the OpenAI-style schemas the room exposes (see
        tools.TOOL_SCHEMAS). Tool results arrive via send(); the generator
        feeds them to the model and streams the continuation.

        The API switch is checked first, on the generator's first
        advance — before any transport call: with the switch off,
        ApiSwitchOff propagates and no paid call is ever made.
        """
        require_bridge()
        from .tools import TOOL_SCHEMAS  # default when caller passes None
        tools = room_tools if room_tools is not None else TOOL_SCHEMAS
        model = self.fast_model
        escalated = False
        # The turn's language is decided HERE, deterministically,
        # and pinned as its own system message directly under the
        # standing prompt: the presence block rides inside that
        # prompt (Spanish, by design) and outweighs a bare request
        # without this pin — an English request was answered in
        # Spanish in the live room (2026-11-01). The same verdict
        # drives the voice's synthesis language (bridge/studio_sink
        # via bridge/live_turn). A caller that already knows the
        # language (the listener's STT switch, via the queue entry)
        # passes it as `lang` and it wins — see resolve_turn_lang.
        turn_lang = resolve_turn_lang(user_text, lang)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "system",
             "content": turn_language_directive(turn_lang)},
            {"role": "user", "content": user_text},
        ]
        rounds = 0
        round_marks: list[dict] = []

        def record_round(round_model: str, rend) -> None:
            """One round's measurement mark for last_turn: model,
            stage times, tool names and the API's own token usage —
            the raw material every published latency number and cost
            figure is computed from."""
            if rend is None:
                return
            usage = rend.get("usage") or {}
            round_marks.append({
                "model": round_model,
                "ttft_ms": rend.get("ttft_ms"),
                "duration_ms": rend.get("duration_ms"),
                "tool_names": [c["name"] for c in rend.get("calls", [])],
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            })

        while True:
            # Round budget checked at the top of EVERY iteration, so no
            # branch (escalation included) can loop past it: at the cap
            # the model answers with what it has, without tools.
            if rounds >= MAX_ROUNDS:
                messages.append({
                    "role": "system",
                    "content": "Tool budget reached. Answer now, briefly, "
                               "with what you have.",
                })
                gen = self._post_stream(model, messages, [])
                for event in gen:
                    if event[0] == "_round_end":
                        record_round(model, event[1])
                    else:
                        yield event
                break
            rounds += 1
            offered = list(tools)
            escalate_offered = model == self.fast_model and not escalated
            if escalate_offered:
                offered = offered + [ESCALATE_SCHEMA]
            # While `escalate` is on the table, the round's text is
            # buffered, not streamed: if the round ends in an escalation,
            # Spark's draft is discarded and the room hears only the deep
            # model's answer — never two contradictory replies.
            buffered: list = []
            round_end = None
            gen = self._post_stream(model, messages, offered)
            for event in gen:
                if event[0] == "_round_end":
                    round_end = event[1]
                elif escalate_offered:
                    buffered.append(event)
                else:
                    yield event
            record_round(model, round_end)
            calls = (round_end or {}).get("calls", [])
            # One normalized id per call, reused verbatim in the
            # assistant message and in every tool response: the API
            # rejects a follow-up request whose tool calls and tool
            # results do not pair up by id.
            for i, c in enumerate(calls):
                c["tool_id"] = c["id"] or f"call_{rounds}_{i}"
            # split room calls from an escalation request
            room_calls, escalation = [], None
            for call in calls:
                if call["name"] == "escalate":
                    escalation = call
                else:
                    room_calls.append(call)
            if escalation is not None:
                # Escalation wins. Room calls from the same round are
                # NOT dispatched (the deep model re-decides from a clean
                # slate), but each still gets its tool response below —
                # a call is never left unanswered in the history.
                assistant_msg = {
                    "role": "assistant",
                    "content": (round_end or {}).get("text") or None,
                    "tool_calls": [
                        {"id": c["tool_id"], "type": "function",
                         "function": {"name": c["name"],
                                      "arguments": c["args"] or "{}"}}
                        for c in calls
                    ],
                }
                messages.append(assistant_msg)
                try:
                    esc_args = json.loads(escalation["args"] or "{}")
                    if not isinstance(esc_args, dict):
                        esc_args = {}
                except ValueError:
                    esc_args = {}
                reason = esc_args.get("reason", "")
                ack = validate_ack(esc_args.get("ack"), lang=turn_lang)
                messages.append({
                    "role": "tool", "tool_call_id": escalation["tool_id"],
                    "content": json.dumps(
                        {"ok": True, "note": "deeper model has the turn"}),
                })
                for call in room_calls:
                    messages.append({
                        "role": "tool", "tool_call_id": call["tool_id"],
                        "content": json.dumps({
                            "ok": False, "deferred": True,
                            "error": "not run: the turn escalated to the "
                                     "deeper model first; re-issue the "
                                     "call if it is still needed",
                        }),
                    })
                model = self.deep_model
                escalated = True
                # The ack goes out BEFORE the deep model's first
                # round is even requested: the loop speaks it at once
                # and it is the only sound the room makes until
                # Wonder's first sentence. Exactly one per turn —
                # `escalate` is never offered again after this.
                yield ("ack", ack)
                yield ("note", f"escalated to {self.deep_model}: {reason}")
                continue
            # No escalation: release the round's buffered text, in order.
            for event in buffered:
                yield event
            if not calls:
                break
            assistant_msg = {
                "role": "assistant",
                "content": (round_end or {}).get("text") or None,
                "tool_calls": [
                    {"id": c["tool_id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": c["args"] or "{}"}}
                    for c in calls
                ],
            }
            messages.append(assistant_msg)
            for call in room_calls:
                args = None
                try:
                    parsed = json.loads(call["args"] or "{}")
                    if isinstance(parsed, dict):
                        args = parsed
                except ValueError:
                    args = None
                if args is None:
                    # Malformed arguments are never dispatched as {}:
                    # the model gets an error result it can correct.
                    yield ("note", f"bad tool arguments for {call['name']}")
                    result = {
                        "ok": False,
                        "error": "tool arguments were not a valid JSON "
                                 "object; re-issue the call with valid "
                                 "JSON arguments",
                    }
                else:
                    result = yield ("tool_call", call["name"], args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["tool_id"],
                    "content": json.dumps(result, ensure_ascii=False,
                                          default=str),
                })
        def _token_total(key: str):
            vals = [m[key] for m in round_marks if m.get(key) is not None]
            return sum(vals) if vals else None

        self.last_turn = {
            "model_final": model,
            "escalated": escalated,
            "turn_lang": turn_lang,
            "rounds": rounds,
            "round_marks": round_marks,
            "prompt_tokens": _token_total("prompt_tokens"),
            "completion_tokens": _token_total("completion_tokens"),
        }
