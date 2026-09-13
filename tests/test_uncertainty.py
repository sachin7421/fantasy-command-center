"""Saying how sure a number is.

Every figure this app prints is stated to one decimal and carries no band:
"+7.7" on a lineup swap, "+117.0" on a trade, "8.3" on a defence. All of them
come from projections that tools/backtest.py measured at an RMSE near 5.6
points a week, out of sample.

So the app sounds equally certain about a 40-point edge and a 2-point one,
and the 2-point one is noise. That is the failure mode that makes a manager
act on something that is not there.
"""
from __future__ import annotations

import pytest


def test_the_spread_comes_from_the_backtest_not_from_a_guess():
    """Measured over 2,198 paired 2025 player-weeks; see tools/backtest.py."""
    from src.analytics.uncertainty import weekly_sd

    assert weekly_sd("QB") == pytest.approx(6.94)
    assert weekly_sd("RB") == pytest.approx(5.87)
    assert weekly_sd("WR") == pytest.approx(5.48)
    assert weekly_sd("TE") == pytest.approx(4.52)


def test_an_unmeasured_position_falls_back_to_the_overall_figure():
    """There were ZERO paired DEFENCE player-weeks in the sample.

    Inventing a defence-specific number would be worse than admitting we only
    have the pooled one.
    """
    from src.analytics.uncertainty import weekly_sd

    assert weekly_sd("DEF") == pytest.approx(5.60)
    assert weekly_sd("K") == pytest.approx(5.60)
    assert weekly_sd(None) == pytest.approx(5.60)


def test_comparing_two_players_combines_both_errors():
    """A swap is a DIFFERENCE, and both sides carry error.

    Using one player's spread understates the uncertainty of the comparison by
    about 30%, which is the difference between "act" and "do not".
    """
    from src.analytics.uncertainty import difference_sd

    combined = difference_sd("RB", "WR")
    assert combined == pytest.approx((5.87**2 + 5.48**2) ** 0.5, abs=0.01)
    assert combined > max(5.87, 5.48)


def test_a_multi_week_total_grows_with_the_square_root_of_weeks():
    """Ten weeks of a weekly error is not ten times the error.

    Independent weeks add in variance, so the spread on a rest-of-season total
    grows as sqrt(n). Treating it as linear would overstate the band by a
    factor of three over a season.
    """
    from src.analytics.uncertainty import season_sd

    one = season_sd("RB", weeks=1)
    nine = season_sd("RB", weeks=9)
    assert nine == pytest.approx(one * 3.0, rel=0.01)


def test_a_difference_inside_the_noise_is_not_meaningful():
    """The whole point. A 2.1-point defence swap is a coin flip."""
    from src.analytics.uncertainty import is_meaningful

    assert is_meaningful(2.1, sd=7.9) is False
    assert is_meaningful(18.0, sd=7.9) is True
    # Symmetric: a loss inside the noise is equally not a finding.
    assert is_meaningful(-2.1, sd=7.9) is False


def test_a_band_is_printed_rather_than_a_false_decimal():
    from src.analytics.uncertainty import describe

    assert describe(117.0, sd=18.0) == "+117 (+99 to +135)"
    assert describe(-24.0, sd=8.0) == "-24 (-32 to -16)"


def test_the_swap_the_app_used_to_recommend_is_now_a_coin_flip():
    """"Start Jauan Jennings over Jaylen Warren (+7.7)" was a real email.

    Two players compared carry a combined spread near 8 points, so a 7.7 point
    edge is inside the noise that produced it. The app stated it to one decimal
    and told the manager to act.
    """
    from src.analytics.uncertainty import describe, difference_sd, is_meaningful

    sd = difference_sd("WR", "RB")
    assert is_meaningful(7.7, sd) is False
    assert "too close" in describe(7.7, sd)


def test_a_number_inside_the_noise_says_so_instead_of_pretending():
    from src.analytics.uncertainty import describe

    text = describe(2.1, sd=7.9)
    assert "2" in text
    assert "noise" in text.lower() or "too close" in text.lower()
