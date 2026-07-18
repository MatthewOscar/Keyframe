from __future__ import annotations

import unittest

from slugify import slugify_title


class SlugifyTitleTests(unittest.TestCase):
    def test_lowercases_and_joins_words(self) -> None:
        self.assertEqual(slugify_title("Hello Keyframe"), "hello-keyframe")

    def test_collapses_mixed_separators(self) -> None:
        self.assertEqual(slugify_title("  Video:  Said + Shown  "), "video-said-shown")

    def test_preserves_ascii_digits(self) -> None:
        self.assertEqual(slugify_title("GPT 5.6 Demo"), "gpt-5-6-demo")

    def test_empty_input_stays_empty(self) -> None:
        self.assertEqual(slugify_title("---"), "")


if __name__ == "__main__":
    unittest.main()
