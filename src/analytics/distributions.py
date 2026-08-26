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

import logging
import math
import random
import statistics
from dataclasses import dataclass, field
from collections.abc import Sequence

from src.lineup_solver import best_lineup

log = logging.getLogger("fcc.distributions")

# Same-team correlations, MEASURED over 22,175 player-weeks (2022-25) by
# tools/calibrate.py, from same-team same-week deviations z-scored within
# player-season so the player level is removed. Only startable players (>= 8
# PPG, >= 6 games) count, because those are the ones a lineup is built from.
#
# The three constants that used to sit here were all wrong, and one had the
# wrong sign:
#
#   qb <-> pass catcher   0.55 asserted   0.362 measured  (n=3,731)
#   any other same team   0.35 asserted  ~0.00  measured  (rb/wr n=3,332)
#   same position        -0.20 asserted  +0.026 measured  (wr n=1,115)
#
# The "two backs split a fixed number of carries" intuition is false in the
# data: in a week the team runs more, both backs gain, and the two effects
# cancel. Only the passing connection survives measurement.
QB_PASSCATCHER_CORRELATION = 0.362
TEAM_CORRELATION = 0.0
SAME_POSITION_CORRELATION = 0.03


#: P(a startable player scores under 2 points), measured over 22,175
#: player-weeks by tools/calibrate.py. A bare gamma puts 2-6x too little mass
#: here: the dud game - an early exit, a blowout game script - is most of what
#: a "floor" is supposed to describe, and it is the reason a start/sit call
#: goes wrong. Modelled as an explicit bust probability rather than by
#: distorting the gamma, so the shape still fits the games he does play.
BUST_PROBABILITY = {"QB": 0.034, "RB": 0.035, "WR": 0.061, "TE": 0.061,
                    "K": 0.03, "DEF": 0.08}
DEFAULT_BUST = 0.05


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

    @property
    def effective_variance(self) -> float:
        """Variance including the chance he simply does not play a game.

        A bust is modelled as a point mass at zero with probability pi, and the
        gamma he is drawn from otherwise is scaled up so the marginal mean is
        still `mean`. For that mixture:

            Var = (Var_gamma + mean^2 * pi) / (1 - pi)

        The closed form has to agree with the sampler, or `win_probability`
        and the simulated percentiles describe different distributions - which
        is exactly the inconsistency this module already had once.
        """
        bust = BUST_PROBABILITY.get(self.position, DEFAULT_BUST)
        if bust <= 0 or bust >= 1:
            return self.variance
        return (self.variance + self.mean ** 2 * bust) / (1.0 - bust)

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
    variance = sum(f.effective_variance for f in forecasts)

    for i, a in enumerate(forecasts):
        for b in forecasts[i + 1:]:
            rho = correlation_between(a, b)
            if rho:
                variance += 2 * rho * math.sqrt(
                    a.effective_variance * b.effective_variance
                )

    # A pairwise rule is not guaranteed to describe a real correlation matrix.
    # With the old constants a QB plus three team-mates produced an eigenvalue
    # of -0.17, i.e. a "variance" no random vector can have, and the negative
    # result was hidden by clamping it at zero. The measured constants are far
    # milder, but the guard stays: a negative total variance means the inputs
    # are inconsistent, and quietly reporting sd=0 would present a lineup as
    # certain.
    if variance < 0:
        independent = sum(f.effective_variance for f in forecasts)
        log.warning(
            "Correlation model produced a negative variance (%.2f); falling "
            "back to the independent sum.", variance,
        )
        variance = independent
    return mean, math.sqrt(variance)


def correlation_between(a: PlayerForecast, b: PlayerForecast) -> float:
    if not a.team or a.team != b.team:
        return 0.0
    positions = {a.position, b.position}
    if "QB" in positions and positions & {"WR", "TE"}:
        return QB_PASSCATCHER_CORRELATION
    if a.position == b.position:
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

#: Risk appetites swept to trace the efficient frontier: -0.8 (play the safe
#: floor) through +1.2 (chase the ceiling), in tenths.
RISK_LEVELS: tuple[float, ...] = tuple(i / 10 for i in range(-8, 13))


def optimise(
    roster: Sequence[PlayerForecast],
    starting_slots: dict[str, int],
    opponent_mean: float,
    opponent_sd: float,
    risk_levels: Sequence[float] = RISK_LEVELS,
) -> LineupOutcome:
    """The lineup with the highest chance of winning this specific matchup.

    Sweeps a risk parameter to trace the efficient frontier - each level gives
    the best lineup for that appetite - then picks the frontier point with the
    highest win probability. Reusing the exact assignment solver at each level
    keeps every candidate a *legal* lineup, which a greedy search would not.
    """
    best: LineupOutcome | None = None

    for risk in risk_levels:
        # A named closure rather than a lambda with a default argument. The
        # default was there to capture `risk` per iteration, which works, but
        # it also erases the parameter type - and this callback is the one
        # place the whole optimiser is parameterised.
        def scorer(player: PlayerForecast, level: float = risk) -> float:
            return player.risk_adjusted(level)

        lineup = best_lineup(
            roster,
            starting_slots,
            points_of=scorer,
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


# --- correlated sampling -----------------------------------------------------



def _cholesky(matrix: list[list[float]]) -> list[list[float]]:
    """Lower-triangular factor, nudging the diagonal until it exists.

    A correlation table built pairwise is not guaranteed to be a valid
    correlation matrix. Rather than fail, add a small ridge to the diagonal -
    the standard repair - and renormalise. With the measured constants this
    almost never triggers; with the old asserted ones it would have.
    """
    n = len(matrix)
    work = [row[:] for row in matrix]
    for _ in range(12):
        lower = [[0.0] * n for _ in range(n)]
        ok = True
        for i in range(n):
            for j in range(i + 1):
                total = sum(lower[i][k] * lower[j][k] for k in range(j))
                if i == j:
                    value = work[i][i] - total
                    if value <= 1e-12:
                        ok = False
                        break
                    lower[i][j] = math.sqrt(value)
                else:
                    lower[i][j] = (work[i][j] - total) / lower[j][j]
            if not ok:
                break
        if ok:
            return lower
        for i in range(n):
            work[i][i] += 0.05
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def _gamma_from_normal(z: float, shape: float, scale: float) -> float:
    """Wilson-Hilferty: a standard normal to a gamma of the given parameters.

    Used instead of an independent gammavariate draw so the correlation
    structure survives into the sample. It is a monotone transform of `z`, so
    the rank correlation of the normals carries over almost exactly, and it is
    accurate for the shapes fantasy forecasts produce (mean/sd around 2, so
    shape around 4).
    """
    if shape <= 0:
        return 0.0
    root = 1.0 - 1.0 / (9.0 * shape) + z / math.sqrt(9.0 * shape)
    return max(0.0, shape * scale * root ** 3)


def sample_lineup(forecasts: Sequence[PlayerForecast], rng: random.Random) -> float:
    """One correlated draw of a lineup total.

    `simulate` used to sum INDEPENDENT gamma draws while reporting the
    correlated standard deviation from `totals()` in the same dictionary - so
    the p10/p90 band and the sd it sat beside described different distributions
    (measured: sd 19.0 against a reported 26.9 on a four-player stack).
    """
    n = len(forecasts)
    if n == 0:
        return 0.0

    matrix = [[1.0 if i == j else correlation_between(forecasts[i], forecasts[j])
               for j in range(n)] for i in range(n)]
    lower = _cholesky(matrix)

    independent = [rng.gauss(0.0, 1.0) for _ in range(n)]
    total = 0.0
    for i, forecast in enumerate(forecasts):
        z = sum(lower[i][k] * independent[k] for k in range(i + 1))
        bust = BUST_PROBABILITY.get(forecast.position, DEFAULT_BUST)
        if rng.random() < bust:
            continue  # he was hurt early, or the game got away from his team
        shape, scale = forecast.gamma_params()
        if shape <= 0:
            total += max(0.0, forecast.mean)
            continue
        # Conditioning on "not a bust" raises the mean of the games he does
        # play, so the marginal still averages out to the forecast.
        total += _gamma_from_normal(z, shape, scale / max(1e-6, 1.0 - bust))
    return total


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
    mean, _closed_form_sd = totals(forecasts)
    opponent_shape = (opponent_mean / opponent_sd) ** 2 if opponent_sd > 0 else 0.0
    opponent_scale = (opponent_sd**2 / opponent_mean) if opponent_mean > 0 else 0.0

    wins = 0
    samples = []
    for _ in range(trials):
        mine = sample_lineup(forecasts, rng)
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

    # Report the spread of the draws these percentiles came from, not the
    # closed form, so nothing in this dictionary describes a different
    # distribution from anything else in it.
    simulated_mean = statistics.fmean(samples)
    simulated_sd = statistics.stdev(samples) if len(samples) > 1 else 0.0

    return {
        "win_probability": round(wins / trials, 4),
        "mean": round(simulated_mean, 2),
        "sd": round(simulated_sd, 2),
        "closed_form_mean": round(mean, 2),
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
