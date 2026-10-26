"""Incremental sentence chunker for streamed model output.

The bridge speaks the first complete sentence as soon as it exists instead of
waiting for the full reply. This module receives streamed text deltas and
emits complete sentences as they close.

Deliberately simple and predictable: a sentence closes on terminal
punctuation (. ! ? …) followed by whitespace or end-of-stream. A small
abbreviation guard avoids splitting on common cases like "Dr." / "Mr." /
"e.g." — good enough for spoken replies, and every rule here is testable.

One exception, for latency: while the FIRST chunk of a turn is still an
unterminated sentence and the buffer grows past ~90 characters, the chunker
releases at the last strong clause boundary (comma, semicolon, em dash) so
the voice can start on a natural pause instead of waiting for a long opener
to close. The cut never lands mid-word (boundaries sit on whitespace) and
never inside a decimal (26.5 / 26,5) or a thousands group (1,500): those
commas are followed by a digit, not a space. Every later chunk keeps pure
sentence semantics.
"""

from __future__ import annotations

_TERMINAL = ".!?…"
_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "sr", "sra", "ej", "e.g", "i.e", "etc", "vs",
    "u.s", "u.s.a",
}

# First-chunk clause release: character budget for an unterminated opener,
# the earliest position a clause cut may release at, and the boundaries it
# may cut on — each is (separator, characters of it kept with the chunk).
_FIRST_CHUNK_LIMIT = 90
_FIRST_CHUNK_MIN = 45
_CLAUSE_SEPARATORS = ((", ", 1), ("; ", 1), (" — ", 3))


class SentenceChunker:
    def __init__(self) -> None:
        self._buf = ""
        self._emitted = 0

    def feed(self, delta: str) -> list[str]:
        """Add a streamed delta; return any sentences completed by it."""
        self._buf += delta
        out: list[str] = []
        while True:
            idx = self._sentence_end(self._buf)
            if idx is not None:
                sentence = self._buf[: idx + 1].strip()
                self._buf = self._buf[idx + 1 :].lstrip()
                if sentence:
                    out.append(sentence)
                    self._emitted += 1
                continue
            if self._emitted == 0:
                cut = self._clause_cut(self._buf)
                if cut is not None:
                    chunk = self._buf[:cut].strip()
                    self._buf = self._buf[cut:].lstrip()
                    if chunk:
                        out.append(chunk)
                        self._emitted += 1
                        continue
            break
        return out

    def flush(self) -> list[str]:
        """End of stream: whatever remains is the final sentence."""
        rest = self._buf.strip()
        self._buf = ""
        return [rest] if rest else []

    @staticmethod
    def _clause_cut(text: str) -> int | None:
        """Cut index for an oversized unterminated first chunk, or None.

        Fires only once the buffer passes the character budget; the cut is
        the last strong clause boundary at or past the minimum position,
        returned as the index just past the boundary's punctuation.
        """
        if len(text) <= _FIRST_CHUNK_LIMIT:
            return None
        best: int | None = None
        for sep, keep in _CLAUSE_SEPARATORS:
            idx = text.rfind(sep, _FIRST_CHUNK_MIN)
            if idx != -1 and (best is None or idx + keep > best):
                best = idx + keep
        return best

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
