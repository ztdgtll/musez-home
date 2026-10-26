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

SYSTEM_PROMPT = (
    "You are MuseZ, the voice of a smart room. You answer out loud, so "
    "keep every spoken reply to one or two short, plain sentences: no "
    "markdown, no lists, no spelling out URLs. Answer in the same language "
    "the user speaks. Use set_wall_scene to show things on the room's wall "
    "(scene titles always in English) and get_weather for weather. When a "
    "request needs planning, weighing options, or several steps of "
    "reasoning, call escalate BEFORE answering: a deeper model will take "
    "the turn from there."
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
            rounds += 1
            offered = list(tools)
            if model == self.fast_model and not escalated:
                offered = offered + [ESCALATE_SCHEMA]
            round_end = None
            gen = self._post_stream(model, messages, offered)
            for event in gen:
                if event[0] == "_round_end":
                    round_end = event[1]
                else:
                    yield event
            calls = (round_end or {}).get("calls", [])
            if not calls:
                break
            # split room calls from an escalation request
            room_calls, escalation = [], None
            for call in calls:
                if call["name"] == "escalate":
                    escalation = call
                else:
                    room_calls.append(call)
            assistant_msg = {
                "role": "assistant",
                "content": (round_end or {}).get("text") or None,
                "tool_calls": [
                    {"id": c["id"] or f"call_{i}", "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"]}}
                    for i, c in enumerate(calls)
                ],
            }
            messages.append(assistant_msg)
            if escalation is not None and not room_calls:
                try:
                    reason = json.loads(escalation["args"] or "{}").get(
                        "reason", "")
                except ValueError:
                    reason = ""
                messages.append({
                    "role": "tool", "tool_call_id": escalation["id"],
                    "content": json.dumps(
                        {"ok": True, "note": "deeper model has the turn"}),
                })
                model = self.deep_model
                escalated = True
                yield ("note", f"escalated to {self.deep_model}: {reason}")
                continue
            for call in room_calls:
                try:
                    args = json.loads(call["args"] or "{}")
                except ValueError:
                    yield ("note", f"bad tool arguments for {call['name']}")
                    args = {}
                result = yield ("tool_call", call["name"], args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"] or call["name"],
                    "content": json.dumps(result, ensure_ascii=False,
                                          default=str),
                })
            if rounds >= MAX_ROUNDS:
                messages.append({
                    "role": "system",
                    "content": "Tool budget reached. Answer now, briefly, "
                               "with what you have.",
                })
                model_tools: list = []
                gen = self._post_stream(model, messages, model_tools)
                for event in gen:
                    if event[0] != "_round_end":
                        yield event
                break
        self.last_turn = {
            "model_final": model,
            "escalated": escalated,
            "rounds": rounds,
        }
