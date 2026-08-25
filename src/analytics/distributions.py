"""Forecast distributions, and start/sit as a win-probability problem.

Head-to-head fantasy is not scored on expected points. It is scored on beating
one specific opponent, once. Those are different objectives, and the difference
is not academic:

    You are projected 96, your opponent 118.

Starting your highest-mean lineup maximises expected points and still loses most
of the time. What you want is the lineup with the best chance of reaching 118 -
which means deliberately choosing variance. Ahead by twenty, the reverse: take
the boring floor and refuse the coin flips.

Two ways to answer it here:

* **Closed form.** Treat both totals as roughly normal (a sum of nine players
  is, by the central limit theorem, even though each is skewed):

      P(win) = Phi( (mu_me - mu_opp) / sqrt(var_me + var_opp) )

  Maximising that is a mean-variance trade-off, so sweeping a risk parameter
  traces an efficient frontier and the best point on it is the answer.

* **Simulation.** Draw from per-player gamma distributions, which respect the
  skew and the hard floor at zero, and count wins. Slower, more faithful, and
  the way to handle correlation between team-mates.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from collections.abc import Sequence

from src.lineup_solver import best_lineup

#: Same-team players share game script, so their scores move together. A QB and
#: his top receiver are strongly correlated; two running backs on one team are
#: negatively correlated (they split the same carries).
TEAM_CORRELATION = 0.35
QB_PASSCATCHER_CORRELATION = 0.55
SAME_POSITION_CORRELATION = -0.20


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class PlayerForecast:
    """One player's weekly outcome as a distribution rather than a number."""

    player_key: str
    name: str
    position: str
    team: str
    mean: float
    sd: float

    @property
    def variance(self) -> float:
        return self.sd**2

    def gamma_params(self) -> tuple[float, float]:
        """Shape and scale for a gamma with this mean and sd.

        Gamma rather than normal because fantasy points are non-negative and
        right-skewed: the downside is bounded at zero, the upside is not.
        """
        if self.mean <= 0 or self.sd <= 0:
            return 0.0, 0.0
        shape = (self.mean / self.sd) ** 2
        scale = self.variance / self.mean
        return shape, scale

    def sample(self, rng: random.Random) -> float:
        shape, scale = self.gamma_params()
        if shape <= 0:
            return max(0.0, self.mean)
        return rng.gammavariate(shape, scale)

    def risk_adjusted(self, risk: float) -> float:
        """mean + risk * sd. Positive risk chases upside, negative buys safety."""
        return self.mean + risk * self.sd


@dataclass
class LineupOutcome:
    risk: float
    total_mean: float
    total_sd: float
    win_probability: float
    players: list[PlayerForecast] = field(default_factory=list)

    def describe(self) -> str:
        posture = (
            "chasing upside" if self.risk > 0.15
            else "protecting the floor" if self.risk < -0.15
            else "balanced"
        )
        return (
            f"{self.total_mean:.1f} +/- {self.total_sd:.1f}  |  "
            f"{self.win_probability:.0%} to win ({posture})"
        )


# --- closed form -------------------------------------------------------------

def totals(forecasts: Sequence[PlayerForecast]) -> tuple[float, float]:
    """Mean and standard deviation of a lineup total, with correlation.

    Independence would understate the spread: team-mates rise and fall together,
    so a lineup stacked on one game is more volatile than the naive sum implies.
    """
    mean = sum(f.mean for f in forecasts)
    variance = sum(f.variance for f in forecasts)

    for i, a in enumerate(forecasts):
        for b in forecasts[i + 1:]:
            rho = correlation_between(a, b)
            if rho:
                variance += 2 * rho * a.sd * b.sd
    return mean, math.sqrt(max(variance, 0.0))


def correlation_between(a: PlayerForecast, b: PlayerForecast) -> float:
    if not a.team or a.team != b.team:
        return 0.0
    positions = {a.position, b.position}
    if "QB" in positions and positions & {"WR", "TE"}:
        return QB_PASSCATCHER_CORRELATION
    if a.position == b.position:
        # Two backs on one team divide a fixed number of carries.
        return SAME_POSITION_CORRELATION
    return TEAM_CORRELATION


def win_probability(
    my_mean: float, my_sd: float, opponent_mean: float, opponent_sd: float
) -> float:
    """P(my total > opponent total), both treated as normal."""
    spread = math.sqrt(my_sd**2 + opponent_sd**2)
    if spread <= 0:
        return 1.0 if my_mean > opponent_mean else 0.0
    return normal_cdf((my_mean - opponent_mean) / spread)


# --- choosing a lineup -------------------------------------------------------

def optimise(
    roster: Sequence[PlayerForecast],
    starting_slots: dict[str, int],
    opponent_mean: float,
    opponent_sd: float,
    risk_levels: Sequence[float] = tuple(x / 10 for x in range(-8, 13)),
) -> LineupOutcome:
    """The lineup with the highest chance of winning this specific matchup.

    Sweeps a risk parameter to trace the efficient frontier - each level gives
    the best lineup for that appetite - then picks the frontier point with the
    highest win probability. Reusing the exact assignment solver at each level
    keeps every candidate a *legal* lineup, which a greedy search would not.
    """
    best: LineupOutcome | None = None

    for risk in risk_levels:
        lineup = best_lineup(
            roster,
            starting_slots,
            points_of=lambda p, r=risk: p.risk_adjusted(r),
            position_of=lambda p: p.position,
        )
        chosen = [s.player for s in lineup.slots if s.player is not None]
        if not chosen:
            continue
        mean, sd = totals(chosen)
        probability = win_probability(mean, sd, opponent_mean, opponent_sd)
        # Several risk levels often yield the SAME lineup, and different lineups
        # can tie on win probability. Break ties toward the higher mean: if the
        # variance estimates are wrong, the higher-mean lineup degrades better.
        better = best is None or (
            probability > best.win_probability + 1e-9
            or (abs(probability - best.win_probability) <= 1e-9
                and mean > best.total_mean)
        )
        if better:
            best = LineupOutcome(
                risk=risk, total_mean=round(mean, 2), total_sd=round(sd, 2),
                win_probability=round(probability, 4), players=chosen,
            )

    return best or LineupOutcome(0.0, 0.0, 0.0, 0.0, [])


def simulate(
    forecasts: Sequence[PlayerForecast],
    opponent_mean: float,
    opponent_sd: float,
    trials: int = 20_000,
    seed: int = 17,
) -> dict[str, float]:
    """Monte-Carlo check on the closed form.

    Draws each player from his own gamma, so the skew and the floor at zero are
    respected rather than assumed away. Worth running when a decision is close;
    the normal approximation is good but not exact in the tails.
    """
    rng = random.Random(seed)
    mean, sd = totals(forecasts)
    opponent_shape = (opponent_mean / opponent_sd) ** 2 if opponent_sd > 0 else 0.0
    opponent_scale = (opponent_sd**2 / opponent_mean) if opponent_mean > 0 else 0.0

    wins = 0
    samples = []
    for _ in range(trials):
        mine = sum(f.sample(rng) for f in forecasts)
        theirs = (
            rng.gammavariate(opponent_shape, opponent_scale)
            if opponent_shape > 0 else opponent_mean
        )
        samples.append(mine)
        if mine > theirs:
            wins += 1

    samples.sort()
    def pct(p: float) -> float:
        return round(samples[min(int(p * len(samples)), len(samples) - 1)], 1)

    return {
        "win_probability": round(wins / trials, 4),
        "mean": round(mean, 2),
        "sd": round(sd, 2),
        "p10": pct(0.10),
        "p50": pct(0.50),
        "p90": pct(0.90),
    }


def swap_impact(
    roster: Sequence[PlayerForecast],
    starting_slots: dict[str, int],
    opponent_mean: float,
    opponent_sd: float,
) -> dict[str, float]:
    """Compare the win probability of the safe and aggressive lineups.

    When these two are far apart the matchup is genuinely swinging on risk
    posture, which is the case worth explaining to a human rather than quietly
    optimising away.
    """
    neutral = best_lineup(roster, starting_slots,
                          points_of=lambda p: p.mean,
                          position_of=lambda p: p.position)
    chosen = [s.player for s in neutral.slots if s.player is not None]
    mean, sd = totals(chosen)
    baseline = win_probability(mean, sd, opponent_mean, opponent_sd)
    optimal = optimise(roster, starting_slots, opponent_mean, opponent_sd)
    return {
        "highest_mean_win_probability": round(baseline, 4),
        "best_win_probability": optimal.win_probability,
        "gain": round(optimal.win_probability - baseline, 4),
        "risk": optimal.risk,
    }
