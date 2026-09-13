import unittest

from solution import slugify


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_extra_whitespace(self):
        self.assertEqual(slugify("  Multiple   Spaces  "), "multiple-spaces")

    def test_already_slugged(self):
        self.assertEqual(slugify("already-slugged"), "already-slugged")

    def test_punctuation(self):
        self.assertEqual(slugify("Punctuation! & Symbols?"), "punctuation-symbols")

    def test_empty(self):
        self.assertEqual(slugify(""), "")

    def test_all_punctuation(self):
        self.assertEqual(slugify("!!!???"), "")

    def test_trailing_hyphens(self):
        self.assertEqual(slugify("Trailing---Hyphens--"), "trailing-hyphens")

    def test_digits(self):
        self.assertEqual(slugify("Version 2 Release"), "version-2-release")


if __name__ == "__main__":
    unittest.main()
