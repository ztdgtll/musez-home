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
import time
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
    + SHORT_OPENER_GUIDANCE
)

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
ACK_MAX_CHARS = 60


def validate_ack(text) -> str:
    """Spark's ack when it is one short sentence, else ACK_FALLBACK.

    Whitespace is collapsed first (an ack is one spoken line), then
    the line must be non-empty, within the character cap, and a
    single sentence: at most one sentence-ending mark, at the end.
    """
    if not isinstance(text, str):
        return ACK_FALLBACK
    text = " ".join(text.split())
    if not text or len(text) > ACK_MAX_CHARS:
        return ACK_FALLBACK
    endings = sum(text.count(c) for c in ".!?…")
    if endings > 1 or (endings == 1
                       and not text.endswith((".", "!", "?", "…"))):
        return ACK_FALLBACK
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

    def stream_turn(self, user_text: str, room_tools: list):
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
        messages = [
            {"role": "system", "content": self.system_prompt},
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
                ack = validate_ack(esc_args.get("ack"))
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
            "rounds": rounds,
            "round_marks": round_marks,
            "prompt_tokens": _token_total("prompt_tokens"),
            "completion_tokens": _token_total("completion_tokens"),
        }
