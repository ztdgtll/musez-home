"""Run the four canned turns through the bridge loop, model-free.

    python3 -m bridge.mock_demo

Proves the loop end to end — streaming, sentence chunking, tool dispatch,
stage timing — with the fake source. The transcript lines below stand in for
local STT output.
"""

from __future__ import annotations

from .mock_source import TURNS
from .tools import DryRoom
from .turn import PrintSink, run_turn

TRANSCRIPTS = {
    "knowledge": "¿qué es una Raspberry Pi?",
    "weather": "¿qué clima hace?",
    "wall": "poné el clima en la pared",
    "plan": "planificá mi sábado",
}


def main() -> None:
    for name, factory in TURNS.items():
        print(f"TURN [{name}] transcript: {TRANSCRIPTS[name]!r}")
        room = DryRoom()
        sink = PrintSink()
        marks = run_turn(factory(), room, sink)
        tools = ", ".join(tc["name"] for tc in marks["tool_calls"]) or "none"
        print(f"  tools: {tools} | first sentence at "
              f"{marks.get('t_first_sentence', 0):.0f} ms | "
              f"full {marks['t_full']:.0f} ms\n")


if __name__ == "__main__":
    main()
