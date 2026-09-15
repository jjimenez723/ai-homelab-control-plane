"""Defensible basic statistics for binomial pass rates and paired comparisons.

D-046/D-049 are this module's reason for existing: a single sample per cell
cannot support a locus claim, and "statistically significant" was used
casually in an earlier pass over N=10 with one sample each. This module
gives the campaign report Wilson confidence intervals (never a bare point
estimate) and an exact paired sign test for two candidates over matched
items -- and nothing fancier, because the sample design here (repeated
sampling per item, not repeated independent experiments) does not justify a
generic p-value framework.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class WilsonInterval:
    point_estimate: float
    lower: float
    upper: float
    n: int
    successes: int


def wilson_interval(successes: int, n: int, *, z: float = 1.959963984540054) -> WilsonInterval:
    """95%-by-default Wilson score interval. Defined (wide) even for n=0."""
    if n <= 0:
        return WilsonInterval(point_estimate=0.0, lower=0.0, upper=1.0, n=0, successes=0)
    if successes < 0 or successes > n:
        raise ValueError(f"successes ({successes}) must be within [0, {n}]")
    p_hat = successes / n
    denominator = 1 + z * z / n
    centre = p_hat + z * z / (2 * n)
    spread = z * math.sqrt((p_hat * (1 - p_hat) + z * z / (4 * n)) / n)
    lower = max(0.0, (centre - spread) / denominator)
    upper = min(1.0, (centre + spread) / denominator)
    return WilsonInterval(point_estimate=p_hat, lower=lower, upper=upper, n=n, successes=successes)


@dataclass(frozen=True)
class PairedComparison:
    """Exact two-sided sign-test result over matched per-item outcomes.

    Deliberately not McNemar's chi-square approximation -- exact binomial is
    correct at the small N this kind of item-matched comparison usually has
    (`docs/19-model-evaluation.md` section 10.5 already uses the exact form
    for exactly this reason).
    """

    gained: int
    lost: int
    tied: int
    p_value: float


def _binomial_two_sided_p(smaller: int, total_discordant: int) -> float:
    if total_discordant == 0:
        return 1.0
    tail = sum(math.comb(total_discordant, k) for k in range(0, smaller + 1)) * (0.5 ** total_discordant)
    return min(1.0, 2 * tail)


def paired_sign_test(baseline_passed: list[bool], candidate_passed: list[bool]) -> PairedComparison:
    """Exact sign test over item-matched pass/fail outcomes (same item order)."""
    if len(baseline_passed) != len(candidate_passed):
        raise ValueError("paired_sign_test requires equal-length, item-matched sequences")
    gained = sum(1 for b, c in zip(baseline_passed, candidate_passed) if (not b) and c)
    lost = sum(1 for b, c in zip(baseline_passed, candidate_passed) if b and (not c))
    tied = len(baseline_passed) - gained - lost
    discordant = gained + lost
    smaller = min(gained, lost)
    p_value = _binomial_two_sided_p(smaller, discordant)
    return PairedComparison(gained=gained, lost=lost, tied=tied, p_value=p_value)


def rate(successes: int, total: int) -> float | None:
    return None if total == 0 else successes / total
