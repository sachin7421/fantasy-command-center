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


def test_a_thin_sample_says_so_and_a_full_one_does_not():
    """The caveat has to mean something, or it teaches you to ignore it.

    It used to fire whenever `confidence < 0.45`, and confidence is the
    shrinkage weight - which for a residual that is almost entirely noise is
    structurally low. Every signal carried "treat as a lean, not a call",
    including ones with a full window behind them.
    """
    thin = regression.analyse(
        "k", "Small Sample", "RB", "KC", weeks([(25, 10), (26, 11), (24, 10)])
    )
    assert any("only 3 games" in r for r in thin.reasons), thin.reasons

    full = regression.analyse(
        "k", "Full Window", "RB", "KC",
        weeks([(25, 10), (26, 11), (24, 10), (25, 11), (26, 10), (24, 11)]),
    )
    assert not any("games so far" in r for r in full.reasons), full.reasons


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


def test_two_backs_on_one_team_do_not_hedge_each_other():
    """The "they split a fixed number of carries" intuition is false.

    It was asserted here as a negative correlation of -0.20 and shipped for
    months. Measured over 22,175 player-weeks (tools/calibrate.py) the figure is
    +0.026 for same-team receivers and +0.011 for backs: in a week the team
    runs more, both backs gain, and the splitting effect cancels against it.

    So two backs are very slightly MORE volatile together than apart, not less
    - and the practical consequence is that a same-team pair never got the
    variance discount the old constant handed it.
    """
    same = [forecast("RB1", "RB", 14, 6, "SF"), forecast("RB2", "RB", 10, 5, "SF")]
    apart = [forecast("RB1", "RB", 14, 6, "SF"), forecast("RB2", "RB", 10, 5, "GB")]
    assert distributions.totals(same)[1] >= distributions.totals(apart)[1]
    # And the effect is small enough to be nearly a rounding difference.
    assert distributions.totals(same)[1] - distributions.totals(apart)[1] < 0.5


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


# --- season simulation -------------------------------------------------------

def _league(n=12, weeks=6):
    from src.analytics.season_sim import Matchup, TeamSeason

    teams = [
        TeamSeason(
            team_key=str(i), name=f"Team {i}", wins=i % 4, losses=(3 - i % 4),
            points_for=500 + i * 10,
            # A spread of strengths so the odds are not all identical.
            mean=95 + i * 2, sd=22,
        )
        for i in range(1, n + 1)
    ]
    schedule = []
    for week in range(1, weeks + 1):
        for i in range(0, n, 2):
            schedule.append(Matchup(week, str(i + 1), str(n - i)))
    return teams, schedule


def test_playoff_odds_are_probabilities_that_sum_to_the_field():
    from src.analytics.season_sim import simulate

    teams, schedule = _league()
    odds = simulate(teams, schedule, playoff_spots=6, trials=600)
    assert all(0.0 <= o.playoff_odds <= 1.0 for o in odds)
    # Six of twelve make it every trial, so the odds must total six.
    assert sum(o.playoff_odds for o in odds) == pytest.approx(6.0, abs=0.05)


def test_title_odds_sum_to_one():
    from src.analytics.season_sim import simulate

    teams, schedule = _league()
    odds = simulate(teams, schedule, trials=600)
    assert sum(o.title_odds for o in odds) == pytest.approx(1.0, abs=0.02)


def test_a_stronger_team_has_better_odds():
    from src.analytics.season_sim import simulate

    teams, schedule = _league()
    odds = simulate(teams, schedule, trials=800)
    best = max(teams, key=lambda t: t.mean).team_key
    worst = min(teams, key=lambda t: t.mean).team_key
    lookup = {o.team_key: o for o in odds}
    assert lookup[best].playoff_odds > lookup[worst].playoff_odds


def test_simulation_is_deterministic_for_a_given_seed():
    from src.analytics.season_sim import simulate

    teams, schedule = _league()
    a = simulate(teams, schedule, trials=300, seed=7)
    b = simulate(teams, schedule, trials=300, seed=7)
    assert [o.playoff_odds for o in a] == [o.playoff_odds for o in b]


def test_improving_a_team_raises_its_playoff_odds():
    """The point of the whole module: value a decision in playoff odds."""
    from src.analytics.season_sim import evaluate_change

    teams, schedule = _league()
    target = teams[0]
    impact = evaluate_change(
        teams, schedule, target.team_key,
        new_mean=target.mean + 18, label="big upgrade", trials=1200,
    )
    assert impact.delta > 0
    assert "worth doing" in impact.verdict


def test_weakening_a_team_lowers_its_playoff_odds():
    from src.analytics.season_sim import evaluate_change

    teams, schedule = _league()
    target = teams[0]
    impact = evaluate_change(
        teams, schedule, target.team_key,
        new_mean=target.mean - 18, label="bad trade", trials=1200,
    )
    assert impact.delta < 0


def test_a_negligible_change_reads_as_negligible():
    """Paired seeds mean a tiny change must not surface as simulation noise."""
    from src.analytics.season_sim import evaluate_change

    teams, schedule = _league()
    impact = evaluate_change(
        teams, schedule, teams[0].team_key,
        new_mean=teams[0].mean + 0.2, label="marginal", trials=1200,
    )
    assert abs(impact.delta) < 0.03


# --- FAAB bidding ------------------------------------------------------------

def _profile(name, beta, budget=100, n=10, **kw):
    from src.analytics.faab import ManagerProfile

    return ManagerProfile(
        team_key=name, name=name, observations=n, beta=beta, raw_beta=beta,
        mean_bid=kw.get("mean_bid", 10.0), max_bid=kw.get("max_bid", 30),
        budget_left=budget,
    )


def test_a_manager_who_cannot_afford_it_never_wins():
    """Budget is a hard ceiling, not a tendency."""
    broke = _profile("Broke", beta=5.0, budget=3)
    assert broke.probability_bids_below(10, value=20) == 1.0


def test_a_richer_more_aggressive_manager_is_likelier_to_outbid_you():
    from src.analytics.faab import win_probability

    tight = [_profile("Tight", beta=0.5)]
    loose = [_profile("Loose", beta=3.0)]
    assert win_probability(15, tight, value=12) > win_probability(15, loose, value=12)


def test_raising_your_bid_raises_your_chance_of_winning():
    from src.analytics.faab import win_probability

    rivals = [_profile("A", 1.2), _profile("B", 1.6)]
    probs = [win_probability(b, rivals, value=12) for b in (2, 6, 12, 25)]
    assert probs == sorted(probs)


def test_more_rivals_makes_a_given_bid_less_likely_to_win():
    from src.analytics.faab import win_probability

    one = [_profile("A", 1.2)]
    many = [_profile(str(i), 1.2) for i in range(6)]
    assert win_probability(12, many, value=12) < win_probability(12, one, value=12)


def test_recommendation_never_exceeds_the_budget():
    from src.analytics.faab import recommend

    advice = recommend(value=40, my_budget=8, rivals=[_profile("A", 1.2)])
    assert advice.recommended <= 8


def test_a_more_valuable_player_earns_a_higher_bid():
    from src.analytics.faab import recommend

    rivals = [_profile("A", 0.6), _profile("B", 0.5)]
    cheap = recommend(value=6, my_budget=100, rivals=rivals)
    dear = recommend(value=45, my_budget=100, rivals=rivals)
    assert dear.recommended > cheap.recommended


def test_worth_is_anchored_on_budget_not_on_the_market():
    """Anchoring worth to the market average means never winning anything.

    Winning an auction means paying more than the average bidder, so a
    valuation set to the market average recommends never bidding - technically
    true and practically useless. Worth therefore comes from your own budget
    allocation instead.
    """
    from src.analytics.faab import worth_to_you

    assert worth_to_you(55, 100, weeks_left=10) > worth_to_you(10, 100, weeks_left=10)
    # Concave: the second-best add of a season is worth much less than the best.
    assert worth_to_you(55, 100) < 2 * worth_to_you(27, 100)
    assert worth_to_you(35, 20) < worth_to_you(35, 100)


def test_late_season_bids_go_up():
    """Budget kept until the end is budget wasted."""
    from src.analytics.faab import recommend

    rivals = [_profile("A", 0.6)]
    early = recommend(value=30, my_budget=100, rivals=rivals, weeks_left=13)
    late = recommend(value=30, my_budget=100, rivals=rivals, weeks_left=2)
    assert late.recommended >= early.recommended


def test_bid_is_capped_by_what_the_player_is_worth():
    """Never pay past value, however much the field is willing to pay."""
    from src.analytics.faab import recommend

    # A field that will pay far more than a modest player justifies.
    rivals = [_profile(str(i), 3.0, budget=100) for i in range(5)]
    advice = recommend(value=10, my_budget=100, rivals=rivals)
    assert advice.recommended <= advice.worth_to_you
    assert advice.price_to_win > advice.recommended
    assert advice.walk_away


def test_an_unwinnable_bid_is_reported_honestly_rather_than_inflated():
    from src.analytics.faab import recommend

    rivals = [_profile(str(i), 2.5, budget=100) for i in range(4)]
    advice = recommend(value=12, my_budget=100, rivals=rivals)
    assert advice.recommended >= 1
    assert any("not win" in n or "well past" in n for n in advice.notes)


def test_broke_rivals_collapse_the_price():
    """The clearest edge the model finds: rivals who cannot pay are not rivals."""
    from src.analytics.faab import recommend

    broke = [_profile(str(i), 1.5, budget=1) for i in range(5)]
    rich = [_profile(str(i), 1.5, budget=95) for i in range(5)]
    cheap = recommend(value=35, my_budget=80, rivals=broke)
    dear = recommend(value=35, my_budget=80, rivals=rich)
    assert cheap.price_to_win < dear.price_to_win
    assert cheap.win_probability > 0.9


def test_no_value_or_no_budget_produces_no_bid():
    from src.analytics.faab import recommend

    assert recommend(0, 100, [_profile("A", 1.2)]).recommended == 0
    assert recommend(20, 0, [_profile("A", 1.2)]).recommended == 0


def test_profiles_shrink_toward_the_league_while_history_is_thin():
    from src.analytics.faab import BidRecord, learn_profiles

    records = [
        # One wild bid from a newcomer, plenty of ordinary ones from others.
        BidRecord("new", "p1", "P1", bid=60, value=10.0),
        *[BidRecord("reg", f"p{w}", "P", bid=10, value=10.0) for w in range(1, 9)],
    ]
    profiles = learn_profiles(records, {"new": "Newcomer", "reg": "Regular"})
    newcomer = profiles["new"]
    assert newcomer.raw_beta == pytest.approx(6.0)
    # One observation should not brand him a maniac.
    assert newcomer.beta < 4.0
    assert newcomer.confidence < 0.3


def test_a_manager_with_no_history_gets_the_league_profile():
    from src.analytics.faab import BidRecord, learn_profiles

    records = [BidRecord("a", "p", "P", bid=12, value=10.0)]
    profiles = learn_profiles(records, {"a": "A", "silent": "Silent"})
    assert "silent" in profiles
    assert profiles["silent"].observations == 0
    assert profiles["silent"].beta > 0


def test_league_report_is_honest_with_no_history():
    from src.analytics.faab import league_report

    assert any("no faab history" in line.lower() for line in league_report({}))


# --- the simulation and the closed form must describe the same thing --------

def test_simulated_spread_matches_the_closed_form():
    """`simulate()` used to sum INDEPENDENT draws while reporting the
    CORRELATED sd from `totals()` in the same dictionary - measured at 19.0
    against a reported 26.9 on a four-player stack, with a p10-p90 band that
    matched neither. Whatever else is true, one dict must describe one
    distribution.
    """
    import random
    import statistics

    stack = [
        distributions.PlayerForecast("qb", "QB1", "QB", "KC", 22.0, 8.0),
        distributions.PlayerForecast("w1", "WR1", "WR", "KC", 14.0, 7.0),
        distributions.PlayerForecast("te", "TE1", "TE", "KC", 12.0, 6.0),
        distributions.PlayerForecast("w2", "WR2", "WR", "KC", 11.0, 6.0),
    ]
    mean, closed_sd = distributions.totals(stack)
    rng = random.Random(11)
    draws = [distributions.sample_lineup(stack, rng) for _ in range(20_000)]

    assert statistics.fmean(draws) == pytest.approx(mean, rel=0.02)
    assert statistics.stdev(draws) == pytest.approx(closed_sd, rel=0.12)

    out = distributions.simulate(stack, 110.0, 20.0, trials=8_000)
    band_sd = (out["p90"] - out["p10"]) / 2.563
    assert band_sd == pytest.approx(out["sd"], rel=0.15), out


def test_a_stack_really_is_drawn_correlated():
    """The QB-to-pass-catcher link has to survive into the samples, not just
    into the closed-form variance."""
    import random
    import statistics

    stacked = [
        distributions.PlayerForecast("qb", "QB1", "QB", "KC", 22.0, 8.0),
        distributions.PlayerForecast("w1", "WR1", "WR", "KC", 14.0, 7.0),
        distributions.PlayerForecast("te", "TE1", "TE", "KC", 12.0, 6.0),
    ]
    apart = [
        distributions.PlayerForecast("qb", "QB1", "QB", "KC", 22.0, 8.0),
        distributions.PlayerForecast("w1", "WR1", "WR", "BUF", 14.0, 7.0),
        distributions.PlayerForecast("te", "TE1", "TE", "SF", 12.0, 6.0),
    ]
    rng_a, rng_b = random.Random(5), random.Random(5)
    sd_stacked = statistics.stdev(
        [distributions.sample_lineup(stacked, rng_a) for _ in range(15_000)]
    )
    sd_apart = statistics.stdev(
        [distributions.sample_lineup(apart, rng_b) for _ in range(15_000)]
    )
    assert sd_stacked > sd_apart * 1.05, (sd_stacked, sd_apart)


def test_the_bust_tail_is_thicker_than_a_bare_gamma():
    """Measured P(points < 2 | startable) is 3.4%-6.1%; a bare gamma gives
    0.2%-2.3%. The dud game is most of what a floor is meant to describe."""
    import random

    forecast = distributions.PlayerForecast("w", "WR1", "WR", "LAR", 16.0, 7.0)
    rng = random.Random(3)
    duds = sum(
        1 for _ in range(20_000)
        if distributions.sample_lineup([forecast], rng) < 2.0
    )
    rate = duds / 20_000
    assert 0.03 < rate < 0.12, rate


# --- FAAB --------------------------------------------------------------------

def _rival(name, beta, budget=80, observations=0, auctions=0):
    from src.analytics.faab import ManagerProfile

    return ManagerProfile(
        team_key=name, name=name, observations=observations, beta=beta,
        raw_beta=beta, mean_bid=20.0, max_bid=40, budget_left=budget,
        auctions_seen=auctions,
    )


def test_the_bid_distinguishes_a_league_winner_from_a_wr5():
    """It used to return the same number for everyone.

    `min(1, value / 55) ** 0.7` pins at 1.0 for every claim worth 55-plus
    points, so with a replacement level of zero every free agent saturated and
    came back at $60 - the model could not tell a Tuesday league-winner from a
    bench receiver, and told you both were worth 60% of your budget.
    """
    from src.analytics import faab

    field = [_rival(f"T{i}", faab.LEAGUE_PRIOR_BETA) for i in range(11)]
    bids = [
        faab.recommend(value=float(v), my_budget=100, rivals=field,
                       weeks_left=10).recommended
        for v in (5, 20, 80, 150)
    ]
    assert bids == sorted(bids), bids
    assert len(set(bids)) == len(bids), f"every value produced the same bid: {bids}"
    assert bids[-1] > bids[0] * 3


def test_a_broke_field_is_cheap_to_beat():
    """A manager with a dollar left is not a rival, whatever his habits."""
    from src.analytics import faab

    broke = [_rival(f"B{i}", faab.LEAGUE_PRIOR_BETA, budget=1) for i in range(11)]
    advice = faab.recommend(value=20.0, my_budget=100, rivals=broke, weeks_left=10)
    assert advice.recommended <= 2
    assert advice.win_probability > 0.9


def test_win_probability_accounts_for_who_actually_bids():
    """Treating all eleven rivals as certain entrants gives 0.5^11 at the
    median - a 0.05% chance of winning a routine claim. That is not a property
    of auctions, it is a mis-specified likelihood, and the module previously
    abandoned expected-surplus maximisation to work around the number."""
    from src.analytics import faab

    field = [_rival(f"T{i}", faab.LEAGUE_PRIOR_BETA) for i in range(11)]
    median_bid = faab.LEAGUE_PRIOR_BETA * 20.0
    probability = faab.win_probability(median_bid, field, 20.0)
    assert probability > 0.10, probability
    assert probability < 0.60, probability


def test_a_manager_who_never_bids_is_barely_a_threat():
    from src.analytics import faab

    quiet = _rival("Quiet", faab.LEAGUE_PRIOR_BETA, observations=1, auctions=40)
    busy = _rival("Busy", faab.LEAGUE_PRIOR_BETA, observations=30, auctions=40)
    assert quiet.participation < busy.participation
    assert faab.win_probability(20.0, [quiet], 20.0) > faab.win_probability(
        20.0, [busy], 20.0
    )


# --- FAAB bid parsing, against yfpy's real Transaction shape ------------------

def _txn(conn, league_key, txn_id, *, bid, status="successful",
         source_type="waivers", team="3", player_id="1001"):
    """One stored transaction, shaped as yfpy serialises a Transaction.

    Field names read off the installed package (yfpy/models.py): Transaction
    carries faab_bid, status, type and players; each player carries
    transaction_data with type and destination_team_key, and source_type says
    whether the player came off waivers or was a free agent.
    """
    import json

    from src import db

    payload = {
        "transaction_id": txn_id,
        "type": "add/drop",
        "status": status,
        "faab_bid": bid,
        "players": [
            {
                "player_id": player_id,
                "full_name": "Waiver Target",
                "transaction_data": {
                    "type": "add",
                    "source_type": source_type,
                    "destination_team_key": f"nfl.l.796511.t.{team}",
                    "destination_type": "team",
                },
            }
        ],
    }
    conn.execute(
        "INSERT INTO transactions(league_key, txn_id, type, timestamp, payload_json) "
        "VALUES (?,?,?,?,?)",
        (league_key, str(txn_id), "add/drop", db.utcnow(), json.dumps(payload)),
    )
    conn.commit()


def test_only_successful_claims_are_learned_from(tmp_path):
    """A claim that did not go through is not evidence of what wins.

    yfpy's own docstring for Transaction.status says "successful", etc. - so
    other values exist, and a pending or failed waiver claim carries a
    faab_bid just like a winning one. Learning from those teaches the model
    that a LOSING bid won, which drags every predicted rival bid downward and
    makes it far too optimistic about winning a player.
    """
    from src import db
    from src.analytics import faab

    conn = db.init_db(tmp_path / "bids.db", force_sqlite=True)
    key = "nfl.l.796511"
    _txn(conn, key, 1, bid=44, status="successful")
    _txn(conn, key, 2, bid=3, status="pending")
    _txn(conn, key, 3, bid=1, status="failed")

    bids = faab.parse_bids(conn, key)
    conn.close()

    assert [b.bid for b in bids] == [44], (
        f"learned from {[(b.bid) for b in bids]} - a claim that did not "
        "succeed is not evidence of what a winning bid costs"
    )


def test_a_zero_dollar_waiver_claim_is_real_evidence(tmp_path):
    """$0 off waivers means nobody else bid, which is worth knowing.

    Skipping every zero threw that away and left the model learning only from
    claims somebody paid for - so it believed the league always pays.
    """
    from src import db
    from src.analytics import faab

    conn = db.init_db(tmp_path / "zero.db", force_sqlite=True)
    key = "nfl.l.796511"
    _txn(conn, key, 1, bid=0, source_type="waivers")

    bids = faab.parse_bids(conn, key)
    conn.close()

    assert [b.bid for b in bids] == [0], "a $0 winning waiver claim is evidence"


def test_a_free_agent_pickup_is_not_a_bid(tmp_path):
    """No auction happened, so there is nothing to learn.

    source_type separates the two: "waivers" went through the claim process,
    "freeagents" was a straight pickup with no bidding at all. Counting the
    second as a $0 bid would invent evidence that nobody competes.
    """
    from src import db
    from src.analytics import faab

    conn = db.init_db(tmp_path / "fa.db", force_sqlite=True)
    key = "nfl.l.796511"
    _txn(conn, key, 1, bid=0, source_type="freeagents")
    _txn(conn, key, 2, bid=None, source_type="freeagents")

    bids = faab.parse_bids(conn, key)
    conn.close()

    assert bids == [], f"invented {len(bids)} bid(s) from free-agent pickups"
