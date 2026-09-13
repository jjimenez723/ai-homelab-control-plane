import time
import unittest

from solution import retry


class TestRetry(unittest.TestCase):
    def test_succeeds_after_failures(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ValueError("not yet")
            return "ok"

        self.assertEqual(retry(flaky, attempts=5, delay=0), "ok")
        self.assertEqual(calls["n"], 3)

    def test_reraises_after_exhausting_attempts(self):
        def always_fails():
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            retry(always_fails, attempts=3, delay=0)

    def test_respects_attempt_count(self):
        calls = {"n": 0}

        def counter():
            calls["n"] += 1
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            retry(counter, attempts=4, delay=0)
        self.assertEqual(calls["n"], 4)

    def test_first_attempt_success_calls_once(self):
        calls = {"n": 0}

        def immediate():
            calls["n"] += 1
            return "first-try"

        self.assertEqual(retry(immediate, attempts=5, delay=0), "first-try")
        self.assertEqual(calls["n"], 1)

    def test_completes_quickly_without_delay(self):
        start = time.time()

        def always_fails():
            raise ValueError("x")

        try:
            retry(always_fails, attempts=3, delay=0)
        except ValueError:
            pass
        self.assertLess(time.time() - start, 2.0)


if __name__ == "__main__":
    unittest.main()
