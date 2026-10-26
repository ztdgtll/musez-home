"""Offline tests for the sentence chunker — pure text, no network.

Pins the chunker's contract: sentences close on terminal punctuation, and
the FIRST chunk of a turn may be released early at a strong clause boundary
once the unterminated opener grows past the character budget, so the voice
can start sooner. Later chunks never get clause-cut.

Deltas are fed word by word, the way a token stream arrives.

Run from the repo root:  python3 -m unittest bridge.test_sentence_chunker -v
"""

from __future__ import annotations

import unittest

from .sentence_chunker import SentenceChunker


def feed_words(chunker: SentenceChunker, text: str) -> list[str]:
    """Feed `text` one word per delta; return everything emitted."""
    out: list[str] = []
    words = text.split(" ")
    for i, word in enumerate(words):
        delta = word if i == len(words) - 1 else word + " "
        out.extend(chunker.feed(delta))
    return out


def run_text(text: str) -> list[str]:
    chunker = SentenceChunker()
    out = feed_words(chunker, text)
    out.extend(chunker.flush())
    return out


class SentenceSemanticsTests(unittest.TestCase):
    def test_sentences_close_on_terminal_punctuation(self):
        chunker = SentenceChunker()
        got: list[str] = []
        for delta in ["Hola, soy ", "Muse. El clima hoy ",
                      "es soleado con 26.5 grados. ",
                      "Dr. Psathakis, ¿salimos", "? Sí, en 5 min."]:
            got.extend(chunker.feed(delta))
        got.extend(chunker.flush())
        self.assertEqual(got, [
            "Hola, soy Muse.",
            "El clima hoy es soleado con 26.5 grados.",
            "Dr. Psathakis, ¿salimos?",
            "Sí, en 5 min.",
        ])


class FirstChunkClauseCutTests(unittest.TestCase):
    def test_long_opener_is_clause_cut_english(self):
        text = ("For this weekend in Tamarac, the forecast looks warm and "
                "mostly sunny, with highs around twenty-nine degrees, and "
                "no rain expected until late Sunday evening.")
        chunker = SentenceChunker()
        head, _, last = text.rpartition(" ")
        early = feed_words(chunker, head)
        # The first chunk leaves before the sentence ever closes: cut at a
        # comma, well short of the full opener.
        self.assertEqual(early, [
            "For this weekend in Tamarac, the forecast looks warm and "
            "mostly sunny,"])
        self.assertTrue(early[0].endswith(","))
        self.assertLess(len(early[0]), len(text))
        rest = chunker.feed(" " + last) + chunker.flush()
        self.assertEqual(rest, [
            "with highs around twenty-nine degrees, and no rain expected "
            "until late Sunday evening."])
        self.assertEqual(" ".join(early + rest), text)

    def test_long_opener_is_clause_cut_spanish(self):
        text = ("Para el sábado a la mañana, el plan es salir temprano con "
                "la moto, recorrer la costa con calma, y volver a casa "
                "antes de que llegue la lluvia de la tarde.")
        chunks = run_text(text)
        self.assertEqual(chunks, [
            "Para el sábado a la mañana, el plan es salir temprano con "
            "la moto, recorrer la costa con calma,",
            "y volver a casa antes de que llegue la lluvia de la tarde.",
        ])
        self.assertEqual(" ".join(chunks), text)

    def test_short_opener_passes_through_untouched(self):
        self.assertEqual(
            run_text("Listo, te lo puse en la pared."),
            ["Listo, te lo puse en la pared."])

    def test_clause_cut_never_applies_after_the_first_chunk(self):
        text = ("Sale el sol. Durante la tarde, sin embargo, el pronóstico "
                "indica tormentas fuertes, viento del este, y una baja "
                "marcada de la temperatura hacia la noche.")
        chunks = run_text(text)
        # The second sentence is long and full of commas, yet it is emitted
        # whole at its period: clause release is first-chunk only.
        self.assertEqual(chunks, [
            "Sale el sol.",
            "Durante la tarde, sin embargo, el pronóstico indica "
            "tormentas fuertes, viento del este, y una baja marcada de "
            "la temperatura hacia la noche.",
        ])

    def test_no_clause_boundary_means_no_cut(self):
        text = ("The projector paints a calm orange disc on the studio "
                "wall while the little computer thinks about the weather "
                "and the music for tonight.")
        # Past the budget but not one clause boundary: nothing is released
        # early — the chunker never cuts on length alone or mid-word.
        chunker = SentenceChunker()
        head, _, last = text.rpartition(" ")
        self.assertEqual(feed_words(chunker, head), [])
        self.assertEqual(chunker.feed(" " + last) + chunker.flush(), [text])


class NumberGuardTests(unittest.TestCase):
    def test_decimals_and_thousands_survive_the_clause_cut(self):
        text = ("The full setup costs 1,500 dollars in total, including "
                "the projector, the smart lights, and the little computer, "
                "and it runs at 26.5 watts on average.")
        chunks = run_text(text)
        self.assertEqual(chunks, [
            "The full setup costs 1,500 dollars in total, including the "
            "projector, the smart lights,",
            "and the little computer, and it runs at 26.5 watts on "
            "average.",
        ])
        # The comma inside 1,500 was never a boundary and 26.5 stays whole.
        self.assertIn("1,500", chunks[0])
        self.assertIn("26.5", chunks[1])
        self.assertFalse(any(c.endswith("1,") for c in chunks))
        self.assertEqual(" ".join(chunks), text)


if __name__ == "__main__":
    unittest.main()
