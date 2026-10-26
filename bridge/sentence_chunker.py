"""Incremental sentence chunker for streamed model output.

The bridge speaks the first complete sentence as soon as it exists instead of
waiting for the full reply. This module receives streamed text deltas and
emits complete sentences as they close.

Deliberately simple and predictable: a sentence closes on terminal
punctuation (. ! ? …) followed by whitespace or end-of-stream. A small
abbreviation guard avoids splitting on common cases like "Dr." / "Mr." /
"e.g." — good enough for spoken replies, and every rule here is testable.
"""

from __future__ import annotations

_TERMINAL = ".!?…"
_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "sr", "sra", "ej", "e.g", "i.e", "etc", "vs",
    "u.s", "u.s.a",
}


class SentenceChunker:
    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        """Add a streamed delta; return any sentences completed by it."""
        self._buf += delta
        out: list[str] = []
        while True:
            idx = self._sentence_end(self._buf)
            if idx is None:
                break
            sentence = self._buf[: idx + 1].strip()
            self._buf = self._buf[idx + 1 :].lstrip()
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> list[str]:
        """End of stream: whatever remains is the final sentence."""
        rest = self._buf.strip()
        self._buf = ""
        return [rest] if rest else []

    @staticmethod
    def _sentence_end(text: str) -> int | None:
        for i, ch in enumerate(text):
            if ch not in _TERMINAL:
                continue
            # Need whitespace or end after the punctuation to close.
            if i + 1 < len(text) and not text[i + 1].isspace():
                continue
            if ch == ".":
                # Abbreviation guard: word before the dot is a known abbrev.
                start = i - 1
                while start >= 0 and (text[start].isalpha() or text[start] == "."):
                    start -= 1
                word = text[start + 1 : i].lower()
                if word in _ABBREVIATIONS:
                    continue
                # Decimal numbers (e.g. 3.5) never reach here: the next char
                # would be a digit, not whitespace — handled above.
            return i
        return None


if __name__ == "__main__":
    c = SentenceChunker()
    got: list[str] = []
    for delta in ["Hola, soy ", "Muse. El clima hoy ", "es soleado con 26.5 grados. ",
                  "Dr. Psathakis, ¿salimos", "? Sí, en 5 min."]:
        got.extend(c.feed(delta))
    got.extend(c.flush())
    expected = [
        "Hola, soy Muse.",
        "El clima hoy es soleado con 26.5 grados.",
        "Dr. Psathakis, ¿salimos?",
        "Sí, en 5 min.",
    ]
    assert got == expected, f"chunker mismatch:\n got={got}\n exp={expected}"
    print("sentence_chunker self-test OK:", len(got), "sentences")
