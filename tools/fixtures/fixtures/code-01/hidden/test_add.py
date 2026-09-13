import unittest

from solution import add


class TestAdd(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)

    def test_negative(self):
        self.assertEqual(add(-2, -3), -5)

    def test_zero(self):
        self.assertEqual(add(0, 0), 0)

    def test_mixed_sign(self):
        self.assertEqual(add(-5, 10), 5)


if __name__ == "__main__":
    unittest.main()
