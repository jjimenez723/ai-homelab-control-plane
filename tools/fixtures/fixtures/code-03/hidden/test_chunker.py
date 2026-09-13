import unittest

from solution import chunk


class TestChunker(unittest.TestCase):
    def test_even_split(self):
        self.assertEqual(chunk([1, 2, 3, 4], 2), [[1, 2], [3, 4]])

    def test_trailing_partial_chunk(self):
        self.assertEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_size_equals_length(self):
        self.assertEqual(chunk([1, 2, 3], 3), [[1, 2, 3]])

    def test_size_one(self):
        self.assertEqual(chunk([1, 2, 3], 1), [[1], [2], [3]])

    def test_empty_input(self):
        self.assertEqual(chunk([], 3), [])

    def test_no_items_dropped(self):
        items = list(range(17))
        chunks = chunk(items, 5)
        self.assertEqual([x for group in chunks for x in group], items)
        self.assertTrue(all(len(group) <= 5 for group in chunks))


if __name__ == "__main__":
    unittest.main()
