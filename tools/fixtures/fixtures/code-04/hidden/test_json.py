import json
import unittest

from solution import summarize_json


class TestSummarizeJson(unittest.TestCase):
    def test_returns_string(self):
        self.assertIsInstance(summarize_json([1, 2, 3]), str)

    def test_parses_to_expected_dict(self):
        parsed = json.loads(summarize_json([1, 2, 3]))
        self.assertEqual(parsed, {"count": 3, "sum": 6, "avg": 2.0})

    def test_empty_input(self):
        parsed = json.loads(summarize_json([]))
        self.assertEqual(parsed, {"count": 0, "sum": 0, "avg": 0})

    def test_negative_numbers(self):
        parsed = json.loads(summarize_json([-1, -2, -3]))
        self.assertEqual(parsed["count"], 3)
        self.assertEqual(parsed["sum"], -6)
        self.assertAlmostEqual(parsed["avg"], -2.0)

    def test_single_value(self):
        parsed = json.loads(summarize_json([10]))
        self.assertEqual(parsed, {"count": 1, "sum": 10, "avg": 10.0})


if __name__ == "__main__":
    unittest.main()
