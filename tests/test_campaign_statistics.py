"""Wilson intervals and the paired sign test."""

from __future__ import annotations

import unittest

from tools.campaign.statistics import paired_sign_test, rate, wilson_interval


class WilsonIntervalTests(unittest.TestCase):
    def test_zero_samples_gives_the_widest_possible_interval(self) -> None:
        interval = wilson_interval(0, 0)
        self.assertEqual(interval.lower, 0.0)
        self.assertEqual(interval.upper, 1.0)

    def test_point_estimate_matches_the_simple_ratio(self) -> None:
        interval = wilson_interval(8, 10)
        self.assertAlmostEqual(interval.point_estimate, 0.8)

    def test_interval_bounds_contain_the_point_estimate(self) -> None:
        interval = wilson_interval(8, 10)
        self.assertLessEqual(interval.lower, interval.point_estimate)
        self.assertGreaterEqual(interval.upper, interval.point_estimate)

    def test_interval_narrows_as_n_grows_for_the_same_rate(self) -> None:
        small = wilson_interval(8, 10)
        large = wilson_interval(800, 1000)
        self.assertGreater(small.upper - small.lower, large.upper - large.lower)

    def test_perfect_score_interval_still_has_a_nonzero_lower_bound_below_one(self) -> None:
        interval = wilson_interval(10, 10)
        self.assertLess(interval.upper, 1.0000001)
        self.assertLess(interval.lower, 1.0)  # a 10/10 sample does not prove p=1

    def test_out_of_range_successes_raises(self) -> None:
        with self.assertRaises(ValueError):
            wilson_interval(11, 10)
        with self.assertRaises(ValueError):
            wilson_interval(-1, 10)


class PairedSignTestTests(unittest.TestCase):
    def test_identical_sequences_are_all_tied(self) -> None:
        result = paired_sign_test([True, False, True], [True, False, True])
        self.assertEqual(result.gained, 0)
        self.assertEqual(result.lost, 0)
        self.assertEqual(result.tied, 3)
        self.assertEqual(result.p_value, 1.0)

    def test_mismatched_lengths_raise(self) -> None:
        with self.assertRaises(ValueError):
            paired_sign_test([True], [True, False])

    def test_all_gains_no_losses_is_the_maximally_lopsided_case(self) -> None:
        result = paired_sign_test([False] * 5, [True] * 5)
        self.assertEqual(result.gained, 5)
        self.assertEqual(result.lost, 0)
        self.assertAlmostEqual(result.p_value, 2 * (0.5 ** 5))

    def test_no_discordant_pairs_gives_p_value_one(self) -> None:
        result = paired_sign_test([True, True], [True, True])
        self.assertEqual(result.p_value, 1.0)


class RateTests(unittest.TestCase):
    def test_zero_total_returns_none_rather_than_dividing_by_zero(self) -> None:
        self.assertIsNone(rate(0, 0))

    def test_normal_rate_computes(self) -> None:
        self.assertAlmostEqual(rate(3, 4), 0.75)


if __name__ == "__main__":
    unittest.main()
