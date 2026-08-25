"""Blending and uncertainty tests (spec 4.3, 4.4)."""
from __future__ import annotations

import pytest

from src.projections import (
    POSITION_VOLATILITY,
    blend_player,
    normalize_weights,
    resolve_risk_mode,
    risk_adjusted_points,
)

WEIGHTS = {"sleeper": 0.5, "espn": 0.5, "yahoo": 0.25}


# --- weighting ---------------------------------------------------------------

def test_weights_renormalize_when_a_source_is_missing():
    """A player only ESPN covers must not be scaled down to a quarter."""
    got = normalize_weights(["espn"], WEIGHTS)
    assert got == {"espn": 1.0}


def test_equal_weights_split_evenly():
    got = normalize_weights(["sleeper", "espn"], WEIGHTS)
    assert got["sleeper"] == pytest.approx(0.5)
    assert got["espn"] == pytest.approx(0.5)


def test_unconfigured_source_is_not_silently_dropped():
    """Regression: a source absent from the weight map was falling to zero,
    which quietly turned a two-source blend into a single source."""
    got = normalize_weights(["sleeper", "espn"], {"espn": 0.5})   # sleeper missing
    assert set(got) == {"sleeper", "espn"}
    assert got["sleeper"] > 0
    assert sum(got.values()) == pytest.approx(1.0)


def test_weights_always_sum_to_one():
    for available in (["sleeper"], ["sleeper", "espn"], ["sleeper", "espn", "yahoo"]):
        assert sum(normalize_weights(available, WEIGHTS).values()) == pytest.approx(1.0)


# --- blending ----------------------------------------------------------------

def test_two_equal_sources_blend_to_the_midpoint():
    blend = blend_player("x", {"sleeper": 300.0, "espn": 330.0}, WEIGHTS, "RB")
    assert blend.points == pytest.approx(315.0)
    assert blend.n_sources == 2


def test_blend_records_each_source_for_transparency():
    blend = blend_player("x", {"sleeper": 300.0, "espn": 330.0}, WEIGHTS, "RB")
    assert blend.detail == {"sleeper": 300.0, "espn": 330.0}


def test_single_source_passes_through_unscaled():
    blend = blend_player("x", {"sleeper": 250.0}, WEIGHTS, "WR")
    assert blend.points == pytest.approx(250.0)
    assert blend.n_sources == 1


def test_no_data_is_zero_not_an_error():
    blend = blend_player("x", {}, WEIGHTS, "WR")
    assert blend.points == 0.0 and blend.n_sources == 0


# --- uncertainty -------------------------------------------------------------

def test_single_source_still_carries_uncertainty():
    """With one opinion there is no disagreement, but there is still risk.
    A zero band would present a lone projection as a certainty."""
    blend = blend_player("x", {"sleeper": 200.0}, WEIGHTS, "RB")
    assert blend.stdev > 0
    assert blend.floor < blend.points < blend.ceiling


def test_wide_disagreement_widens_the_band():
    agree = blend_player("a", {"sleeper": 200.0, "espn": 202.0}, WEIGHTS, "RB")
    differ = blend_player("b", {"sleeper": 140.0, "espn": 260.0}, WEIGHTS, "RB")
    assert differ.stdev > agree.stdev
    assert (differ.ceiling - differ.floor) > (agree.ceiling - agree.floor)


def test_agreement_does_not_imply_certainty():
    """Two sources agreeing exactly must not collapse the band to zero."""
    blend = blend_player("x", {"sleeper": 200.0, "espn": 200.0}, WEIGHTS, "WR")
    assert blend.stdev > 0


def test_volatile_positions_get_wider_weekly_bands():
    """Week to week, a receiver swings more than a quarterback."""
    qb = blend_player("q", {"sleeper": 18.0}, WEIGHTS, "QB", week=5)
    wr = blend_player("w", {"sleeper": 18.0}, WEIGHTS, "WR", week=5)
    assert POSITION_VOLATILITY["WR"] > POSITION_VOLATILITY["QB"]
    assert wr.stdev > qb.stdev


def test_over_a_season_the_quarterback_band_is_the_widest():
    """The ordering INVERTS at season scale, and that is not a bug.

    Weekly spread and season spread are different quantities. A season total is
    games played times points per game, and quarterbacks play the fewest games
    of any position - a measured mean of 10.8 with a standard deviation of 5.8,
    against about 14.3 +/- 4.3 for a receiver. That availability term dominates,
    so the position with the steadiest weeks has the least certain season.

    The old model could not express this at all: it derived the season figure as
    weekly / sqrt(17), which assumes everyone plays every week, and produced
    bands two to three times too tight for every position.
    """
    from src.projections import SEASON_VOLATILITY

    qb = blend_player("q", {"sleeper": 200.0}, WEIGHTS, "QB")
    wr = blend_player("w", {"sleeper": 200.0}, WEIGHTS, "WR")
    assert SEASON_VOLATILITY["QB"] > SEASON_VOLATILITY["WR"]
    assert qb.stdev > wr.stdev


# --- risk mode ---------------------------------------------------------------

def test_ceiling_mode_raises_and_floor_mode_lowers():
    assert risk_adjusted_points(100, 80, 130, "ceiling") == pytest.approx(115)
    assert risk_adjusted_points(100, 80, 130, "floor") == pytest.approx(90)
    assert risk_adjusted_points(100, 80, 130, "neutral") == pytest.approx(100)


def test_auto_risk_mode_follows_the_matchup():
    """Underdogs need variance; favourites should protect a lead."""
    assert resolve_risk_mode("auto", projected_margin=-12) == "ceiling"
    assert resolve_risk_mode("auto", projected_margin=+12) == "floor"
    assert resolve_risk_mode("auto", projected_margin=0) == "neutral"
    assert resolve_risk_mode("auto", projected_margin=None) == "neutral"


def test_explicit_risk_mode_overrides_auto():
    assert resolve_risk_mode("ceiling", projected_margin=+50) == "ceiling"
