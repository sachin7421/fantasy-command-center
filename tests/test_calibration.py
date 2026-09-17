"""The shipped constants must still match what the data says.

Several constants in this project were documented as "measured" or "calibrated"
with no artifact that produced them, and a review found most of them wrong by
2-8x - some in the wrong units, one with the wrong sign. `tools/calibrate.py`
now derives them; these tests assert the modules agree with it.

They are structural, not numeric: they do not re-measure (that needs four
seasons of usage data, which CI does not have). They check the properties that
were violated when the constants were guessed - so a future edit that
reintroduces an unmeasured number has to break one of these to land.
"""
from __future__ import annotations

import pytest

from src.analytics import distributions, priors
from src.analytics.uncertainty import MEASURED_WEEKLY_SD
from src import projections


def test_correlation_constants_match_the_measurement():
    """Measured over 22,175 player-weeks by tools/calibrate.py.

    The asserted values were 0.55 / 0.35 / -0.20; the measured ones are
    0.362 / ~0.00 / +0.026, and the same-position figure had the wrong sign.
    """
    assert pytest.approx(0.362, abs=0.03) == distributions.QB_PASSCATCHER_CORRELATION
    assert pytest.approx(0.0, abs=0.05) == distributions.TEAM_CORRELATION
    # Positive, not negative: two backs do not hedge each other.
    assert distributions.SAME_POSITION_CORRELATION >= 0.0
    assert pytest.approx(0.03, abs=0.05) == distributions.SAME_POSITION_CORRELATION


def test_season_volatility_is_not_the_weekly_figure_over_root_seventeen():
    """The old derivation assumed every player plays all seventeen weeks.

    Measured mean games played is 10.8 for a quarterback. If someone
    reintroduces the sqrt(17) rule, every season band silently narrows by a
    factor of two to three and this test is what says so.
    """
    import math

    for position in ("QB", "RB", "WR", "TE"):
        weekly = projections.POSITION_VOLATILITY[position]
        season = projections.SEASON_VOLATILITY[position]
        naive = weekly / math.sqrt(projections.REGULAR_SEASON_GAMES)
        assert season > naive * 1.5, (
            f"{position}: season CV {season} is close to the naive {naive:.3f}"
        )


def test_weekly_spread_has_a_real_intercept():
    """`sd = points * CV` forces the line through the origin; it does not.

    With a fitted intercept a low scorer gets a wider band and a high scorer a
    narrower one than a constant CV implies - and since start/sit ranks on
    `mean + risk * sd`, the constant-CV version pushed stars in and out of the
    lineup for the wrong reason.
    """
    for position, (intercept, slope) in projections.VOLATILITY_FIT.items():
        assert intercept > 0.5, position
        assert 0.0 < slope < 1.0, position

    low = projections.intrinsic_spread(6.0, "QB", week=5)
    high = projections.intrinsic_spread(25.0, "QB", week=5)
    assert low > 6.0 * projections.POSITION_VOLATILITY["QB"]
    assert high < 25.0 * projections.POSITION_VOLATILITY["QB"]


def test_residual_persistence_is_per_position():
    """A single constant cannot represent a range from 0.11 to 0.25."""
    assert isinstance(priors.RESIDUAL_PERSISTENCE, dict), (
        "persistence differs by more than 2x across positions"
    )
    assert priors.persistence_for("QB") > priors.persistence_for("WR")
    for position in ("QB", "RB", "WR", "TE"):
        assert 0.0 < priors.persistence_for(position) < 0.5


def test_the_bust_tail_is_modelled_at_all():
    """A bare gamma puts 2-6x too little mass below two points."""
    for position in ("QB", "RB", "WR", "TE"):
        assert distributions.BUST_PROBABILITY[position] > 0.02


# --- the assumed spread, checked against what actually happened --------------

#: Measured by tools/backtest.py over 2,205 paired 2025 player-weeks - a
#: projection made before a week against the points scored in it. The first
#: non-circular measurement in this project: every earlier performance claim
#: scored both sides with our own projections.
#:
#: The spread is imported rather than copied. This file used to hold its own
#: copy, which went stale the day `points_actual` gained fumbles and the
#: source constants were refit - and stayed green, because the band is loose.
MEASURED_ERROR_SD = MEASURED_WEEKLY_SD
MEAN_PROJECTION = {"QB": 17.02, "RB": 8.13, "WR": 6.64, "TE": 4.56}


def test_the_assumed_spread_is_close_to_the_measured_one():
    """A structural critique that measurement did not bear out.

    The quant review argued the lineup model is systematically overconfident:
    `VOLATILITY_FIT` fits dispersion around a REALIZED season mean, which is a
    different quantity from the spread around a PROJECTION and carries no
    projection error at all. That reasoning is correct.

    The predicted consequence was not. Measured against 2025:

        QB  model 8.14  measured 7.07  -> 15% too WIDE
        RB  model 6.11  measured 5.88  ->  4% too wide
        WR  model 5.40  measured 5.49  ->  2% too narrow
        TE  model 3.73  measured 4.51  -> 21% too narrow

    Within a fifth either way, and the direction varies by position rather
    than running one way. The two quantities happen to be similar in size, so
    the wrong estimand is not costing much - which is why this is measured
    rather than argued.

    The band is deliberately loose. It is a drift alarm, not a calibration:
    one season of one source is not evidence enough to overwrite constants
    fitted on 22,175 player-weeks.
    """
    from src.projections import effective_volatility

    for position, measured in MEASURED_ERROR_SD.items():
        assumed = effective_volatility(position, week=1) * MEAN_PROJECTION[position]
        ratio = measured / assumed
        assert 0.6 <= ratio <= 1.6, (
            f"{position}: the model assumes sd {assumed:.2f} and 2025 measured "
            f"{measured:.2f} ({ratio:.2f}x). Re-run tools/backtest.py - either "
            "the constants moved or the projections changed character."
        )


def test_tight_ends_are_the_one_position_worth_watching():
    """TE is 21% too narrow AND projected 0.87 low, both pointing the same way.

    Neither is large alone. Together they mean a tight end's floor is
    overstated and his upside understated, which is the wrong way round for
    the position most often streamed. Recorded so the next person measuring
    starts here rather than rediscovering it.
    """
    assert MEASURED_ERROR_SD["TE"] / MEAN_PROJECTION["TE"] > 0.9, (
        "TE weekly outcome spread is nearly its entire mean - a TE projection "
        "is barely more than a guess, and the model should say so"
    )
