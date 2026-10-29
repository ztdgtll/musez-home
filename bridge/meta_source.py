"""Live model source: the Meta Model API, behind the bridge event protocol.

Speaks the same events as `mock_source` —

  ("token", text_delta)        streamed reply text
  ("tool_call", name, args)    a room tool the model decided to call
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
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API_URL = "https://api.meta.ai/v1/chat/completions"
FAST_MODEL = "muse-spark-1.3"
DEEP_MODEL = "muse-wonder-1.0"
MAX_ROUNDS = 5
TIMEOUT_S = 90

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
    "(scene titles always in English) and get_weather for weather. When a "
    "request needs planning, weighing options, or several steps of "
    "reasoning, call escalate BEFORE answering: a deeper model will take "
    "the turn from there. "
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
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
}


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

    def _post_stream(self, model: str, messages: list, tools: list):
        body = {"model": model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
        resp = self._transport(API_URL, body, {})
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
                delta = obj["choices"][0]["delta"]
            except (ValueError, KeyError, IndexError, TypeError):
                continue
            content = delta.get("content")
            if content:
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
        yield ("_round_end", {"text": "".join(text_parts),
                              "calls": [calls[k] for k in sorted(calls)]})

    # -- one conversational turn ----------------------------------------

    def stream_turn(self, user_text: str, room_tools: list):
        """Yield bridge events for one user turn; read tool results back.

        `room_tools` are the OpenAI-style schemas the room exposes (see
        tools.TOOL_SCHEMAS). Tool results arrive via send(); the generator
        feeds them to the model and streams the continuation.
        """
        from .tools import TOOL_SCHEMAS  # default when caller passes None
        tools = room_tools if room_tools is not None else TOOL_SCHEMAS
        model = self.fast_model
        escalated = False
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_text},
        ]
        rounds = 0
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
                    if event[0] != "_round_end":
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
                    reason = json.loads(escalation["args"] or "{}").get(
                        "reason", "")
                except ValueError:
                    reason = ""
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
        self.last_turn = {
            "model_final": model,
            "escalated": escalated,
            "rounds": rounds,
        }
