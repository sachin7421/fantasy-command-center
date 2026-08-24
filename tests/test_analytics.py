"""Tests for the quantitative models.

These pin *behaviour that would be wrong if inverted* - that shrinkage actually
shrinks, that a small sample moves the answer less than a large one, that
chasing variance only happens when behind. A model that silently flips sign is
worse than no model, because it is confidently wrong.
"""
from __future__ import annotations

import pytest

from src.analytics import accuracy, distributions, regression, shrinkage
from src.analytics.distributions import PlayerForecast
from src.analytics.regression import UsageWeek

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 2, "DEF": 1}


# --- shrinkage ---------------------------------------------------------------

def test_no_sample_returns_the_prior():
    assert shrinkage.shrink(None, 12.0, 0, "points") == 12.0
    assert shrinkage.shrink(30.0, 12.0, 0, "points") == 12.0


def test_more_games_means_more_weight_on_the_sample():
    weights = [shrinkage.weight(n, "target_share") for n in (1, 3, 6, 12, 20)]
    assert weights == sorted(weights)
    assert weights[0] < 0.3 < weights[-1]


def test_shrunk_estimate_lies_between_sample_and_prior():
    value = shrinkage.shrink(observed=30.0, prior=10.0, n=4, metric="points")
    assert 10.0 < value < 30.0


def test_a_loud_small_sample_is_pulled_hard_toward_the_prior():
    """Three huge games should not make someone a 30-point player."""
    value = shrinkage.shrink(observed=30.0, prior=10.0, n=3, metric="points")
    assert value < 15.0


def test_usage_stabilises_faster_than_scoring():
    """The central point: not every statistic deserves the same trust.

    Target share is a coaching decision and settles quickly; touchdown rate is
    mostly noise inside a single season.
    """
    assert shrinkage.stabilisation_for("target_share") < shrinkage.stabilisation_for("td_rate")
    n = 6
    assert shrinkage.weight(n, "target_share") > shrinkage.weight(n, "td_rate")


def test_recency_weighting_favours_recent_games():
    rising = [5.0, 5.0, 5.0, 20.0, 20.0]
    plain = sum(rising) / len(rising)
    assert shrinkage.recency_weighted_mean(rising, half_life=2.0) > plain


def test_recency_weighting_handles_an_empty_history():
    assert shrinkage.recency_weighted_mean([]) is None


def test_james_stein_shrinks_a_tightly_clustered_group_more():
    tight = shrinkage.james_stein_factor([10.1, 10.0, 9.9, 10.0], 10.0, 3.0)
    spread = shrinkage.james_stein_factor([2.0, 10.0, 18.0, 25.0], 10.0, 3.0)
    assert tight < spread


# --- regression --------------------------------------------------------------

def weeks(pairs, snaps=None):
    return [
        UsageWeek(week=i + 1, points_actual=a, points_expected=e,
                  snap_pct=(snaps[i] if snaps else None))
        for i, (a, e) in enumerate(pairs)
    ]


def test_overperformer_is_a_sell():
    signal = regression.analyse(
        "k", "Lucky Guy", "RB", "DET",
        weeks([(22, 12), (25, 13), (20, 11), (24, 12), (21, 12), (23, 13)]),
    )
    assert signal.verdict == "sell"
    assert signal.residual > 0


def test_underperformer_is_a_buy():
    signal = regression.analyse(
        "k", "Unlucky Guy", "WR", "LAR",
        weeks([(6, 15), (7, 16), (5, 14), (8, 15), (6, 16), (7, 15)]),
    )
    assert signal.verdict == "buy"
    assert signal.residual < 0


def test_a_player_scoring_what_he_should_is_a_hold():
    signal = regression.analyse(
        "k", "Steady", "WR", "BUF",
        weeks([(12, 12), (13, 12), (11, 12), (12, 13), (12, 12), (13, 12)]),
    )
    assert signal.verdict == "hold"


def test_falling_snap_share_cancels_a_buy():
    """The distinction that matters: unlucky versus losing his job.

    Both look identical in the residual alone. A player underperforming while
    his snap share collapses is not a buy - his role is disappearing.
    """
    falling = regression.analyse(
        "k", "Benched Guy", "RB", "NYJ",
        # snap share halving across the window
        weeks([(6, 15), (7, 16), (5, 14), (8, 15), (6, 16), (7, 15)],
              snaps=[0.80, 0.78, 0.75, 0.45, 0.35, 0.30]),
    )
    assert falling.verdict == "hold"
    assert any("shrinking role" in r for r in falling.reasons)


def test_rising_snap_share_reinforces_a_buy():
    rising = regression.analyse(
        "k", "Emerging Guy", "RB", "SEA",
        weeks([(6, 15), (7, 16), (5, 14), (8, 15), (6, 16), (7, 15)],
              snaps=[0.30, 0.35, 0.40, 0.62, 0.70, 0.75]),
    )
    assert rising.verdict == "buy"
    assert any("rising" in r for r in rising.reasons)


def test_too_few_games_produces_no_signal():
    assert regression.analyse("k", "New Guy", "WR", "GB", weeks([(30, 5), (28, 6)])) is None


def test_small_samples_are_flagged_as_leans():
    signal = regression.analyse(
        "k", "Small Sample", "RB", "KC", weeks([(25, 10), (26, 11), (24, 10)])
    )
    assert signal.confidence < 0.45
    assert any("lean" in r for r in signal.reasons)


def test_shrinkage_makes_the_reported_residual_smaller_than_the_raw_one():
    signal = regression.analyse(
        "k", "Extreme", "RB", "DAL", weeks([(30, 10), (32, 11), (28, 9), (31, 10)])
    )
    assert abs(signal.residual) < abs(signal.raw_residual)


def test_adjusted_projection_moves_against_the_residual():
    sell = regression.analyse(
        "k", "Hot", "RB", "DET",
        weeks([(22, 12), (25, 13), (20, 11), (24, 12), (21, 12), (23, 13)]),
    )
    assert regression.adjusted_projection(20.0, sell) < 20.0
    assert regression.adjusted_projection(20.0, None) == 20.0


# --- distributions -----------------------------------------------------------

def forecast(name, position, mean, sd, team="AAA"):
    return PlayerForecast(name, name, position, team, mean, sd)


def test_win_probability_is_a_coin_flip_between_identical_teams():
    assert distributions.win_probability(100, 20, 100, 20) == pytest.approx(0.5, abs=1e-6)


def test_being_ahead_raises_win_probability():
    assert distributions.win_probability(120, 20, 100, 20) > 0.7


def test_variance_helps_the_underdog_and_hurts_the_favourite():
    """The core insight: the right amount of risk depends on the scoreboard."""
    underdog_safe = distributions.win_probability(90, 10, 110, 20)
    underdog_wild = distributions.win_probability(90, 30, 110, 20)
    assert underdog_wild > underdog_safe

    favourite_safe = distributions.win_probability(120, 10, 100, 20)
    favourite_wild = distributions.win_probability(120, 30, 100, 20)
    assert favourite_safe > favourite_wild


def test_teammates_increase_lineup_variance():
    """Stacking one game makes a lineup swingier than independence implies."""
    stacked = [forecast("QB1", "QB", 20, 8, "KC"), forecast("WR1", "WR", 15, 7, "KC")]
    spread_out = [forecast("QB1", "QB", 20, 8, "KC"), forecast("WR1", "WR", 15, 7, "BUF")]
    assert distributions.totals(stacked)[1] > distributions.totals(spread_out)[1]


def test_two_backs_on_one_team_are_negatively_correlated():
    """They divide a fixed number of carries, so they hedge each other."""
    same = [forecast("RB1", "RB", 14, 6, "SF"), forecast("RB2", "RB", 10, 5, "SF")]
    apart = [forecast("RB1", "RB", 14, 6, "SF"), forecast("RB2", "RB", 10, 5, "GB")]
    assert distributions.totals(same)[1] < distributions.totals(apart)[1]


def test_gamma_parameters_respect_the_floor_at_zero():
    f = forecast("X", "WR", 12.0, 6.0)
    shape, scale = f.gamma_params()
    assert shape > 0 and scale > 0
    import random

    draws = [f.sample(random.Random(3)) for _ in range(200)]
    assert min(draws) >= 0.0


def roster():
    return [
        forecast("QB1", "QB", 20, 5, "AAA"),
        forecast("RB1", "RB", 15, 4, "BBB"), forecast("RB2", "RB", 12, 3, "CCC"),
        forecast("RB3", "RB", 9, 9, "DDD"),        # boom/bust
        forecast("WR1", "WR", 14, 4, "EEE"), forecast("WR2", "WR", 11, 3, "FFF"),
        forecast("WR3", "WR", 8, 10, "GGG"),       # boom/bust
        forecast("TE1", "TE", 9, 3, "HHH"),
        forecast("DEF1", "DEF", 7, 4, "III"),
    ]


def test_optimiser_returns_a_legal_and_complete_lineup():
    outcome = distributions.optimise(roster(), SLOTS, opponent_mean=95, opponent_sd=18)
    assert len(outcome.players) == sum(SLOTS.values())
    assert 0.0 <= outcome.win_probability <= 1.0


def test_big_underdog_takes_the_variance_a_favourite_refuses():
    """The decision that matters, tested on the lineup rather than the knob.

    Two interchangeable options with the SAME mean and different spread: an
    underdog should take the volatile one, a favourite the steady one. Testing
    the swept `risk` parameter instead would test an artifact - many risk levels
    produce the same lineup, and a boom/bust player with a *lower* mean is
    correctly rejected even when behind.
    """
    def build(third_wr_sd):
        return [
            forecast("QB1", "QB", 20, 5, "AAA"),
            forecast("RB1", "RB", 15, 4, "BBB"), forecast("RB2", "RB", 12, 3, "CCC"),
            forecast("RB3", "RB", 9, 3, "DDD"),
            forecast("WR1", "WR", 14, 4, "EEE"), forecast("WR2", "WR", 11, 3, "FFF"),
            forecast("STEADY", "WR", 10, 2, "GGG"),
            forecast("VOLATILE", "WR", 10, 12, "HHH"),
            forecast("TE1", "TE", 9, 3, "III"),
            forecast("DEF1", "DEF", 7, 4, "JJJ"),
        ]

    behind = distributions.optimise(build(12), SLOTS, opponent_mean=150, opponent_sd=18)
    ahead = distributions.optimise(build(12), SLOTS, opponent_mean=60, opponent_sd=18)

    names_behind = {p.name for p in behind.players}
    names_ahead = {p.name for p in ahead.players}
    assert "VOLATILE" in names_behind, "a big underdog must buy variance"
    assert "STEADY" in names_ahead, "a big favourite must avoid it"
    assert behind.total_sd > ahead.total_sd


def test_simulation_broadly_agrees_with_the_closed_form():
    players = roster()[:5]
    mean, sd = distributions.totals(players)
    closed = distributions.win_probability(mean, sd, 60, 12)
    simulated = distributions.simulate(players, 60, 12, trials=4000)["win_probability"]
    assert abs(closed - simulated) < 0.08


def test_simulation_reports_a_sensible_spread():
    result = distributions.simulate(roster(), 95, 18, trials=3000)
    assert result["p10"] < result["p50"] < result["p90"]


# --- accuracy ----------------------------------------------------------------

def acc(source, position, n, mae, bias=0.0):
    return accuracy.Accuracy(source, position, n, mae, mae * 1.3, bias)


def test_the_more_accurate_source_earns_more_weight():
    weights = accuracy.derive_weights(
        [acc("good", "RB", 400, 3.0), acc("poor", "RB", 400, 6.0)]
    )
    assert weights["good"] > weights["poor"]
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)


def test_weights_stay_near_equal_while_the_evidence_is_thin():
    """Two weeks of data should not swing the blend."""
    thin = accuracy.derive_weights([acc("a", "RB", 5, 3.0), acc("b", "RB", 5, 6.0)])
    thick = accuracy.derive_weights([acc("a", "RB", 800, 3.0), acc("b", "RB", 800, 6.0)])
    assert abs(thin["a"] - 0.5) < abs(thick["a"] - 0.5)


def test_a_single_source_takes_the_whole_weight():
    assert accuracy.derive_weights([acc("only", "RB", 100, 4.0)]) == {"only": 1.0}


def test_report_is_honest_when_there_is_no_data():
    lines = accuracy.report([])
    assert any("not enough" in line.lower() for line in lines)


def test_report_names_the_better_source():
    lines = accuracy.report([acc("good", "RB", 400, 2.0), acc("poor", "RB", 400, 5.0)])
    assert any("good" in line and "better" in line for line in lines)
