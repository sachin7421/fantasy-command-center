"""How sure a number is, said out loud.

Every figure this application printed came to one decimal with no band:
"+7.7" on a lineup swap, "+117.0" on a trade, "8.3" on a defence. All of them
descend from projections that `tools/backtest.py` measured, out of sample,
at an RMSE near 5.6 points a week.

So the app sounded exactly as certain about a 40-point edge as about a
2-point one. The 2-point one is noise, and a tool that spends its credibility
on coin flips is not believed about the things that matter.

The numbers here are MEASURED, not assumed - 2,205 paired 2025 player-weeks,
a projection made before a week against the points scored in it. Where there
was no measurement there is no invention: defences had zero paired
player-weeks, so they get the pooled figure and a note saying so.
"""
from __future__ import annotations

import math

#: Standard deviation of (actual - projected) for one player-week, by position.
#: From tools/backtest.py over the 2025 season.
#:
#:   pos  n    mean proj  sd(error)  bias    r
#:   QB   242  17.02      7.07       +0.49   0.32
#:   RB   593   8.13      5.88       -0.19   0.66
#:   WR   914   6.64      5.49       +0.24   0.52
#:   TE   456   4.56      4.51       +0.87   0.51
#:
#: Refit after `points_actual` was corrected. The ground truth these were
#: originally measured against omitted fumbles and two-point conversions, so
#: the "measured" spread was measured against a number that was itself wrong.
#: QB moved most (6.94 -> 7.07), which is the expected direction: quarterbacks
#: fumble more than anyone, mostly on strip sacks, and every one of those was
#: previously scored as though it cost nothing.
MEASURED_WEEKLY_SD: dict[str, float] = {
    "QB": 7.07,
    "RB": 5.88,
    "WR": 5.49,
    "TE": 4.51,
}

#: The pooled figure, used for positions the backtest could not measure.
#: Defence and kicker had zero paired player-weeks in 2025.
POOLED_WEEKLY_SD = 5.62

#: How many standard deviations a difference must clear to be worth acting on.
#: One sigma is about 68% - roughly "more likely than not, by a margin". Two
#: would be statistically tidier and would silence almost every real weekly
#: decision, which is its own kind of wrong: the job is to help someone choose,
#: not to refuse to answer.
MEANINGFUL_Z = 1.0


def weekly_sd(position: str | None) -> float:
    """Spread of one player-week around its projection."""
    if not position:
        return POOLED_WEEKLY_SD
    return MEASURED_WEEKLY_SD.get(str(position).upper(), POOLED_WEEKLY_SD)


def is_measured(position: str | None) -> bool:
    """Whether this position's spread was measured or is the pooled fallback."""
    return bool(position) and str(position).upper() in MEASURED_WEEKLY_SD


def difference_sd(position_a: str | None, position_b: str | None = None) -> float:
    """Spread on the DIFFERENCE between two players.

    Both sides carry error, so they add in variance. Using one player's spread
    understates the uncertainty of a comparison by about 30% - which is the
    difference between "act on this" and "do not".
    """
    a = weekly_sd(position_a)
    b = weekly_sd(position_b if position_b is not None else position_a)
    return math.sqrt(a * a + b * b)


def season_sd(position: str | None, weeks: int, compared: bool = False) -> float:
    """Spread on a total over several weeks.

    Independent weeks add in VARIANCE, so the spread grows as sqrt(n), not n.
    Treating it as linear would overstate a rest-of-season band threefold.

    That independence is an assumption, and an imperfect one - a player who
    loses his job is wrong every week afterwards, which correlates the errors
    and makes the true band wider than this. So this is a floor on the
    uncertainty, which is the safe direction for a number used to decide
    whether a difference is real.
    """
    base = difference_sd(position) if compared else weekly_sd(position)
    return base * math.sqrt(max(1, int(weeks)))


def is_meaningful(difference: float, sd: float, z: float = MEANINGFUL_Z) -> bool:
    """Whether a difference is bigger than the noise that produced it."""
    if sd <= 0:
        return difference != 0
    return abs(difference) >= z * sd


def band(value: float, sd: float, z: float = MEANINGFUL_Z) -> tuple[float, float]:
    return (value - z * sd, value + z * sd)


def describe(value: float, sd: float, z: float = MEANINGFUL_Z) -> str:
    """A number with its band, or a plain statement that it is noise.

    Whole points deliberately. A decimal place on a figure whose band is
    eighteen points wide is a claim to precision that does not exist, and it
    is the specific thing that makes a reader trust the wrong digit.
    """
    if not is_meaningful(value, sd, z):
        return f"{value:+.0f} - too close to call (noise is about {sd:.0f})"
    low, high = band(value, sd, z)
    return f"{value:+.0f} ({low:+.0f} to {high:+.0f})"
