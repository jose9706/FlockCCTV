import unittest

from flock_cctv.evil import evil_messages, upside_down


class EvilTextTests(unittest.TestCase):
    def test_flips_letters_and_punctuation(self):
        self.assertEqual(upside_down("abc!?"), "¿¡ɔqɐ")
        self.assertEqual(upside_down("Hello, world!"), "¡pןɹoʍ 'oןןǝH")

    def test_keeps_emoji_clusters_together_and_preserves_line_order(self):
        self.assertEqual(upside_down("hi 👨‍👩‍👧\nbye"), "👨‍👩‍👧 ᴉɥ\nǝʎq")

    def test_chunks_at_discord_limit_and_skips_empty_text(self):
        self.assertEqual(evil_messages(" "), [])
        chunks = evil_messages("a" * 4000)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 1900 for chunk in chunks))
        self.assertEqual(sum(len(chunk) - 2 for chunk in chunks), 4000)
