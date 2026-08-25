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
