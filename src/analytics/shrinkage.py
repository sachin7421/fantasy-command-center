"""Empirical-Bayes shrinkage: how much should I believe a small sample?

Three games is not evidence. A receiver with a 30% target share over three games
is not a 30% target-share receiver - he is somewhere between that and what we
believed about him before the season, and the fewer games, the closer to the
prior he sits.

The estimator:

    theta_hat = w * observed + (1 - w) * prior,      w = n / (n + k)

`k` is the number of observations at which the sample and the prior carry equal
weight. It is NOT one number: it depends on how quickly a statistic stabilises.
Target share settles within a handful of games; touchdown rate barely stabilises
within a season at all. Using one `k` for everything is the usual mistake, and it
is why people drop good players in week 3 and pay a fortune for three-week
flukes.

The stabilisation points below come from the published reliability work on NFL
box-score rates (Yurko et al. on nflfastR-derived measures, and the long line of
split-half reliability studies that follow Blake Cutler's fantasy analyses).
They are deliberately conservative: when in doubt, trust the sample less.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean, pstdev
from collections.abc import Iterable, Sequence

#: Games at which the observed rate and the prior are weighted equally.
#: Larger = slower to believe. These are *rates*, not counts.
STABILISATION: dict[str, float] = {
    # Usage: what a coach chooses to do. Settles fast.
    "snap_pct": 4.0,
    "target_share": 5.0,
    "rush_share": 5.0,
    "air_yards": 6.0,
    # Volume-ish: still mostly decision, some game-script noise.
    "targets": 6.0,
    "rush_attempts": 6.0,
    "receptions": 7.0,
    # Efficiency: much noisier.
    "yards_per_target": 14.0,
    "yards_per_carry": 20.0,
    # Scoring rate: barely stabilises inside one season. Trust the prior.
    "td_rate": 30.0,
    "points": 12.0,
    "points_expected": 8.0,
}
DEFAULT_STABILISATION = 10.0


def stabilisation_for(metric: str) -> float:
    return STABILISATION.get(metric, DEFAULT_STABILISATION)


def weight(n: int, metric: str = "", k: float | None = None) -> float:
    """How much of the observed sample to believe, in [0, 1]."""
    if n <= 0:
        return 0.0
    k = stabilisation_for(metric) if k is None else k
    return n / (n + k)


def shrink(
    observed: float | None,
    prior: float,
    n: int,
    metric: str = "",
    k: float | None = None,
) -> float:
    """Pull an observed value toward the prior in proportion to sample size."""
    if observed is None or n <= 0:
        return prior
    w = weight(n, metric, k)
    return w * observed + (1.0 - w) * prior


@dataclass
class Estimate:
    value: float
    observed: float | None
    prior: float
    n: int
    weight: float
    metric: str

    @property
    def moved_toward_prior(self) -> float:
        """How far the estimate sits from the raw sample, in points."""
        if self.observed is None:
            return 0.0
        return round(self.observed - self.value, 3)

    def explain(self) -> str:
        if self.observed is None or self.n == 0:
            return f"no sample yet; using the prior ({self.prior:.1f})"
        return (
            f"{self.n} game(s): {self.weight:.0%} sample / {1 - self.weight:.0%} prior "
            f"-> {self.value:.1f} (raw {self.observed:.1f}, prior {self.prior:.1f})"
        )


def estimate(
    observations: Sequence[float],
    prior: float,
    metric: str = "",
    k: float | None = None,
) -> Estimate:
    """Shrink the mean of `observations` toward `prior`."""
    n = len(observations)
    observed = fmean(observations) if n else None
    w = weight(n, metric, k)
    return Estimate(
        value=shrink(observed, prior, n, metric, k),
        observed=observed,
        prior=prior,
        n=n,
        weight=w,
        metric=metric,
    )


def recency_weighted_mean(
    observations: Sequence[float], half_life: float = 4.0
) -> float | None:
    """Mean with exponentially decaying weight on older games.

    A role change three weeks ago matters more than what happened in week 1,
    but week 1 is not worthless. `half_life` is how many games it takes for a
    game's influence to halve. Observations must be oldest-first.
    """
    if not observations:
        return None
    weights = [0.5 ** ((len(observations) - 1 - i) / half_life)
               for i in range(len(observations))]
    total = sum(weights)
    return sum(o * w for o, w in zip(observations, weights, strict=True)) / total if total else None


def population_prior(values: Iterable[float]) -> tuple[float, float]:
    """Mean and spread of a positional population, for use as a prior."""
    data = [v for v in values if v is not None]
    if not data:
        return 0.0, 0.0
    return fmean(data), pstdev(data) if len(data) > 1 else 0.0


def james_stein_factor(
    observations: Sequence[float], population_mean: float, population_sd: float
) -> float:
    """Classical James-Stein shrinkage factor for a group of estimates.

    Included for the case where a whole set of players is being estimated at
    once and the amount of shrinkage should be learned from how spread out the
    group is, rather than assumed. Returns the weight on the observed values.
    """
    n = len(observations)
    if n < 3 or population_sd <= 0:
        return 1.0
    variance = population_sd**2
    ss = sum((o - population_mean) ** 2 for o in observations)
    if ss <= 0:
        return 0.0
    factor = 1.0 - ((n - 2) * variance) / ss
    return max(0.0, min(1.0, factor))
