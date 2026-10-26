"""The bridge turn loop: transcript -> model stream -> speech + room actions.

One loop for every source of model events (mock today, live Muse Spark when
the API credit lands). The loop:

- feeds streamed tokens to the SentenceChunker,
- speaks each complete sentence the moment it closes (sink.say),
- routes tool calls to the room (tools.dispatch) and records their results,
- times every stage and returns the marks, probe-style.
"""

from __future__ import annotations

import time

from .sentence_chunker import SentenceChunker
from .tools import dispatch


class PrintSink:
    """Dev sink: prints sentences as they would be spoken."""

    def __init__(self) -> None:
        self.sentences: list[str] = []

    def say(self, sentence: str) -> None:
        self.sentences.append(sentence)
        print(f"  SPEAK: {sentence}")


def run_turn(events, room, sink) -> dict:
    chunker = SentenceChunker()
    t0 = time.perf_counter()
    marks: dict = {"tool_calls": []}

    def note(key: str) -> None:
        marks.setdefault(key, (time.perf_counter() - t0) * 1000.0)

    # The source is driven with send(): after a tool call, its result goes
    # back into the generator so a live source can feed it to the model and
    # stream the continuation. Sources that ignore sent values (the mock)
    # behave exactly as with plain iteration.
    iterator = iter(events)
    send_value = None
    started = False
    while True:
        try:
            if started and hasattr(iterator, "send"):
                event = iterator.send(send_value)
            else:
                event = next(iterator)
        except StopIteration:
            break
        started = True
        send_value = None
        kind = event[0]
        if kind == "token":
            note("t_first_token")
            for sentence in chunker.feed(event[1]):
                note("t_first_sentence")
                sink.say(sentence)
        elif kind == "tool_call":
            _, name, args = event
            result = dispatch(room, name, args)
            marks["tool_calls"].append({"name": name, "args": args, "result": result})
            note("t_first_tool_result")
            send_value = result
        elif kind == "note":
            marks.setdefault("notes", []).append(event[1])
        else:
            raise ValueError(f"unknown event: {event!r}")
    for sentence in chunker.flush():
        note("t_first_sentence")
        sink.say(sentence)
    marks["t_full"] = (time.perf_counter() - t0) * 1000.0
    return marks
